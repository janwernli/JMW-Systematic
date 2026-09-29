"""v2 long-short: sizing, short accounting, borrow fees, stop-loss, buffer, neutrality, crash guard."""

import numpy as np
import pytest

from app.backtest.engine import run_backtest
from app.data.store import data_version, get_panel, run_import
from app.db import Database
from app.ledger.paper import PaperLedger
from app.strategy.config import StrategyConfig
from app.strategy.execution import (
    CostModel,
    Fill,
    apply_corporate_actions,
    borrow_fee,
    cover_shorts,
    execute_rebalance,
    exposures,
    mark_to_market,
    short_stop_triggers,
    update_basis,
)
from app.strategy.long_short import waterfill
from app.strategy.signals import compute_signals

from .helpers import FixtureProvider, make_panel, weekday_calendar

ZERO = CostModel(0, 0, 0)


def ls_cfg(**kw):
    d = dict(mode="long_short", lookback_sessions=10, skip_sessions=2, min_history_sessions=10, adv_window=3,
             min_adv_usd=0, min_price=1, short_min_price=1, slippage_bps=0, vol_lookback_sessions=20,
             beta_lookback_sessions=60, beta_shrink=0.0, min_names_per_side=2, max_names_per_side=5,
             long_pct=0.10, short_pct=0.10, buffer_exit_pct=0.30, max_long_weight=0.5, max_short_weight=0.5,
             crash_guard=False, crash_market_lookback_sessions=21, initial_capital=100_000)
    d.update(kw)
    return StrategyConfig(**d)


# ---------------------------------------------------------------- sizing primitives
def test_waterfill_caps_and_redistributes():
    w = waterfill(np.array([1.0, 1.0, 2.0]), 1.0, 0.4)
    assert w.tolist() == pytest.approx([0.3, 0.3, 0.4])      # 0.5 capped at 0.4, excess 0.1 split evenly
    assert waterfill(np.array([1.0, 3.0]), 1.0, 0.3).tolist() == [0.3, 0.3]  # capacity < total: all at cap


# ---------------------------------------------------------------- short accounting
CAL10 = weekday_calendar("2021-01-04", 10)


def test_short_sale_cash_and_mark_to_market():
    ex = execute_rebalance({}, 10_000.0, {"A": -0.10}, ["A"], {"A": 100.0}, {}, ZERO)
    (f,) = ex.fills
    assert (f.side, f.shares, f.effect, f.reason) == ("sell", 10, "open_short", "short")
    assert ex.cash_after == 11_000.0 and ex.shares_after == {"A": -10}
    p = make_panel(CAL10, {"A": {"close": np.array([100, 110, 110, 110, 110, 110, 110, 110, 110, 110.0])}})
    value, _, _ = mark_to_market(p, 1, {"A": -10})
    assert value == -1100.0                                   # NAV = 11,000 - 1,100 = 9,900: lost $100
    assert exposures(p, 1, {"A": -10, }) == (0.0, -1100.0)


def test_short_pays_dividend_and_split_fractions():
    close = np.array([100, 100, 49, 49, 49, 49, 49, 49, 49, 49.0])
    p = make_panel(CAL10, {"A": {"close": close}}, actions=[("A", CAL10.sessions[2], "split", 2.0, None),
                                                            ("A", CAL10.sessions[2], "cash_dividend", None, 1.0)])
    shares, evs = apply_corporate_actions(p, 2, {"A": -10})
    assert shares == {"A": -20}
    assert [(e.kind, e.amount) for e in evs] == [("dividend", -20.0)]          # short owes 20 sh x $1
    rev = make_panel(CAL10, {"B": {"close": np.array([10, 10, 20, 20, 20, 20, 20, 20, 20, 20.0])}},
                     actions=[("B", CAL10.sessions[2], "split", 0.5, None)])
    shares, evs = apply_corporate_actions(rev, 2, {"B": -15})
    assert shares == {"B": -7}                                                  # -7.5 -> -7 (toward zero)
    assert evs[0].kind == "cash_in_lieu" and evs[0].amount == -10.0             # pays 0.5 sh x $20


def test_borrow_fee_is_daily_share_of_annual_rate():
    p = make_panel(CAL10, {"A": {"close": np.full(10, 100.0)}})
    fee, short_value = borrow_fee(p, 0, {"A": -100}, 0.005)
    assert short_value == 10_000.0 and fee == 0.20            # 10,000 x 0.5% / 252 = 0.198 -> $0.20


