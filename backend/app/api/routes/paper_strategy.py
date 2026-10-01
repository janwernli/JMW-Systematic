"""Switch the paper strategy to a pre-defined variant (same logic as `python -m app paper-config`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...ledger.paper import LedgerError
from ...paper_strategy import SwitchError, active_variant, apply, preview
from ...services import AppContext
from ...strategy.config import StrategyConfig
from ..deps import get_ctx

router = APIRouter(prefix="/paper/strategy", tags=["paper"])


class ActiveVariant(BaseModel):
    key: str | None
    name: str


class SwitchRequest(BaseModel):
    variant: str
    replan_now: bool = True
    expected_orders: int | None = None   # the order count the user reviewed (guards against a changed preview)


class TargetBook(BaseModel):
    signal_session: str
    blocked_reason: str | None
    spy_weight: float
    longs: int
    shorts: int
    long_gross: float | None
    short_gross: float | None
    total_gross: float | None
    net_exposure: float | None
    overlay_beta: float | None
    beta_long: float | None
    beta_short: float | None
    crash_guard: bool
    max_sector_net: float | None
    binding: list[str]
    notes: list[str]


class PreviewOrder(BaseModel):
    origin: str
    symbol: str
    side: str
    qty: int
    effect: str
    ref: float
    value: float
    why: str
    plan_id: int


class SwitchPreview(BaseModel):
    variant: dict[str, str]
    current: ActiveVariant
    changes: dict[str, list[Any]]
    config: StrategyConfig
    config_warnings: list[str]
    ledger_as_of: str
    rebalance_mode: str
    trading_enabled: bool
    replan: dict[str, Any]
    book: TargetBook | None
    orders: list[PreviewOrder]
    orders_basis: str | None


class SwitchResult(BaseModel):
    message: str
    changed: bool
    plan_id: int | None
    replan: dict[str, Any]


@router.get("", response_model=ActiveVariant)
def current(ctx: AppContext = Depends(get_ctx)):
    return active_variant(ctx)


@router.post("/preview", response_model=SwitchPreview)
def switch_preview(req: SwitchRequest, ctx: AppContext = Depends(get_ctx)):
    """Dry run: settings, target book and orders, without changing anything."""
    try:
        return preview(ctx, req.variant, req.replan_now)
    except (SwitchError, LedgerError) as e:
        raise HTTPException(400, str(e)) from e


@router.post("/apply", response_model=SwitchResult)
def switch_apply(req: SwitchRequest, ctx: AppContext = Depends(get_ctx)):
    """Switch the active model portfolio (and so the Alpaca paper account) to the variant."""
    try:
        if req.expected_orders is not None:
            n = len(preview(ctx, req.variant, req.replan_now)["orders"])
            if n != req.expected_orders:
                raise HTTPException(409, f"The order list changed ({n} orders now, you reviewed {req.expected_orders}). "
                                         "Review it again.")
        res = apply(ctx, req.variant, req.replan_now)
    except (SwitchError, LedgerError) as e:
        raise HTTPException(409, str(e)) from e
    return {k: res[k] for k in ("message", "changed", "plan_id", "replan")}
