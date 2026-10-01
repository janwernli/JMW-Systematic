"""Strategy-variant comparison: run the pre-defined variants on the same data and period, side by side."""

from __future__ import annotations

import json
import logging

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...backtest.compare import run_report
from ...backtest.engine import earliest_start
from ...backtest.metrics import drawdown
from ...backtest.variants import VARIANT_BY_KEY, VARIANTS
from ...db import log_event, utcnow
from ...services import AppContext
from ...strategy.config import StrategyConfig
from ...paper_strategy import active_variant
from ..deps import get_ctx, loads

log = logging.getLogger(__name__)
router = APIRouter(prefix="/research/variants", tags=["research"])


class VariantDef(BaseModel):
    key: str
    name: str
    description: str
    overrides: dict
    config: StrategyConfig
    config_warnings: list[str]


class StudyRun(BaseModel):
    key: str
    name: str
    run_id: int
    status: str
    progress: float
    error: str | None = None


class VariantStudy(BaseModel):
    id: int
    created_at: str
    start_date: str
    end_date: str
    data_version: str
    runs: list[StudyRun]
    complete: bool


class VariantsOverview(BaseModel):
    variants: list[VariantDef]
    studies: list[VariantStudy]
    default_start: str
    default_end: str
    paper_default: str


class StudyRequest(BaseModel):
    start_date: str | None = None
    end_date: str | None = None


class AlphaSummary(BaseModel):
    alpha_annual: float
    t_alpha: float
    start: str
    end: str
    months: int
    beta_mkt: float | None = None
    beta_mom: float | None = None


class PeriodStats(BaseModel):
    start: str
    end: str
    total_return: float
    cagr: float | None
    ann_vol: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float
    max_dd_peak: str
    max_dd_trough: str
    max_dd_recovery: str | None
    alpha: AlphaSummary | None = None


class WorstMonth(BaseModel):
    year: int
    month: int
    value: float


class MarginFlag(BaseModel):
    session: str
    breaches: list[str]
    gross_exposure: float
    equity: float
    short_requirement: float
    long_requirement: float


class MarginReport(BaseModel):
    limits: dict[str, float]
    gross_breach_days: int
    maintenance_breach_days: int
    first_gross_breach: str | None
    first_maintenance_breach: str | None
    max_gross_exposure: float | None
    max_gross_session: str | None
    min_equity_to_requirement: float | None
    min_equity_to_requirement_session: str | None
    flagged_days: list[MarginFlag]
    flagged_days_truncated: bool


class VariantReport(PeriodStats):
    worst_month: WorstMonth | None
    beta: float | None
    correlation: float | None
    alpha_model: str | None
    avg_turnover: float | None
    annualized_turnover: float | None
    total_costs: float
    slippage_commission: float | None
    borrow_fees: float | None
    margin_interest: float | None
    min_cash_weight: float | None
    cost_drag: float | None
    avg_long_gross: float | None
    avg_short_gross: float | None
    avg_gross_exposure: float | None
    avg_net_exposure: float | None
    max_gross_exposure: float | None
    halves: list[PeriodStats]
    margin: MarginReport | None
    rf_note: str


class StudyVariantResult(BaseModel):
    key: str
    name: str
    run_id: int
    status: str
    report: VariantReport | None


class StudySeriesPoint(BaseModel):
    session: str
    benchmark: float | None
    benchmark_drawdown: float | None
    navs: dict[str, float | None]
    drawdowns: dict[str, float | None]


class StudyResult(BaseModel):
    study: VariantStudy
    results: list[StudyVariantResult]
    series: list[StudySeriesPoint]
    notes: list[str]


def _study(ctx: AppContext, row: dict) -> dict:
    runs = []
    for r in json.loads(row["runs_json"]):
        b = ctx.db.query_one("SELECT status, progress, error FROM backtest_runs WHERE id=?", (r["run_id"],)) or {}
        runs.append({"key": r["key"], "name": VARIANT_BY_KEY[r["key"]].name if r["key"] in VARIANT_BY_KEY else r["key"],
                     "run_id": r["run_id"], "status": b.get("status", "missing"), "progress": b.get("progress") or 0.0,
                     "error": b.get("error")})
    return {**{k: row[k] for k in ("id", "created_at", "start_date", "end_date", "data_version")}, "runs": runs,
            "complete": all(r["status"] in ("completed", "failed") for r in runs)}


