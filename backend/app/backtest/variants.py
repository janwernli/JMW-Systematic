"""Pre-defined strategy variants for the side-by-side comparison.

These are fixed structural alternatives, NOT parameters tuned on the backtest: each is defined up front and
run once on the same data and period. Everything not listed in `overrides` is the current default
(StrategyConfig()), which remains the paper default until a variant is chosen explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..strategy.config import StrategyConfig


@dataclass(frozen=True)
class Variant:
    key: str
    name: str
    description: str
    overrides: dict

    def config(self, start_date: str | None = None, end_date: str | None = None) -> StrategyConfig:
        return StrategyConfig(**self.overrides, start_date=start_date, end_date=end_date)


VARIANTS: tuple[Variant, ...] = (
    Variant("neutral_10", "Neutral 10%",
            "Current defaults: beta- and sector-neutral long/short, 10% vol target, 50-75% gross per side, "
            "150% total.",
            {}),
    Variant("neutral_15", "Neutral 15%",
            "Same book at a 15% vol target: up to 100% gross per side and 200% total (Alpaca Reg T 2x limit).",
            {"target_vol": 0.15, "max_side_gross": 1.0, "max_total_gross": 2.0}),
    Variant("spy_overlay", "SPY + overlay",
            "100% of NAV in SPY (rebalanced monthly) plus a long/short overlay from the same composite signal "
            "(deciles, buffer, inverse-vol weights, sector-neutral): long book fixed at 30% of NAV, short book sized for "
            "overlay beta neutrality (~30%, capped at 40%). Total gross <= 160% (both overlay sides scaled together if "
            "needed); crash guard scales the overlay short book. Stop-loss, $10 short floor, HTB screen and easy-to-"
            "borrow check unchanged.",
            {"core_beta": 1.0, "sizing": "fixed", "fixed_long_gross": 0.30, "min_side_gross": 0.30,
             "max_side_gross": 0.40, "max_total_gross": 1.6}),
    Variant("ext_130_30", "130/30",
            "Long book 130%, short book 30% of NAV from the same signal; no beta neutralization, no vol target, "
            "gross cap 160%. Sector neutrality is off: a 100% net-long book cannot keep every sector within +/-2% "
            "net. Per-name long cap 2.6% (= 130% / the 50-name minimum book) so the long book can reach 130%.",
            {"sizing": "fixed", "fixed_long_gross": 1.30, "fixed_short_gross": 0.30, "beta_neutral": False,
             "max_total_gross": 1.6, "sector_neutral": False, "max_long_weight": 0.026}),
)

VARIANT_BY_KEY = {v.key: v for v in VARIANTS}

# Config fields that do not define a strategy (excluded when recognising which variant a config is).
_NON_STRATEGY = {"initial_capital", "start_date", "end_date", "benchmark_symbol"}


def variant_of(cfg: StrategyConfig) -> Variant | None:
    """The pre-defined variant whose strategy settings equal `cfg` (None = a custom config)."""
    mine = {k: v for k, v in cfg.model_dump().items() if k not in _NON_STRATEGY}
    for v in VARIANTS:
        theirs = {k: x for k, x in v.config().model_dump().items() if k not in _NON_STRATEGY}
        if mine == theirs:
            return v
    return None
