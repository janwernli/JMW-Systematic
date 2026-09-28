"""Ledger & Diagnostics: orders, fills, cash, audit events, reproducibility."""

from __future__ import annotations

import platform
from importlib.metadata import version as pkg_version

from fastapi import APIRouter, Depends, Query

from ... import __version__
from ...backtest.runner import git_commit
from ...services import AppContext
from ..deps import event_row, get_ctx, loads
from ..schemas import CashPage, EventsPage, FillsPage, OrdersPage, Reproducibility

router = APIRouter(prefix="/ledger", tags=["ledger"])


def _page(ctx: AppContext, table: str, where: str, params: tuple, limit: int, offset: int, order: str = "id DESC"):
    total = ctx.db.scalar(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
    rows = ctx.db.query(f"SELECT * FROM {table} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset))
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


def _pid(ctx: AppContext) -> int:
    p = ctx.ledger.active()
    return p["id"] if p else -1


@router.get("/orders", response_model=OrdersPage)
def orders(limit: int = Query(100, le=2000), offset: int = 0, ctx: AppContext = Depends(get_ctx)):
    return _page(ctx, "paper_orders", "portfolio_id=?", (_pid(ctx),), limit, offset)


@router.get("/fills", response_model=FillsPage)
def fills(limit: int = Query(100, le=2000), offset: int = 0, ctx: AppContext = Depends(get_ctx)):
    return _page(ctx, "paper_fills", "portfolio_id=?", (_pid(ctx),), limit, offset)


@router.get("/cash", response_model=CashPage)
def cash(limit: int = Query(100, le=2000), offset: int = 0, ctx: AppContext = Depends(get_ctx)):
    return _page(ctx, "cash_transactions", "portfolio_id=?", (_pid(ctx),), limit, offset)


@router.get("/events", response_model=EventsPage)
def events(limit: int = Query(100, le=2000), offset: int = 0, level: str | None = None, category: str | None = None,
           ctx: AppContext = Depends(get_ctx)):
    where, params = "1=1", []
    if level:
        where += " AND level=?"
        params.append(level)
    if category:
        where += " AND category=?"
        params.append(category)
    page = _page(ctx, "system_events", where, tuple(params), limit, offset)
    page["rows"] = [event_row(r) for r in page["rows"]]
    return page


@router.get("/reproducibility", response_model=Reproducibility)
def reproducibility(ctx: AppContext = Depends(get_ctx)):
    pid = _pid(ctx)
    cfgs = ctx.db.query(
        "SELECT DISTINCT c.id, c.config_hash, c.created_at, c.config_json FROM strategy_configs c JOIN rebalance_plans p "
        "ON p.config_id=c.id WHERE p.portfolio_id=? UNION SELECT c.id, c.config_hash, c.created_at, c.config_json FROM "
        "strategy_configs c JOIN paper_portfolios pp ON pp.config_id=c.id WHERE pp.id=? ORDER BY 1", (pid, pid))
    for c in cfgs:
        c["config"] = loads(c.pop("config_json"), {})
    return {
        "app_version": __version__, "git_commit": git_commit(), "python": platform.python_version(),
        "packages": {p: pkg_version(p) for p in ("fastapi", "pydantic", "numpy", "pandas", "exchange-calendars", "httpx")},
        "database_path": str(ctx.settings.database_path),
        "schema_migrations": ctx.db.query("SELECT * FROM schema_migrations ORDER BY version"),
        "provider": ctx.provider.info.key if ctx.provider else "none",
        "data_version": ctx.data_version() if ctx.has_data() else None,
        "portfolio_configs": cfgs,
        "plan_data_versions": ctx.db.query(
            "SELECT id AS plan_id, signal_session, status, data_version, config_id FROM rebalance_plans "
            "WHERE portfolio_id=? ORDER BY signal_session DESC", (pid,)),
    }
