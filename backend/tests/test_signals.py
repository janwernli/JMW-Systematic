"""Momentum lookback, eligibility, ranking and no-look-ahead at the signal level."""

import numpy as np
import pytest

from app.strategy.config import StrategyConfig
from app.strategy.signals import compute_signals

from .helpers import make_panel, weekday_calendar

N = 300
CAL = weekday_calendar("2020-01-01", N)
BASE = 100.0 + np.arange(N)  # close_i = 100 + i


def cfg(**kw):
    base = dict(min_adv_usd=0, min_price=5)
    base.update(kw)
    return StrategyConfig(**base)


def month_end_index(min_idx: int) -> int:
    for s in CAL.sessions:
        i = CAL.index(s)
        if i >= min_idx and CAL.is_month_end(s):
            return i
    raise AssertionError


def test_12_1_momentum_uses_t21_over_t252():
    t = month_end_index(260)
    p = make_panel(CAL, {"AAA": {"close": BASE}})
    sig = compute_signals(p, t, cfg())
    row = sig.table.set_index("symbol").loc["AAA"]
    expected = (100 + t - 21) / (100 + t - 252) - 1
    assert row["momentum"] == pytest.approx(expected, rel=1e-12)
    assert row["session_t21"] == CAL.sessions[t - 21]
    assert row["session_t252"] == CAL.sessions[t - 252]
    assert row["eligible"] and row["rank"] == 1


def test_requires_252_prior_sessions():
    t = month_end_index(260)
    late = BASE.copy()
    late[: t - 251] = np.nan  # listed at t-251: only 251 bars before t, and no bar at t-252
    sig = compute_signals(make_panel(CAL, {"NEW": {"close": late}, "OLD": {"close": BASE}}), t, cfg())
    tab = sig.table.set_index("symbol")
    assert tab.loc["NEW", "reason"] == "insufficient_history"
    assert tab.loc["NEW", "valid_history"] == 251
    assert tab.loc["OLD", "eligible"] and sig.eligible_count == 1


def test_lookback_measured_in_exchange_sessions_not_stock_rows():
    t = month_end_index(260)
    gap = BASE.copy()
    gap[t - 100] = np.nan          # a halt between the anchors does not shift the anchors
    miss = BASE.copy()
    miss[t - 21] = np.nan          # missing the t-21 anchor itself -> ineligible, never "nearest row"
    p = make_panel(CAL, {"AAA": {"close": BASE}, "GAP": {"close": gap}, "MIS": {"close": miss}})
    tab = compute_signals(p, t, cfg()).table.set_index("symbol")
    assert tab.loc["GAP", "momentum"] == pytest.approx(tab.loc["AAA", "momentum"], rel=1e-12)
    assert tab.loc["MIS", "reason"] == "missing_lookback_price"


def test_split_inside_lookback_does_not_distort_momentum():
    t = month_end_index(260)
    k = t - 100
    raw = BASE.copy()
    raw[k:] = raw[k:] / 2.0  # 2-for-1 split effective at session k
    p = make_panel(CAL, {"AAA": {"close": BASE}, "SPL": {"close": raw}}, actions=[("SPL", CAL.sessions[k], "split", 2.0, None)])
    tab = compute_signals(p, t, cfg()).table.set_index("symbol")
    assert tab.loc["SPL", "momentum"] == pytest.approx(tab.loc["AAA", "momentum"], rel=1e-12)


def test_dividend_inside_lookback_counts_as_total_return():
    t = month_end_index(260)
    k = t - 100
    p = make_panel(CAL, {"DIV": {"close": BASE}}, actions=[("DIV", CAL.sessions[k], "cash_dividend", None, 5.0)])
    tab = compute_signals(p, t, cfg()).table.set_index("symbol")
    price_ratio = (100 + t - 21) / (100 + t - 252)
    expected = price_ratio * (BASE[k] + 5.0) / BASE[k] - 1
    assert tab.loc["DIV", "momentum"] == pytest.approx(expected, rel=1e-12)


