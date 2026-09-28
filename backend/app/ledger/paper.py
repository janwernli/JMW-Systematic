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
    CostModel,
    apply_corporate_actions,
    close_prices,
    delisting_cashouts,
    execute_rebalance,
    mark_to_market,
    marks_at,
    open_prices,
    preopen_marks,
    r2,
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
        " universe_count, eligible_count, selected_count, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (context, run_id, portfolio_id, sig.session, cfg_hash, data_version, sig.universe_count,
         sig.eligible_count, sig.selected_count, utcnow()))
    set_id = cur.lastrowid
    tab = sig.table
    conn.executemany(
        "INSERT INTO signal_rows (set_id, symbol, close_raw, adv20, session_t21, session_t252, tr_t21, tr_t252,"
        " valid_history, momentum, eligible, reason, rank, selected, target_weight) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(set_id, r.symbol, r.close_raw, r.adv20, r.session_t21, r.session_t252, r.tr_t21, r.tr_t252,
          r.valid_history, r.momentum, int(r.eligible), r.reason, _int_or_none(r.rank), int(r.selected), float(r.target_weight)) for r in tab.itertuples(index=False)],
    )
    return set_id


def _int_or_none(v) -> int | None:
    try:
        return None if v is None or v is pd.NA or np.isnan(v) else int(v)
    except TypeError:
        return int(v)


@dataclass
class AdvanceResult:
    processed: list[str]
    as_of: str
    stopped_reason: str
    pending_plan_id: int | None