def test_execution_order_reduce_short_cover_buy_with_flip():
    ex = execute_rebalance({"A": 10, "B": -5}, 1_000.0, {"A": -0.2, "C": 0.3, "B": 0.0}, ["C", "A"],
                           {"A": 100.0, "B": 50.0, "C": 20.0}, {}, ZERO)
    # NAV = 1,000 + 10*100 - 5*50 = 1,750
    assert ex.nav_at_open == 1750.0
    steps = [(f.symbol, f.side, f.shares, f.effect) for f in ex.fills]
    assert steps == [("A", "sell", 10, "close_long"), ("A", "sell", 3, "open_short"),
                     ("B", "buy", 5, "close_short"), ("C", "buy", 26, "open_long")]
    # A target: -floor(0.2*1750/100) = -3 ; C: floor(0.3*1750/20) = 26
    assert ex.shares_after == {"A": -3, "C": 26}
    assert ex.cash_after == 1000 + 1000 + 300 - 250 - 520


def test_signed_cost_basis_and_stop_trigger():
    basis: dict[str, float] = {}
    update_basis(basis, 0, Fill("A", "sell", 10, 100.0, 100.0, 1000.0, 0.0, 0.0, "short", "open_short"))
    assert basis["A"] == -1000.0                              # short: -(net proceeds)
    close = np.array([100, 120, 149.9, 150, 150, 150, 150, 150, 150, 150.0])
    p = make_panel(CAL10, {"A": {"close": close}})
    assert short_stop_triggers(p, 2, {"A": -10}, basis, 0.5) == []           # +49.9%: no stop
    (trig,) = short_stop_triggers(p, 3, {"A": -10}, basis, 0.5)             # +50%: stop
    assert trig["symbol"] == "A" and trig["entry"] == 100.0
    ex = cover_shorts({"A": -10, "B": 5}, 2000.0, ["A"], {"A": 151.0}, ZERO)
    assert ex.shares_after == {"B": 5} and ex.fills[0].reason == "stop_loss_cover"
    assert ex.cash_after == 490.0                             # 2,000 - 10 x 151
    part = cover_shorts({"A": -10}, 500.0, ["A"], {"A": 151.0}, ZERO)
    assert part.shares_after == {"A": -7}                    # cash-limited: 3 shares covered, reported
    assert part.unfilled[0]["reason"] == "partial_insufficient_cash"


# ---------------------------------------------------------------- book construction
N = 160
CAL = weekday_calendar("2020-01-01", N)


def trending_universe(n_stocks=20, seed=0, market=None, beta=None):
    rng = np.random.default_rng(seed)
    m = market if market is not None else rng.normal(0.0003, 0.01, N)
    series = {"BMK": {"close": 100 * np.exp(np.cumsum(m))}}
    for k in range(n_stocks):
        drift = (k - n_stocks / 2) * 0.0008               # stock 0 = strongest loser ... last = strongest winner
        b = 1.0 if beta is None else beta[k]
        r = b * m + drift + rng.normal(0, 0.012, N)
        series[f"S{k:02d}"] = {"close": 50 * np.exp(np.cumsum(r))}
    return series


def month_end(min_idx):
    return next(CAL.index(s) for s in CAL.sessions if CAL.index(s) >= min_idx and CAL.is_month_end(s))


def test_books_are_disjoint_deciles_with_min_names_and_neutral_beta():
    p = make_panel(CAL, trending_universe(), benchmark="BMK")
    t = month_end(120)
    sig = compute_signals(p, t, ls_cfg())
    tab = sig.table.set_index("symbol")
    longs = tab[tab["side"] == "long"]
    shorts = tab[tab["side"] == "short"]
    assert len(longs) == 2 and len(shorts) == 2              # decile of 20 = 2, min names 2
    assert longs["rank"].max() < shorts["rank"].min()
    d = sig.diagnostics
    assert d["ex_ante_net_beta"] == pytest.approx(0.0, abs=1e-9)
    assert d["short_gross"] == pytest.approx(d["long_gross"] * d["beta_ratio"])
    assert longs["target_weight"].sum() == pytest.approx(d["long_gross"])
    assert -shorts["target_weight"].sum() == pytest.approx(d["short_gross"])


def test_buffer_keeps_held_names_until_exit_threshold():
    p = make_panel(CAL, trending_universe(), benchmark="BMK")
    t = month_end(120)
    base = compute_signals(p, t, ls_cfg()).table.set_index("symbol")
    elig = base[base["eligible"]].sort_values("rank")
    fourth = elig.index[3]                                    # rank 4 of 20 -> percentile 0.15: inside 30%, outside 10%
    tenth = elig.index[9]                                     # rank 10 -> percentile 0.45: outside the buffer
    tab = compute_signals(p, t, ls_cfg(), held_long={fourth, tenth}).table.set_index("symbol")
    assert tab.loc[fourth, "side"] == "long"                  # kept by the buffer
    assert tab.loc[tenth, "side"] is None                     # exits
    assert base.loc[fourth, "side"] is None                   # would not have been entered fresh


