"""Strategy configuration for the long-short cross-sectional momentum strategy, and its canonical hash."""

from __future__ import annotations

import hashlib
import json
import warnings
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Keys from earlier versions (long-only v1) that are silently dropped when old stored configs are read.
_LEGACY_KEYS = {"mode", "top_n"}


class ConfigWarning(UserWarning):
    """A config that is valid but cannot behave as its parameters suggest."""


class StrategyConfig(BaseModel):
    """All tunable inputs of the strategy.

    Long the strongest / short the weakest decile by the selected momentum signal, with a rank
    buffer, inverse-volatility weights with per-name caps, beta- and sector-neutral books scaled
    to a volatility target within gross limits, a momentum-crash guard, a short price floor, a
    hard-to-borrow liquidity screen, borrow fees and a short stop-loss.
    Every backtest run and every paper-portfolio configuration version stores the full JSON.
    """

    model_config = ConfigDict(extra="forbid")

    initial_capital: float = Field(100_000.0, gt=0, le=1e10, description="Starting capital, USD")
    signal: Literal["composite", "momentum_12_1"] = Field(
        "composite", description="Ranking signal: composite (residual + sector-demeaned momentum + FIP) or plain 12-1")
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

    # ---- composite signal ------------------------------------------------------------------------
    w_residual: float = Field(0.60, ge=0, le=1, description="Composite weight: residual momentum")
    w_sector_demeaned: float = Field(0.25, ge=0, le=1, description="Composite weight: sector-demeaned 12-1")
    w_fip: float = Field(0.15, ge=0, le=1, description="Composite weight: frog-in-the-pan (information discreteness)")
    residual_window_months: int = Field(36, ge=12, le=120, description="Rolling regression window for residual momentum")
    winsor_sigma: float = Field(3.0, gt=0, le=10, description="Winsorize each component at mean ± this many std devs")

    # ---- books ----------------------------------------------------------------------------------
    long_pct: float = Field(0.10, gt=0, le=0.5, description="Enter longs from the top fraction of eligible stocks")
    short_pct: float = Field(0.10, gt=0, le=0.5, description="Enter shorts from the bottom fraction of eligible stocks")
    min_names_per_side: int = Field(50, ge=1, le=1000, description="Minimum names per book (filled by next ranks)")
    max_names_per_side: int = Field(100, ge=1, le=1000, description="Maximum names per book (best ranks kept)")
    buffer_exit_pct: float = Field(
        0.30, gt=0, le=0.5,
        description="Buffer: a held long stays while in the top fraction, a held short while in the bottom fraction")

    # ---- sizing ---------------------------------------------------------------------------------
    vol_lookback_sessions: int = Field(126, ge=20, le=504, description="Realized-vol window (sessions), ~6 months")
    beta_lookback_sessions: int = Field(252, ge=60, le=756, description="Beta estimation window (sessions)")
    beta_shrink: float = Field(0.33, ge=0, le=1, description="Beta shrinkage toward 1.0 (Vasicek-style)")
    target_vol: float = Field(0.10, gt=0, le=1, description="Annualized portfolio volatility target (ex-ante, trailing)")
    min_side_gross: float = Field(0.50, ge=0, le=2, description="Minimum gross per side, fraction of NAV")
    max_side_gross: float = Field(0.75, gt=0, le=2, description="Maximum gross per side, fraction of NAV")
    max_total_gross: float = Field(1.50, gt=0, le=4, description="Cap on long + short gross, fraction of NAV")
    max_long_weight: float = Field(0.02, gt=0, le=1, description="Per-name cap on longs, fraction of NAV")
    max_short_weight: float = Field(0.015, gt=0, le=1, description="Per-name cap on shorts, fraction of NAV")
    beta_neutral: bool = Field(
        True, description="Size the short book so the overlay is beta-neutral (short gross = long gross x beta_L / beta_S); "
                          "off = equal dollar gross per side under vol targeting")
    sizing: Literal["vol_target", "fixed"] = Field(
        "vol_target", description="vol_target: gross set by target_vol within the side limits; fixed: fixed_long_gross / "
                                  "fixed_short_gross (e.g. 130/30), no vol target")
    fixed_long_gross: float = Field(1.30, ge=0, le=3, description="Long book gross when sizing = fixed, fraction of NAV")
    fixed_short_gross: float = Field(
        0.30, ge=0, le=3, description="Short book gross when sizing = fixed and beta_neutral is off. With beta_neutral on, "
                                      "the short book is sized for overlay beta neutrality (long x beta_L / beta_S), "
                                      "capped at max_side_gross")
    core_beta: float = Field(
        0.0, ge=0, le=1, description="SPY core: hold core_beta x NAV in the benchmark ETF under the long/short overlay. "
                                     "Rebalanced monthly; exempt from beta/sector neutrality; counts toward max_total_gross")
    margin_debit_spread: float = Field(
        0.025, ge=0, le=0.5, description="ASSUMED margin-loan rate over RF (Ken French), per year, ACT/360. Only books "
                                         "whose longs exceed NAV + short proceeds (SPY core + overlay, 130/30) borrow")
    sector_neutral: bool = Field(True, description="Constrain net exposure per sector")
    max_sector_net: float = Field(0.02, ge=0, le=1, description="Max |long - short| weight per sector, fraction of NAV")

    # ---- short side ----------------------------------------------------------------------------
    short_min_price: float = Field(10.0, ge=0, description="Raw close must exceed this to be shorted, USD")
    htb_adv_window: int = Field(60, ge=5, le=252, description="Dollar-volume window for the hard-to-borrow screen")
    htb_exclude_pct: float = Field(
        0.20, ge=0, lt=1, description="Exclude the least liquid fraction of eligible stocks from the short book")
    borrow_fee_annual: float = Field(0.005, ge=0, le=1, description="Assumed borrow cost, per year, charged per calendar day / 360")
    cash_interest: bool = Field(
        False, description="Credit the risk-free rate (Ken French RF) on positive cash, per calendar day / 360. "
                           "The alpha regression then uses excess returns (r - RF); otherwise raw returns.")
    short_stop_loss: float | None = Field(
        0.50, gt=0, le=10, description="Cover a short when its close is this fraction above entry (fill next open)")
    crash_guard: bool = Field(True, description="Scale down the short book in bear/high-vol markets")
    crash_market_lookback_sessions: int = Field(504, ge=21, le=2000, description="Market return window (~24 months)")
    crash_market_vol_threshold: float = Field(0.20, gt=0, le=2, description="Market 6-month realized vol deemed 'high'")
    crash_short_scale: float = Field(0.50, ge=0, le=1, description="Short-book multiplier when the crash guard is on")

    start_date: str | None = Field(None, description="Backtest start (ISO date); ignored by the paper ledger")
    end_date: str | None = Field(None, description="Backtest end (ISO date); ignored by the paper ledger")
    benchmark_symbol: str | None = Field(None, description="Benchmark; default = the provider's SPY")

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy(cls, data: Any) -> Any:
        if isinstance(data, dict) and _LEGACY_KEYS & data.keys():
            data = {k: v for k, v in data.items() if k not in _LEGACY_KEYS}
        return data

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
        if self.signal == "composite" and self.w_residual + self.w_sector_demeaned + self.w_fip <= 0:
            raise ValueError("composite weights must not all be zero")
        if self.core_beta >= self.max_total_gross:
            raise ValueError("core_beta must be below max_total_gross (the SPY core counts toward the gross cap)")
        for msg in self.config_warnings():
            warnings.warn(msg, ConfigWarning, stacklevel=2)
        return self

    def config_warnings(self) -> list[str]:
        """Valid-but-self-limiting settings, surfaced in the UI, plan checks and backtest warnings."""
        out = []
        if self.sizing == "fixed":
            short_max = self.max_side_gross if self.beta_neutral else self.fixed_short_gross
            for side, gross, cap in (("long", self.fixed_long_gross, self.max_long_weight),
                                     ("short", short_max, self.max_short_weight)):
                if self.min_names_per_side * cap < gross - 1e-12:
                    out.append(f"Per-name {side} cap limits the fixed {side} book: {self.min_names_per_side} x {cap:.2%} = "
                               f"{self.min_names_per_side * cap:.1%} < {gross:.0%} unless more names qualify.")
            worst = self.core_beta + self.fixed_long_gross + short_max
            if worst > self.max_total_gross + 1e-12:
                out.append(f"Total gross could exceed {self.max_total_gross:.0%}: core {self.core_beta:.0%} + long "
                           f"{self.fixed_long_gross:.0%} + short up to {short_max:.0%} = {worst:.0%}. When it would, both "
                           f"overlay sides are scaled down together (beta ratio kept) to stay within {self.max_total_gross:.0%}; "
                           "between rebalances, price moves can still push gross above it.")
            if self.sector_neutral and abs(self.fixed_long_gross - self.fixed_short_gross) > self.max_sector_net:
                out.append("Sector neutrality cannot hold for a net-long fixed book (sector nets sum to the book's net "
                           "exposure); the sector QP will cut gross heavily or fail. Turn sector_neutral off.")
            return out
        cap_s = self.min_names_per_side * self.max_short_weight
        if cap_s < self.max_side_gross - 1e-12:
            out.append(
                f"Per-name short cap limits the short book: with the minimum {self.min_names_per_side} shorts at "
                f"{self.max_short_weight:.2%} each, capacity is {cap_s:.1%} < max side gross {self.max_side_gross:.0%}. "
                "The vol target may be unreachable unless more names qualify (raise max_short_weight or min_names_per_side).")
        cap_l = self.min_names_per_side * self.max_long_weight
        if cap_l < self.max_side_gross - 1e-12:
            out.append(
                f"Per-name long cap limits the long book: {self.min_names_per_side} x {self.max_long_weight:.2%} = "
                f"{cap_l:.1%} < max side gross {self.max_side_gross:.0%}.")
        return out

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:16]

    def paper_view(self) -> "StrategyConfig":
        """Config as used by the paper ledger / live trading (no backtest date window)."""
        return self.model_copy(update={"start_date": None, "end_date": None})
