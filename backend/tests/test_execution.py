"""Whole-share sizing, sell-before-buy, cash accounting and transaction costs (hand-computed numbers)."""

import pytest

from app.strategy.execution import CostModel, execute_rebalance, r2

ZERO = CostModel(0, 0, 0)


def test_whole_shares_and_residual_cash():
    ex = execute_rebalance({}, 1000.0, {"A": 0.5, "B": 0.5}, ["A", "B"], {"A": 33.0, "B": 70.0}, {}, ZERO)
    assert ex.target_shares == {"A": 15, "B": 7}      # floor(500/33), floor(500/70)
    assert [(f.symbol, f.side, f.shares, f.gross_value) for f in ex.fills] == [("A", "buy", 15, 495.0), ("B", "buy", 7, 490.0)]
    assert ex.cash_after == 15.0
    assert ex.shares_after == {"A": 15, "B": 7}


def test_sell_before_buy_with_slippage():
    ex = execute_rebalance({"C": 10}, 0.0, {"A": 1.0}, ["A"], {"A": 50.0, "C": 100.0}, {}, CostModel(10, 0, 0))
    assert ex.nav_at_open == 1000.0
    sides = [(f.symbol, f.side) for f in ex.fills]
    assert sides == [("C", "sell"), ("A", "buy")]
    sell, buy = ex.fills
    assert (sell.fill_price, sell.gross_value, sell.slippage_cost) == (99.9, 999.0, 1.0)
    assert buy.fill_price == 50.05 and buy.shares == 19          # floor(1000 / 50.05)
    assert buy.gross_value == 950.95 and buy.slippage_cost == 0.95
    assert ex.cash_after == 48.05                                 # 999.00 - 950.95
    assert ex.turnover == pytest.approx((999.0 + 950.95) / 2 / 1000.0)


def test_commission_and_bps_costs():
    costs = CostModel(slippage_bps=5, commission_per_order=0.5, commission_bps=10)
    ex = execute_rebalance({}, 2100.0, {"A": 1.0}, ["A"], {"A": 20.0}, {}, costs)
    (f,) = ex.fills
    assert f.fill_price == 20.01
    assert f.shares == 104                     # floor((2100 - 0.5) / (20.01 * 1.001))
    assert f.gross_value == 2081.04
    assert f.commission == 2.58                # 0.5 + 10bps * 2081.04
    assert f.slippage_cost == 1.04             # 104 * 0.01
    assert ex.cash_after == r2(2100 - 2081.04 - 2.58) == 16.38


def test_missing_open_is_not_substituted():
    ex = execute_rebalance({}, 1000.0, {"A": 0.5, "B": 0.5}, ["A", "B"], {"A": None, "B": 20.0}, {"A": 19.0}, ZERO)
    assert [f.symbol for f in ex.fills] == ["B"]
    assert ex.fills[0].shares == 25
    assert ex.cash_after == 500.0
    assert {"symbol": "A", "reason": "no_open_price"}.items() <= ex.unfilled[0].items()


def test_unsellable_holding_limits_buys_partial_fill():
    # C has no open (halted): it cannot be sold, is valued at its mark, and the buy is cash-limited.
    ex = execute_rebalance({"C": 1}, 100.0, {"A": 1.0}, ["A"], {"A": 10.0, "C": None}, {"C": 100.0}, ZERO)
    assert ex.nav_at_open == 200.0 and ex.stale_valuations == ["C"]
    assert ex.target_shares["A"] == 20
    assert ex.fills[0].shares == 10 and ex.cash_after == 0.0
    reasons = {(u["symbol"], u["reason"]) for u in ex.unfilled}
    assert reasons == {("C", "no_open_price"), ("A", "partial_insufficient_cash")}
    assert ex.shares_after == {"C": 1, "A": 10}


def test_trim_and_no_leverage_invariants():
    ex = execute_rebalance({"A": 100, "B": 5}, 10.0, {"A": 0.5, "B": 0.5}, ["B", "A"], {"A": 10.0, "B": 10.0}, {},
                           CostModel(10, 1.0, 0))
    assert ex.fills[0].side == "sell" and ex.fills[0].symbol == "A"
    assert ex.cash_after >= 0
    assert all(q > 0 for q in ex.shares_after.values())
    total = ex.cash_after + sum(q * 10.0 for q in ex.shares_after.values())
    assert total == pytest.approx(ex.nav_at_open - ex.slippage_cost - ex.commission, abs=0.02)