@router.get("", response_model=VariantsOverview)
def overview(ctx: AppContext = Depends(get_ctx)):
    panel = ctx.panel()
    variants = []
    for v in VARIANTS:
        cfg = v.config()
        variants.append({"key": v.key, "name": v.name, "description": v.description, "overrides": v.overrides,
                         "config": cfg, "config_warnings": cfg.config_warnings()})
    studies = [_study(ctx, r) for r in ctx.db.query("SELECT * FROM variant_studies ORDER BY id DESC LIMIT 20")]
    return {"variants": variants, "studies": studies, "default_start": earliest_start(panel, StrategyConfig()),
            "default_end": panel.sessions[-1], "paper_default": active_variant(ctx)["name"]}


@router.post("/studies", response_model=VariantStudy)
def launch(req: StudyRequest, ctx: AppContext = Depends(get_ctx)):
    """Queue one backtest per variant on the same data version and period (no parameter search)."""
    panel = ctx.panel()
    start = req.start_date or earliest_start(panel, StrategyConfig())
    end = req.end_date or panel.sessions[-1]
    if start < panel.sessions[0] or end > panel.sessions[-1] or start >= end:
        raise HTTPException(400, f"Period must lie within {panel.sessions[0]}..{panel.sessions[-1]} with start < end.")
    runs = [{"key": v.key, "run_id": ctx.submit_backtest(v.config(start, end), f"Variant: {v.name}")}
            for v in VARIANTS]
    with ctx.db.transaction() as conn:
        sid = conn.execute("INSERT INTO variant_studies (created_at, start_date, end_date, data_version, runs_json)"
                           " VALUES (?,?,?,?,?)", (utcnow(), start, end, ctx.data_version(), json.dumps(runs))).lastrowid
        log_event(conn, "info", "backtest", f"Variant comparison #{sid} queued ({start}..{end})",
                  payload={"runs": runs})
    return _study(ctx, ctx.db.query_one("SELECT * FROM variant_studies WHERE id=?", (sid,)))


@router.get("/studies/{study_id}", response_model=StudyResult)
def result(study_id: int, ctx: AppContext = Depends(get_ctx)):
    row = ctx.db.query_one("SELECT * FROM variant_studies WHERE id=?", (study_id,))
    if not row:
        raise HTTPException(404, f"Variant comparison #{study_id} not found")
    study = _study(ctx, row)
    notes = []
    factors = None
    try:
        from ...backtest.factors import load_factors
        from ...config import REPO_ROOT

        factors = load_factors(REPO_ROOT / "data" / "factors")
    except Exception as e:  # noqa: BLE001
        notes.append(f"Ken French factors unavailable ({e}): no alpha, and Sharpe/Sortino use RF = 0.")
    prov = ctx.require_provider()
    if not prov.info.point_in_time_universe:
        notes.append(prov.info.survivorship_note)
        notes.append("Survivorship bias favours LONG exposure: the net-long variants (SPY + overlay, 130/30) benefit "
                     "most from a universe of today's survivors; their long-book results are the most inflated.")
    results, navs, bench = [], {}, None
    for r in study["runs"]:
        report = None
        if r["status"] == "completed":
            run = ctx.db.query_one("SELECT metrics_json, config_json FROM backtest_runs WHERE id=?", (r["run_id"],))
            cfg = StrategyConfig.model_validate_json(run["config_json"])
            nav = pd.DataFrame(ctx.db.query(
                "SELECT session, nav, cash, benchmark_nav, long_value, short_value FROM backtest_nav WHERE run_id=? "
                "ORDER BY session", (r["run_id"],))).set_index("session")
            report = run_report(nav, loads(run["metrics_json"], {}), cfg.cash_interest, factors, cfg.initial_capital)
            navs[r["key"]] = nav["nav"] / cfg.initial_capital
            if bench is None and nav["benchmark_nav"].notna().any():
                bench = nav["benchmark_nav"].astype(float) / cfg.initial_capital
        results.append({"key": r["key"], "name": r["name"], "run_id": r["run_id"], "status": r["status"],
                        "report": report})
    series = []
    if navs:
        frame = pd.DataFrame(navs)
        dds = {k: drawdown(frame[k].dropna()) for k in frame}
        bdd = drawdown(bench.dropna()) if bench is not None else None

        def f(v):
            return None if v is None or pd.isna(v) else float(v)

        for s in frame.index:
            series.append({
                "session": s, "benchmark": f(bench.get(s)) if bench is not None else None,
                "benchmark_drawdown": f(bdd.get(s)) if bdd is not None else None,
                "navs": {k: f(frame.at[s, k]) for k in frame}, "drawdowns": {k: f(dds[k].get(s)) for k in frame}})
    return {"study": study, "results": results, "series": series, "notes": notes}