def test_price_filter_uses_raw_point_in_time_price():
    t = month_end_index(260)
    raw = np.full(N, 7.0) * (1 + np.arange(N) * 0.001)
    k = t - 50
    raw[k:] = raw[k:] / 2.0  # split takes raw price to ~4-5 at the signal date
    p = make_panel(CAL, {"LOW": {"close": raw}}, actions=[("LOW", CAL.sessions[k], "split", 2.0, None)])
    row = compute_signals(p, t, cfg()).table.iloc[0]
    assert row["close_raw"] < 5 and row["reason"] == "price_below_min"


def test_liquidity_filter_and_complete_window():
    t = month_end_index(260)
    vol_ok = np.full(N, 60_000.0)       # ~ $ (100+t) * 60k > $5m... at t ~ 300 -> $18m
    vol_low = np.full(N, 1_000.0)       # ~ $0.3m
    holey = BASE.copy()
    holey[t - 3] = np.nan               # one missing bar in the 20-session ADV window
    p = make_panel(CAL, {"LIQ": {"close": BASE, "volume": vol_ok}, "ILL": {"close": BASE, "volume": vol_low},
                         "HOL": {"close": holey, "volume": vol_ok}})
    tab = compute_signals(p, t, StrategyConfig()).table.set_index("symbol")
    assert tab.loc["LIQ", "eligible"]
    assert tab.loc["ILL", "reason"] == "adv_below_min"
    assert tab.loc["HOL", "reason"] == "missing_liquidity_data"
    window = BASE[t - 19: t + 1] * 60_000
    assert tab.loc["LIQ", "adv20"] == pytest.approx(window.mean())


def test_deterministic_tie_break_and_top_n():
    t = month_end_index(260)
    series = {s: {"close": BASE, "volume": np.full(N, v)} for s, v in [("CCC", 1e6), ("BBB", 2e6), ("AAA", 1e6)]}
    sig = compute_signals(make_panel(CAL, series), t, cfg(top_n=2))
    order = list(sig.table.sort_values("rank")["symbol"])
    assert order == ["BBB", "AAA", "CCC"]  # equal momentum -> higher ADV, then symbol ascending
    assert list(sig.selected["symbol"]) == ["BBB", "AAA"]
    assert sig.selected["target_weight"].tolist() == [0.5, 0.5]


def test_excludes_non_common_and_benchmark():
    t = month_end_index(260)
    p = make_panel(CAL, {"AAA": {"close": BASE}, "ETF": {"close": BASE}, "SPYX": {"close": BASE}},
                   benchmark="SPYX", types={"ETF": "etf"})
    tab = compute_signals(p, t, cfg()).table.set_index("symbol")
    assert tab.loc["ETF", "reason"] == "excluded_asset_type"
    assert tab.loc["SPYX", "reason"] == "benchmark"
    assert tab.loc["AAA", "eligible"]


def test_no_look_ahead_future_data_cannot_change_signal():
    t = month_end_index(260)
    rng = np.random.default_rng(1)
    a = BASE * np.exp(rng.normal(0, 0.01, N).cumsum())
    b = BASE[::-1] + 50
    p1 = make_panel(CAL, {"AAA": {"close": a}, "BBB": {"close": b}})
    a2, b2 = a.copy(), b.copy()
    a2[t + 1:] *= 3.0
    b2[t + 1:] = np.nan  # delisted right after
    p2 = make_panel(CAL, {"AAA": {"close": a2}, "BBB": {"close": b2}},
                    actions=[("AAA", CAL.sessions[t + 5], "split", 4.0, None),
                             ("AAA", CAL.sessions[t + 7], "cash_dividend", None, 3.0)])
    s1 = compute_signals(p1, t, cfg()).table
    s2 = compute_signals(p2, t, cfg()).table
    cols = ["symbol", "momentum", "rank", "eligible", "reason", "close_raw", "adv20"]
    assert s1[cols].equals(s2[cols])


def test_blocks_when_coverage_insufficient():
    t = month_end_index(260)
    series = {f"S{i:02d}": {"close": BASE.copy()} for i in range(10)}
    for i in range(3):
        series[f"S{i:02d}"]["close"][t] = np.nan  # 70% coverage at the signal session
    sig = compute_signals(make_panel(CAL, series), t, cfg())
    assert sig.coverage == pytest.approx(0.7)
    assert sig.blocked_reason and "70%" in sig.blocked_reason
