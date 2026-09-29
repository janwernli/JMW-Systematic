"""Strategy configuration (v1 long-only and v2 long-short) and its canonical hash."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrategyConfig(BaseModel):
    """All tunable inputs of the 12-1 momentum strategy.

    mode="long_only"  -> v1: top_n winners, equal weight, cash-funded, no shorts.
    mode="long_short" -> v2: decile winners long / decile losers short with a
        rank buffer, inverse-volatility weights with per-name caps, beta-neutral
        books scaled to a volatility target within gross limits, a momentum-crash
        guard, a $10 short price floor, borrow fees and a short stop-loss.
    Every backtest run and every paper-portfolio configuration version stores the
    full JSON of this model.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["long_only", "long_short"] = Field("long_short", description="Strategy variant")
    initial_capital: float = Field(100_000.0, gt=0, le=1e10, description="Starting virtual cash, USD")
    top_n: int = Field(50, ge=1, le=1000, description="Number of top-ranked eligible stocks to hold")
    lookback_sessions: int = Field(252, ge=2, le=1000, description="Older momentum anchor: sessions before signal")
    skip_sessions: int = Field(21, ge=0, le=252, description="Recent anchor: sessions before signal (skip month)")
    min_history_sessions: int = Field(252, ge=0, le=2000, description="Valid bars required before the signal session")
    min_price: float = Field(5.0, ge=0, description="Raw (unadjusted) close at the signal session must exceed this, USD")
    adv_window: int = Field(20, ge=1, le=252, description="Sessions in the average dollar-volume window")
    min_adv_usd: float = Field(5_000_000.0, ge=0, description="Minimum trailing average daily dollar volume, USD")
    slippage_bps: float = Field(10.0, ge=0, le=500, description="Assumed adverse slippage vs. the open, basis points")
    commission_per_order: float = Field(0.0, ge=0, le=1000, description="Assumed flat commission per fill, USD")
    commission_bps: float = Field(0.0, ge=0, le=500, description="Assumed commission as bps of traded value")
    min_session_coverage: float = Field(
        0.90, ge=0, le=1,
        description="Share of the point-in-time universe that must have a bar at the signal session; otherwise the rebalance is blocked",
    )

    # ---- v2 long-short (ignored when mode="long_only") ---------------------------------------------
    long_pct: float = Field(0.10, gt=0, le=0.5, description="Enter longs from the top fraction of eligible stocks")
    short_pct: float = Field(0.10, gt=0, le=0.5, description="Enter shorts from the bottom fraction of eligible stocks")
    min_names_per_side: int = Field(50, ge=1, le=1000, description="Minimum names per book (filled by next ranks)")
    max_names_per_side: int = Field(100, ge=1, le=1000, description="Maximum names per book (best ranks kept)")
    buffer_exit_pct: float = Field(
        0.30, gt=0, le=0.5,
        description="Buffer: a held long stays while in the top fraction, a held short while in the bottom fraction")
    vol_lookback_sessions: int = Field(126, ge=20, le=504, description="Realized-vol window (sessions), ~6 months")
    beta_lookback_sessions: int = Field(252, ge=60, le=756, description="Beta estimation window (sessions)")
    beta_shrink: float = Field(0.33, ge=0, le=1, description="Beta shrinkage toward 1.0 (Vasicek-style)")
    target_vol: float = Field(0.10, gt=0, le=1, description="Annualized portfolio volatility target (ex-ante, trailing)")
    min_side_gross: float = Field(0.50, ge=0, le=2, description="Minimum gross per side, fraction of NAV")
    max_side_gross: float = Field(0.75, gt=0, le=2, description="Maximum gross per side, fraction of NAV")
    max_total_gross: float = Field(1.50, gt=0, le=4, description="Cap on long + short gross, fraction of NAV")
    max_long_weight: float = Field(0.02, gt=0, le=1, description="Per-name cap on longs, fraction of NAV")
    max_short_weight: float = Field(0.01, gt=0, le=1, description="Per-name cap on shorts, fraction of NAV")
    short_min_price: float = Field(10.0, ge=0, description="Raw close must exceed this to be shorted, USD")
    borrow_fee_annual: float = Field(0.005, ge=0, le=1, description="Assumed borrow cost on short market value, per year")
    short_stop_loss: float | None = Field(
        0.50, gt=0, le=10, description="Cover a short when its close is this fraction above entry (fill next open)")
    crash_guard: bool = Field(True, description="Scale down the short book in bear/high-vol markets")
    crash_market_lookback_sessions: int = Field(504, ge=21, le=2000, description="Market return window (~24 months)")
    crash_market_vol_threshold: float = Field(0.20, gt=0, le=2, description="Market 6-month realized vol deemed 'high'")
    crash_short_scale: float = Field(0.50, ge=0, le=1, description="Short-book multiplier when the crash guard is on")

    start_date: str | None = Field(None, description="Backtest start (ISO date); ignored by the paper ledger")
    end_date: str | None = Field(None, description="Backtest end (ISO date); ignored by the paper ledger")
    benchmark_symbol: str | None = Field(None, description="Benchmark; default = provider's SPY (or demo stand-in)")

    @field_validator("start_date", "end_date")
    @classmethod
    def _iso(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        return date.fromisoformat(v).isoformat()

    @model_validator(mode="after")
    def _check(self) -> "StrategyConfig":
        if self.skip_sessions >= self.lookback_sessions:
            raise ValueError("skip_sessions must be smaller than lookback_sessions")
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        if self.min_names_per_side > self.max_names_per_side:
            raise ValueError("min_names_per_side must not exceed max_names_per_side")
        if self.min_side_gross > self.max_side_gross:
            raise ValueError("min_side_gross must not exceed max_side_gross")
        if self.long_pct > self.buffer_exit_pct or self.short_pct > self.buffer_exit_pct:
            raise ValueError("buffer_exit_pct must be at least long_pct / short_pct")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:16]

    @property
    def is_long_short(self) -> bool:
        return self.mode == "long_short"

    @classmethod
    def long_only_v1(cls, **kw) -> "StrategyConfig":
        return cls(mode="long_only", **kw)

    def paper_view(self) -> "StrategyConfig":
        """Config as used by the paper ledger (no backtest date window)."""
        return self.model_copy(update={"start_date": None, "end_date": None})
