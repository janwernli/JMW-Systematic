"""Backtest event ordering, calendar boundaries, no look-ahead, costs, repeatability."""

import numpy as np
import pytest

from app.backtest.engine import run_backtest
from app.backtest.metrics import compute_metrics, monthly_returns
from app.strategy.config import StrategyConfig

from .helpers import make_panel, weekday_calendar

SMALL = dict(lookback_sessions=10, skip_sessions=2, min_history_sessions=10, adv_window=3, min_adv_usd=0,
             min_price=1, slippage_bps=0, top_n=1, initial_capital=10_000)


def small_cfg(**kw):
    d = dict(SMALL)
    d.update(kw)
    return StrategyConfig(**d)


def test_fill_at_next_session_open_after_holiday():
    # 2020-03-02 is a (synthetic) holiday: Feb month-end signal fills at the 2020-03-03 open.
    cal = weekday_calendar("2020-01-01", 60, holidays=("2020-03-02",))
    n = 60
    close = 20 + np.arange(n) * 0.1
    opn = close - 0.05
    p = make_panel(cal, {"AAA": {"close": close, "open": opn}})
    res = run_backtest(p, cal, small_cfg(start_date="2020-02-03", end_date="2020-03-20"))
    feb = next(r for r in res.rebalances if r.signal_session == "2020-02-28")
    assert feb.fill_session == "2020-03-03"
    (fill,) = feb.fills
    t = cal.index("2020-03-03")
    assert fill.ref_price == pytest.approx(opn[t])
    assert fill.shares == int(10_000 // opn[t])
    # NAV on the signal session is still all cash: nothing trades at the signal close.
    assert res.nav.loc["2020-02-28", "positions"] == 0
    assert res.nav.loc["2020-03-03", "positions"] == 1


def test_missing_fill_open_does_not_trade():
    cal = weekday_calendar("2020-01-01", 60)
    close = 20 + np.arange(60) * 0.1
    opn = close.copy()
    t = cal.index("2020-03-02")
    opn[t] = np.nan
    p = make_panel(cal, {"AAA": {"close": close, "open": opn}})
    res = run_backtest(p, cal, small_cfg(start_date="2020-02-03", end_date="2020-03-20"))
    feb = next(r for r in res.rebalances if r.signal_session == "2020-02-28")
    assert feb.fills == [] and feb.unfilled[0]["reason"] == "no_open_price"
    assert res.nav.loc["2020-03-02", "cash"] == 10_000


def test_backtest_no_look_ahead():
    cal = weekday_calendar("2020-01-01", 200)
    rng = np.random.default_rng(7)
    series = {s: {"close": 50 * np.exp(rng.normal(0.001 * k, 0.02, 200).cumsum())} for k, s in enumerate("ABCDE")}
    p1 = make_panel(cal, series)
    cut = cal.index("2020-06-30")
    series2 = {s: {"close": d["close"].copy()} for s, d in series.items()}
    for d in series2.values():
        d["close"][cut + 1:] *= rng.uniform(0.5, 2.0)
    p2 = make_panel(cal, series2)
    cfg = small_cfg(top_n=2)
    r1, r2 = run_backtest(p1, cal, cfg), run_backtest(p2, cal, cfg)
    s = cal.sessions[cut]
    assert r1.nav.loc[:s].equals(r2.nav.loc[:s])
    sel1 = {r.signal_session: list(r.signals.selected["symbol"]) for r in r1.rebalances if r.signal_session <= s}
    sel2 = {r.signal_session: list(r.signals.selected["symbol"]) for r in r2.rebalances if r.signal_session <= s}
    assert sel1 == sel2


def test_dividend_not_double_counted_in_backtest():
    cal = weekday_calendar("2020-01-01", 60)
    close = np.full(60, 50.0)
    k = cal.index("2020-03-10")
    close[k:] = 49.0
    p = make_panel(cal, {"AAA": {"close": close}}, actions=[("AAA", cal.sessions[k], "cash_dividend", None, 1.0)])
    res = run_backtest(p, cal, small_cfg(start_date="2020-02-03", end_date="2020-03-20"))
    nav = res.nav["nav"]
    prev = cal.sessions[k - 1]
    assert res.nav.loc[prev, "positions"] == 1
    assert nav.loc[cal.sessions[k]] == pytest.approx(nav.loc[prev])  # price drop offset by cash dividend
    assert any(e.kind == "dividend" and e.amount == 200.0 for _, e in res.cash_events)  # 200 shares * $1


def test_gross_equals_net_plus_costs_and_costs_reduce_returns():
    cal = weekday_calendar("2020-01-01", 200)
    rng = np.random.default_rng(3)
    series = {s: {"close": 30 * np.exp(rng.normal(0.0005 * k, 0.02, 200).cumsum())} for k, s in enumerate("ABCDEF")}
    p = make_panel(cal, series)
    free = run_backtest(p, cal, small_cfg(top_n=3))
    costly = run_backtest(p, cal, small_cfg(top_n=3, slippage_bps=25, commission_per_order=1.0))
    costs = sum(r.slippage_cost + r.commission for r in costly.rebalances)
    assert costs > 0
    assert costly.nav["gross_nav"].iloc[-1] - costly.nav["nav"].iloc[-1] == pytest.approx(costs, abs=0.05)
    assert costly.nav["nav"].iloc[-1] < free.nav["nav"].iloc[-1]
    assert free.nav["gross_nav"].equals(free.nav["nav"])


def test_nav_is_cash_plus_marked_positions_every_day():
    cal = weekday_calendar("2020-01-01", 150)
    rng = np.random.default_rng(11)
    series = {s: {"close": 30 * np.exp(rng.normal(0, 0.02, 150).cumsum())} for s in "ABCD"}
    res = run_backtest(make_panel(cal, series), cal, small_cfg(top_n=2, slippage_bps=10))
    diff = (res.nav["cash"] + res.nav["positions_value"] - res.nav["nav"]).abs()
    assert diff.max() < 0.011
    assert (res.nav["cash"] >= 0).all()


def test_repeatable_runs_are_identical():
    cal = weekday_calendar("2020-01-01", 180)
    rng = np.random.default_rng(5)
    series = {s: {"close": 40 * np.exp(rng.normal(0, 0.02, 180).cumsum())} for s in "ABCDEFGH"}
    p = make_panel(cal, series)
    cfg = small_cfg(top_n=3, slippage_bps=10)
    a, b = run_backtest(p, cal, cfg), run_backtest(p, cal, cfg)
    assert a.nav.equals(b.nav)
    assert [(s, f.symbol, f.shares, f.fill_price) for s, f in a.fills] == [(s, f.symbol, f.shares, f.fill_price) for s, f in b.fills]


def test_metrics_formulas():
    import pandas as pd
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