class PaperLedger:
    def __init__(self, db: Database, calendar: TradingCalendar, provider_key: str,
                 panel_fn: Callable[[], Panel], data_version_fn: Callable[[], str], is_demo: bool):
        self.db = db
        self.cal = calendar
        self.provider = provider_key
        self.panel_fn = panel_fn
        self.data_version_fn = data_version_fn
        self.is_demo = is_demo

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
        return {r["symbol"]: r["shares"] for r in self.db.query(
            "SELECT symbol, shares FROM positions WHERE portfolio_id=? AND shares>0", (pid,))}

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
                (name or ("Demo virtual portfolio" if self.is_demo else "Virtual portfolio"), self.provider, "active",
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
            if self.cal.is_month_end(inception):
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
                if auto_apply:
                    self.apply_plan(plan["id"])
                    with self.db.transaction() as conn:
                        log_event(conn, "info", "rebalance", f"Plan #{plan['id']} auto-applied (demo seed)", portfolio_id=pid)
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
            shares = {r["symbol"]: r["shares"] for r in conn.execute(
                "SELECT symbol, shares FROM positions WHERE portfolio_id=? AND shares>0", (pid,))}
            before = dict(shares)

            # 1. pre-open corporate actions
            shares, evs = apply_corporate_actions(panel, t, shares)
            for e in evs:
                cash = r2(cash + e.amount)
                self._cash(conn, pid, s, e.kind, e.symbol, e.amount, cash, e.note)
            for sym in before:
                if shares.get(sym, 0) != before[sym]:
                    self._set_position(conn, pid, sym, shares.get(sym, 0), s, split_from=before[sym])

            # 2. open: execute an applied plan whose fill session is today
            plan = conn.execute("SELECT * FROM rebalance_plans WHERE portfolio_id=? AND status='applied' AND fill_session=?",
                                (pid, s)).fetchone()
            if plan:
                cash, cum_costs, shares = self._execute_plan(conn, panel, pid, t, plan, cfg, shares, cash, cum_costs)

            # 3. delisting cash-outs at the close
            shares2, evs = delisting_cashouts(panel, t, shares)
            for e in evs:
                cash = r2(cash + e.amount)
                self._cash(conn, pid, s, e.kind, e.symbol, e.amount, cash, e.note)
                self._set_position(conn, pid, e.symbol, 0, s)
                log_event(conn, "warning", "corporate_action", f"{e.symbol} delisted: {e.note}", portfolio_id=pid, session=s)
            shares = shares2

            # 4. close valuation
            self._record_close(conn, panel, pid, t, cash, shares, cum_costs)
            conn.execute("UPDATE paper_portfolios SET as_of_session=?, cash=?, cum_costs=? WHERE id=?",
                         (s, cash, cum_costs, pid))

            # 5. month-end signal
            if self.cal.is_month_end(s):
                self._form_plan(conn, panel, pid, t, cfg, port["config_id"], shares, cash, self.data_version_fn())

    def _execute_plan(self, conn, panel: Panel, pid: int, t: int, plan, cfg: StrategyConfig,
                      shares: dict[str, int], cash: float, cum_costs: float):
        s = panel.sessions[t]
        sig_rows = conn.execute("SELECT symbol, rank, target_weight FROM signal_rows WHERE set_id=? AND selected=1 "
                                "ORDER BY rank", (plan["signal_set_id"],)).fetchall()
        targets = {r["symbol"]: r["target_weight"] for r in sig_rows}
        rank_order = [r["symbol"] for r in sig_rows]
        symbols = set(shares) | set(targets)
        costs = CostModel(cfg.slippage_bps, cfg.commission_per_order, cfg.commission_bps)
        ex = execute_rebalance(shares, cash, targets, rank_order, open_prices(panel, t, symbols),
                               preopen_marks(panel, t, symbols), costs)
        now = utcnow()
        orders = {r["symbol"]: r for r in conn.execute("SELECT * FROM paper_orders WHERE plan_id=?", (plan["id"],))}
        filled_syms: dict[str, int] = {}
        for f in ex.fills:
            oid = orders[f.symbol]["id"] if f.symbol in orders else None
            if oid is None:
                oid = conn.execute(
                    "INSERT INTO paper_orders (portfolio_id, plan_id, symbol, intended_side, target_weight, est_shares,"
                    " fill_session, status, created_at, updated_at, status_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, plan["id"], f.symbol, f.side, targets.get(f.symbol, 0.0), 0, s, "pending", now, now,
                     "Created at execution: sizing at the open differed from the close-based estimate")).lastrowid
                orders[f.symbol] = {"id": oid, "est_shares": 0}
            fid = conn.execute(
                "INSERT INTO paper_fills (portfolio_id, order_id, plan_id, session, symbol, side, shares, ref_price,"
                " fill_price, gross_value, slippage_cost, commission, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, oid, plan["id"], s, f.symbol, f.side, f.shares, f.ref_price, f.fill_price, f.gross_value,
                 f.slippage_cost, f.commission, now)).lastrowid
            cash = r2(cash + (f.gross_value if f.side == "sell" else -f.gross_value))
            self._cash(conn, pid, s, f.side, f.symbol, f.gross_value if f.side == "sell" else -f.gross_value, cash,
                       f"{f.side} {f.shares} @ {f.fill_price:.4f} (open {f.ref_price:.4f})", fid)
            if f.commission:
                cash = r2(cash - f.commission)
                self._cash(conn, pid, s, "commission", f.symbol, -f.commission, cash, "Assumed commission", fid)
            new_q = shares.get(f.symbol, 0) + (f.shares if f.side == "buy" else -f.shares)
            self._set_position(conn, pid, f.symbol, new_q, s, fill=f)
            shares[f.symbol] = new_q
            filled_syms[f.symbol] = filled_syms.get(f.symbol, 0) + f.shares
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
                  f"residual cash ${ex.cash_after:,.2f}", portfolio_id=pid, session=s, payload=execution)
        return cash, cum_costs, {k: v for k, v in shares.items() if v > 0}

    def _form_plan(self, conn, panel: Panel, pid: int, t: int, cfg: StrategyConfig, cfg_id: int,
                   shares: dict[str, int], cash: float, dv: str) -> int:
        s = panel.sessions[t]
        fill = self.cal.next_session(s)
        sig = compute_signals(panel, t, cfg)
        set_id = store_signal_set(conn, sig, "paper", cfg.config_hash(), dv, portfolio_id=pid)
        targets = sig.target_weights()
        symbols = set(shares) | set(targets)
        costs = CostModel(cfg.slippage_bps, cfg.commission_per_order, cfg.commission_bps)
        # Estimate with the (frozen) signal-session closes; real sizing happens at the fill-session open.
        est = execute_rebalance(shares, cash, targets, list(sig.selected["symbol"]), close_prices(panel, t, symbols),
                                marks_at(panel, t, symbols), costs)
        nav = est.nav_at_open
        checks = [
            {"rule": "Signal frozen before fills", "ok": True,
             "detail": f"Signals use data through the close of {s}; fills at the open of {fill}."},
            {"rule": "Data coverage at signal session", "ok": sig.coverage >= cfg.min_session_coverage,
             "detail": f"{sig.coverage:.1%} of {sig.universe_count} universe stocks have a bar on {s} "
                       f"(minimum {cfg.min_session_coverage:.0%})."},
            {"rule": "Long-only, no leverage", "ok": est.cash_after >= 0 and all(q >= 0 for q in est.shares_after.values()),
             "detail": f"Estimated residual cash ${est.cash_after:,.2f}; no short positions."},
            {"rule": "Selection count", "ok": sig.selected_count <= cfg.top_n,
             "detail": f"{sig.selected_count} selected of {sig.eligible_count} eligible (limit {cfg.top_n})."},
            {"rule": "All targets eligible", "ok": bool(sig.selected["eligible"].all()) if sig.selected_count else True,
             "detail": "Every target passes price, liquidity and history filters at the signal session."},
            {"rule": "Weights sum to at most 100%", "ok": sum(targets.values()) <= 1 + 1e-9,
             "detail": f"Sum of target weights {sum(targets.values()):.4f}."},
        ]
        status = "blocked" if sig.blocked_reason else "proposed"
        estimate = {
            "nav": nav, "cash_before": est.cash_before, "est_buy_value": est.buy_value, "est_sell_value": est.sell_value,
            "est_slippage": est.slippage_cost, "est_commission": est.commission, "est_turnover": est.turnover,
            "est_cash_after": est.cash_after, "est_positions_after": len(est.shares_after),
            "price_basis": f"Signal-session close ({s}); actual fills use the {fill} open.",
            "universe_count": sig.universe_count, "eligible_count": sig.eligible_count,
            "selected_count": sig.selected_count, "coverage": sig.coverage, "unfilled_estimate": est.unfilled,
        }
        cur = conn.execute(
            "INSERT INTO rebalance_plans (portfolio_id, signal_session, fill_session, signal_set_id, config_id, data_version,"
            " status, block_reason, estimate_json, checks_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, s, fill, set_id, cfg_id, dv, status, sig.blocked_reason, json.dumps(estimate), json.dumps(checks), utcnow()))
        plan_id = cur.lastrowid
        cur_marks = marks_at(panel, t, symbols)
        by_sym = {f.symbol: f for f in est.fills}
        for sym in sorted(symbols):
            cur_q = shares.get(sym, 0)
            tgt_q = est.target_shares.get(sym, 0)
            f = by_sym.get(sym)
            if f is None and cur_q == tgt_q:
                continue
            px = f.fill_price if f else (cur_marks.get(sym) or 0.0)
            conn.execute(
                "INSERT INTO plan_orders (plan_id, symbol, side, current_shares, target_shares, est_shares, est_price,"
                " est_value, est_cost, current_weight, target_weight, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, sym, f.side if f else ("buy" if tgt_q > cur_q else "sell"), cur_q, tgt_q,
                 f.shares if f else 0, px, f.gross_value if f else 0.0,
                 (f.slippage_cost + f.commission) if f else 0.0,
                 (cur_q * cur_marks.get(sym, 0.0) / nav) if nav else 0.0, targets.get(sym, 0.0),
                 None if f else "Estimated not executable (see unfilled estimate)"))
        level = "warning" if status == "blocked" else "info"
        msg = (f"Rebalance BLOCKED for signal {s}: {sig.blocked_reason}" if status == "blocked" else
               f"Rebalance plan #{plan_id} proposed from signal {s}: {sig.selected_count} targets, "
               f"est. turnover {est.turnover:.1%}; awaiting decision before open of {fill}")
        log_event(conn, level, "rebalance", msg, portfolio_id=pid, session=s)
        return plan_id

    # ------------------------------------------------------------------ helpers
    def _cash(self, conn, pid, session, kind, symbol, amount, balance, note, fill_id=None):
        conn.execute(
            "INSERT INTO cash_transactions (portfolio_id, session, kind, symbol, amount, balance_after, fill_id, note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)", (pid, session, kind, symbol, r2(amount), r2(balance), fill_id, note, utcnow()))

    def _set_position(self, conn, pid, symbol, shares, session, fill=None, split_from=None):
        row = conn.execute("SELECT * FROM positions WHERE portfolio_id=? AND symbol=?", (pid, symbol)).fetchone()
        basis = row["cost_basis"] if row else 0.0
        old = row["shares"] if row else 0
        if fill is not None and fill.side == "buy":
            basis = r2(basis + fill.gross_value + fill.commission)
        elif fill is not None and fill.side == "sell" and old > 0:
            basis = r2(basis * (old - fill.shares) / old)
        elif split_from is not None and split_from > 0 and shares == 0:
            basis = 0.0
        if shares <= 0:
            conn.execute("DELETE FROM positions WHERE portfolio_id=? AND symbol=?", (pid, symbol))
            return
        if row:
            conn.execute("UPDATE positions SET shares=?, cost_basis=?, updated_session=? WHERE portfolio_id=? AND symbol=?",
                         (shares, basis, session, pid, symbol))
        else:
            conn.execute("INSERT INTO positions (portfolio_id, symbol, shares, cost_basis, opened_session, updated_session)"
                         " VALUES (?,?,?,?,?,?)", (pid, symbol, shares, basis, session, session))

    def _record_close(self, conn, panel: Panel, pid: int, t: int, cash: float, shares: dict[str, int], cum_costs: float):
        s = panel.sessions[t]
        value, stale_n, marks = mark_to_market(panel, t, shares)
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
            " positions, stale_marks, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, s, cash, value, nav, r2(nav + cum_costs), bench, len(shares), stale_n, utcnow()))
        if stale_n:
            log_event(conn, "warning", "valuation", f"{stale_n} holding(s) valued at a carried-forward price on {s}",
                      portfolio_id=pid, session=s)
