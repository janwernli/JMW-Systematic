"""Fully automatic daily cycle for the Alpaca PAPER account.

Run by the Windows scheduler (14:00 and 23:30 Europe/Zurich) or manually. Idempotent: safe to run
any number of times per day.

  1. DATA      refresh end-of-day bars (+ missing SEC sectors); check they reach the latest completed session
  2. SYNC      Alpaca account, positions, order statuses and daily equity history
  3. MODEL     advance the internal model ledger (theoretical track) and auto-apply its month-end plan
  4. STOPS     short stop-loss on the ACTUAL paper positions: close >= avg entry x (1 + stop) -> cover
  5. REBALANCE reconcile ACTUAL positions to the frozen month-end targets (sized from account equity
               and the signal close) for up to 5 sessions after the signal; flips are split into
               close-now / open-next-run; new shorts require Alpaca's easy-to-borrow flag
  6. SUBMIT    only if BROKER_TRADING_ENABLED=true AND automation is switched on AND data is fresh.
               Inside 19:00-09:28 ET: market-on-open ("opg"); during the fill session's regular
               hours: market "day" order (late, flagged); otherwise orders stay planned for the next run.

Nothing here can reach a live-money account: the broker adapter only accepts the paper host.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .broker.alpaca_paper import AlpacaPaperBroker, BrokerError
from .db import log_event, utcnow
from .strategy.config import StrategyConfig

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
TERMINAL_OK = {"filled"}
TERMINAL_FAILED = {"canceled", "expired", "rejected", "done_for_day", "stopped", "suspended"}
PENDING = {"submitted", "new", "accepted", "pending_new", "partially_filled", "held", "accepted_for_bidding",
           "pending_replace", "replaced", "calculated"}
RECONCILE_SESSIONS = 5


@dataclass
class Step:
    name: str
    status: str = "ok"
    detail: str = ""
    data: dict = field(default_factory=dict)


def get_setting(db, key: str, default: str) -> str:
    v = db.scalar("SELECT value FROM app_settings WHERE key=?", (key,))
    return default if v is None else v


def set_setting(db, key: str, value: str) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value, updated_at=excluded.updated_at", (key, value, utcnow()))
        log_event(conn, "warning", "automation", f"Setting {key} changed to {value}")


def submission_mode(now: datetime, fill_session: str, clock_is_open: bool | None) -> tuple[str | None, str]:
    """Return (time_in_force, explanation) for orders meant to execute at `fill_session`'s open."""
    et = now.astimezone(NY)
    fs = datetime.fromisoformat(fill_session).date()
    cutoff = datetime.combine(fs, time(9, 28), NY)
    if et < cutoff and (et.time() < time(9, 28) or et.time() >= time(19, 0)):
        return "opg", "market-on-open (opening auction)"
    if et.date() == fs and clock_is_open:
        return "day", "LATE: opening-auction window missed; market order during the session"
    return None, "outside the submission window; orders stay planned for the next run"


