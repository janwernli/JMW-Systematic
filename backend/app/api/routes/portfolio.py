"""Command Center, positions, and internal virtual-portfolio controls."""

from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Depends

from ...data.store import quality_report
from ...db import log_event
from ...strategy.config import StrategyConfig
from ...services import AppContext
from ..deps import event_row, get_ctx
from ..schemas import (
    AdvanceResponse,
    CommandCenter,
    PaperAdvanceRequest,
    PaperConfigResponse,
    PaperInitRequest,
    PortfolioMeta,
    PositionsResponse,
)
from .universe import latest_plan

router = APIRouter(tags=["portfolio"])

STOP_TEXT = {
    "decision_required": "A month-end rebalance plan is waiting. Apply it to the internal virtual portfolio or skip it "
                         "in the Rebalance Desk before advancing past its fill session.",
    "no_newer_data": "All stored sessions have been processed. Refresh market data to continue.",
    "reached_target": "Reached the requested session.",
    "max_sessions": "Processed the requested number of sessions.",
    "session_without_data": "The next session has no stored bars; advancing halted rather than substituting prices.",
}


def _returns_1d(ctx: AppContext, symbols, session: str) -> dict[str, float | None]:
    panel = ctx.panel()
    t = panel.sess_index.get(session)
    out: dict[str, float | None] = {}
    for s in symbols:
        j = panel.sym_index.get(s)
        if t is None or t == 0 or j is None:
            out[s] = None
            continue
        a, b = panel.tr[t, j], panel.tr[t - 1, j]
        out[s] = None if np.isnan(a) or np.isnan(b) else float(a / b - 1)
    return out


@router.get("/portfolio/summary", response_model=CommandCenter)
def command_center(ctx: AppContext = Depends(get_ctx)):
    prov = ctx.require_provider()
    port = ctx.ledger.active()
    q = quality_report(ctx.db, prov, ctx.calendar)
    base = {
        "data_label": "Paper Simulation" if port else ctx.data_label(), "portfolio": port, "valuation_session": None,
        "nav": None, "cash": None, "invested": None, "positions": 0, "daily_return": None, "daily_pnl": None,
        "inception_return": None, "gross_inception_return": None, "benchmark_symbol": prov.info.benchmark_symbol,
        "benchmark_return_basis": prov.info.benchmark_return_basis, "benchmark_inception_return": None,
        "return_difference": None, "drawdown": None, "max_drawdown": None,
        "next_rebalance": {"signal_session": None, "fill_session": None, "pending_plan_id": None,
                           "pending_plan_status": None, "blocked_reason": None},
        "nav_series": [], "top_movers": [], "recent_fills": [], "alerts": [],
        "data_is_stale": q["is_stale"], "data_stale_reason": q["stale_reason"], "data_end": q["coverage_end"],
    }
    # Alerts for the active portfolio, plus system-wide events (imports etc.) since it was created.
    if port:
        events = ctx.db.query(
            "SELECT * FROM system_events WHERE level IN ('warning','error') AND (portfolio_id=? OR "
            "(portfolio_id IS NULL AND ts>=?)) ORDER BY id DESC LIMIT 12", (port["id"], port["created_at"]))
    else:
        events = ctx.db.query("SELECT * FROM system_events WHERE level IN ('warning','error') AND portfolio_id IS NULL "
                              "ORDER BY id DESC LIMIT 12")
    base["alerts"] = [event_row(e) for e in events]
    if not port:
        return base
    pid = port["id"]
    navs = ctx.db.query("SELECT * FROM paper_nav WHERE portfolio_id=? ORDER BY session", (pid,))
    cap = port["initial_capital"]
    b0 = navs[0]["benchmark_index"] if navs else None
    peak = -np.inf
    series = []
    for r in navs:
        peak = max(peak, r["nav"])
        series.append({"session": r["session"], "nav": r["nav"], "gross_nav": r["gross_nav"], "cash": r["cash"],
                       "benchmark_nav": (cap * r["benchmark_index"] / b0) if b0 and r["benchmark_index"] else None,
                       "drawdown": r["nav"] / peak - 1})
    last = navs[-1]
    prev = navs[-2] if len(navs) > 1 else None
    shares = ctx.ledger.positions(pid)
    snaps = ctx.db.query("SELECT symbol, market_value FROM position_snapshots WHERE portfolio_id=? AND session=?",
                         (pid, last["session"]))
    r1 = _returns_1d(ctx, shares, last["session"])
    movers = []
    for sn in snaps:
        ret = r1.get(sn["symbol"])
        pnl = sn["market_value"] - sn["market_value"] / (1 + ret) if ret is not None else 0.0
        movers.append({"symbol": sn["symbol"], "return_1d": ret, "pnl_1d": pnl, "weight": sn["market_value"] / last["nav"]})
    movers.sort(key=lambda m: abs(m["return_1d"] or 0), reverse=True)
    fills = ctx.db.query("SELECT * FROM paper_fills WHERE portfolio_id=? ORDER BY id DESC LIMIT 12", (pid,))

    plan = ctx.ledger.pending_plan(pid)
    latest = ctx.db.query_one("SELECT * FROM rebalance_plans WHERE portfolio_id=? ORDER BY id DESC LIMIT 1", (pid,))
    if plan:
        nxt = {"signal_session": plan["signal_session"], "fill_session": plan["fill_session"],
               "pending_plan_id": plan["id"], "pending_plan_status": plan["status"], "blocked_reason": None}
    else:
        sig = ctx.calendar.next_month_end(ctx.calendar.next_session(port["as_of_session"]) or port["as_of_session"])
        nxt = {"signal_session": sig, "fill_session": ctx.calendar.next_session(sig) if sig else None,
               "pending_plan_id": None, "pending_plan_status": None,
               "blocked_reason": latest["block_reason"] if latest and latest["status"] == "blocked" else None}
    bench_ret = series[-1]["benchmark_nav"] / cap - 1 if series and series[-1]["benchmark_nav"] else None
    lv = last.get("long_value") or 0.0
    sv = -(last.get("short_value") or 0.0)
    inc = last["nav"] / cap - 1
    base.update({
        "valuation_session": last["session"], "nav": last["nav"], "cash": last["cash"],
        "invested": last["positions_value"], "positions": last["positions"],
        "daily_return": (last["nav"] / prev["nav"] - 1) if prev else None,
        "daily_pnl": (last["nav"] - prev["nav"]) if prev else None,
        "inception_return": inc, "gross_inception_return": last["gross_nav"] / cap - 1,
        "benchmark_inception_return": bench_ret,
        "return_difference": (inc - bench_ret) if bench_ret is not None else None,
        "drawdown": series[-1]["drawdown"], "max_drawdown": min(s["drawdown"] for s in series),
        "mode": ctx.ledger.config_of(port).mode, "long_gross": lv / last["nav"], "short_gross": sv / last["nav"],
        "net_exposure": (lv - sv) / last["nav"],
        "next_rebalance": nxt, "nav_series": series, "top_movers": movers[:8], "recent_fills": fills,
    })
    return base


