"""Alpaca PAPER account (source of truth for the traded portfolio) and the daily automation."""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...automation import get_setting, set_setting
from ...services import AppContext
from ..deps import get_ctx, loads

router = APIRouter(tags=["broker"])
_run_lock = threading.Lock()


class BrokerPosition(BaseModel):
    symbol: str
    qty: int
    side: str
    avg_entry_price: float | None
    current_price: float | None
    market_value: float | None
    unrealized_pl: float | None
    weight: float | None
    model_weight: float | None
    drift: float | None
    sector: str | None


class EquityPoint(BaseModel):
    session: str
    equity: float | None
    model_nav: float | None
    benchmark: float | None


class AutomationRun(BaseModel):
    id: int
    started_at: str
    finished_at: str | None
    trigger: str
    dry_run: bool
    status: str
    summary: str | None
    steps: list[dict]


class BrokerOverview(BaseModel):
    connected: bool
    error: str | None
    account: dict | None
    synced_at: str | None
    positions: list[BrokerPosition]
    long_value: float
    short_value: float
    equity_history: list[EquityPoint]
    trading_enabled_env: bool
    automation_enabled: bool
    last_runs: list[AutomationRun]
    schedule_note: str
    open_orders: int
    approval_mode: str
    awaiting_approval: list["BrokerOrder"]


class BrokerOrder(BaseModel):
    id: int
    client_order_id: str
    broker_order_id: str | None
    origin: str
    plan_id: int | None
    symbol: str
    side: str
    position_effect: str | None
    qty: int
    time_in_force: str
    intended_session: str
    ref_price: float | None
    status: str
    status_reason: str | None
    filled_qty: int
    filled_avg_price: float | None
    filled_at: str | None
    submitted_at: str | None
    approved_at: str | None = None
    created_at: str
    updated_at: str


BrokerOverview.model_rebuild()


class BrokerOrdersPage(BaseModel):
    total: int
    limit: int
    offset: int
    rows: list[BrokerOrder]


def _run_row(r: dict) -> dict:
    r = dict(r)
    r["dry_run"] = bool(r["dry_run"])
    r["steps"] = loads(r.pop("steps_json", "[]"), [])
    for st in r["steps"]:
        st.get("data", {}).pop("orders", None)  # keep the payload small
    return r


@router.get("/broker/overview", response_model=BrokerOverview)
def overview(ctx: AppContext = Depends(get_ctx)):
    acct = ctx.db.query_one("SELECT * FROM broker_account_snapshots ORDER BY as_of DESC LIMIT 1")
    synced = acct["as_of"] if acct else None
    pos = ctx.db.query("SELECT * FROM broker_positions WHERE as_of=? ORDER BY ABS(market_value) DESC", (synced,)) if synced else []
    equity = acct["equity"] if acct else None
    model_w: dict[str, float] = {}
    port = ctx.ledger.active()
    if port:
        plan = ctx.db.query_one("SELECT signal_set_id FROM rebalance_plans WHERE portfolio_id=? AND status IN "
                                "('applied','executed') ORDER BY signal_session DESC LIMIT 1", (port["id"],))
        if plan and plan["signal_set_id"]:
            model_w = {r["symbol"]: r["target_weight"] for r in ctx.db.query(
                "SELECT symbol, target_weight FROM signal_rows WHERE set_id=? AND selected=1", (plan["signal_set_id"],))}
    sectors = {r["symbol"]: r["sector"] for r in ctx.db.query(
        "SELECT symbol, sector FROM instruments WHERE provider=?", (ctx.require_provider().info.key,))} if ctx.provider else {}
    rows = []
    for p in pos:
        w = (p["market_value"] / equity) if equity and p["market_value"] is not None else None
        mw = model_w.get(p["symbol"], 0.0) if model_w else None
        rows.append({**{k: p[k] for k in ("symbol", "qty", "avg_entry_price", "current_price", "market_value",
                                          "unrealized_pl")}, "side": "short" if p["qty"] < 0 else "long",
                     "weight": w, "model_weight": mw, "drift": (w - mw) if w is not None and mw is not None else None,
                     "sector": sectors.get(p["symbol"])})
    # equity history (Alpaca) with model NAV and benchmark rebased to the first broker equity value
    hist = ctx.db.query("SELECT session, equity FROM broker_equity WHERE equity > 0 ORDER BY session")
    model = {r["session"]: r["nav"] for r in ctx.db.query(
        "SELECT session, nav FROM paper_nav WHERE portfolio_id=? ORDER BY session", (port["id"],))} if port else {}
    bench = {r["session"]: r["benchmark_index"] for r in ctx.db.query(
        "SELECT session, benchmark_index FROM paper_nav WHERE portfolio_id=? ORDER BY session", (port["id"],))} if port else {}
    sessions = sorted(set(model) | {h["session"] for h in hist})
    heq = {h["session"]: h["equity"] for h in hist}
    base_b = next((bench[s] for s in sessions if bench.get(s)), None)
    base_eq = next((heq[s] for s in sessions if heq.get(s)), None) or (port["initial_capital"] if port else None)
    points = [{"session": s, "equity": heq.get(s), "model_nav": model.get(s),
               "benchmark": (base_eq * bench[s] / base_b) if base_b and bench.get(s) and base_eq else None}
              for s in sessions]
    runs = [_run_row(r) for r in ctx.db.query("SELECT * FROM automation_runs ORDER BY id DESC LIMIT 10")]
    return {
        "connected": ctx.broker is not None, "error": ctx.broker_error,
        "account": {k: acct[k] for k in ("equity", "cash", "long_market_value", "short_market_value", "buying_power",
                                         "regt_buying_power", "multiplier", "shorting_enabled", "status")} if acct else None,
        "synced_at": synced, "positions": rows,
        "long_value": sum(p["market_value"] or 0 for p in pos if p["qty"] > 0),
        "short_value": sum(p["market_value"] or 0 for p in pos if p["qty"] < 0),
        "equity_history": points,
        "trading_enabled_env": ctx.settings.broker_trading_enabled,
        "automation_enabled": get_setting(ctx.db, "automation_enabled", "true") == "true",
        "last_runs": runs,
        "schedule_note": ("Windows Task Scheduler runs `npm run daily` at 14:00 (orders: 08:00/09:00 ET, before the "
                          "09:28 ET opening-auction cutoff) and 23:30 Europe/Zurich (after the US close: sync fills). "
                          "Install with `npm run schedule:install`."),
        "open_orders": ctx.db.scalar("SELECT COUNT(*) FROM broker_orders WHERE status IN ('submitted','new','accepted',"
                                     "'pending_new','partially_filled','held')") or 0,
        "approval_mode": get_setting(ctx.db, "rebalance_approval", "auto"),
        "awaiting_approval": ctx.db.query(
            "SELECT * FROM broker_orders WHERE status='planned' AND approved_at IS NULL AND origin IN ('rebalance','catch_up')"
            " AND status_reason LIKE 'awaiting approval%' ORDER BY plan_id, side, symbol"),
    }


