"""System status, data quality, imports and data refresh."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ... import __version__
from ...data.store import quality_report, run_import
from ...db import log_event
from ...strategy.config import StrategyConfig
from ...strategy.signals import compute_signals
from ...services import AppContext
from ..deps import get_ctx, import_row
from ..schemas import DataImport, DataQuality, SystemStatus

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/status", response_model=SystemStatus)
def status(ctx: AppContext = Depends(get_ctx)):
    now = datetime.now(UTC)
    prov = ctx.provider
    rng = None
    if prov:
        rng = ctx.db.query_one(
            "SELECT MIN(b.session) lo, MAX(b.session) hi FROM bars b JOIN instruments i ON i.id=b.instrument_id "
            "WHERE i.provider=?", (prov.info.key,))
    port = ctx.ledger.active() if prov else None
    has_data = bool(rng and rng["hi"])
    return {
        "app_version": __version__,
        "mode": ("demo" if prov.info.is_demo else "live") if prov else
                ("demo" if ctx.settings.market_data_provider == "demo" else "live"),
        "data_label": ctx.data_label(),
        "provider": prov.info.to_dict() if prov else None,
        "provider_error": ctx.provider_error,
        "market_clock": ctx.calendar.market_clock(now),
        "data_version": ctx.data_version() if has_data else None,
        "data_start": rng["lo"] if rng else None,
        "data_end": rng["hi"] if rng else None,
        "has_portfolio": port is not None,
        "portfolio_as_of": port["as_of_session"] if port else None,
        "server_time_utc": now.isoformat(timespec="seconds"),
        "bind_host": ctx.settings.host,
    }


@router.get("/data/quality", response_model=DataQuality)
def data_quality(ctx: AppContext = Depends(get_ctx)):
    prov = ctx.require_provider()
    port = ctx.ledger.active()
    cfg = ctx.ledger.config_of(port) if port else StrategyConfig()
    rep = quality_report(ctx.db, prov, ctx.calendar, cfg.min_session_coverage)
    rep["latest_import"] = import_row(rep["latest_import"])
    rep["latest_successful_import"] = import_row(rep["latest_successful_import"])
    rep["eligible_count"] = None
    if rep["has_data"]:
        panel = ctx.panel()
        rep["eligible_count"] = compute_signals(panel, panel.T - 1, cfg).eligible_count
    rep["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return rep


@router.get("/data/imports", response_model=list[DataImport])
def imports(ctx: AppContext = Depends(get_ctx)):
    return [import_row(r) for r in ctx.db.query("SELECT * FROM data_imports ORDER BY id DESC LIMIT 100")]


class RefreshResponse(BaseModel):
    started: bool
    message: str


@router.post("/data/refresh", response_model=RefreshResponse)
def refresh(ctx: AppContext = Depends(get_ctx)):
    """Re-import from the configured provider in the background (idempotent upserts).

    Live providers fetch from 10 sessions before the latest stored session, so late
    corrections are picked up; the demo fixture is simply re-imported.
    """
    prov = ctx.require_provider()
    start = None
    if not prov.info.is_demo:
        hi = ctx.db.scalar("SELECT MAX(b.session) FROM bars b JOIN instruments i ON i.id=b.instrument_id "
                           "WHERE i.provider=?", (prov.info.key,))
        if hi:
            start = ctx.calendar.offset(ctx.calendar.session_on_or_before(hi), -10)

    def job():
        try:
            run_import(ctx.db, prov, ctx.calendar, start=start)
        except Exception as e:  # noqa: BLE001 - recorded in data_imports + events by run_import
            with ctx.db.transaction() as conn:
                log_event(conn, "error", "data_import", f"Background refresh failed: {e}")

    ctx.executor.submit(job)
    return {"started": True, "message": f"Import from {prov.info.name} started"
                                        + (f" (from {start})" if start else " (full history)")}
