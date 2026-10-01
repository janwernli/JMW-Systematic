"""Rebalance Desk: frozen plans, rule checks, apply / skip (internal virtual portfolio only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...data.store import quality_report
from ...strategy.config import StrategyConfig
from ...services import AppContext
from ..deps import get_ctx, loads
from ..schemas import PlanDetail, PlanSummary

router = APIRouter(prefix="/rebalance", tags=["rebalance"])


def _summary(p: dict) -> dict:
    est = loads(p["estimate_json"], {})
    ex = loads(p.get("execution_json"), None)
    return {
        "id": p["id"], "signal_session": p["signal_session"], "fill_session": p["fill_session"], "status": p["status"],
        "kind": p["kind"] if "kind" in p.keys() else "month_end",
        "block_reason": p["block_reason"], "created_at": p["created_at"], "decided_at": p["decided_at"],
        "executed_at": p["executed_at"], "selected_count": est.get("selected_count", 0),
        "est_turnover": est.get("est_turnover"), "realized_turnover": ex.get("turnover") if ex else None,
    }


def _detail(ctx: AppContext, plan: dict) -> dict:
    port = ctx.db.query_one("SELECT * FROM paper_portfolios WHERE id=?", (plan["portfolio_id"],))
    cfg_row = ctx.db.query_one("SELECT config_json FROM strategy_configs WHERE id=?", (plan["config_id"],))
    orders = ctx.db.query(
        "SELECT po.*, o.status AS order_status, o.filled_shares, o.status_reason AS order_reason FROM plan_orders po "
        "LEFT JOIN paper_orders o ON o.plan_id=po.plan_id AND o.symbol=po.symbol WHERE po.plan_id=? "
        "ORDER BY po.side DESC, po.est_value DESC", (plan["id"],))
    # Current weights at the signal-session close, from the immutable position snapshots
    # (covers holdings that need no trade and therefore have no order row).
    nav_row = ctx.db.query_one("SELECT nav FROM paper_nav WHERE portfolio_id=? AND session=?",
                               (plan["portfolio_id"], plan["signal_session"]))
    cur_w = {r["symbol"]: r["market_value"] / nav_row["nav"] for r in ctx.db.query(
        "SELECT symbol, market_value FROM position_snapshots WHERE portfolio_id=? AND session=?",
        (plan["portfolio_id"], plan["signal_session"]))} if nav_row and nav_row["nav"] else {}
    targets = [
        {**t, "current_weight": cur_w.get(t["symbol"], 0.0)} for t in ctx.db.query(
            "SELECT symbol, rank, momentum, close_raw, adv20, target_weight, side, vol, beta, percentile, composite, sector "
            "FROM signal_rows "
            "WHERE set_id=? AND selected=1 ORDER BY rank", (plan["signal_set_id"],))
    ] if plan["signal_set_id"] else []
    fills = ctx.db.query("SELECT * FROM paper_fills WHERE plan_id=? ORDER BY id", (plan["id"],))
    checks = loads(plan["checks_json"], [])
    can, why = False, None
    if plan["status"] != "proposed":
        why = {"applied": "Already applied - fills execute at the next session's open when you advance.",
               "executed": "Already executed.", "skipped": "Skipped by user.",
               "blocked": f"Blocked: {plan['block_reason']}"}.get(plan["status"], plan["status"])
    elif port["status"] != "active":
        why = "Portfolio is archived."
    elif port["as_of_session"] != plan["signal_session"]:
        why = "The portfolio has moved past this plan's fill session."
    else:
        q = quality_report(ctx.db, ctx.require_provider(), ctx.calendar)
        failed = [c["rule"] for c in checks if not c["ok"]]
        if failed:
            why = "Rule checks failed: " + ", ".join(failed)
        elif q["coverage_end"] and q["coverage_end"] < plan["signal_session"]:
            why = "Stored data does not reach the signal session."
        else:
            can = True
    dv_now = ctx.data_version()
    if plan["data_version"] != dv_now and plan["status"] == "proposed":
        checks = checks + [{"rule": "Data unchanged since signal", "ok": True,
                            "detail": f"Data was updated after the signal ({plan['data_version']} -> {dv_now}). "
                                      "The frozen signal is used unchanged."}]
    return {
        **_summary(plan), "data_label": "Paper Simulation", "data_version": plan["data_version"],
        "config": StrategyConfig.model_validate_json(cfg_row["config_json"]),
        "estimate": loads(plan["estimate_json"], {}), "execution": loads(plan["execution_json"], None),
        "checks": checks, "orders": orders, "targets": targets, "fills": fills, "can_apply": can,
        "apply_disabled_reason": why, "portfolio_as_of": port["as_of_session"],
    }


@router.get("/plans", response_model=list[PlanSummary])
def plans(ctx: AppContext = Depends(get_ctx)):
    port = ctx.ledger.active()
    if not port:
        return []
    return [_summary(p) for p in ctx.db.query(
        "SELECT * FROM rebalance_plans WHERE portfolio_id=? ORDER BY signal_session DESC, id DESC", (port["id"],))]


@router.get("/current", response_model=PlanDetail | None)
def current(ctx: AppContext = Depends(get_ctx)):
    port = ctx.ledger.active()
    if not port:
        return None
    plan = ctx.ledger.pending_plan(port["id"]) or ctx.db.query_one(
        "SELECT * FROM rebalance_plans WHERE portfolio_id=? AND status <> 'superseded' ORDER BY signal_session DESC, "
        "id DESC LIMIT 1", (port["id"],))
    return _detail(ctx, plan) if plan else None


@router.get("/plans/{plan_id}", response_model=PlanDetail)
def plan_detail(plan_id: int, ctx: AppContext = Depends(get_ctx)):
    plan = ctx.db.query_one("SELECT * FROM rebalance_plans WHERE id=?", (plan_id,))
    if not plan:
        raise HTTPException(404, f"Plan #{plan_id} not found")
    return _detail(ctx, plan)


class SkipRequest(BaseModel):
    note: str | None = None


@router.post("/plans/{plan_id}/apply", response_model=PlanDetail)
def apply(plan_id: int, ctx: AppContext = Depends(get_ctx)):
    """Apply the plan to the INTERNAL VIRTUAL PORTFOLIO ONLY. Nothing is sent to any broker."""
    detail = plan_detail(plan_id, ctx)
    if not detail["can_apply"] and detail["status"] == "proposed":
        raise HTTPException(409, detail["apply_disabled_reason"] or "Plan cannot be applied")
    ctx.ledger.apply_plan(plan_id)
    return plan_detail(plan_id, ctx)


@router.post("/plans/{plan_id}/skip", response_model=PlanDetail)
def skip(plan_id: int, req: SkipRequest, ctx: AppContext = Depends(get_ctx)):
    ctx.ledger.skip_plan(plan_id, req.note)
    return plan_detail(plan_id, ctx)
