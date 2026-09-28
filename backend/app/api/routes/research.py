"""Research Lab: backtest configuration, launch, progress and results."""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from ...backtest.engine import earliest_start
from ...backtest.metrics import drawdown, monthly_returns, rolling_metrics
from ...strategy.config import StrategyConfig
from ...services import AppContext
from ..deps import get_ctx, loads
from ..schemas import FillsPage, MonthlyReturn, ResearchDefaults, RunDetail, RunRebalance, RunRequest, RunSeriesPoint, RunSummary

router = APIRouter(prefix="/research", tags=["research"])


def _headline(m: dict | None) -> dict | None:
    if not m:
        return None
    b = m.get("benchmark") or {}
    return {"net_total_return": m["net"]["total_return"], "net_cagr": m["net"]["cagr"],
            "gross_total_return": m["gross"]["total_return"], "ann_vol": m["net"]["ann_vol"],
            "max_drawdown": m["net"]["max_drawdown"], "benchmark_total_return": b.get("total_return"),
            "return_difference": m.get("return_difference"), "avg_turnover": m.get("avg_turnover"),
            "start": m["start_session"], "end": m["end_session"]}


def _run(r: dict, detail: bool = False) -> dict:
    m = loads(r["metrics_json"], None)
    out = {k: r[k] for k in ("id", "name", "status", "progress", "progress_note", "created_at", "started_at",
                             "finished_at", "error", "provider", "data_label", "data_version")}
    out["config"] = StrategyConfig.model_validate_json(r["config_json"])
    out["headline"] = _headline(m)
    if detail:
        out.update(metrics=m, assumptions=loads(r["assumptions_json"], []), warnings=loads(r["warnings_json"], []),
                   repro=loads(r["repro_json"], None))
    return out


def _get(ctx: AppContext, run_id: int) -> dict:
    r = ctx.db.query_one("SELECT * FROM backtest_runs WHERE id=?", (run_id,))
    if not r:
        raise HTTPException(404, f"Backtest run #{run_id} not found")
    return r


@router.get("/defaults", response_model=ResearchDefaults)
def defaults(ctx: AppContext = Depends(get_ctx)):
    panel = ctx.panel()
    prov = ctx.require_provider()
    cfg = StrategyConfig()
    return {"data_label": ctx.data_label(), "config": cfg, "earliest_start": earliest_start(panel, cfg),
            "latest_end": panel.sessions[-1], "benchmark_symbol": panel.benchmark,
            "survivorship_warning": None if prov.info.point_in_time_universe else prov.info.survivorship_note}


@router.post("/runs", response_model=RunSummary)
def launch(req: RunRequest, ctx: AppContext = Depends(get_ctx)):
    panel = ctx.panel()
    cfg = req.config
    if cfg.end_date and cfg.end_date > panel.sessions[-1]:
        raise HTTPException(400, f"end_date is after the last stored session ({panel.sessions[-1]}).")
    if cfg.start_date and cfg.start_date < panel.sessions[0]:
        raise HTTPException(400, f"start_date is before the first stored session ({panel.sessions[0]}).")
    run_id = ctx.submit_backtest(cfg, req.name)
    return _run(_get(ctx, run_id))


@router.get("/runs", response_model=list[RunSummary])
def runs(ctx: AppContext = Depends(get_ctx)):
    prov = ctx.require_provider()
    return [_run(r) for r in ctx.db.query("SELECT * FROM backtest_runs WHERE provider=? ORDER BY id DESC",
                                          (prov.info.key,))]


@router.get("/runs/{run_id}", response_model=RunDetail)
def run_detail(run_id: int, ctx: AppContext = Depends(get_ctx)):
    return _run(_get(ctx, run_id), detail=True)


@router.get("/runs/{run_id}/series", response_model=list[RunSeriesPoint])
def run_series(run_id: int, ctx: AppContext = Depends(get_ctx)):
    _get(ctx, run_id)
    df = pd.DataFrame(ctx.db.query("SELECT * FROM backtest_nav WHERE run_id=? ORDER BY session", (run_id,)))
    if df.empty:
        return []
    df = df.set_index("session")
    df["benchmark_nav"] = df["benchmark_nav"].astype(float)
    roll = rolling_metrics(df)
    dd = drawdown(df["nav"])
    bdd = drawdown(df["benchmark_nav"]) if df["benchmark_nav"].notna().any() else None

    def f(v):
        return None if pd.isna(v) else float(v)

    return [{
        "session": s, "nav": r.nav, "gross_nav": r.gross_nav, "benchmark_nav": f(r.benchmark_nav),
        "drawdown": float(dd[s]), "benchmark_drawdown": f(bdd[s]) if bdd is not None else None,
        "cash_weight": r.cash / r.nav if r.nav else 0.0, "positions": int(r.positions),
        "rolling_return": f(roll.at[s, "rolling_return"]), "rolling_vol": f(roll.at[s, "rolling_vol"]),
        "rolling_bench_return": f(roll.at[s, "rolling_bench_return"]),
        "rolling_difference": f(roll.at[s, "rolling_difference"]),
    } for s, r in df.iterrows()]


@router.get("/runs/{run_id}/monthly", response_model=list[MonthlyReturn])
def run_monthly(run_id: int, ctx: AppContext = Depends(get_ctx)):
    r = _get(ctx, run_id)
    cap = StrategyConfig.model_validate_json(r["config_json"]).initial_capital
    df = pd.DataFrame(ctx.db.query("SELECT session, nav, benchmark_nav FROM backtest_nav WHERE run_id=? ORDER BY session",
                                   (run_id,)))
    if df.empty:
        return []
    df = df.set_index("session")
    strat = monthly_returns(df["nav"], cap)
    bench = {(m["year"], m["month"]): m["return"] for m in monthly_returns(df["benchmark_nav"].astype(float), cap)} \
        if df["benchmark_nav"].notna().any() else {}
    return [{"year": m["year"], "month": m["month"], "strategy": m["return"],
             "benchmark": bench.get((m["year"], m["month"]))} for m in strat]


@router.get("/runs/{run_id}/trades", response_model=FillsPage)
def run_trades(run_id: int, limit: int = Query(200, le=5000), offset: int = 0, symbol: str | None = None,
               ctx: AppContext = Depends(get_ctx)):
    _get(ctx, run_id)
    where, params = "run_id=?", [run_id]
    if symbol:
        where += " AND symbol=?"
        params.append(symbol.upper())
    total = ctx.db.scalar(f"SELECT COUNT(*) FROM backtest_fills WHERE {where}", tuple(params))
    rows = ctx.db.query(f"SELECT * FROM backtest_fills WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                        (*params, limit, offset))
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


@router.get("/runs/{run_id}/rebalances", response_model=list[RunRebalance])
def run_rebalances(run_id: int, ctx: AppContext = Depends(get_ctx)):
    _get(ctx, run_id)
    rows = ctx.db.query("SELECT * FROM backtest_rebalances WHERE run_id=? ORDER BY signal_session DESC", (run_id,))
    for r in rows:
        r["unfilled"] = loads(r.pop("unfilled_json"), [])
    return rows