@router.get("/portfolio/positions", response_model=PositionsResponse)
def positions(ctx: AppContext = Depends(get_ctx)):
    port = ctx.ledger.require_active()
    pid = port["id"]
    last = ctx.db.query_one("SELECT * FROM paper_nav WHERE portfolio_id=? ORDER BY session DESC LIMIT 1", (pid,))
    s = last["session"]
    nav = last["nav"]
    rows_db = ctx.db.query(
        "SELECT ps.*, p.opened_session, i.name, i.sector, i.sector_source FROM position_snapshots ps "
        "LEFT JOIN positions p ON p.portfolio_id=ps.portfolio_id AND p.symbol=ps.symbol "
        "LEFT JOIN instruments i ON i.symbol=ps.symbol AND i.provider=? "
        "WHERE ps.portfolio_id=? AND ps.session=? ORDER BY ABS(ps.market_value) DESC",
        (ctx.require_provider().info.key, pid, s))
    plan = latest_plan(ctx, pid)
    targets: dict[str, float] = {}
    if plan and plan["signal_set_id"]:
        targets = {r["symbol"]: r["target_weight"] for r in ctx.db.query(
            "SELECT symbol, target_weight FROM signal_rows WHERE set_id=? AND selected=1", (plan["signal_set_id"],))}
    r1 = _returns_1d(ctx, [r["symbol"] for r in rows_db], s)
    rows = []
    for r in rows_db:
        w = r["market_value"] / nav if nav else 0.0
        tw = targets.get(r["symbol"], 0.0) if plan else None
        pnl = r["market_value"] - r["cost_basis"]
        rows.append({
            "symbol": r["symbol"], "side": "short" if r["shares"] < 0 else "long",
            "name": r["name"], "sector": r["sector"], "shares": r["shares"],
            "mark_price": r["mark_price"], "mark_session": r["mark_session"], "stale_mark": r["mark_session"] != s,
            "market_value": r["market_value"], "weight": w, "target_weight": tw,
            "drift": (w - tw) if tw is not None else None, "cost_basis": r["cost_basis"], "unrealized_pnl": pnl,
            "unrealized_pct": pnl / abs(r["cost_basis"]) if r["cost_basis"] else None,
            "contribution": pnl / nav if nav else 0,
            "return_1d": r1.get(r["symbol"]), "opened_session": r["opened_session"],
        })
    weights = sorted((abs(x["weight"]) for x in rows), reverse=True)
    invested = sum(weights)
    long_g = sum(x["weight"] for x in rows if x["weight"] > 0)
    short_g = -sum(x["weight"] for x in rows if x["weight"] < 0)
    hhi = sum((w / invested) ** 2 for w in weights) if invested else 0.0
    sector_ok = rows and all(x["sector"] for x in rows)
    sources = {r["sector_source"] for r in rows_db if r["sector_source"]}
    sectors = None
    if sector_ok:
        agg: dict[str, list] = {}
        for x in rows:
            agg.setdefault(x["sector"], []).append(x["weight"])
        sectors = sorted(({"sector": k, "weight": sum(v), "long_weight": sum(w for w in v if w > 0),
                           "short_weight": sum(w for w in v if w < 0), "positions": len(v)} for k, v in agg.items()),
                         key=lambda d: -abs(d["weight"]))
    return {
        "data_label": "Paper Simulation", "valuation_session": s,
        "valuation_note": f"Marked at raw closes of {s}; stale marks carry the last available close.",
        "nav": nav, "cash": last["cash"], "cash_weight": last["cash"] / nav if nav else 0.0, "rows": rows,
        "long_gross": long_g, "short_gross": short_g, "net_exposure": long_g - short_g,
        "mode": ctx.ledger.config_of(port).mode,
        "sector_exposure": sectors,
        "sector_note": (f"Sector source: {', '.join(sorted(sources))}." if sector_ok else
                        "Sector exposure hidden: the data provider does not supply reliable sector metadata."),
        "concentration": {
            "top5_weight": sum(weights[:5]), "top10_weight": sum(weights[:10]),
            "largest_symbol": max(rows, key=lambda x: abs(x["weight"]))["symbol"] if rows else None,
            "largest_weight": weights[0] if weights else 0.0,
            "hhi": hhi, "effective_n": (1 / hhi) if hhi else None,
        },
        "plan_signal_session": plan["signal_session"] if plan else None,
    }