class DailyCycle:
    def __init__(self, ctx, broker: AlpacaPaperBroker | None = None, now_fn=lambda: datetime.now(UTC)):
        self.ctx = ctx
        self.db = ctx.db
        self.cal = ctx.calendar
        self.broker = broker
        self.now_fn = now_fn

    # ------------------------------------------------------------------ public
    def run(self, trigger: str = "manual", dry_run: bool = False) -> dict:
        started = utcnow()
        with self.db.transaction() as conn:
            run_id = conn.execute("INSERT INTO automation_runs (started_at, trigger, dry_run, status) VALUES (?,?,?,?)",
                                  (started, trigger, int(dry_run), "running")).lastrowid
        steps: list[Step] = []
        try:
            self._run(steps, dry_run)
        except Exception as e:  # noqa: BLE001 - recorded, never silently lost
            log.exception("daily cycle failed")
            steps.append(Step("fatal", "error", f"{type(e).__name__}: {e}"))
        worst = "error" if any(s.status == "error" for s in steps) else \
            "warning" if any(s.status == "warning" for s in steps) else "ok"
        summary = "; ".join(f"{s.name}: {s.detail}" for s in steps if s.detail)[:2000]
        with self.db.transaction() as conn:
            conn.execute("UPDATE automation_runs SET finished_at=?, status=?, summary=?, steps_json=? WHERE id=?",
                         (utcnow(), worst, summary, json.dumps([s.__dict__ for s in steps], default=str), run_id))
            log_event(conn, "error" if worst == "error" else "warning" if worst == "warning" else "info", "automation",
                      f"Daily cycle #{run_id} ({trigger}{', dry run' if dry_run else ''}): {worst}", payload={"summary": summary})
        return {"run_id": run_id, "status": worst, "steps": [s.__dict__ for s in steps]}

    # ------------------------------------------------------------------ steps
    def _run(self, steps: list[Step], dry_run: bool) -> None:
        now = self.now_fn()
        ctx = self.ctx

        # 1. data
        st = Step("data")
        try:
            imp = ctx.refresh_data()
            st.detail = f"import #{imp['id']} {imp['status']} through {imp['coverage_end']}"
        except Exception as e:  # noqa: BLE001
            st.status, st.detail = "error", f"refresh failed: {e}"
        latest = self.db.scalar("SELECT MAX(b.session) FROM bars b JOIN instruments i ON i.id=b.instrument_id "
                                "WHERE i.provider=?", (ctx.require_provider().info.key,))
        expected = self.cal.latest_completed_session(now)
        fresh = bool(latest and expected and latest >= expected)
        if not fresh:
            st.status = "error"
            st.detail += f"; STALE: latest stored {latest}, expected {expected} - no new orders"
        st.data = {"latest": latest, "expected": expected, "fresh": fresh}
        steps.append(st)

        # 2. broker sync
        positions: dict[str, dict] = {}
        account: dict = {}
        clock_open = None
        st = Step("sync")
        if self.broker is None:
            st.status, st.detail = "warning", "no broker configured (keys missing?)"
        else:
            try:
                account, positions, clock_open = self._sync(now)
                st.detail = (f"equity ${float(account.get('equity', 0)):,.2f}, {len(positions)} positions, "
                             f"market {'open' if clock_open else 'closed'}")
            except BrokerError as e:
                st.status, st.detail = "error", str(e)
        steps.append(st)

        # 3. model ledger
        st = Step("model")
        plan = None
        try:
            if not ctx.ledger.active():
                ctx.ledger.initialize(StrategyConfig(), inception_session=latest, name="Model portfolio")
            res = ctx.ledger.advance(auto_apply=True)
            pid = ctx.ledger.active()["id"]
            plan = self.db.query_one("SELECT * FROM rebalance_plans WHERE portfolio_id=? ORDER BY signal_session DESC "
                                     "LIMIT 1", (pid,))
            if plan and plan["status"] == "proposed" and plan["signal_session"] == ctx.ledger.active()["as_of_session"]:
                ctx.ledger.apply_plan(plan["id"])
                plan = self.db.query_one("SELECT * FROM rebalance_plans WHERE id=?", (plan["id"],))
            st.detail = f"model as of {res.as_of} ({len(res.processed)} sessions processed)"
            if plan:
                st.detail += f"; latest plan #{plan['id']} signal {plan['signal_session']} {plan['status']}"
        except Exception as e:  # noqa: BLE001
            st.status, st.detail = "error", f"{type(e).__name__}: {e}"
        steps.append(st)

        if self.broker is None or not account or not fresh:
            steps.append(Step("orders", "skipped", "no trading: " + ("data stale" if not fresh else "broker unavailable")))
            return

        # 4-6. orders
        cfg = ctx.ledger.config_of(ctx.ledger.active())
        panel = ctx.panel()
        t = panel.sess_index[latest]
        closes = {s: float(panel.close[t, j]) for s, j in panel.sym_index.items() if np.isfinite(panel.close[t, j])}
        equity = float(account["equity"])
        next_s = self.cal.next_session(latest)
        with self.db.transaction() as conn:  # drafts that were never sent for an earlier session are void
            conn.execute("UPDATE broker_orders SET status='expired_unsent', updated_at=? WHERE status='planned' "
                         "AND intended_session<?", (utcnow(), next_s))
        orders: list[dict] = []
        orders += self._stop_orders(cfg, positions, closes, latest, next_s)
        stopping = {o["symbol"] for o in orders}
        if plan and plan["status"] in ("applied", "executed") and not plan["block_reason"]:
            age = self.cal.index(latest) - self.cal.index(plan["signal_session"])
            if 0 <= age <= RECONCILE_SESSIONS:
                orders += self._reconcile(plan, positions, equity, closes, latest, next_s, stopping)
        st = self._submit(orders, now, next_s, clock_open, dry_run)
        steps.append(st)

    # ------------------------------------------------------------------ helpers
    def _sync(self, now: datetime):
        b = self.broker
        acct = b.account()
        pos = b.positions()
        clock = b.clock()
        stamp = utcnow()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO broker_account_snapshots (as_of, equity, cash, long_market_value, short_market_value,"
                " buying_power, regt_buying_power, multiplier, shorting_enabled, status, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (stamp, float(acct.get("equity") or 0), float(acct.get("cash") or 0),
                 float(acct.get("long_market_value") or 0), float(acct.get("short_market_value") or 0),
                 float(acct.get("buying_power") or 0), float(acct.get("regt_buying_power") or 0),
                 float(acct.get("multiplier") or 0), int(bool(acct.get("shorting_enabled"))), acct.get("status"),
                 json.dumps(acct)))
            conn.executemany(
                "INSERT OR REPLACE INTO broker_positions (as_of, symbol, qty, avg_entry_price, market_value, current_price,"
                " unrealized_pl) VALUES (?,?,?,?,?,?,?)",
                [(stamp, p["symbol"], p["qty"], p["avg_entry_price"], p["market_value"], p["current_price"],
                  p["unrealized_pl"]) for p in pos])
            for o in conn.execute("SELECT * FROM broker_orders WHERE status IN (%s)" % ",".join("?" * len(PENDING)),
                                  tuple(PENDING)).fetchall():
                try:
                    bo = b.order_by_client_id(o["client_order_id"])
                except BrokerError as e:
                    log.warning("order sync failed", extra={"cid": o["client_order_id"], "error": str(e)})
                    continue
                if not bo:
                    continue
                conn.execute(
                    "UPDATE broker_orders SET status=?, broker_order_id=?, filled_qty=?, filled_avg_price=?, filled_at=?,"
                    " updated_at=?, raw_json=? WHERE id=?",
                    (bo.get("status"), bo.get("id"), int(float(bo.get("filled_qty") or 0)),
                     float(bo["filled_avg_price"]) if bo.get("filled_avg_price") else None, bo.get("filled_at"),
                     utcnow(), json.dumps(bo), o["id"]))
        try:
            hist = b.portfolio_history()
            with self.db.transaction() as conn:
                for h in hist:
                    if not h["equity"] or h["equity"] <= 0:
                        continue  # Alpaca reports 0 equity for dates before the account existed
                    d = datetime.fromtimestamp(h["ts"], UTC).astimezone(NY).date().isoformat()
                    if self.cal.is_session(d):
                        conn.execute("INSERT OR REPLACE INTO broker_equity (session, equity, profit_loss, updated_at) "
                                     "VALUES (?,?,?,?)", (d, h["equity"], h["profit_loss"], utcnow()))
        except BrokerError as e:
            log.warning("portfolio history unavailable", extra={"error": str(e)})
        return acct, {p["symbol"]: p for p in pos}, bool(clock.get("is_open"))

    def _stop_orders(self, cfg: StrategyConfig, positions: dict, closes: dict, latest: str, next_s: str) -> list[dict]:
        if not cfg.short_stop_loss:
            return []
        pending = {r["symbol"] for r in self.db.query(
            "SELECT symbol FROM broker_orders WHERE origin='stop_loss' AND status IN (%s)" % ",".join("?" * len(PENDING)),
            tuple(PENDING))}
        out = []
        for sym, p in positions.items():
            entry, close = p["avg_entry_price"], closes.get(sym)
            if p["qty"] >= 0 or not entry or close is None or sym in pending:
                continue
            if close >= entry * (1 + cfg.short_stop_loss):
                out.append({"cid": f"stop-{latest}-{sym}", "origin": "stop_loss", "plan_id": None, "symbol": sym,
                            "side": "buy", "qty": -p["qty"], "effect": "close_short", "ref": close,
                            "why": f"close {close:.2f} >= {1 + cfg.short_stop_loss:.2f} x avg entry {entry:.2f}"})
        return out

    def _reconcile(self, plan: dict, positions: dict, equity: float, closes: dict, latest: str, next_s: str,
                   skip: set[str]) -> list[dict]:
        rows = self.db.query("SELECT symbol, target_weight FROM signal_rows WHERE set_id=? AND selected=1",
                             (plan["signal_set_id"],))
        targets = {r["symbol"]: r["target_weight"] for r in rows}
        prior = {}
        for o in self.db.query("SELECT symbol, status FROM broker_orders WHERE plan_id=? ORDER BY id", (plan["id"],)):
            prior.setdefault(o["symbol"], []).append(o["status"])
        out = []
        for sym in sorted(set(targets) | set(positions)):
            if sym in skip:
                continue
            st = prior.get(sym, [])
            if any(s in PENDING for s in st):
                continue  # an order for this plan is still working at the broker
            st = [s for s in st if s not in ("planned", "expired_unsent")]  # unsent drafts are re-planned in place
            w = targets.get(sym, 0.0)
            cur = positions.get(sym, {}).get("qty", 0)
            ref = closes.get(sym)
            if ref is None or ref <= 0:
                continue
            tgt = int(math.trunc(w * equity / ref))
            if any(s in TERMINAL_OK for s in st) and (tgt == 0) == (cur == 0) and (tgt > 0) == (cur > 0):
                continue  # already traded for this plan and on the right side: no re-trading on equity drift
            if tgt == cur:
                continue
            if cur > 0 and tgt < 0:
                side, qty, effect, why = "sell", cur, "close_long", "flip long->short: close first, short next run"
            elif cur < 0 and tgt > 0:
                side, qty, effect, why = "buy", -cur, "close_short", "flip short->long: cover first, buy next run"
            elif tgt > cur:
                side, qty = "buy", tgt - cur
                effect, why = ("close_short" if cur < 0 else "open_long"), "rebalance to target"
            else:
                side, qty = "sell", cur - tgt
                effect, why = ("close_long" if cur > 0 and tgt >= 0 else "open_short"), "rebalance to target"
            n = len(st)
            out.append({"cid": f"p{plan['id']}-{sym}-{n}", "origin": "rebalance" if n == 0 else "catch_up",
                        "plan_id": plan["id"], "symbol": sym, "side": side, "qty": qty, "effect": effect, "ref": ref,
                        "why": f"{why}; target {tgt} sh ({w:+.2%} of ${equity:,.0f}), current {cur}"})
        return out

    def _submit(self, orders: list[dict], now: datetime, next_s: str, clock_open: bool | None, dry_run: bool) -> Step:
        st = Step("orders")
        enabled = self.ctx.settings.broker_trading_enabled and get_setting(self.db, "automation_enabled", "true") == "true"
        tif, how = submission_mode(now, next_s, clock_open)
        sent = skipped = planned = 0
        for o in orders:
            status, reason = "planned", o["why"]
            if o["effect"] == "open_short":
                try:
                    a = self.broker.asset(o["symbol"])
                    if not (a.get("shortable") and a.get("easy_to_borrow")):
                        status, reason = "skipped", "not easy-to-borrow at Alpaca; short not opened"
                except BrokerError as e:
                    status, reason = "skipped", f"asset check failed: {e}"
            if self.db.scalar("SELECT 1 FROM broker_orders WHERE client_order_id=?", (o["cid"],)):
                row = self.db.query_one("SELECT status FROM broker_orders WHERE client_order_id=?", (o["cid"],))
                if row["status"] != "planned":
                    continue
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO broker_orders (client_order_id, origin, plan_id, symbol, side, position_effect, qty,"
                    " order_type, time_in_force, intended_session, ref_price, status, status_reason, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(client_order_id) DO UPDATE SET"
                    " side=excluded.side, position_effect=excluded.position_effect, qty=excluded.qty,"
                    " time_in_force=excluded.time_in_force, intended_session=excluded.intended_session,"
                    " ref_price=excluded.ref_price, status=excluded.status, status_reason=excluded.status_reason,"
                    " updated_at=excluded.updated_at WHERE broker_orders.status IN ('planned', 'skipped', 'expired_unsent')",
                    (o["cid"], o["origin"], o["plan_id"], o["symbol"], o["side"], o["effect"], o["qty"], "market",
                     tif or "opg", next_s, o["ref"], status, reason, utcnow(), utcnow()))
            if status == "skipped":
                skipped += 1
                continue
            if dry_run or not enabled or tif is None:
                planned += 1
                continue
            try:
                bo = self.broker.submit_order(o["symbol"], o["qty"], o["side"], tif, o["cid"])
                with self.db.transaction() as conn:
                    conn.execute("UPDATE broker_orders SET status=?, broker_order_id=?, submitted_at=?, time_in_force=?,"
                                 " raw_json=?, updated_at=? WHERE client_order_id=?",
                                 (bo.get("status", "submitted"), bo.get("id"), utcnow(), tif, json.dumps(bo), utcnow(),
                                  o["cid"]))
                sent += 1
            except BrokerError as e:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE broker_orders SET status='rejected', status_reason=?, updated_at=? "
                                 "WHERE client_order_id=?", (str(e)[:500], utcnow(), o["cid"]))
                    log_event(conn, "error", "broker", f"Order {o['cid']} rejected: {e}")
                st.status = "error"
        why = ("DRY RUN" if dry_run else "trading disabled (BROKER_TRADING_ENABLED / automation switch)"
               if not enabled else how)
        st.detail = f"{len(orders)} orders computed for {next_s}: {sent} sent, {planned} planned, {skipped} skipped ({why})"
        if tif == "day" and sent:
            st.status = "warning"
        st.data = {"tif": tif, "enabled": enabled, "orders": orders}
        return st
