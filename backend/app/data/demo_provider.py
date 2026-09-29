"""Deterministic synthetic DEMO data set.

Everything produced here is *fabricated* for demonstration and testing. Tickers
deliberately contain a digit (e.g. ``KOVA3``) -- a pattern no real US-listed
common stock uses -- so demo rows can never be mistaken for real securities.

The data set intentionally contains the awkward cases a real engine must handle:
IPOs mid-sample, delistings, stock splits and reverse splits, cash dividends,
trading halts (missing bars), bars with a missing open, penny stocks, illiquid
names, ETFs / funds / preferreds that must be excluded, and a synthetic
total-return benchmark that stands in for SPY.

Generation is a pure function of a fixed seed and pinned numpy version.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from ..calendar import xnys_calendar
from .provider import (
    ACTION_COLUMNS,
    BAR_COLUMNS,
    InstrumentRecord,
    MarketDataProvider,
    ProviderInfo,
)

DEMO_SEED = 20_260_101
DEMO_START = "2016-01-04"
DEMO_END = "2026-09-25"
DEMO_BENCHMARK = "MKT0"
N_COMMON = 140

SECTORS = [
    "Technology", "Health Care", "Financials", "Industrials", "Consumer Discretionary",
    "Consumer Staples", "Energy", "Materials", "Utilities", "Communication Services",
]
_CONS = "BCDFGHJKLMNPRSTVXZ"
_VOW = "AEIOU"
_NAME_A = ["Aster", "Boreal", "Cobalt", "Delta", "Ember", "Fjord", "Garnet", "Halcyon", "Indigo", "Juniper",
           "Kestrel", "Lumen", "Meridian", "Nimbus", "Onyx", "Pinnacle", "Quartz", "Riven", "Solstice", "Tundra",
           "Umber", "Vantage", "Willow", "Xenon", "Yarrow", "Zephyr"]
_NAME_B = ["Dynamics", "Holdings", "Systems", "Industries", "Therapeutics", "Networks", "Resources", "Labs",
           "Logistics", "Energy", "Foods", "Materials", "Capital", "Devices", "Software", "Retail"]


def _make_tickers(rng: np.random.Generator, n: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    while len(out) < n:
        t = (rng.choice(list(_CONS)) + rng.choice(list(_VOW)) + rng.choice(list(_CONS))
             + rng.choice(list(_VOW)) + str(rng.integers(1, 10)))
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


@lru_cache(maxsize=1)
def generate_demo_dataset() -> tuple[list[InstrumentRecord], pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(DEMO_SEED)
    sessions = xnys_calendar().between(DEMO_START, DEMO_END)
    T = len(sessions)

    # ---- instrument master ------------------------------------------------------------------
    n_other = 8  # 3 ETFs, 2 closed-end funds, 3 preferreds -> must be excluded from the universe
    tickers = _make_tickers(rng, N_COMMON + n_other)
    kinds = ["common_stock"] * N_COMMON + ["etf"] * 3 + ["fund"] * 2 + ["preferred"] * 3
    n = len(tickers)
    sector_idx = rng.integers(0, len(SECTORS), size=n)

    start_idx = np.zeros(n, dtype=int)
    end_idx = np.full(n, T - 1, dtype=int)
    ipo = rng.choice(N_COMMON, size=16, replace=False)
    start_idx[ipo] = rng.integers(250, T - 400, size=16)
    delist = rng.choice(np.setdiff1d(np.arange(N_COMMON), ipo), size=10, replace=False)
    end_idx[delist] = rng.integers(500, T - 30, size=10)

    # ---- factor model -----------------------------------------------------------------------
    vol_state = np.empty(T)
    v = 0.009
    for t in range(T):  # slow-moving volatility regime with occasional stress episodes
        shock = 0.0015 if rng.random() < 0.004 else 0.0
        v = 0.985 * v + 0.015 * 0.009 + shock + rng.normal(0, 0.0002)
        vol_state[t] = min(max(v, 0.005), 0.04)
    mkt = rng.normal(0.00035, 1.0, T) * vol_state
    n_sec = len(SECTORS)
    sec_drift = np.zeros((T, n_sec))
    d = np.zeros(n_sec)
    for t in range(T):
        d = 0.996 * d + rng.normal(0, 0.00003, n_sec)
        sec_drift[t] = d
    sec_ret = sec_drift + rng.normal(0, 0.005, (T, n_sec))

    beta = rng.uniform(0.6, 1.5, n)
    idio_vol = rng.uniform(0.011, 0.028, n)
    stock_drift = np.zeros((T, n))
    sd = rng.normal(0, 0.0004, n)
    for t in range(T):  # persistent idiosyncratic drift -> gives momentum something to find
        sd = 0.997 * sd + rng.normal(0, 0.000035, n)
        stock_drift[t] = sd
    log_ret = (beta * mkt[:, None] + sec_ret[:, sector_idx] + stock_drift
               + rng.normal(0, 1.0, (T, n)) * idio_vol)
    # Delisted names deteriorate in their final ~120 sessions (typical of real delistings).
    for i in delist:
        lo = max(end_idx[i] - 120, start_idx[i] + 1)
        log_ret[lo:end_idx[i] + 1, i] -= 0.004
    etf_cols = [i for i, k in enumerate(kinds) if k in ("etf", "fund")]
    log_ret[:, etf_cols] = mkt[:, None] * 0.95 + rng.normal(0, 0.003, (T, len(etf_cols)))
    gross = np.exp(log_ret)

    # ---- raw prices with explicit splits / dividends ------------------------------------------
    start_px = np.exp(rng.uniform(np.log(12), np.log(220), n))
    penny = rng.choice(N_COMMON, size=10, replace=False)
    start_px[penny] = rng.uniform(1.5, 6.0, size=10)
    pays_div = rng.random(n) < 0.45
    q_yield = rng.uniform(0.002, 0.009, n)
    div_phase = rng.integers(0, 63, n)
    dv_base = np.exp(rng.normal(np.log(25e6), 1.4, n))

    close = np.full((T, n), np.nan)
    opn = np.full((T, n), np.nan)
    high = np.full((T, n), np.nan)
    low = np.full((T, n), np.nan)
    vol = np.full((T, n), np.nan)
    actions: list[tuple] = []
    last_split = np.full(n, -10_000)
    prev = start_px.copy()
    for t in range(T):
        alive = (t >= start_idx) & (t <= end_idx)
        ratio = np.ones(n)
        div = np.zeros(n)
        first = t == start_idx
        cand = prev * gross[t]
        # Forward split when a price gets large, reverse split when it collapses.
        up = alive & ~first & (cand > 330) & (t - last_split > 250) & (rng.random(n) < 0.08)
        down = alive & ~first & (cand < 1.2) & (t - last_split > 250) & (rng.random(n) < 0.08)
        ratio[up] = np.where(rng.random(n)[up] < 0.7, 2.0, 3.0)
        ratio[down] = 0.2
        last_split[up | down] = t
        paying = alive & ~first & pays_div & (((t + div_phase) % 63) == 0)
        pre = cand / ratio
        div[paying] = np.round(q_yield[paying] * pre[paying], 4)
        # Invariant (matches app.data.panel): gross_TR = ratio * (close + div) / prev_close
        c = np.where(first, start_px, pre - div)
        base = np.where(first, start_px, prev / ratio - div)
        o = base * np.exp(0.35 * np.log(c / base) + rng.normal(0, 0.004, n))
        hi = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, 0.006, n)))
        lo_ = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, 0.006, n)))
        dv = dv_base * np.exp(rng.normal(0, 0.35, n)) * (1 + 8 * np.abs(log_ret[t]))
        close[t, alive] = np.round(c[alive], 4)
        opn[t, alive] = np.round(o[alive], 4)
        high[t, alive] = np.round(hi[alive], 4)
        low[t, alive] = np.round(lo_[alive], 4)
        vol[t, alive] = np.round(dv[alive] / c[alive])
        for i in np.flatnonzero(alive & (ratio != 1.0)):
            actions.append((tickers[i], sessions[t], "split", float(ratio[i]), None))
        for i in np.flatnonzero(paying & (div > 0)):
            actions.append((tickers[i], sessions[t], "cash_dividend", None, float(div[i])))
        prev = np.where(alive, close[t], prev)

    # Trading halts: remove whole bars (never on first/last bar, never for ETFs/benchmark).
    halt = rng.random((T, N_COMMON)) < 0.0012
    for t, i in zip(*np.nonzero(halt)):
        if start_idx[i] < t < end_idx[i] and not any(a[0] == tickers[i] and a[1] == sessions[t] for a in actions):
            close[t, i] = opn[t, i] = high[t, i] = low[t, i] = vol[t, i] = np.nan
    # A few bars where the provider delivered no opening print.
    miss_open = rng.random((T, N_COMMON)) < 0.0003
    opn[:, :N_COMMON][miss_open] = np.nan

    # ---- benchmark: synthetic total-return market proxy (stand-in for SPY) ----------------------
    b_close = np.empty(T)
    b_open = np.empty(T)
    b_prev = 200.0
    b_actions: list[tuple] = []
    for t in range(T):
        c = b_prev * np.exp(mkt[t] + 0.00002)
        dv_ = 0.0
        if t > 0 and t % 63 == 20:
            dv_ = round(0.0035 * c, 4)
            b_actions.append((DEMO_BENCHMARK, sessions[t], "cash_dividend", None, dv_))
        c -= dv_
        b_open[t] = round((b_prev - dv_) * np.exp(0.35 * np.log(c / (b_prev - dv_))), 4)
        b_close[t] = round(c, 4)
        b_prev = b_close[t]

    # ---- assemble --------------------------------------------------------------------------
    instruments: list[InstrumentRecord] = []
    for i, tk in enumerate(tickers):
        kind = kinds[i]
        name = f"{_NAME_A[i % len(_NAME_A)]} {_NAME_B[(i * 7) % len(_NAME_B)]}"
        if kind == "etf":
            name = f"{SECTORS[sector_idx[i]]} Select ETF"
        elif kind == "fund":
            name = f"{_NAME_A[i % len(_NAME_A)]} Income Fund"
        elif kind == "preferred":
            name = f"{name} 6.25% Series A Preferred"
        instruments.append(InstrumentRecord(
            symbol=tk, name=f"{name} (synthetic)", exchange="DEMO", asset_type=kind,
            asset_type_source="demo fixture", sector=SECTORS[sector_idx[i]] if kind == "common_stock" else None,
            sector_source="synthetic demo metadata" if kind == "common_stock" else None,
            list_date=sessions[start_idx[i]],
            delist_date=sessions[end_idx[i]] if end_idx[i] < T - 1 else None,
            active=bool(end_idx[i] == T - 1),
            metadata={"synthetic": True},
        ))
    instruments.append(InstrumentRecord(
        symbol=DEMO_BENCHMARK, name="Synthetic Market Index Fund (demo stand-in for SPY)", exchange="DEMO",
        asset_type="etf", asset_type_source="demo fixture", is_benchmark=True, list_date=sessions[0],
        metadata={"synthetic": True},
    ))

    sess_arr = np.array(sessions)
    frames = []
    for i, tk in enumerate(tickers):
        m = ~np.isnan(close[:, i])
        frames.append(pd.DataFrame({
            "symbol": tk, "session": sess_arr[m], "open": opn[m, i], "high": high[m, i],
            "low": low[m, i], "close": close[m, i], "volume": vol[m, i],
        }))
    frames.append(pd.DataFrame({
        "symbol": DEMO_BENCHMARK, "session": sess_arr, "open": b_open, "high": np.maximum(b_open, b_close),
        "low": np.minimum(b_open, b_close), "close": b_close, "volume": np.round(8e9 / b_close),
    }))
    bars = pd.concat(frames, ignore_index=True)[BAR_COLUMNS]
    acts = pd.DataFrame(actions + b_actions, columns=ACTION_COLUMNS)
    return instruments, bars, acts


class DemoProvider(MarketDataProvider):
    info = ProviderInfo(
        key="demo",
        name="Synthetic demo fixture",
        feed="synthetic-fixture",
        data_label="Demo Data",
        is_demo=True,
        benchmark_symbol=DEMO_BENCHMARK,
        benchmark_return_basis="total_return",
        point_in_time_universe=True,
        coverage_note=f"Fabricated daily bars on real NYSE sessions {DEMO_START} to {DEMO_END}. Not market data.",
        entitlement_note="No entitlement required; generated locally from a fixed random seed.",
        adjustment_note="Raw prices plus explicit synthetic splits and cash dividends.",
        survivorship_note=(
            "Synthetic universe includes simulated IPOs and delistings, so it is survivorship-free by "
            "construction -- but it is not a real market and results say nothing about real performance."
        ),
        requires_key=False,
    )

    def list_instruments(self, symbols: list[str] | None = None) -> list[InstrumentRecord]:
        records = generate_demo_dataset()[0]
        return records if symbols is None else [r for r in records if r.symbol in set(symbols)]

    def fetch_bars(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        bars = generate_demo_dataset()[1]
        m = bars["symbol"].isin(symbols) & (bars["session"] >= start) & (bars["session"] <= end)
        return bars[m].reset_index(drop=True)

    def fetch_corporate_actions(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        acts = generate_demo_dataset()[2]
        m = acts["symbol"].isin(symbols) & (acts["ex_date"] >= start) & (acts["ex_date"] <= end)
        return acts[m].reset_index(drop=True)

    def default_history_range(self) -> tuple[str, str]:
        return DEMO_START, DEMO_END
