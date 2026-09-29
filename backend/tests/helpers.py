"""Builders for small, hand-checkable datasets."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.calendar import TradingCalendar
from app.data.panel import Panel, build_panel
from app.data.provider import (
    ACTION_COLUMNS,
    BAR_COLUMNS,
    InstrumentRecord,
    MarketDataProvider,
    ProviderInfo,
)


def weekday_calendar(start: str = "2020-01-01", n: int = 400, holidays: tuple[str, ...] = ()) -> TradingCalendar:
    d = date.fromisoformat(start)
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return TradingCalendar(out, name="TEST")


def bars_frame(cal: TradingCalendar, series: dict[str, dict]) -> pd.DataFrame:
    """series[sym] = {"close": array(len(cal)) with NaN for missing, optional "open", "volume"}."""
    frames = []
    for sym, d in series.items():
        close = np.asarray(d["close"], dtype=float)
        n = len(close)
        opn = np.asarray(d.get("open", close), dtype=float)
        vol = np.asarray(d.get("volume", np.full(n, 1e6)), dtype=float)
        m = ~np.isnan(close)
        sess = np.array(cal.sessions[:n])
        frames.append(pd.DataFrame({"symbol": sym, "session": sess[m], "open": opn[m], "high": np.maximum(opn, close)[m],
                                    "low": np.minimum(opn, close)[m], "close": close[m], "volume": vol[m]}))
    return pd.concat(frames, ignore_index=True)[BAR_COLUMNS]


def instruments_frame(symbols: list[str], benchmark: str | None = None, types: dict[str, str] | None = None,
                      delist: dict[str, str] | None = None) -> pd.DataFrame:
    types = types or {}
    delist = delist or {}
    return pd.DataFrame([{
        "symbol": s, "asset_type": "etf" if s == benchmark else types.get(s, "common_stock"),
        "is_benchmark": int(s == benchmark), "list_date": None, "delist_date": delist.get(s),
        "sector": None, "name": s,
    } for s in symbols])


def make_panel(cal: TradingCalendar, series: dict[str, dict], actions: list[tuple] | None = None,
               benchmark: str | None = None, types: dict[str, str] | None = None,
               delist: dict[str, str] | None = None) -> Panel:
    bars = bars_frame(cal, series)
    acts = pd.DataFrame(actions or [], columns=ACTION_COLUMNS)
    inst = instruments_frame(list(series), benchmark, types, delist)
    return build_panel(bars, acts, inst, cal, benchmark)


class FixtureProvider(MarketDataProvider):
    """In-memory provider for tests (and as a template for new adapters)."""

    def __init__(self, cal: TradingCalendar, series: dict[str, dict], actions: list[tuple] | None = None,
                 benchmark: str | None = None, sectors: dict[str, str] | None = None, etfs: set[str] | None = None,
                 key: str = "fixture"):
        self._bars = bars_frame(cal, series)
        self._acts = pd.DataFrame(actions or [], columns=ACTION_COLUMNS)
        etfs = set(etfs or ())
        self._inst = [InstrumentRecord(symbol=s, name=s, exchange="TEST",
                                       asset_type="etf" if s == benchmark or s in etfs else "common_stock",
                                       asset_type_source="test", is_benchmark=s == benchmark,
                                       sector=(sectors or {}).get(s), sector_source="test" if sectors else None)
                      for s in series]
        self._range = (cal.sessions[0], cal.sessions[len(next(iter(series.values()))["close"]) - 1])
        self.info = ProviderInfo(
            key=key, name="Test fixture", feed="fixture", data_label="Demo Data", is_demo=True,
            benchmark_symbol=benchmark or "", benchmark_return_basis="total_return", point_in_time_universe=True,
            coverage_note="test", entitlement_note="test", adjustment_note="test", survivorship_note="test",
            requires_key=False)

    def list_instruments(self, symbols=None):
        return self._inst if symbols is None else [r for r in self._inst if r.symbol in set(symbols)]

    def fetch_bars(self, symbols, start, end):
        b = self._bars
        return b[b["symbol"].isin(symbols) & (b["session"] >= start) & (b["session"] <= end)].reset_index(drop=True)

    def fetch_corporate_actions(self, symbols, start, end):
        a = self._acts
        return a[a["symbol"].isin(symbols) & (a["ex_date"] >= start) & (a["ex_date"] <= end)].reset_index(drop=True)

    def default_history_range(self):
        return self._range


SECTOR_ETF = {"Information Technology": "XLK", "Financials": "XLF", "Health Care": "XLV", "Consumer Discretionary": "XLY",
              "Consumer Staples": "XLP", "Energy": "XLE", "Industrials": "XLI", "Materials": "XLB", "Utilities": "XLU",
              "Real Estate": "XLRE", "Communication Services": "XLC"}


def rich_fixture(n_stocks: int = 60, n_sessions: int = 1050, seed: int = 11, start: str = "2019-01-01"):
    """Realistic-ish universe: market + 11 sector factors + persistent idiosyncratic drift; SPY + sector ETFs."""
    cal = weekday_calendar(start, n_sessions + 30)
    rng = np.random.default_rng(seed)
    T = n_sessions
    mkt = rng.normal(0.0004, 0.01, T)
    secs = list(SECTOR_ETF)
    sec_r = {sec: rng.normal(0, 0.006, T) for sec in secs}
    series = {"SPY": {"close": np.round(300 * np.exp(np.cumsum(mkt)), 4), "volume": np.full(T, 5e7)}}
    for sec, etf in SECTOR_ETF.items():
        series[etf] = {"close": np.round(50 * np.exp(np.cumsum(mkt + sec_r[sec])), 4), "volume": np.full(T, 1e7)}
    sectors = {}
    for k in range(n_stocks):
        sym = f"S{k:03d}"
        sec = secs[k % len(secs)]
        sectors[sym] = sec
        drift = np.cumsum(rng.normal(0, 0.00004, T)) + rng.normal(0, 0.0004)
        r = rng.uniform(0.7, 1.4) * mkt + sec_r[sec] + drift + rng.normal(0, 0.015, T)
        close = np.round(rng.uniform(15, 150) * np.exp(np.cumsum(r)), 4)
        series[sym] = {"close": close, "volume": np.round(rng.uniform(2e5, 4e6) * np.ones(T))}
    for d in series.values():
        d["open"] = np.round(d["close"] * (1 + rng.normal(0, 0.002, T)), 4)
    prov = FixtureProvider(cal, series, benchmark="SPY", sectors=sectors, etfs=set(SECTOR_ETF.values()))
    return cal, prov, series, sectors