def test_short_price_floor_and_blocked_names():
    uni = trending_universe()
    uni["S00"]["close"] = uni["S00"]["close"] / 50 * 8        # strongest loser trades around $8
    p = make_panel(CAL, uni, benchmark="BMK")
    t = month_end(120)
    tab = compute_signals(p, t, ls_cfg(short_min_price=10)).table.set_index("symbol")
    assert tab.loc["S00", "close_raw"] < 10 and tab.loc["S00", "side"] is None
    blocked = compute_signals(p, t, ls_cfg(), blocked_shorts={"S01"}).table.set_index("symbol")
    assert blocked.loc["S01", "side"] is None


def test_crash_guard_halves_short_book():
    crash = -0.004 + 0.03 * np.where(np.arange(N) % 2 == 0, 1.0, -1.0)   # falling, ~48% annualized vol
    p = make_panel(CAL, trending_universe(market=crash, seed=4), benchmark="BMK")
    t = month_end(120)
    off = compute_signals(p, t, ls_cfg()).diagnostics
    on = compute_signals(p, t, ls_cfg(crash_guard=True, crash_market_vol_threshold=0.2)).diagnostics
    assert on["crash_guard"]["active"] and on["crash_guard"]["market_return"] < 0
    assert on["short_gross"] == pytest.approx(off["short_gross"] * 0.5)
    assert on["long_gross"] == pytest.approx(off["long_gross"])


def test_per_name_caps_hold():
    p = make_panel(CAL, trending_universe(n_stocks=40), benchmark="BMK")
    t = month_end(120)
    sig = compute_signals(p, t, ls_cfg(max_long_weight=0.02, max_short_weight=0.01, max_names_per_side=20,
                                       min_names_per_side=10))
    w = sig.table.set_index("symbol")["target_weight"]
    assert w.max() <= 0.02 + 1e-12 and w.min() >= -0.01 - 1e-12
    assert sig.diagnostics["short_gross"] <= 10 * 0.01 + 1e-9  # capacity: names x cap


# ---------------------------------------------------------------- engine + ledger
def test_backtest_stop_loss_covers_next_open_and_paper_matches(tmp_path):
    uni = trending_universe(seed=7)
    probe = make_panel(CAL, uni, benchmark="BMK")
    may = compute_signals(probe, CAL.index("2020-05-29"), ls_cfg()).table
    victim = may[may["side"] == "short"]["symbol"].iloc[0]
    # a shorted loser squeezes +80% after the first rebalance
    k = CAL.index("2020-06-10")
    uni[victim]["close"][k:] *= 1.8
    for d in uni.values():
        d["open"] = np.round(d["close"] * 0.999, 4)
        d["close"] = np.round(d["close"], 4)
    prov = FixtureProvider(CAL, uni, benchmark="BMK")
    db = Database(tmp_path / "ls.db")
    run_import(db, prov, CAL)
    panel = get_panel(db, prov.info.key, CAL, "BMK")
    cfg = ls_cfg(slippage_bps=10, borrow_fee_annual=0.005, short_stop_loss=0.5)
    res = run_backtest(panel, CAL, cfg.model_copy(update={"start_date": "2020-05-29"}))
    stop = next(e for e in res.stops if e.symbol == victim)
    assert stop.trigger_session == "2020-06-10" and stop.fill_session == "2020-06-11"
    assert stop.fills[0].effect == "close_short" and stop.fills[0].ref_price == pytest.approx(uni[victim]["open"][k + 1])
    assert any(e.kind == "borrow_fee" for _, e in res.cash_events)
    jun = next(r for r in res.rebalances if r.signal_session == "2020-06-30")
    assert jun.signals.table.set_index("symbol").loc[victim, "side"] != "short"   # not re-shorted after the stop

    led = PaperLedger(db, CAL, prov.info.key, lambda: get_panel(db, prov.info.key, CAL, "BMK"),
                      lambda: data_version(db, prov.info.key), is_demo=True)
    led.initialize(cfg, inception_session="2020-05-29")
    led.advance(auto_apply=True)
    paper = {r["session"]: r["nav"] for r in db.query("SELECT session, nav FROM paper_nav ORDER BY session")}
    assert list(paper) == list(res.nav.index)
    assert list(paper.values()) == list(res.nav["nav"])
    orders = db.query("SELECT * FROM paper_orders WHERE origin='stop_loss'")
    assert orders and orders[0]["symbol"] == victim and orders[0]["status"] == "filled"
    assert db.scalar("SELECT COUNT(*) FROM cash_transactions WHERE kind='borrow_fee'") > 0
