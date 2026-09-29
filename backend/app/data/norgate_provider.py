"""Norgate Data provider: point-in-time Russell 1000 (current & past members, incl. delisted stocks).

Requires a Norgate Data subscription with the Norgate Data Updater (NDU) running on Windows and the
`norgatedata` Python package (`pip install norgatedata`). Written against Norgate's documented Python API:

  norgatedata.watchlist_symbols("Russell 1000 Current & Past")          -> list of symbols (delisted ones
                                                                             carry a "-YYYYMM" style suffix)
  norgatedata.price_timeseries(sym, stock_price_adjustment_setting=..., padding_setting=...,
                               start_date=..., end_date=..., timeseriesformat="pandas-dataframe")
        columns: Open, High, Low, Close, Volume, Turnover, Unadjusted Close, Dividend
  norgatedata.index_constituent_timeseries(sym, "Russell 1000", padding_setting=..., start_date=...,
                               timeseriesformat="pandas-dataframe")    -> column "Index Constituent" (1/0)
  norgatedata.security_name(sym), norgatedata.last_quoted_date(sym)

Mapping into this app's model (raw bars + explicit corporate actions):
  * bars          = unadjusted OHLCV (StockPriceAdjustmentType.NONE)
  * cash dividends = the "Dividend" column of the unadjusted series (ex-date, per share)
  * splits        = changes in F_t = CapitalAdjustedClose_t / UnadjustedClose_t; ratio at s = F_s / F_{s-1}
  * membership    = runs of Index Constituent == 1 -> (start, end) intervals, used for point-in-time eligibility

NOT verified against a live Norgate installation in this repository (no subscription available); the
mapping logic is unit-tested with a simulated `norgatedata` module.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from .alpaca_provider import BENCHMARK, SECTOR_ETFS
from .provider import ACTION_COLUMNS, BAR_COLUMNS, InstrumentRecord, MarketDataProvider, ProviderError, ProviderInfo

log = logging.getLogger(__name__)


def _load_norgate():
    try:
        import norgatedata  # type: ignore[import-not-found]
    except ImportError as e:
        raise ProviderError("MARKET_DATA_PROVIDER=norgate needs a Norgate Data subscription, the Norgate Data Updater "
                            "running on this PC and `pip install norgatedata` in backend/.venv.") from e
    return norgatedata


class NorgateProvider(MarketDataProvider):
    def __init__(self, index_name: str = "Russell 1000", history_start: str = "2000-01-01", nd=None):
        self.nd = nd if nd is not None else _load_norgate()
        self.index_name = index_name
        self.watchlist = f"{index_name} Current & Past"
        self.history_start = history_start
        self._unadj: dict[str, pd.DataFrame] = {}
        self.info = ProviderInfo(
            key="norgate", name="Norgate Data", feed="norgate-eod", data_label="End-of-Day Market Data", is_demo=False,
            benchmark_symbol=BENCHMARK, benchmark_return_basis="total_return", point_in_time_universe=True,
            coverage_note=f"Daily bars from {history_start}; {self.watchlist} incl. delisted securities.",
            entitlement_note="Norgate Data subscription (US Stocks, with historical index constituents) and a running NDU.",
            adjustment_note="Unadjusted bars; splits derived from capital-adjusted vs unadjusted closes; dividends from "
                            "the Dividend column.",
            survivorship_note=f"Point-in-time {index_name} membership including delisted stocks (Norgate).",
            requires_key=False,
        )

    # ------------------------------------------------------------------ helpers
    def _series(self, symbol: str, adjustment: str, start: str, end: str | None) -> pd.DataFrame:
        nd = self.nd
        df = nd.price_timeseries(
            symbol,
            stock_price_adjustment_setting=getattr(nd.StockPriceAdjustmentType, adjustment),
            padding_setting=nd.PaddingType.NONE,
            start_date=start, end_date=end, timeseriesformat="pandas-dataframe",
        )
        if df is None or len(df) == 0:
            return pd.DataFrame()
        df = df.copy()
        df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
        return df

    def _unadjusted(self, symbol: str, start: str, end: str | None) -> pd.DataFrame:
        key = f"{symbol}|{start}|{end}"
        if key not in self._unadj:
            self._unadj[key] = self._series(symbol, "NONE", start, end)
        return self._unadj[key]

    # ------------------------------------------------------------------ interface
    def list_instruments(self, symbols: list[str] | None = None) -> list[InstrumentRecord]:
        nd = self.nd
        members = list(symbols) if symbols is not None else list(nd.watchlist_symbols(self.watchlist))
        out = []
        for s in dict.fromkeys([m for m in members if m not in SECTOR_ETFS + [BENCHMARK]] + [BENCHMARK] + SECTOR_ETFS):
            ref = s == BENCHMARK or s in SECTOR_ETFS
            name = getattr(nd, "security_name", lambda _s: _s)(s) or s
            last = getattr(nd, "last_quoted_date", lambda _s: None)(s)
            last = pd.Timestamp(last).strftime("%Y-%m-%d") if last is not None and not pd.isna(last) else None
            delisted = last is not None and (datetime.now(UTC) - pd.Timestamp(last).tz_localize(UTC)).days > 10
            out.append(InstrumentRecord(
                symbol=s, name=str(name), exchange=None, asset_type="etf" if ref else "common_stock",
                asset_type_source="Norgate index constituent (common stock)" if not ref else "reference ETF",
                is_benchmark=s == BENCHMARK, delist_date=last if delisted else None, active=not delisted,
                metadata={"role": "benchmark" if s == BENCHMARK else "sector_etf" if ref else "universe",
                          "watchlist": self.watchlist},
            ))
        return out

    def fetch_bars(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        frames = []
        for s in symbols:
            df = self._unadjusted(s, start, end)
            if df.empty:
                continue
            frames.append(pd.DataFrame({"symbol": s, "session": df.index, "open": df["Open"].to_numpy(float),
                                        "high": df["High"].to_numpy(float), "low": df["Low"].to_numpy(float),
                                        "close": df["Close"].to_numpy(float), "volume": df["Volume"].to_numpy(float)}))
        return pd.concat(frames, ignore_index=True)[BAR_COLUMNS] if frames else pd.DataFrame(columns=BAR_COLUMNS)

    def fetch_corporate_actions(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        rows = []
        for s in symbols:
            un = self._unadjusted(s, start, end)
            if un.empty:
                continue
            if "Dividend" in un.columns:
                for d, amt in un["Dividend"].items():
                    if amt and amt > 0:
                        rows.append((s, d, "cash_dividend", None, float(amt)))
            cap = self._series(s, "CAPITAL", start, end)
            if cap.empty:
                continue
            j = un.join(cap[["Close"]].rename(columns={"Close": "CapClose"}), how="inner")
            f = (j["CapClose"] / j["Close"]).to_numpy(float)
            dates = list(j.index)
            for k in range(1, len(f)):
                if np.isfinite(f[k]) and np.isfinite(f[k - 1]) and f[k - 1] > 0:
                    r = f[k] / f[k - 1]
                    if abs(r - 1) > 1e-4:
                        rows.append((s, dates[k], "split", float(round(r, 6)), None))
        return pd.DataFrame(rows, columns=ACTION_COLUMNS)

    def index_membership(self, symbols: list[str]) -> pd.DataFrame | None:
        nd = self.nd
        rows = []
        for s in symbols:
            if s == BENCHMARK or s in SECTOR_ETFS:
                continue
            df = nd.index_constituent_timeseries(s, self.index_name, padding_setting=nd.PaddingType.ALLMARKETDAYS,
                                                 start_date=self.history_start, timeseriesformat="pandas-dataframe")
            if df is None or len(df) == 0:
                continue
            flag = df.iloc[:, 0].to_numpy()
            dates = list(pd.to_datetime(df.index).strftime("%Y-%m-%d"))
            start = None
            for d, v in zip(dates, flag):
                if v and start is None:
                    start = d
                elif not v and start is not None:
                    rows.append((s, self.index_name, start, prev))
                    start = None
                prev = d
            if start is not None:
                rows.append((s, self.index_name, start, None))
        return pd.DataFrame(rows, columns=["symbol", "index_name", "start", "end"])

    def default_history_range(self) -> tuple[str, str]:
        from ..calendar import xnys_calendar

        end = xnys_calendar().latest_completed_session(datetime.now(UTC))
        return self.history_start, end or datetime.now(UTC).date().isoformat()
