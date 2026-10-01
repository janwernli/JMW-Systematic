"""In-memory point-in-time panel (sessions x symbols) built from raw bars + corporate actions.

Key derived series
------------------
``tr`` (total-return index) is built *forward in time* ("causally"):

    for a stock with valid bars at sessions p < t and none in between:
        hold 1 share from the close of p; for every ex-date e in (p, t]:
            shares *= split_ratio[e];  cash += shares * dividend[e]
        tr[t] = tr[p] * (shares * close[t] + cash) / close[p]

The 12-1 momentum ratio tr[t-21] / tr[t-252] is therefore identical to the ratio of
provider-style back-adjusted closes, but it is impossible for it to depend on any
event after the signal session (a back-adjustment factor from a later event would
multiply numerator and denominator equally anyway).

``mark`` is the price used to value a holding at a session's close: the raw close
if a bar exists, otherwise the last raw close carried forward and restated for
any splits/dividends since (``stale`` = True).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..calendar import TradingCalendar


@dataclass
class Panel:
    sessions: list[str]
    symbols: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    split_ratio: np.ndarray        # 1.0 where no split
    dividend: np.ndarray           # 0.0 where no dividend
    tr: np.ndarray                 # causal total-return index, NaN where no bar
    mark: np.ndarray               # valuation price (close or restated carried close)
    stale: np.ndarray              # bool: mark is carried forward
    valid_count: np.ndarray        # cumulative number of valid bars up to and including t
    instruments: pd.DataFrame      # indexed by symbol
    benchmark: str | None
    sess_index: dict[str, int] = field(default_factory=dict)
    sym_index: dict[str, int] = field(default_factory=dict)
    delist_idx: np.ndarray | None = None   # session index of last trading day, -1 if not delisted
    list_idx: np.ndarray | None = None     # session index of first bar, len(sessions) if none
    membership: np.ndarray | None = None   # bool (T, N): point-in-time index membership; None = not provided
    fundamentals: object | None = None     # PointInTimeFundamentals (value signal); None = not loaded

    def __post_init__(self) -> None:
        self.sess_index = {s: i for i, s in enumerate(self.sessions)}
        self.sym_index = {s: j for j, s in enumerate(self.symbols)}

    @property
    def T(self) -> int:
        return len(self.sessions)

    def col(self, symbol: str) -> int:
        return self.sym_index[symbol]

    def has_bar(self, t: int, j: int) -> bool:
        return not np.isnan(self.close[t, j])


def build_panel(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    instruments: pd.DataFrame,
    calendar: TradingCalendar,
    benchmark: str | None,
    end: str | None = None,
    membership: pd.DataFrame | None = None,
) -> Panel:
    """bars: symbol, session, open, high, low, close, volume (raw).
    actions: symbol, ex_date, action_type, ratio, amount.
    instruments: DataFrame with at least symbol, asset_type, is_benchmark, list_date, delist_date.
    """
    if bars.empty:
        raise ValueError("no bars available to build a panel")
    if end is not None:
        bars = bars[bars["session"] <= end]
        actions = actions[actions["ex_date"] <= end]
    first, last = bars["session"].min(), bars["session"].max()
    sessions = calendar.between(first, last)
    sess_index = {s: i for i, s in enumerate(sessions)}
    symbols = sorted(set(bars["symbol"]) | set(instruments["symbol"]))
    sym_index = {s: j for j, s in enumerate(symbols)}
    T, N = len(sessions), len(symbols)

    bars = bars[bars["session"].isin(sess_index)]
    ti = bars["session"].map(sess_index).to_numpy()
    si = bars["symbol"].map(sym_index).to_numpy()

    def mat(colname: str) -> np.ndarray:
        m = np.full((T, N), np.nan)
        m[ti, si] = bars[colname].to_numpy(dtype=float)
        return m

    opn, high, low, close, vol = (mat(c) for c in ("open", "high", "low", "close", "volume"))
    # A bar without a positive close is not a valid bar.
    bad = ~(close > 0)
    for m in (opn, high, low, close, vol):
        m[bad] = np.nan
    opn[~(opn > 0)] = np.nan

    split = np.ones((T, N))
    div = np.zeros((T, N))
    if not actions.empty:
        a = actions[actions["ex_date"].isin(sess_index) & actions["symbol"].isin(sym_index)]
        at = a["ex_date"].map(sess_index).to_numpy()
        aj = a["symbol"].map(sym_index).to_numpy()
        is_split = (a["action_type"] == "split").to_numpy()
        ratio = a["ratio"].to_numpy(dtype=float)
        amount = a["amount"].to_numpy(dtype=float)
        for t, j, sp, r, amt in zip(at, aj, is_split, ratio, amount):
            if sp and r > 0:
                split[t, j] *= r
            elif not sp and amt > 0:
                div[t, j] += amt

    tr = np.full((T, N), np.nan)
    mark = np.full((T, N), np.nan)
    stale = np.zeros((T, N), dtype=bool)
    valid_count = np.zeros((T, N), dtype=np.int32)
    prev_close = np.full(N, np.nan)
    prev_tr = np.full(N, np.nan)
    gap_shares = np.ones(N)
    gap_cash = np.zeros(N)
    last_mark = np.full(N, np.nan)
    count = np.zeros(N, dtype=np.int32)
    for t in range(T):
        gap_shares *= split[t]
        gap_cash += gap_shares * div[t]
        has = ~np.isnan(close[t])
        started = ~np.isnan(prev_close)
        upd = has & started
        tr[t, upd] = prev_tr[upd] * (gap_shares[upd] * close[t, upd] + gap_cash[upd]) / prev_close[upd]
        new = has & ~started
        tr[t, new] = close[t, new]
        # restated carried mark for names without a bar today
        carried = ~has & ~np.isnan(last_mark)
        last_mark[carried] = last_mark[carried] / split[t, carried] - div[t, carried]
        last_mark[has] = close[t, has]
        mark[t] = last_mark
        stale[t] = carried
        count += has
        valid_count[t] = count
        prev_close[has] = close[t, has]
        prev_tr[has] = tr[t, has]
        gap_shares[has] = 1.0
        gap_cash[has] = 0.0

    inst = instruments.set_index("symbol").reindex(symbols)
    inst["asset_type"] = inst["asset_type"].fillna("other")
    inst["is_benchmark"] = inst["is_benchmark"].fillna(0).astype(bool)
    delist_idx = np.full(N, -1)
    list_idx = np.full(N, T)
    has_any = ~np.isnan(close)
    for j, sym in enumerate(symbols):
        rows = np.flatnonzero(has_any[:, j])
        if rows.size:
            list_idx[j] = rows[0]
        dd = inst.at[sym, "delist_date"] if "delist_date" in inst.columns else None
        if isinstance(dd, str) and dd in sess_index:
            delist_idx[j] = sess_index[dd]
        elif isinstance(dd, str) and dd < sessions[0]:
            delist_idx[j] = -2  # delisted before the panel starts

    member = None
    if membership is not None and not membership.empty:
        # rows: symbol, start (first session in the index), end (last session, None = still a member)
        member = np.zeros((T, N), dtype=bool)
        for sym, start, stop in membership[["symbol", "start", "end"]].itertuples(index=False):
            j = sym_index.get(sym)
            if j is None:
                continue
            lo = int(np.searchsorted(np.array(sessions), start))
            hi = int(np.searchsorted(np.array(sessions), stop, side="right")) if isinstance(stop, str) else T
            member[lo:hi, j] = True

    return Panel(
        sessions=sessions, symbols=symbols, open=opn, high=high, low=low, close=close, volume=vol,
        split_ratio=split, dividend=div, tr=tr, mark=mark, stale=stale, valid_count=valid_count,
        instruments=inst, benchmark=benchmark, delist_idx=delist_idx, list_idx=list_idx, membership=member,
    )
