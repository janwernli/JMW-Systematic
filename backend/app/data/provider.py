"""The MarketDataProvider interface.

Every data source (demo fixtures, Alpaca, ...) is isolated behind this interface.
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

ASSET_TYPES = ("common_stock", "etf", "fund", "preferred", "warrant", "unit", "right", "other")


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
    key: str                      # "demo" | "alpaca"
    name: str
    feed: str                     # e.g. "synthetic-fixture", "iex", "sip"
    data_label: str               # "Demo Data" | "Delayed Market Data"
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
    def list_instruments(self) -> list[InstrumentRecord]:
        """Instrument master: symbols, asset types, listing/delisting dates where known."""

    @abstractmethod
    def fetch_bars(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        """Raw daily bars (columns BAR_COLUMNS), session = ISO date of the XNYS session."""

    @abstractmethod
    def fetch_corporate_actions(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        """Splits (ratio = new shares per old) and cash dividends (amount per share), columns ACTION_COLUMNS."""

    @abstractmethod
    def default_history_range(self) -> tuple[str, str]:
        """(start, end) ISO dates to request for a full refresh."""


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=BAR_COLUMNS)


def empty_actions() -> pd.DataFrame:
    return pd.DataFrame(columns=ACTION_COLUMNS)
