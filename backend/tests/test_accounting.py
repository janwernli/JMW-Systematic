"""Split / dividend / delisting treatment of holdings."""

import numpy as np
import pytest

from app.strategy.execution import apply_corporate_actions, delisting_cashouts, mark_to_market

from .helpers import make_panel, weekday_calendar

CAL = weekday_calendar("2021-01-04", 10)


def test_forward_split_with_cash_in_lieu():
    close = np.array([10, 10, 10 / 1.5, 10 / 1.5, 10 / 1.5, 10 / 1.5, 10 / 1.5, 10 / 1.5, 10 / 1.5, 10 / 1.5])
    p = make_panel(CAL, {"A": {"close": close}}, actions=[("A", CAL.sessions[2], "split", 1.5, None)])
    shares, evs = apply_corporate_actions(p, 2, {"A": 101})
    assert shares == {"A": 151}                       # 101 * 1.5 = 151.5
    assert [(e.kind, e.amount) for e in evs] == [("cash_in_lieu", 3.33)]  # 0.5 * 10/1.5


def test_reverse_split():
    close = np.array([10, 10, 20, 20, 20, 20, 20, 20, 20, 20.0])
    p = make_panel(CAL, {"A": {"close": close}}, actions=[("A", CAL.sessions[2], "split", 0.5, None)])
    shares, evs = apply_corporate_actions(p, 2, {"A": 101})
    assert shares == {"A": 50}
    assert evs[0].amount == 10.0                      # 0.5 share * $20


def test_dividend_credited_once_and_nav_continuous():
    close = np.array([50, 50, 49, 49, 49, 49, 49, 49, 49, 49.0])
    p = make_panel(CAL, {"A": {"close": close}}, actions=[("A", CAL.sessions[2], "cash_dividend", None, 1.0)])
    shares, evs = apply_corporate_actions(p, 2, {"A": 150})
    assert [(e.kind, e.amount) for e in evs] == [("dividend", 150.0)]
    before, _, _ = mark_to_market(p, 1, {"A": 150})
    after, _, _ = mark_to_market(p, 2, shares)
    # raw price fell by the dividend; cash rose by the dividend -> NAV unchanged (no double count)
    assert after + 150.0 == pytest.approx(before)


def test_split_and_dividend_same_day_dividend_on_post_split_shares():
    close = np.array([100, 100, 49, 49, 49, 49, 49, 49, 49, 49.0])
    p = make_panel(CAL, {"A": {"close": close}}, actions=[("A", CAL.sessions[2], "split", 2.0, None),
                                                         ("A", CAL.sessions[2], "cash_dividend", None, 1.0)])
    shares, evs = apply_corporate_actions(p, 2, {"A": 10})
    assert shares == {"A": 20} and evs[0].amount == 20.0


def test_delisting_cashout_at_last_close():
    close = np.array([10, 11, 12, 9, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan])
    p = make_panel(CAL, {"A": {"close": close}, "B": {"close": np.full(10, 5.0)}}, delist={"A": CAL.sessions[3]})
    shares, evs = delisting_cashouts(p, 3, {"A": 100, "B": 10})
    assert shares == {"B": 10}
    assert (evs[0].kind, evs[0].symbol, evs[0].amount) == ("delisting_cashout", "A", 900.0)


def test_stale_mark_carried_forward_and_flagged():
    close = np.array([10, 11, np.nan, 12, 12, 12, 12, 12, 12, 12.0])
    p = make_panel(CAL, {"A": {"close": close}})
    value, stale, marks = mark_to_market(p, 2, {"A": 10})
    assert value == 110.0 and stale == 1 and marks["A"] == (11.0, True)
