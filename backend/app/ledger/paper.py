"""Internal virtual (paper) portfolio ledger.

INTERNAL SIMULATION ONLY -- no broker, no orders leave this process.

Lifecycle
---------
* ``initialize``   funds the portfolio with virtual cash at the close of an inception session.
* ``advance``      processes subsequent sessions in order, one DB transaction per session:
                   pre-open corporate actions -> open fills of an *applied* plan ->
                   delisting cash-outs -> close valuation -> month-end signal & plan.
* A month-end plan is created as ``proposed``. Advancing *stops* at the signal
  session until the user applies (or skips) the plan, because fills must occur
  at the very next session's open. Applying twice is impossible (DB-guarded).
* Settings changes create a new config version that affects only future plans;
  fills, cash transactions, NAV rows and snapshots are append-only (DB triggers).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from ..calendar import TradingCalendar
from ..data.panel import Panel
from ..db import Database, log_event, utcnow
from ..strategy.config import StrategyConfig
from ..strategy.execution import (
    debit_interest,
    max_debit_fraction,
    CostModel,
    apply_corporate_actions,
    borrow_fee,
    cash_interest,
    close_prices,
    cover_shorts,
    delisting_cashouts,
    execute_rebalance,
    exposures,
    mark_to_market,
    marks_at,
    open_prices,
    preopen_marks,
    r2,
    short_stop_triggers,
    update_basis,
)
from ..strategy.signals import SignalResult, compute_signals

log = logging.getLogger(__name__)


class LedgerError(Exception):
    """User-facing ledger error (maps to HTTP 409/400)."""

    def __init__(self, message: str, code: str = "ledger_error"):
        super().__init__(message)
        self.code = code


def store_config(conn: sqlite3.Connection, cfg: StrategyConfig) -> int:
    h = cfg.config_hash()
    conn.execute("INSERT OR IGNORE INTO strategy_configs (config_hash, config_json, created_at) VALUES (?,?,?)",
                 (h, cfg.canonical_json(), utcnow()))
    return conn.execute("SELECT id FROM strategy_configs WHERE config_hash=?", (h,)).fetchone()[0]


def store_signal_set(conn: sqlite3.Connection, sig: SignalResult, context: str, cfg_hash: str, data_version: str,
                     run_id: int | None = None, portfolio_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO signal_sets (context, run_id, portfolio_id, signal_session, config_hash, data_version,"
        " universe_count, eligible_count, selected_count, created_at, diagnostics_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (context, run_id, portfolio_id, sig.session, cfg_hash, data_version, sig.universe_count,
         sig.eligible_count, sig.selected_count, utcnow(), json.dumps(sig.diagnostics, default=str)))
    set_id = cur.lastrowid
    tab = sig.table
    conn.executemany(
        "INSERT INTO signal_rows (set_id, symbol, close_raw, adv20, session_t21, session_t252, tr_t21, tr_t252,"
        " valid_history, momentum, eligible, reason, rank, selected, target_weight, side, percentile, vol, beta,"
        " adv60, sector, resid_mom, sector_mom, fip, composite, score)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(set_id, r.symbol, r.close_raw, r.adv20, r.session_t21, r.session_t252, r.tr_t21, r.tr_t252,
          r.valid_history, r.momentum, int(r.eligible), r.reason, _int_or_none(r.rank), int(r.selected),
          float(r.target_weight), r.side if isinstance(r.side, str) else None, _float_or_none(r.percentile),
          _float_or_none(r.vol), _float_or_none(r.beta), _float_or_none(getattr(r, "adv60", None)),
          getattr(r, "sector", None) if isinstance(getattr(r, "sector", None), str) else None,
          _float_or_none(getattr(r, "resid_mom", None)), _float_or_none(getattr(r, "sector_mom", None)),
          _float_or_none(getattr(r, "fip", None)), _float_or_none(getattr(r, "composite", None)),
          _float_or_none(getattr(r, "score", None))) for r in tab.itertuples(index=False)],
    )
    return set_id


def _float_or_none(v) -> float | None:
    try:
        return None if v is None or v is pd.NA or np.isnan(v) else float(v)
    except TypeError:
        return None


def _int_or_none(v) -> int | None:
    try:
        return None if v is None or v is pd.NA or np.isnan(v) else int(v)
    except TypeError:
        return int(v)


def execution_order(rows) -> list[str]:
    """Execution priority for stored signal rows: SPY core, longs by rank (best first), shorts (worst first)."""
    core = [r["symbol"] for r in rows if r["side"] == "core"]
    longs = sorted((r for r in rows if r["target_weight"] > 0 and r["side"] != "core"), key=lambda r: r["rank"])
    shorts = sorted((r for r in rows if r["target_weight"] < 0), key=lambda r: -r["rank"])
    return core + [r["symbol"] for r in longs] + [r["symbol"] for r in shorts]


@dataclass
class AdvanceResult:
    processed: list[str]
    as_of: str
    stopped_reason: str
    pending_plan_id: int | None


class PaperLedger:
    def __init__(self, db: Database, calendar: TradingCalendar, provider_key: str,
                 panel_fn: Callable[[], Panel], data_version_fn: Callable[[], str], is_demo: bool = False,
                 rf_fn: Callable[[], object] | None = None):
        self.db = db
        self.cal = calendar
        self.provider = provider_key
        self.panel_fn = panel_fn
        self.data_version_fn = data_version_fn
        self.is_demo = is_demo
        self.rf_fn = rf_fn

    # ------------------------------------------------------------------ queries
    def active(self) -> dict | None:
        return self.db.query_one("SELECT * FROM paper_portfolios WHERE provider=? AND status='active'", (self.provider,))

    def require_active(self) -> dict:
        p = self.active()
        if not p:
            raise LedgerError("No virtual portfolio has been initialized for this data provider.", "no_portfolio")
        return p

    def config_of(self, portfolio: dict) -> StrategyConfig:
        row = self.db.query_one("SELECT config_json FROM strategy_configs WHERE id=?", (portfolio["config_id"],))
        return StrategyConfig.model_validate_json(row["config_json"])

    def positions(self, pid: int) -> dict[str, int]:
        """Signed share counts (negative = short)."""
        return {r["symbol"]: r["shares"] for r in self.db.query(
            "SELECT symbol, shares FROM positions WHERE portfolio_id=? AND shares<>0", (pid,))}

    # ------------------------------------------------------------------ lifecycle
    def initialize(self, cfg: StrategyConfig, inception_session: str | None = None, name: str | None = None) -> dict:
        if self.active():
            raise LedgerError("An active virtual portfolio already exists. Reset (archive) it first.", "exists")
        panel = self.panel_fn()
        inception = inception_session or panel.sessions[-1]
        if inception not in panel.sess_index:
            raise LedgerError(f"{inception} is not a session with stored data "
                              f"({panel.sessions[0]} .. {panel.sessions[-1]}).", "bad_session")
        cfg = cfg.paper_view()
        dv = self.data_version_fn()
        with self.db.transaction() as conn:
            cfg_id = store_config(conn, cfg)
            cap = r2(cfg.initial_capital)
            cur = conn.execute(
                "INSERT INTO paper_portfolios (name, provider, status, initial_capital, cash, inception_session,"
                " as_of_session, config_id, created_at, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (name or "Model portfolio", self.provider, "active",
                 cap, cap, inception, inception, cfg_id, utcnow(),
                 "INTERNAL PAPER SIMULATION - no broker connection"))
            pid = cur.lastrowid
            conn.execute(
                "INSERT INTO cash_transactions (portfolio_id, session, kind, amount, balance_after, note, created_at)"
                " VALUES (?,?,?,?,?,?,?)", (pid, inception, "initial_deposit", cap, cap, "Virtual cash funding", utcnow()))
            t = panel.sess_index[inception]
            self._record_close(conn, panel, pid, t, cap, {}, 0.0)
            log_event(conn, "info", "paper", f"Virtual portfolio #{pid} initialized with ${cap:,.2f} at close of {inception}",
                      portfolio_id=pid, session=inception, payload={"config": cfg.model_dump(), "data_version": dv})
            if self.cal.is_month_end(inception) and cfg.rebalances_in(inception):
                self._form_plan(conn, panel, pid, t, cfg, cfg_id, {}, cap, dv)
        return self.active()

    def reset(self) -> None:
        p = self.require_active()
        with self.db.transaction() as conn:
            conn.execute("UPDATE paper_portfolios SET status='archived', archived_at=? WHERE id=?", (utcnow(), p["id"]))
            log_event(conn, "warning", "paper", f"Virtual portfolio #{p['id']} archived by user (history retained)",
                      portfolio_id=p["id"])

    def update_config(self, cfg: StrategyConfig) -> dict:
        p = self.require_active()
        cfg = cfg.paper_view()
        with self.db.transaction() as conn:
            new_id = store_config(conn, cfg)
            conn.execute("UPDATE paper_portfolios SET config_id=? WHERE id=?", (new_id, p["id"]))
            log_event(conn, "info", "paper", "Paper strategy config changed; applies to future rebalance plans only",
                      portfolio_id=p["id"], payload={"old_config_id": p["config_id"], "new_config_id": new_id,
                                                     "config": cfg.model_dump()})
        return self.active()

    # ------------------------------------------------------------------ plans
    def pending_plan(self, pid: int) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM rebalance_plans WHERE portfolio_id=? AND status IN ('proposed','applied') ORDER BY id DESC LIMIT 1",
            (pid,))

    def apply_plan(self, plan_id: int) -> dict:
        p = self.require_active()
        with self.db.transaction() as conn:
            plan = conn.execute("SELECT * FROM rebalance_plans WHERE id=? AND portfolio_id=?", (plan_id, p["id"])).fetchone()
            if not plan:
                raise LedgerError(f"Rebalance plan #{plan_id} not found for the active portfolio.", "not_found")
            if plan["status"] in ("applied", "executed"):
                raise LedgerError(f"Plan #{plan_id} was already applied; duplicate application prevented.", "duplicate")
            if plan["status"] != "proposed":
                raise LedgerError(f"Plan #{plan_id} is '{plan['status']}' and cannot be applied.", "not_applicable")
            if p["as_of_session"] != plan["signal_session"]:
                raise LedgerError("The portfolio has moved past this plan's fill session; it can no longer be applied.",
                                  "expired")
            cur = conn.execute("UPDATE rebalance_plans SET status='applied', decided_at=?, decision_note=? "
                               "WHERE id=? AND status='proposed'", (utcnow(), "Applied to internal virtual portfolio", plan_id))
            if cur.rowcount != 1:
                raise LedgerError("Plan state changed concurrently; not applied.", "duplicate")
            now = utcnow()
            for o in conn.execute("SELECT * FROM plan_orders WHERE plan_id=?", (plan_id,)).fetchall():
                conn.execute(
                    "INSERT INTO paper_orders (portfolio_id, plan_id, symbol, intended_side, target_weight, est_shares,"
                    " fill_session, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (p["id"], plan_id, o["symbol"], o["side"], o["target_weight"], o["est_shares"], plan["fill_session"],
                     "pending", now, now))
            log_event(conn, "info", "rebalance", f"Plan #{plan_id} (signal {plan['signal_session']}) applied to the internal "
                      f"virtual portfolio; fills at open of {plan['fill_session']}", portfolio_id=p["id"],
                      session=plan["signal_session"])
        return self.db.query_one("SELECT * FROM rebalance_plans WHERE id=?", (plan_id,))

    def skip_plan(self, plan_id: int, note: str | None = None) -> dict:
        p = self.require_active()
        with self.db.transaction() as conn:
            cur = conn.execute("UPDATE rebalance_plans SET status='skipped', decided_at=?, decision_note=? "
                               "WHERE id=? AND portfolio_id=? AND status='proposed'",
                               (utcnow(), note or "Skipped by user", plan_id, p["id"]))
            if cur.rowcount != 1:
                raise LedgerError(f"Plan #{plan_id} is not in 'proposed' state.", "not_applicable")
            log_event(conn, "warning", "rebalance", f"Plan #{plan_id} skipped: {note or 'by user'}", portfolio_id=p["id"])
        return self.db.query_one("SELECT * FROM rebalance_plans WHERE id=?", (plan_id,))

    def latest_month_end(self, upto: str, cfg: StrategyConfig | None = None) -> str | None:
        """Last rebalance (month-end, or quarter-end for quarterly configs) session on or before `upto` with data."""
        panel = self.panel_fn()
        cfg = cfg or (self.config_of(self.active()) if self.active() else None)
        ends = [x for x in self.cal.month_end_sessions(panel.sessions[0], upto) if x in panel.sess_index
                and (cfg is None or cfg.rebalances_in(x))]
        return ends[-1] if ends else None

    def replan(self, note: str = "strategy switch") -> dict:
        """Rebuild the plan of the latest month-end the ledger has processed, using the ACTIVE config.

        Reuses the month-end signal session (the ranking is not recomputed mid-month: the signal is the month-end
        close's), re-runs book construction and sizing with the new config, and prices the plan at the ledger's
        current session with current holdings. The new plan (kind 'replan') is applied immediately and fills at
        the next session's open; any unexecuted plan is superseded. Returns the new plan row.
        """
        p = self.require_active()
        pid = p["id"]
        panel = self.panel_fn()
        as_of = p["as_of_session"]
        s = self.latest_month_end(as_of)
        if s is None:
            raise LedgerError("No month-end signal session on or before the ledger's as-of session.", "no_signal")
        if self.cal.next_session(as_of) is None:
            raise LedgerError("No next session in the calendar for the fill.", "no_session")
        cfg = self.config_of(p)
        current = self.db.query_one(
            "SELECT * FROM rebalance_plans WHERE portfolio_id=? AND signal_session=? AND status IN "
            "('proposed','applied','executed') ORDER BY id DESC LIMIT 1", (pid, s))
        if current and current["config_id"] == p["config_id"]:
            raise LedgerError(f"Plan #{current['id']} for the {s} signal already uses the active config; nothing to "
                              "replan.", "current")
        dv = self.data_version_fn()
        with self.db.transaction() as conn:
            open_plans = [r["id"] for r in conn.execute(
                "SELECT id FROM rebalance_plans WHERE portfolio_id=? AND status IN ('proposed','applied')", (pid,))]
            port = conn.execute("SELECT * FROM paper_portfolios WHERE id=?", (pid,)).fetchone()
            shares = self.positions(pid)
            extra = []
            if current and current["signal_set_id"]:
                old = {r["symbol"]: r["rank"] for r in conn.execute(
                    "SELECT symbol, rank FROM signal_rows WHERE set_id=? AND rank IS NOT NULL", (current["signal_set_id"],))}
                extra.append({"rule": "Month-end signal reused", "ok": True,
                              "detail": f"Ranking from the {s} close (plan #{current['id']}); only books and sizing change."})
            else:
                old = None
            plan_id = self._form_plan(conn, panel, pid, panel.sess_index[s], cfg, p["config_id"], shares, port["cash"], dv,
                                      t_price=panel.sess_index[as_of], kind="replan", replaces=open_plans,
                                      extra_checks=extra)
            plan = conn.execute("SELECT * FROM rebalance_plans WHERE id=?", (plan_id,)).fetchone()
            if old is not None:
                new = {r["symbol"]: r["rank"] for r in conn.execute(
                    "SELECT symbol, rank FROM signal_rows WHERE set_id=? AND rank IS NOT NULL", (plan["signal_set_id"],))}
                if new != old:
                    log_event(conn, "warning", "rebalance", f"Replan #{plan_id}: ranking differs from plan "
                              f"#{current['id']} (signal parameters changed with the config)", portfolio_id=pid)
            if plan["status"] == "blocked":
                raise LedgerError(f"The replan is blocked: {plan['block_reason']}", "blocked")
            now = utcnow()
            for old_id in open_plans:
                conn.execute("UPDATE rebalance_plans SET status='superseded', decided_at=?, decision_note=? WHERE id=?",
                             (now, f"Superseded by replan #{plan_id} ({note})", old_id))
                conn.execute("UPDATE paper_orders SET status='no_trade', status_reason=?, updated_at=? WHERE plan_id=? "
                             "AND status='pending'", (f"Plan superseded by replan #{plan_id}", now, old_id))
            conn.execute("UPDATE rebalance_plans SET status='applied', decided_at=?, decision_note=? WHERE id=?",
                         (now, f"Replan applied ({note})", plan_id))
            for o in conn.execute("SELECT * FROM plan_orders WHERE plan_id=?", (plan_id,)).fetchall():
                conn.execute(
                    "INSERT INTO paper_orders (portfolio_id, plan_id, symbol, intended_side, target_weight, est_shares,"
                    " fill_session, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid, plan_id, o["symbol"], o["side"], o["target_weight"], o["est_shares"], plan["fill_session"],
                     "pending", now, now))
            log_event(conn, "warning", "rebalance", f"Replan #{plan_id} from the {s} signal applied; fills at the open of "
                      f"{plan['fill_session']}" + (f"; superseded plan(s) {open_plans}" if open_plans else ""),
                      portfolio_id=pid, session=as_of)
        return self.db.query_one("SELECT * FROM rebalance_plans WHERE id=?", (plan_id,))

    # ------------------------------------------------------------------ advancing
    def advance(self, until: str | None = None, max_sessions: int | None = None, auto_apply: bool = False) -> AdvanceResult:
        """Process sessions after the portfolio's as-of session.

        until: last session to process (default: latest stored data session).
        Stops early when a proposed plan needs a decision (unless auto_apply).
        """
        p = self.require_active()
        pid = p["id"]
        panel = self.panel_fn()
        last_data = panel.sessions[-1]
        target = min(until, last_data) if until else last_data
        processed: list[str] = []
        stopped = "reached_target"
        while True:
            p = self.require_active()
            as_of = p["as_of_session"]
            plan = self.pending_plan(pid)
            if plan and plan["status"] == "proposed":
                if auto_apply and plan["fill_session"] <= target:
                    self.apply_plan(plan["id"])
                    with self.db.transaction() as conn:
                        log_event(conn, "info", "rebalance", f"Plan #{plan['id']} auto-applied (automation)", portfolio_id=pid)
                else:
                    stopped = "decision_required"
                    break
            nxt = self.cal.next_session(as_of)
            if nxt is None or nxt > last_data or nxt not in panel.sess_index:
                stopped = "no_newer_data"
                break
            if nxt > target:
                stopped = "reached_target"
                break
            if max_sessions is not None and len(processed) >= max_sessions:
                stopped = "max_sessions"
                break
            if not np.isfinite(panel.close[panel.sess_index[nxt]]).any():
                stopped = "session_without_data"
                with self.db.transaction() as conn:
                    log_event(conn, "warning", "data", f"No bars stored for session {nxt}; advancing halted "
                              "(no prices are substituted).", portfolio_id=pid, session=nxt)
                break
            self._process_session(panel, pid, panel.sess_index[nxt])
            processed.append(nxt)
        p = self.require_active()
        plan = self.pending_plan(pid)
        return AdvanceResult(processed, p["as_of_session"], stopped, plan["id"] if plan else None)

    def _process_session(self, panel: Panel, pid: int, t: int) -> None:
        s = panel.sessions[t]
        with self.db.transaction() as conn:
            port = conn.execute("SELECT * FROM paper_portfolios WHERE id=?", (pid,)).fetchone()
            if self.cal.next_session(port["as_of_session"]) != s:
                raise LedgerError(f"Session {s} is not the next session after {port['as_of_session']}.", "sequence")
            undecided = conn.execute("SELECT id FROM rebalance_plans WHERE portfolio_id=? AND status='proposed' "
                                     "AND fill_session<=?", (pid, s)).fetchone()
            if undecided:
                raise LedgerError("A proposed plan must be applied or skipped before its fill session.", "decision_required")
            cfg_row = conn.execute("SELECT config_json FROM strategy_configs WHERE id=?", (port["config_id"],)).fetchone()
            cfg = StrategyConfig.model_validate_json(cfg_row["config_json"])
            cash = port["cash"]
            cum_costs = port["cum_costs"]
            rows = conn.execute("SELECT symbol, shares, cost_basis FROM positions WHERE portfolio_id=?", (pid,)).fetchall()
            shares = {r["symbol"]: r["shares"] for r in rows if r["shares"] != 0}
            basis = {r["symbol"]: r["cost_basis"] for r in rows if r["shares"] != 0}

            # 1. pre-open corporate actions (shorts pay dividends / cash-in-lieu)
            shares, evs = apply_corporate_actions(panel, t, shares)
            for e in evs:
                cash = r2(cash + e.amount)
                self._cash(conn, pid, s, e.kind, e.symbol, e.amount, cash, e.note)
            for sym in list(basis):
                if sym not in shares:
                    basis.pop(sym)

            # 2a. open: stop-loss covers triggered at the previous close
            stop_orders = conn.execute(
                "SELECT * FROM paper_orders WHERE portfolio_id=? AND origin='stop_loss' AND status='pending' "
                "AND fill_session=?", (pid, s)).fetchall()
            if stop_orders:
                cash, cum_costs, shares = self._execute_stops(conn, panel, pid, t, stop_orders, cfg, shares, basis,
                                                              cash, cum_costs)

            # 2b. open: execute an applied plan whose fill session is today
            plan = conn.execute("SELECT * FROM rebalance_plans WHERE portfolio_id=? AND status='applied' AND fill_session=?",
                                (pid, s)).fetchone()
            if plan:
                cash, cum_costs, shares = self._execute_plan(conn, panel, pid, t, plan, cfg, shares, basis, cash,
                                                             cum_costs)

            # 3. delisting close-outs at the close
            shares2, evs = delisting_cashouts(panel, t, shares)
            for e in evs:
                cash = r2(cash + e.amount)
                self._cash(conn, pid, s, e.kind, e.symbol, e.amount, cash, e.note)
                basis.pop(e.symbol, None)
                log_event(conn, "warning", "corporate_action", f"{e.symbol} delisted: {e.note}", portfolio_id=pid, session=s)
            shares = shares2

            # 4. cash interest (optional), borrow fee on short market value, then close valuation
            if cfg.cash_interest:
                if self.rf_fn is None:
                    raise LedgerError("cash_interest is on but no risk-free rate source is configured.", "config")
                rate = self.rf_fn().annual(s)
                interest = cash_interest(panel, t, cash, rate)
                if interest:
                    cash = r2(cash + interest)
                    self._cash(conn, pid, s, "interest", None, interest, cash, f"RF {rate:.2%}/yr on cash (ACT/360)")
            if cash < 0:   # margin loan (SPY core / net-long books): RF + spread, ACT/360, as the backtest engine
                base = 0.0
                try:
                    base = self.rf_fn().annual(s) if self.rf_fn is not None else 0.0
                except Exception as e:  # noqa: BLE001 - charge the spread only, and say so
                    log_event(conn, "warning", "paper", f"RF unavailable ({e}); margin interest at the spread only",
                              portfolio_id=pid, session=s)
                rate = base + cfg.margin_debit_spread
                debit = debit_interest(panel, t, cash, rate)
                if debit:
                    cash = r2(cash - debit)
                    cum_costs = r2(cum_costs + debit)
                    self._cash(conn, pid, s, "interest", None, -debit, cash,
                               f"margin loan interest {rate:.2%}/yr on ${-cash:,.2f} debit (ACT/360)")
            fee, short_value = borrow_fee(panel, t, shares, cfg.borrow_fee_annual)
            if fee:
                cash = r2(cash - fee)
                cum_costs = r2(cum_costs + fee)
                self._cash(conn, pid, s, "borrow_fee", None, -fee, cash,
                           f"{cfg.borrow_fee_annual:.2%}/yr on short value ${short_value:,.2f} (assumed)")
            self._sync_positions(conn, pid, shares, basis, s)
            self._record_close(conn, panel, pid, t, cash, shares, cum_costs)
            conn.execute("UPDATE paper_portfolios SET as_of_session=?, cash=?, cum_costs=? WHERE id=?",
                         (s, cash, cum_costs, pid))

            # 5. short stop-loss checks on the close (automatic cover at the next open)
            if True:  # short stop-losses
                pending = {r["symbol"] for r in conn.execute(
                    "SELECT symbol FROM paper_orders WHERE portfolio_id=? AND origin='stop_loss' AND status='pending'", (pid,))}
                nxt = self.cal.next_session(s)
                for trig in short_stop_triggers(panel, t, shares, basis, cfg.short_stop_loss):
                    if trig["symbol"] in pending or nxt is None:
                        continue
                    self._stop_order(conn, pid, trig["symbol"], -shares[trig["symbol"]], s, nxt,
                                     f"Close {trig['close']:.2f} is {trig['move']:.0%} above average short entry "
                                     f"{trig['entry']:.2f} (stop +{cfg.short_stop_loss:.0%}).")

            # 6. month-end signal
            if self.cal.is_month_end(s) and cfg.rebalances_in(s):
                self._form_plan(conn, panel, pid, t, cfg, port["config_id"], shares, cash, self.data_version_fn())

    def _stop_order(self, conn, pid: int, symbol: str, qty: int, trigger: str, fill_session: str, why: str) -> None:
        now = utcnow()
        conn.execute(
            "INSERT INTO paper_orders (portfolio_id, plan_id, symbol, intended_side, target_weight, est_shares, fill_session,"
            " status, status_reason, created_at, updated_at, origin, trigger_session) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, None, symbol, "buy", 0.0, qty, fill_session, "pending", why, now, now, "stop_loss", trigger))
        log_event(conn, "warning", "stop_loss", f"Short stop-loss triggered for {symbol}: {why} Cover {qty} sh at the "
                  f"{fill_session} open (automatic, internal simulation).", portfolio_id=pid, session=trigger)

    def _record_fills(self, conn, pid: int, s: str, fills, order_ids: dict[str, int], plan_id: int | None,
                      shares: dict[str, int], basis: dict[str, float], cash: float) -> tuple[float, dict[str, int]]:
        """Persist fills + cash transactions; update shares and signed cost basis in place."""
        now = utcnow()
        filled: dict[str, int] = {}
        for f in fills:
            fid = conn.execute(
                "INSERT INTO paper_fills (portfolio_id, order_id, plan_id, session, symbol, side, shares, ref_price,"
                " fill_price, gross_value, slippage_cost, commission, created_at, position_effect)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, order_ids.get(f.symbol), plan_id, s, f.symbol, f.side, f.shares, f.ref_price, f.fill_price,
                 f.gross_value, f.slippage_cost, f.commission, now, f.effect)).lastrowid
            amount = f.gross_value if f.side == "sell" else -f.gross_value
            cash = r2(cash + amount)
            label = {"open_short": "short sale", "close_short": "buy to cover"}.get(f.effect, f.side)
            self._cash(conn, pid, s, f.side, f.symbol, amount, cash,
                       f"{label} {f.shares} @ {f.fill_price:.4f} (open {f.ref_price:.4f})", fid)
            if f.commission:
                cash = r2(cash - f.commission)
                self._cash(conn, pid, s, "commission", f.symbol, -f.commission, cash, "Assumed commission", fid)
            before = shares.get(f.symbol, 0)
            update_basis(basis, before, f)
            shares[f.symbol] = before + (f.shares if f.side == "buy" else -f.shares)
            if shares[f.symbol] == 0:
                shares.pop(f.symbol)
            filled[f.symbol] = filled.get(f.symbol, 0) + f.shares
        return cash, filled

    def _execute_stops(self, conn, panel: Panel, pid: int, t: int, orders, cfg: StrategyConfig,
                       shares: dict[str, int], basis: dict[str, float], cash: float, cum_costs: float):
        s = panel.sessions[t]
        costs = CostModel(cfg.slippage_bps, cfg.commission_per_order, cfg.commission_bps)
        syms = [o["symbol"] for o in orders if shares.get(o["symbol"], 0) < 0]
        ex = cover_shorts(shares, cash, syms, open_prices(panel, t, syms), costs)
        ids = {o["symbol"]: o["id"] for o in orders}
        cash, filled = self._record_fills(conn, pid, s, ex.fills, ids, None, shares, basis, cash)
        now = utcnow()
        for o in orders:
            q = filled.get(o["symbol"], 0)
            if q:
                st, why = "filled", None
            elif shares.get(o["symbol"], 0) >= 0:
                st, why = "no_trade", "Position no longer short at the open"
            else:
                st, why = "unfilled", "No opening price; cover retried at the next open"
                nxt = self.cal.next_session(s)
                if nxt:
                    self._stop_order(conn, pid, o["symbol"], -shares[o["symbol"]], o["trigger_session"], nxt,
                                     "Retry of stop-loss cover (no opening price).")
            conn.execute("UPDATE paper_orders SET status=?, status_reason=COALESCE(?, status_reason), filled_shares=?,"
                         " updated_at=? WHERE id=? AND status='pending'", (st, why, q, now, o["id"]))
        return cash, r2(cum_costs + ex.slippage_cost + ex.commission), shares

    def _execute_plan(self, conn, panel: Panel, pid: int, t: int, plan, cfg: StrategyConfig,
                      shares: dict[str, int], basis: dict[str, float], cash: float, cum_costs: float):
        s = panel.sessions[t]
        sig_rows = conn.execute("SELECT symbol, rank, target_weight, side FROM signal_rows WHERE set_id=? AND selected=1",
                                (plan["signal_set_id"],)).fetchall()
        targets = {r["symbol"]: r["target_weight"] for r in sig_rows}
        rank_order = execution_order(sig_rows)
        symbols = set(shares) | set(targets)
        costs = CostModel(cfg.slippage_bps, cfg.commission_per_order, cfg.commission_bps)
        ex = execute_rebalance(shares, cash, targets, rank_order, open_prices(panel, t, symbols),
                               preopen_marks(panel, t, symbols), costs,
                               max_debit_frac=max_debit_fraction(targets, cfg.allow_margin))
        now = utcnow()
        orders = {r["symbol"]: r for r in conn.execute("SELECT * FROM paper_orders WHERE plan_id=?", (plan["id"],))}
        for f in ex.fills:
            if f.symbol not in orders:
                oid = conn.execute(
                    "INSERT INTO paper_orders (portfolio_id, plan_id, symbol, intended_side, target_weight, est_shares,"
                    " fill_session, status, created_at, updated_at, status_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, plan["id"], f.symbol, f.side, targets.get(f.symbol, 0.0), 0, s, "pending", now, now,
                     "Created at execution: sizing at the open differed from the close-based estimate")).lastrowid
                orders[f.symbol] = {"id": oid, "est_shares": 0}
        cash, filled_syms = self._record_fills(conn, pid, s, ex.fills, {k: v["id"] for k, v in orders.items()},
                                               plan["id"], shares, basis, cash)
        if abs(cash - ex.cash_after) > 0.011:
            raise LedgerError(f"Cash reconciliation failed ({cash} vs {ex.cash_after}).", "reconciliation")
        unfilled = {u["symbol"]: u for u in ex.unfilled}
        for sym, o in orders.items():
            q = filled_syms.get(sym, 0)
            if sym in unfilled and q == 0:
                st, why = "unfilled", unfilled[sym]["detail"]
            elif sym in unfilled:
                st, why = "partially_filled", unfilled[sym]["detail"]
            elif q > 0:
                st, why = "filled", None
            else:
                st, why = "no_trade", "No trade needed at the open (sizing with open prices)"
            conn.execute("UPDATE paper_orders SET status=?, status_reason=COALESCE(?, status_reason), filled_shares=?,"
                         " updated_at=? WHERE id=? AND status='pending'", (st, why, q, now, o["id"]))
        cum_costs = r2(cum_costs + ex.slippage_cost + ex.commission)
        execution = {"nav_at_open": ex.nav_at_open, "buy_value": ex.buy_value, "sell_value": ex.sell_value,
                     "turnover": ex.turnover, "slippage_cost": ex.slippage_cost, "commission": ex.commission,
                     "cash_before": ex.cash_before, "cash_after": ex.cash_after, "fills": len(ex.fills),
                     "unfilled": ex.unfilled, "stale_valuations": ex.stale_valuations}
        conn.execute("UPDATE rebalance_plans SET status='executed', executed_at=?, execution_json=? WHERE id=?",
                     (now, json.dumps(execution), plan["id"]))
        log_event(conn, "warning" if ex.unfilled else "info", "rebalance",
                  f"Plan #{plan['id']} executed at open of {s}: {len(ex.fills)} fills, {len(ex.unfilled)} unfilled, "
                  f"cash ${ex.cash_after:,.2f}", portfolio_id=pid, session=s, payload=execution)
        return cash, cum_costs, shares

    def _form_plan(self, conn, panel: Panel, pid: int, t: int, cfg: StrategyConfig, cfg_id: int,
                   shares: dict[str, int], cash: float, dv: str, t_price: int | None = None, kind: str = "month_end",
                   replaces: list[int] | None = None, extra_checks: list[dict] | None = None) -> int:
        """Form a plan from the month-end signal at session t.

        Month-end plans (kind 'month_end') are priced at t and fill at the next session. A replan (kind 'replan',
        after a strategy switch) reuses the month-end signal session t but is estimated at the current session
        t_price with current holdings and fills at the session after t_price.
        """
        s = panel.sessions[t]
        tp = t if t_price is None else t_price
        price_s = panel.sessions[tp]
        fill = self.cal.next_session(price_s)
        last_signal = conn.execute("SELECT MAX(signal_session) FROM rebalance_plans WHERE portfolio_id=? AND "
                                   "signal_session<?", (pid, s)).fetchone()[0] or ""
        stopped = {r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM paper_orders WHERE portfolio_id=? AND origin='stop_loss' AND trigger_session>? "
            "AND trigger_session<=?", (pid, last_signal, price_s))}
        held_long = {x for x, q in shares.items() if q > 0}
        held_short = {x for x, q in shares.items() if q < 0 and x not in stopped}
        sig = compute_signals(panel, t, cfg, held_long, held_short, stopped)
        set_id = store_signal_set(conn, sig, "paper", cfg.config_hash(), dv, portfolio_id=pid)
        targets = sig.target_weights()
        symbols = set(shares) | set(targets)
        costs = CostModel(cfg.slippage_bps, cfg.commission_per_order, cfg.commission_bps)
        # Estimate with closes (signal session, or today's for a replan); real sizing happens at the fill-session open.
        debit = max_debit_fraction(targets, cfg.allow_margin)
        est = execute_rebalance(shares, cash, targets, list(sig.selected["symbol"]), close_prices(panel, tp, symbols),
                                marks_at(panel, tp, symbols), costs, max_debit_frac=debit)
        t = tp   # valuation / estimate session below
        nav = est.nav_at_open
        d = sig.diagnostics
        longs = {k: v for k, v in targets.items() if v > 0}
        shorts = {k: v for k, v in targets.items() if v < 0}
        checks = list(extra_checks or []) + [
            {"rule": "Signal frozen before fills", "ok": True,
             "detail": f"Signals use data through the close of {s}; fills at the open of {fill}."},
            {"rule": "Data coverage at signal session", "ok": sig.coverage >= cfg.min_session_coverage,
             "detail": f"{sig.coverage:.1%} of {sig.universe_count} universe stocks have a bar on {s} "
                       f"(minimum {cfg.min_session_coverage:.0%})."},
            {"rule": "All targets eligible", "ok": bool(sig.selected["eligible"].all()) if sig.selected_count else True,
             "detail": "Every target passes price, liquidity and history filters at the signal session."},
        ]
        if True:
            gl, gs = sum(longs.values()), -sum(shorts.values())
            close = dict(zip(sig.table["symbol"], sig.table["close_raw"]))
            checks += [
                {"rule": "Gross exposure within cap", "ok": gl + gs <= cfg.max_total_gross + 1e-9,
                 "detail": f"Long {gl:.1%} + short {gs:.1%} = {gl + gs:.1%} (cap {cfg.max_total_gross:.0%}); "
                           f"net {gl - gs:+.1%}."},
                {"rule": "Per-name caps", "ok": max(longs.values(), default=0) <= cfg.max_long_weight + 1e-9
                    and max((-v for v in shorts.values()), default=0) <= cfg.max_short_weight + 1e-9,
                 "detail": f"Largest long {max(longs.values(), default=0):.2%} (cap {cfg.max_long_weight:.0%}), largest "
                           f"short {max((-v for v in shorts.values()), default=0):.2%} (cap {cfg.max_short_weight:.0%})."},
                {"rule": "Beta-neutral (ex-ante)", "ok": abs(d.get("ex_ante_net_beta", 0)) <= 0.05 or
                    bool(d.get("crash_guard", {}).get("active")),
                 "detail": f"Net beta {d.get('ex_ante_net_beta', 0):+.3f} (β long {d.get('beta_long', 0):.2f}, "
                           f"β short {d.get('beta_short', 0):.2f})"
                           + ("; crash guard deliberately leaves the book net long." if d.get("crash_guard", {}).get("active") else ".")},
                {"rule": "Short price floor", "ok": all((close.get(x) or 0) > cfg.short_min_price for x in shorts),
                 "detail": f"All {len(shorts)} shorts close above ${cfg.short_min_price:g}."},
                {"rule": "Volatility target" if cfg.sizing == "vol_target" else "Fixed sizing", "ok": True,
                 "detail": (f"Ex-ante vol {d.get('ex_ante_vol', 0):.1%} vs target {cfg.target_vol:.0%}"
                            if cfg.sizing == "vol_target" else
                            f"Overlay long {d.get('long_gross', 0):.1%} / short {d.get('short_gross', 0):.1%} "
                            f"(ex-ante vol {d.get('ex_ante_vol', 0):.1%})"
                            + (f"; SPY core {d['core']['weight']:.0%}" if d.get("core", {}).get("weight") else ""))
                           + (f" ({'; '.join(d['binding'])})" if d.get("binding") else "") + "."},
                {"rule": "Crash guard", "ok": True,
                 "detail": ("ON: short book scaled" if d.get("crash_guard", {}).get("active") else "Off") +
                           f" (market 24m return {_pct(d.get('crash_guard', {}).get('market_return'))}, "
                           f"6m vol {_pct(d.get('crash_guard', {}).get('market_vol'))}; threshold "
                           f"{cfg.crash_market_vol_threshold:.0%})."},
                {"rule": "Cash stays positive" if debit == 0 else "Margin loan within plan",
                 "ok": est.cash_after >= -debit * nav - 0.01,
                 "detail": (f"Estimated cash after trading ${est.cash_after:,.2f} (short proceeds held as cash)."
                            if debit == 0 else
                            f"Estimated cash after trading ${est.cash_after:,.2f}: a margin loan of up to "
                            f"{debit:.1%} of NAV (longs beyond NAV + short proceeds), charged RF + "
                            f"{cfg.margin_debit_spread:.1%}.")},
            ]
        status = "blocked" if sig.blocked_reason else "proposed"
        estimate = {
            "nav": nav, "cash_before": est.cash_before, "est_buy_value": est.buy_value, "est_sell_value": est.sell_value,
            "est_slippage": est.slippage_cost, "est_commission": est.commission, "est_turnover": est.turnover,
            "est_cash_after": est.cash_after, "est_positions_after": len(est.shares_after),
            "price_basis": (f"Signal-session close ({s}); actual fills use the {fill} open." if kind == "month_end" else
                            f"Replan of the {s} month-end signal with the new config, priced at the {price_s} close; "
                            f"fills at the {fill} open."),
            "kind": kind, "replaces": replaces or [],
            "universe_count": sig.universe_count, "eligible_count": sig.eligible_count,
            "selected_count": sig.selected_count, "coverage": sig.coverage, "unfilled_estimate": est.unfilled,
            "signal": cfg.signal, "long_count": len(longs), "short_count": len(shorts),
            "long_gross": sum(longs.values()), "short_gross": -sum(shorts.values()),
            "stopped_shorts_excluded": sorted(stopped), "diagnostics": d,
        }
        cur = conn.execute(
            "INSERT INTO rebalance_plans (portfolio_id, signal_session, fill_session, signal_set_id, config_id, data_version,"
            " status, block_reason, estimate_json, checks_json, created_at, kind, replaces_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, s, fill, set_id, cfg_id, dv, status, sig.blocked_reason, json.dumps(estimate, default=str),
             json.dumps(checks), utcnow(), kind, json.dumps(replaces) if replaces else None))
        plan_id = cur.lastrowid
        cur_marks = marks_at(panel, t, symbols)
        for sym in sorted(symbols):
            cur_q = shares.get(sym, 0)
            tgt_q = est.target_shares.get(sym, 0)
            fs = [f for f in est.fills if f.symbol == sym]
            if not fs and cur_q == tgt_q:
                continue
            side = ("buy" if tgt_q > cur_q else "sell") if not fs else fs[0].side
            qty = sum(f.shares for f in fs)
            value = sum(f.gross_value for f in fs)
            px = fs[-1].fill_price if fs else (cur_marks.get(sym) or 0.0)
            conn.execute(
                "INSERT INTO plan_orders (plan_id, symbol, side, current_shares, target_shares, est_shares, est_price,"
                " est_value, est_cost, current_weight, target_weight, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, sym, side, cur_q, tgt_q, qty, px, value, sum(f.slippage_cost + f.commission for f in fs),
                 (cur_q * cur_marks.get(sym, 0.0) / nav) if nav else 0.0, targets.get(sym, 0.0),
                 ", ".join(f.effect.replace("_", " ") for f in fs) if fs else "Estimated not executable (see unfilled estimate)"))
        level = "warning" if status == "blocked" else "info"
        msg = (f"Rebalance BLOCKED for signal {s}: {sig.blocked_reason}" if status == "blocked" else
               f"Rebalance plan #{plan_id} {'proposed' if kind == 'month_end' else 'rebuilt (replan)'} from signal {s}: "
               f"{len(longs)} long / {len(shorts)} short targets, est. turnover {est.turnover:.1%}; "
               f"fills at the open of {fill}")
        log_event(conn, level, "rebalance", msg, portfolio_id=pid, session=s)
        return plan_id

    # ------------------------------------------------------------------ helpers
    def _cash(self, conn, pid, session, kind, symbol, amount, balance, note, fill_id=None):
        conn.execute(
            "INSERT INTO cash_transactions (portfolio_id, session, kind, symbol, amount, balance_after, fill_id, note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)", (pid, session, kind, symbol, r2(amount), r2(balance), fill_id, note, utcnow()))

    def _sync_positions(self, conn, pid: int, shares: dict[str, int], basis: dict[str, float], session: str) -> None:
        """Write current holdings (signed shares, signed cost basis) to the positions table."""
        existing = {r["symbol"]: r for r in conn.execute("SELECT * FROM positions WHERE portfolio_id=?", (pid,))}
        for sym in set(existing) | set(shares):
            q = shares.get(sym, 0)
            b = r2(basis.get(sym, 0.0))
            row = existing.get(sym)
            if q == 0:
                if row:
                    conn.execute("DELETE FROM positions WHERE portfolio_id=? AND symbol=?", (pid, sym))
            elif row is None:
                conn.execute("INSERT INTO positions (portfolio_id, symbol, shares, cost_basis, opened_session, updated_session)"
                             " VALUES (?,?,?,?,?,?)", (pid, sym, q, b, session, session))
            elif row["shares"] != q or abs(row["cost_basis"] - b) > 0.005:
                flipped = (row["shares"] > 0) != (q > 0)
                conn.execute("UPDATE positions SET shares=?, cost_basis=?, updated_session=?, opened_session=? "
                             "WHERE portfolio_id=? AND symbol=?",
                             (q, b, session, session if flipped else row["opened_session"], pid, sym))

    def _record_close(self, conn, panel: Panel, pid: int, t: int, cash: float, shares: dict[str, int], cum_costs: float):
        s = panel.sessions[t]
        value, stale_n, marks = mark_to_market(panel, t, shares)
        long_v, short_v = exposures(panel, t, shares)
        nav = r2(cash + value)
        basis = {r["symbol"]: r["cost_basis"] for r in conn.execute(
            "SELECT symbol, cost_basis FROM positions WHERE portfolio_id=?", (pid,))}
        for sym, q in sorted(shares.items()):
            px, is_stale = marks[sym]
            j = panel.sym_index[sym]
            mark_session = s
            if is_stale:
                valid = np.flatnonzero(~np.isnan(panel.close[: t + 1, j]))
                mark_session = panel.sessions[valid[-1]] if valid.size else s
            conn.execute(
                "INSERT INTO position_snapshots (portfolio_id, session, symbol, shares, mark_price, mark_session,"
                " market_value, cost_basis) VALUES (?,?,?,?,?,?,?,?)",
                (pid, s, sym, q, px, mark_session, r2(q * px), basis.get(sym, 0.0)))
        bj = panel.sym_index.get(panel.benchmark) if panel.benchmark else None
        bench = None
        if bj is not None and not np.isnan(panel.tr[t, bj]):
            bench = float(panel.tr[t, bj])
        conn.execute(
            "INSERT INTO paper_nav (portfolio_id, session, cash, positions_value, nav, gross_nav, benchmark_index,"
            " positions, stale_marks, created_at, long_value, short_value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, s, cash, value, nav, r2(nav + cum_costs), bench, len(shares), stale_n, utcnow(), long_v, short_v))
        if stale_n:
            log_event(conn, "warning", "valuation", f"{stale_n} holding(s) valued at a carried-forward price on {s}",
                      portfolio_id=pid, session=s)


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:+.1%}"