@router.get("/broker/orders", response_model=BrokerOrdersPage)
def orders(limit: int = Query(100, le=2000), offset: int = 0, ctx: AppContext = Depends(get_ctx)):
    total = ctx.db.scalar("SELECT COUNT(*) FROM broker_orders")
    rows = ctx.db.query("SELECT * FROM broker_orders ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


@router.get("/automation/runs", response_model=list[AutomationRun])
def runs(limit: int = Query(50, le=500), ctx: AppContext = Depends(get_ctx)):
    return [_run_row(r) for r in ctx.db.query("SELECT * FROM automation_runs ORDER BY id DESC LIMIT ?", (limit,))]


class ApprovalModeRequest(BaseModel):
    mode: str


@router.put("/automation/approval")
def approval_mode(req: ApprovalModeRequest, ctx: AppContext = Depends(get_ctx)) -> dict:
    """'auto': rebalances are sent automatically. 'manual': they wait for approval (stop-losses stay automatic)."""
    if req.mode not in ("auto", "manual"):
        raise HTTPException(400, "mode must be 'auto' or 'manual'")
    set_setting(ctx.db, "rebalance_approval", req.mode)
    return {"approval_mode": req.mode}


class DecisionRequest(BaseModel):
    plan_id: int | None = None


@router.post("/broker/approve")
def approve(req: DecisionRequest, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Approve all orders awaiting approval (optionally for one plan), then run a cycle to send them."""
    from ...db import log_event, utcnow

    with ctx.db.transaction() as conn:
        n = conn.execute(
            "UPDATE broker_orders SET approved_at=?, status_reason=REPLACE(status_reason, 'awaiting approval: ', 'approved: '),"
            " updated_at=? WHERE status='planned' AND approved_at IS NULL AND origin IN ('rebalance','catch_up')"
            + (" AND plan_id=?" if req.plan_id else ""),
            (utcnow(), utcnow(), *([req.plan_id] if req.plan_id else []))).rowcount
        log_event(conn, "warning", "broker", f"User approved {n} rebalance order(s)"
                  + (f" for plan #{req.plan_id}" if req.plan_id else ""))
    started = run_now(AutomationRunRequest(dry_run=False), ctx) if n else {"started": False}
    return {"approved": n, "cycle_started": started.get("started", False),
            "message": f"{n} order(s) approved" + ("; sending now if inside the 19:00-09:28 ET window, otherwise at the "
                                                    "next scheduled run" if n else "")}


@router.post("/broker/decline")
def decline(req: DecisionRequest, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Decline all orders awaiting approval (optionally for one plan). Declined orders are not regenerated."""
    from ...db import log_event, utcnow

    with ctx.db.transaction() as conn:
        n = conn.execute(
            "UPDATE broker_orders SET status='declined', status_reason='declined by user', updated_at=? "
            "WHERE status='planned' AND approved_at IS NULL AND origin IN ('rebalance','catch_up')"
            + (" AND plan_id=?" if req.plan_id else ""), (utcnow(), *([req.plan_id] if req.plan_id else []))).rowcount
        log_event(conn, "warning", "broker", f"User declined {n} rebalance order(s)"
                  + (f" for plan #{req.plan_id}" if req.plan_id else ""))
    return {"declined": n}


class ToggleRequest(BaseModel):
    enabled: bool


@router.put("/automation/enabled")
def toggle(req: ToggleRequest, ctx: AppContext = Depends(get_ctx)) -> dict:
    set_setting(ctx.db, "automation_enabled", "true" if req.enabled else "false")
    return {"automation_enabled": req.enabled}


class AutomationRunRequest(BaseModel):
    dry_run: bool = True


@router.post("/automation/run")
def run_now(req: AutomationRunRequest, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Run the daily cycle in the background (dry run by default: computes orders, sends nothing)."""
    if not _run_lock.acquire(blocking=False):
        return {"started": False, "message": "A daily cycle is already running."}

    def job():
        try:
            ctx.daily_cycle(trigger="manual", dry_run=req.dry_run)
        finally:
            _run_lock.release()

    ctx.executor.submit(job)
    return {"started": True, "message": "Daily cycle started" + (" (dry run: no orders are sent)" if req.dry_run else "")}
