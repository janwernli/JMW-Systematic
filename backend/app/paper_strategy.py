"""Switching the paper strategy (model ledger + Alpaca paper account) to a pre-defined variant.

    python -m app paper-config --variant spy_overlay [--replan-now] [--dry-run]

preview()  computes everything WITHOUT writing: config changes, the target book from the latest month-end signal
           and the orders the Alpaca paper account would get.
apply()    stores the new config version on the active model portfolio (the Alpaca account follows the model
           ledger's plans), optionally replans now, logs and notifies.

Replan now: rebuilds the plan of the latest month-end signal the ledger has processed with the new config (the
ranking is the month-end close's; only books and sizing change), applies it to the model ledger (fills at the next
open) and supersedes any unexecuted plan. The daily cycle then reconciles the Alpaca positions to it at the next
08:00 ET run: names not in the new target are sold / covered, SPY is bought. Approve mode still applies (the new
plan needs approval); auto mode sends it. If the ledger has not processed the latest month-end yet, no replan is
needed: the next daily run forms that month-end plan with the new config.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from .automation import PENDING, rebalance_mode, rebalance_orders
from .backtest.variants import VARIANT_BY_KEY, Variant, variant_of
from .broker.alpaca_paper import BrokerError
from .db import log_event, utcnow
from .ledger.paper import LedgerError
from .notify import send
from .strategy.config import StrategyConfig
from .strategy.signals import compute_signals


class SwitchError(Exception):
    pass


def active_variant(ctx) -> dict:
    port = ctx.ledger.active()
    if not port:
        return {"key": None, "name": "No model portfolio"}
    v = variant_of(ctx.ledger.config_of(port))
    return {"key": v.key, "name": v.name} if v else {"key": None, "name": "Custom configuration"}


def target_config(ctx, variant: Variant) -> StrategyConfig:
    old = ctx.ledger.config_of(ctx.ledger.require_active())
    return variant.config().model_copy(update={"initial_capital": old.initial_capital,
                                               "benchmark_symbol": old.benchmark_symbol}).paper_view()


def _account(ctx) -> tuple[dict[str, dict], float | None, str]:
    """Current Alpaca paper positions and equity (live, read-only; else the last sync; else the model ledger)."""
    if ctx.broker is not None:
        try:
            acct = ctx.broker.account()
            pos = {p["symbol"]: p for p in ctx.broker.positions()}
            return pos, float(acct["equity"]), "Alpaca paper account (live)"
        except (BrokerError, KeyError, TypeError, ValueError):
            pass
    snap = ctx.db.query_one("SELECT * FROM broker_account_snapshots ORDER BY as_of DESC LIMIT 1")
    if snap:
        pos = {r["symbol"]: {"qty": r["qty"]} for r in ctx.db.query(
            "SELECT symbol, qty FROM broker_positions WHERE as_of=?", (snap["as_of"],))}
        return pos, float(snap["equity"]), f"last Alpaca sync ({snap['as_of']})"
    port = ctx.ledger.require_active()
    nav = ctx.db.scalar("SELECT nav FROM paper_nav WHERE portfolio_id=? ORDER BY session DESC LIMIT 1", (port["id"],))
    return {s: {"qty": q} for s, q in ctx.ledger.positions(port["id"]).items()}, nav, "model ledger (no Alpaca data)"


def preview(ctx, variant_key: str, replan_now: bool, now: datetime | None = None) -> dict:
    variant = VARIANT_BY_KEY.get(variant_key)
    if variant is None:
        raise SwitchError(f"Unknown variant '{variant_key}'. Choose one of: {', '.join(VARIANT_BY_KEY)}.")
    ledger, cal, db = ctx.ledger, ctx.calendar, ctx.db
    port = ledger.require_active()
    old_cfg, new_cfg = ledger.config_of(port), target_config(ctx, variant)
    old_d, new_d = old_cfg.model_dump(), new_cfg.model_dump()
    changes = {k: [old_d.get(k), new_d[k]] for k in new_d if old_d.get(k) != new_d[k]}
    panel = ctx.panel()
    as_of = port["as_of_session"]
    now = now or datetime.now(UTC)
    expected = cal.latest_completed_session(now) or panel.sessions[-1]
    s_ledger = ledger.latest_month_end(as_of)
    s_due = (cal.month_end_sessions(panel.sessions[0], expected) or [None])[-1]

    # ---- what happens to the plans
    open_plans = [r["id"] for r in db.query("SELECT id FROM rebalance_plans WHERE portfolio_id=? AND status IN "
                                            "('proposed','applied')", (port["id"],))]
    working = db.query(f"SELECT client_order_id, symbol, status FROM broker_orders WHERE origin IN "
                       f"('rebalance','catch_up') AND status IN ({','.join('?' * len(PENDING))})", tuple(PENDING))
    if not replan_now:
        replan = {"action": "none", "detail": "The new config applies from the next month-end plan."}
    elif s_due and s_due > as_of:
        replan = {"action": "not_needed",
                  "detail": f"The model ledger (as of {as_of}) has not processed the {s_due} month-end yet: the next daily "
                            "run forms that plan with the new config and trades it at the following open."}
    else:
        cur = db.query_one("SELECT * FROM rebalance_plans WHERE portfolio_id=? AND signal_session=? AND status IN "
                           "('proposed','applied','executed') ORDER BY id DESC LIMIT 1", (port["id"], s_ledger))
        if not changes and cur and cur["config_id"] == port["config_id"]:
            replan = {"action": "already_current", "detail": f"Plan #{cur['id']} already uses this config."}
        else:
            replan = {"action": "replan", "signal_session": s_ledger, "fill_session": cal.next_session(as_of),
                      "supersedes": open_plans, "working_orders": working,
                      "detail": f"New plan from the {s_ledger} month-end signal, priced at the {as_of} close, filled at "
                                f"the {cal.next_session(as_of)} open (Alpaca: next 08:00 ET run"
                                + (", after your approval)" if rebalance_mode(db) == "approve" else ")")
                                + (f"; supersedes plan(s) {open_plans}" if open_plans else "") + "."}
            if working:
                replan["blocked"] = (f"{len(working)} rebalance order(s) are still working at Alpaca (e.g. "
                                     f"{working[0]['client_order_id']}). Wait for them to fill or cancel them in Alpaca, "
                                     "then switch.")

    # ---- the target book (from the latest month-end signal with stored data; data <= that close only)
    s_book = s_ledger if replan["action"] == "replan" else (ledger.latest_month_end(panel.sessions[-1]) or s_ledger)
    book, orders, source = None, [], None
    if s_book:
        held = ledger.positions(port["id"])
        sig = compute_signals(panel, panel.sess_index[s_book], new_cfg, {x for x, q in held.items() if q > 0},
                              {x for x, q in held.items() if q < 0}, set())
        d = sig.diagnostics
        tw = sig.target_weights()
        core = d.get("core", {}).get("weight", 0.0) or 0.0
        book = {
            "signal_session": s_book, "blocked_reason": sig.blocked_reason,
            "spy_weight": core, "longs": sum(1 for s, w in tw.items() if w > 0 and s != d.get("core", {}).get("symbol")),
            "shorts": sum(1 for w in tw.values() if w < 0),
            "long_gross": d.get("long_gross"), "short_gross": d.get("short_gross"),
            "total_gross": d.get("total_gross"), "net_exposure": d.get("total_net_exposure"),
            "overlay_beta": d.get("ex_ante_net_beta"), "beta_long": d.get("beta_long"), "beta_short": d.get("beta_short"),
            "crash_guard": bool(d.get("crash_guard", {}).get("active")),
            "max_sector_net": d.get("sector_neutrality", {}).get("final_max_abs_net"),
            "binding": d.get("binding", []), "notes": d.get("notes", []),
        }
        positions, equity, source = _account(ctx)
        t = len(panel.sessions) - 1
        closes = {s: float(panel.close[t, j]) for s, j in panel.sym_index.items() if np.isfinite(panel.close[t, j])}
        if equity:
            core_sym = d.get("core", {}).get("symbol")
            orders = sorted(rebalance_orders(0, tw, positions, equity, closes, {}, set()),
                            key=lambda o: (o["symbol"] != core_sym, -o["qty"] * o["ref"]))
            for o in orders:
                o.pop("cid", None)
                o["value"] = round(o["qty"] * o["ref"], 2)
    return {
        "variant": {"key": variant.key, "name": variant.name, "description": variant.description},
        "current": active_variant(ctx), "changes": changes, "config": new_cfg, "config_warnings": new_cfg.config_warnings(),
        "ledger_as_of": as_of, "rebalance_mode": rebalance_mode(db),
        "trading_enabled": bool(ctx.settings.broker_trading_enabled), "replan": replan, "book": book,
        "orders": orders, "orders_basis": (f"Against {source}, sized at the {panel.sessions[-1]} closes"
                                           + ("" if replan["action"] == "replan" else
                                              "; illustrative: without a replan these orders come at the next month-end")
                                           + ". New shorts are checked for easy-to-borrow when sent.") if source else None,
    }


def apply(ctx, variant_key: str, replan_now: bool, now: datetime | None = None) -> dict:
    pv = preview(ctx, variant_key, replan_now, now)
    replan = pv["replan"]
    if replan.get("blocked"):
        raise SwitchError(replan["blocked"])
    name = pv["variant"]["name"]
    port = ctx.ledger.require_active()
    new_cfg = target_config(ctx, VARIANT_BY_KEY[variant_key])
    if pv["changes"]:
        ctx.ledger.update_config(new_cfg)
    with ctx.db.transaction() as conn:
        log_event(conn, "warning", "paper", f"Paper strategy changed to {name}" if pv["changes"] else
                  f"Paper strategy confirmed as {name} (unchanged)", portfolio_id=port["id"],
                  payload={"variant": variant_key, "changes": pv["changes"], "replan_now": replan_now})
    plan = None
    if replan["action"] == "replan":
        try:
            plan = ctx.ledger.replan(note=f"switch to {name}")
        except LedgerError as e:
            if e.code != "current":
                raise
        if plan:
            ids = replan["supersedes"]
            if ids:
                with ctx.db.transaction() as conn:
                    conn.execute(f"UPDATE broker_orders SET status='superseded', status_reason=?, updated_at=? "
                                 f"WHERE status='planned' AND plan_id IN ({','.join('?' * len(ids))})",
                                 (f"plan superseded by replan #{plan['id']}", utcnow(), *ids))
    msg = (f"Paper strategy changed to {name}." if pv["changes"] else f"Paper strategy is {name} (no change).")
    if plan:
        msg += (f" Replan #{plan['id']} (signal {plan['signal_session']}) fills at the {plan['fill_session']} open; "
                + ("approve it on the Trading page." if pv["rebalance_mode"] == "approve" else "sent automatically."))
    elif replan["action"] == "not_needed":
        msg += " " + replan["detail"]
    send(ctx.settings, msg, title=f"Paper strategy changed to {name}", priority="high", tags=("arrows_counterclockwise",))
    return {"message": msg, "variant": pv["variant"], "changed": bool(pv["changes"]),
            "plan_id": plan["id"] if plan else None, "replan": replan, "preview": pv}


def format_preview(pv: dict, limit: int = 400) -> str:
    """Plain-text rendering for the CLI."""
    pct = lambda v: "n/a" if v is None else f"{v:.1%}"   # noqa: E731
    out = [f"Variant: {pv['variant']['name']}  (currently: {pv['current']['name']})",
           f"Model ledger as of {pv['ledger_as_of']}; rebalance mode: {pv['rebalance_mode']}; "
           f"BROKER_TRADING_ENABLED={str(pv['trading_enabled']).lower()}"]
    if pv["changes"]:
        out.append("Config changes:")
        out += [f"  {k}: {a} -> {b}" for k, (a, b) in sorted(pv["changes"].items())]
    else:
        out.append("Config changes: none (already this variant)")
    out += [f"Warning: {w}" for w in pv["config_warnings"]]
    r = pv["replan"]
    out.append(f"Replan: {r['action']} - {r['detail']}")
    if r.get("blocked"):
        out.append(f"BLOCKED: {r['blocked']}")
    b = pv["book"]
    if b:
        beta = "n/a" if b.get("overlay_beta") is None else f"{b['overlay_beta']:+.3f}"
        out.append(f"Target book (signal {b['signal_session']}): SPY {pct(b['spy_weight'])}, {b['longs']} longs / "
                   f"{b['shorts']} shorts, overlay long {pct(b['long_gross'])} / short {pct(b['short_gross'])}, "
                   f"total gross {pct(b['total_gross'])}, net {pct(b['net_exposure'])}, est. overlay beta {beta}")
        if b["crash_guard"]:
            out.append("  crash guard ON: overlay short book scaled")
        out += [f"  {x}" for x in b["binding"] + b["notes"]]
        if b["blocked_reason"]:
            out.append(f"  BLOCKED: {b['blocked_reason']}")
    if pv["orders_basis"]:
        buys = sum(o["value"] for o in pv["orders"] if o["side"] == "buy")
        sells = sum(o["value"] for o in pv["orders"] if o["side"] == "sell")
        out.append(f"Orders ({len(pv['orders'])}; buy ${buys:,.0f} / sell ${sells:,.0f}) - {pv['orders_basis']}")
        for o in pv["orders"][:limit]:
            out.append(f"  {o['side']:4s} {o['qty']:>7d} {o['symbol']:<6s} ~${o['value']:>12,.2f}  {o['effect']}")
        if len(pv["orders"]) > limit:
            out.append(f"  ... {len(pv['orders']) - limit} more")
    return "\n".join(out)
