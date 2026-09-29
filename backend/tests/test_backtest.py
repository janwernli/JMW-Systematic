"""Backtest event ordering, calendar boundaries, no look-ahead, costs, repeatability (long-short)."""

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import run_backtest
from app.backtest.metrics import compute_metrics, monthly_returns
from app.strategy.config import StrategyConfig

from .helpers import make_panel, weekday_calendar

SMALL = dict(signal="momentum_12_1",
             htb_exclude_pct=0.0,
             sector_neutral=False,
             crash_guard=False,
             borrow_fee_annual=0.0,
             short_min_price=0,
             min_names_per_side=1,
             max_names_per_side=1,
             vol_lookback_sessions=20,
             beta_lookback_sessions=60,
             max_long_weight=1.0,
             max_short_weight=1.0,
             lookback_sessions=10, skip_sessions=2, min_history_sessions=10, adv_window=3,
             min_adv_usd=0, min_price=1, slippage_bps=0, initial_capital=10_000)


def small_cfg(**kw):
    d = dict(SMALL)
    d.update(kw)
    return StrategyConfig(**d)


def two_stock(n=60, **cal_kw):
    cal = weekday_calendar("2020-01-01", n, **cal_kw)
    up = 20 + np.arange(n) * 0.1
    down = 40 - np.arange(n) * 0.1
    return cal, {"UP": {"close": up, "open": up - 0.05}, "DN": {"close": down, "open": down + 0.05}}


def test_fill_at_next_session_open_after_holiday():
    # 2020-03-02 is a (synthetic) holiday: the Feb month-end signal fills at the 2020-03-03 open.
    cal, series = two_stock(holidays=("2020-03-02",))
    p = make_panel(cal, series)
    res = run_backtest(p, cal, small_cfg(start_date="2020-02-03", end_date="2020-03-20"))
    feb = next(r for r in res.rebalances if r.signal_session == "2020-02-28")
    assert feb.fill_session == "2020-03-03"
    t = cal.index("2020-03-03")
    fills = {f.symbol: f for f in feb.fills}
    assert fills["UP"].side == "buy" and fills["UP"].ref_price == pytest.approx(series["UP"]["open"][t])
    assert fills["DN"].side == "sell" and fills["DN"].effect == "open_short"
    assert res.nav.loc["2020-02-28", "positions"] == 0           # nothing trades at the signal close
    assert res.nav.loc["2020-03-03", "positions"] == 2


def test_missing_fill_open_does_not_trade():
    cal, series = two_stock()
    t = cal.index("2020-03-02")
    for d in series.values():
        d["open"] = d["open"].copy()
        d["open"][t] = np.nan
    p = make_panel(cal, series)
    res = run_backtest(p, cal, small_cfg(start_date="2020-02-03", end_date="2020-03-20"))
    feb = next(r for r in res.rebalances if r.signal_session == "2020-02-28")
    assert feb.fills == [] and {u["reason"] for u in feb.unfilled} == {"no_open_price"}
    assert res.nav.loc["2020-03-02", "cash"] == 10_000


def test_backtest_no_look_ahead():
    cal = weekday_calendar("2020-01-01", 200)
    rng = np.random.default_rng(7)
    series = {s: {"close": 50 * np.exp(rng.normal(0.001 * (k - 2), 0.02, 200).cumsum())} for k, s in enumerate("ABCDE")}
    p1 = make_panel(cal, series)
    cut = cal.index("2020-06-30")
    series2 = {s: {"close": d["close"].copy()} for s, d in series.items()}
    for d in series2.values():
        d["close"][cut + 1:] *= rng.uniform(0.5, 2.0)
    p2 = make_panel(cal, series2)
    cfg = small_cfg()
    r1, r2 = run_backtest(p1, cal, cfg), run_backtest(p2, cal, cfg)
    s = cal.sessions[cut]
    assert r1.nav.loc[:s].equals(r2.nav.loc[:s])
    sel = lambda r: {x.signal_session: dict(zip(x.signals.selected["symbol"], x.signals.selected["side"]))  # noqa: E731
                     for x in r.rebalances if x.signal_session <= s}
    assert sel(r1) == sel(r2)