# ------------------------------------------------------------------ paper controls
@router.post("/paper/init", response_model=PortfolioMeta)
def paper_init(req: PaperInitRequest, ctx: AppContext = Depends(get_ctx)):
    return ctx.ledger.initialize(req.config or StrategyConfig(), req.inception_session, req.name)


@router.post("/paper/advance", response_model=AdvanceResponse)
def paper_advance(req: PaperAdvanceRequest, ctx: AppContext = Depends(get_ctx)):
    res = ctx.ledger.advance(until=req.until, max_sessions=req.max_sessions)
    return {"processed": res.processed, "processed_count": len(res.processed), "as_of": res.as_of,
            "stopped_reason": res.stopped_reason, "stopped_explanation": STOP_TEXT.get(res.stopped_reason, ""),
            "pending_plan_id": res.pending_plan_id}


@router.post("/paper/reset")
def paper_reset(ctx: AppContext = Depends(get_ctx)) -> dict:
    ctx.ledger.reset()
    return {"ok": True, "message": "Virtual portfolio archived. Its full history remains in the ledger."}


@router.get("/paper/config", response_model=PaperConfigResponse)
def paper_config(ctx: AppContext = Depends(get_ctx)):
    port = ctx.ledger.require_active()
    hist = ctx.db.query(
        "SELECT e.ts, e.payload_json FROM system_events e WHERE e.portfolio_id=? AND e.category='paper' "
        "AND e.message LIKE 'Paper strategy config changed%' ORDER BY e.id", (port["id"],))
    from ..deps import loads
    return {"config_id": port["config_id"], "config": ctx.ledger.config_of(port),
            "history": [{"ts": h["ts"], **loads(h["payload_json"], {})} for h in hist]}


@router.put("/paper/config", response_model=PaperConfigResponse)
def paper_config_update(cfg: StrategyConfig, ctx: AppContext = Depends(get_ctx)):
    ctx.ledger.update_config(cfg)
    return paper_config(ctx)
