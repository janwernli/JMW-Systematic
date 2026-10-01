"""The MarketDataProvider interface.

Every data source (Alpaca, Norgate, test fixtures) is isolated behind this interface.
Providers deliver *raw* (unadjusted) daily OHLCV bars plus explicit corporate
actions (splits and cash dividends). All adjustment is done by our own code from
those documented fields, so the same adjustment logic is used for every provider
and dividends are never counted twice.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

import pandas as pd

BAR_COLUMNS = ["symbol", "session", "open", "high", "low", "close", "volume"]
ACTION_COLUMNS = ["symbol", "ex_date", "action_type", "ratio", "amount"]

ASSET_TYPES = ("common_stock", "etf", "fund", "preferred", "warrant", "unit", "right", "adr", "spac", "debt", "other")


@dataclass
class InstrumentRecord:
    symbol: str
    name: str
    exchange: str | None
    asset_type: str
    asset_type_source: str
    sector: str | None = None
    sector_source: str | None = None
    is_benchmark: bool = False
    list_date: str | None = None
    delist_date: str | None = None
    active: bool = True
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProviderInfo:
    key: str                      # "alpaca" | "norgate" | "fixture" (tests)
    name: str
    feed: str                     # e.g. "sip", "iex", "norgate-eod"
    data_label: str               # "Delayed Market Data" | "End-of-Day Market Data"
    is_demo: bool
    benchmark_symbol: str
    benchmark_return_basis: str   # "total_return" | "price_return"
    point_in_time_universe: bool  # True only if delisted names / historical membership are included
    coverage_note: str
    entitlement_note: str
    adjustment_note: str
    survivorship_note: str
    requires_key: bool

    def to_dict(self) -> dict:
        return asdict(self)


class ProviderError(RuntimeError):
    """Raised with a human-readable explanation when a provider cannot deliver data."""


class MarketDataProvider(ABC):
    info: ProviderInfo

    @abstractmethod
    def list_instruments(self, symbols: list[str] | None = None) -> list[InstrumentRecord]:
        """Instrument master: symbols, asset types, listing/delisting dates where known.

        With `symbols`, return records for exactly those symbols (used to keep the
        universe frozen across incremental refreshes instead of re-selecting it).
        """

    @abstractmethod
    def fetch_bars(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        """Raw daily bars (columns BAR_COLUMNS), session = ISO date of the XNYS session."""

    @abstractmethod
    def fetch_corporate_actions(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        """Splits (ratio = new shares per old) and cash dividends (amount per share), columns ACTION_COLUMNS."""

    @abstractmethod
    def default_history_range(self) -> tuple[str, str]:
        """(start, end) ISO dates to request for a full refresh."""

    def select_universe(self, asof: str, n: int, window: int = 60) -> list[str] | None:
        """Optional: the top `n` common stocks by mean dollar volume over the `window` sessions ending at `asof`
        (data <= asof only). Providers with a fixed or point-in-time universe return None (no re-selection)."""
        return None

    def index_membership(self, symbols: list[str]) -> pd.DataFrame | None:
        """Optional point-in-time index membership: columns symbol, index_name, start, end (end None = current).

        Providers without historical membership return None; the universe is then everything imported
        (and flagged as survivorship-biased unless `info.point_in_time_universe`)."""
        return None


def top_by_dollar_volume(bars: pd.DataFrame, asof: str, n: int, window: int = 60, min_frac: float = 0.8) -> list[str]:
    """Symbols ranked by mean close x volume over their last `window` sessions on or before `asof`; a symbol needs
    at least min_frac x window bars in that window. Ties broken by symbol."""
    b = bars[bars["session"] <= asof]
    if b.empty:
        return []
    sessions = sorted(b["session"].unique())[-window:]
    b = b[b["session"].isin(sessions)]
    dv = (b["close"].astype(float) * b["volume"].astype(float)).groupby(b["symbol"]).agg(["mean", "count"])
    dv = dv[dv["count"] >= min_frac * len(sessions)]
    ranked = sorted(dv.index, key=lambda s: (-dv.at[s, "mean"], s))
    return ranked[:n]


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=BAR_COLUMNS)


def empty_actions() -> pd.DataFrame:
    return pd.DataFrame(columns=ACTION_COLUMNS)
