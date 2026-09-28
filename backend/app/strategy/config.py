"""Strategy configuration (v1 defaults) and its canonical hash."""

from __future__ import annotations

import hashlib
import json
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrategyConfig(BaseModel):
    """All tunable inputs of the 12-1 momentum strategy.

    Defaults are the documented v1 specification. Every backtest run and every
    paper-portfolio configuration version stores the full JSON of this model.
    """

    model_config = ConfigDict(extra="forbid")

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
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:16]

    def paper_view(self) -> "StrategyConfig":
        """Config as used by the paper ledger (no backtest date window)."""
        return self.model_copy(update={"start_date": None, "end_date": None})