def test_dividend_not_double_counted_in_backtest():
    cal = weekday_calendar("2020-01-01", 60)
    i = np.arange(60)
    up = 40 + i * 0.2 + 0.3 * np.sin(i)    # a winner with some day-to-day movement (needs non-zero vol)
    dn = 40 - i * 0.2 + 0.3 * np.cos(i)    # a loser (short)
    k = cal.index("2020-03-10")
    up[k:] = up[k:] - up[k] + (up[k - 1] - 1.0)   # ex-date: price drops by exactly the $1 dividend
    dn[k] = dn[k - 1]                               # the short does not move on the ex-date
    p = make_panel(cal, {"UP": {"close": up}, "DN": {"close": dn}},
                   actions=[("UP", cal.sessions[k], "cash_dividend", None, 1.0)])
    res = run_backtest(p, cal, small_cfg(start_date="2020-02-03", end_date="2020-03-20"))
    nav = res.nav["nav"]
    prev = cal.sessions[k - 1]
    assert res.nav.loc[prev, "positions"] == 2
    assert nav.loc[cal.sessions[k]] == pytest.approx(nav.loc[prev])   # price drop offset by the cash dividend
    assert any(e.kind == "dividend" and e.symbol == "UP" and e.amount > 0 for _, e in res.cash_events)


def test_gross_equals_net_plus_costs_and_costs_reduce_returns():
    cal = weekday_calendar("2020-01-01", 200)
    rng = np.random.default_rng(3)
    series = {s: {"close": 30 * np.exp(rng.normal(0.0005 * (k - 3), 0.02, 200).cumsum())} for k, s in enumerate("ABCDEF")}
    p = make_panel(cal, series)
    free = run_backtest(p, cal, small_cfg())
    costly = run_backtest(p, cal, small_cfg(slippage_bps=25, commission_per_order=1.0, borrow_fee_annual=0.02))
    trading = sum(r.slippage_cost + r.commission for r in costly.rebalances)
    borrow = -sum(e.amount for _, e in costly.cash_events if e.kind == "borrow_fee")
    assert trading > 0 and borrow > 0
    assert costly.nav["gross_nav"].iloc[-1] - costly.nav["nav"].iloc[-1] == pytest.approx(trading + borrow, abs=0.05)
    assert costly.nav["nav"].iloc[-1] < free.nav["nav"].iloc[-1]
    assert free.nav["gross_nav"].equals(free.nav["nav"])


def test_nav_is_cash_plus_marked_positions_every_day():
    cal = weekday_calendar("2020-01-01", 150)
    rng = np.random.default_rng(11)
    series = {s: {"close": 30 * np.exp(rng.normal(0, 0.02, 150).cumsum())} for s in "ABCD"}
    res = run_backtest(make_panel(cal, series), cal, small_cfg(slippage_bps=10))
    diff = (res.nav["cash"] + res.nav["positions_value"] - res.nav["nav"]).abs()
    assert diff.max() < 0.011
    assert (res.nav["long_value"] + res.nav["short_value"] - res.nav["positions_value"]).abs().max() < 0.011


def test_repeatable_runs_are_identical():
    cal = weekday_calendar("2020-01-01", 180)
    rng = np.random.default_rng(5)
    series = {s: {"close": 40 * np.exp(rng.normal(0, 0.02, 180).cumsum())} for s in "ABCDEFGH"}
    p = make_panel(cal, series)
    cfg = small_cfg(slippage_bps=10, max_names_per_side=3)
    a, b = run_backtest(p, cal, cfg), run_backtest(p, cal, cfg)
    assert a.nav.equals(b.nav)
    assert [(s, f.symbol, f.shares, f.fill_price) for s, f in a.fills] == [(s, f.symbol, f.shares, f.fill_price) for s, f in b.fills]


def test_metrics_formulas():
    sessions = [f"2021-01-{d:02d}" for d in (4, 5, 6, 7)]
    nav = pd.DataFrame({"nav": [100.0, 110.0, 99.0, 105.6], "gross_nav": [100.0, 110.0, 99.0, 105.6],
                        "benchmark_nav": [100.0, 101.0, 102.0, 103.0], "positions": 1, "cash": 0.0},
                       index=sessions)
    m = compute_metrics(nav, [], 100.0)
    assert m["net"]["total_return"] == pytest.approx(0.056)
    assert m["net"]["max_drawdown"] == pytest.approx(99 / 110 - 1)
    assert m["net"]["cagr"] is None and m["cagr_meaningful"] is False  # < 1 year: CAGR not reported
    assert m["return_difference"] == pytest.approx(0.056 - 0.03)
    assert monthly_returns(nav["nav"], 100.0) == [{"year": 2021, "month": 1, "return": pytest.approx(0.056)}]
