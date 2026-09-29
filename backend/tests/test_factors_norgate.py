"""Ken French parsing + Newey-West alpha test; NorgateProvider mapping with a simulated norgatedata module."""

import types

import numpy as np
import pandas as pd
import pytest

from app.backtest.factors import FACTORS, alpha_test, newey_west_ols, parse_french_csv
from app.data.norgate_provider import NorgateProvider
from app.data.store import get_panel, run_import
from app.db import Database
from app.strategy.config import StrategyConfig
from app.strategy.signals import compute_signals

from .helpers import weekday_calendar

FF_SAMPLE = """This file was created by CMPT_ME_BEME_RETS using the 202608 CRSP database.
The 1-month TBill return is from Ibbotson and Associates, Inc.

,Mkt-RF,SMB,HML,RMW,CMA,RF
202601,   1.00,   0.50,  -0.20,   0.10,   0.00,   0.30
202602,  -2.00,   0.10,   0.40,  -0.30,   0.20,   0.30

 Annual Factors: January-December
,Mkt-RF,SMB,HML,RMW,CMA,RF
 2025,  10.00,  1.00, 2.00, 3.00, 4.00, 5.00
"""


def test_parse_french_monthly_block_only():
    df = parse_french_csv(FF_SAMPLE)
    assert list(df.index) == ["202601", "202602"]
    assert df.loc["202602", "Mkt-RF"] == pytest.approx(-0.02) and df.loc["202601", "RF"] == pytest.approx(0.003)


def test_newey_west_recovers_alpha_and_matches_ols_point_estimates():
    rng = np.random.default_rng(0)
    T = 240
    X = np.column_stack([np.ones(T), rng.normal(0, 0.04, (T, 2))])
    y = X @ np.array([0.004, 0.8, -0.3]) + rng.normal(0, 0.01, T)
    r = newey_west_ols(y, X)
    ols = np.linalg.lstsq(X, y, rcond=None)[0]
    assert np.allclose(r["beta"], ols)
    assert r["lags"] == int(np.floor(4 * (T / 100) ** (2 / 9)))
    assert r["beta"][0] == pytest.approx(0.004, abs=0.002) and r["t"][0] > 3
    r0 = newey_west_ols(y, X, lags=0)                     # lags=0 reduces to White's heteroskedasticity-robust SE
    e = y - X @ ols
    white = np.sqrt(np.diag(np.linalg.inv(X.T @ X) @ (X.T * e ** 2) @ X @ np.linalg.inv(X.T @ X)))
    assert np.allclose(r0["se"], white)


def test_alpha_test_full_sample_and_halves():
    rng = np.random.default_rng(1)
    idx = [f"{y}{m:02d}" for y in range(2016, 2026) for m in range(1, 13)]
    fac = pd.DataFrame(rng.normal(0, 0.03, (len(idx), 6)), index=idx, columns=FACTORS)
    fac["RF"] = 0.002
    ret = 0.005 + fac["RF"] + 0.2 * fac["Mkt-RF"] + 0.6 * fac["Mom"] + rng.normal(0, 0.005, len(idx))
    monthly = [{"year": int(i[:4]), "month": int(i[4:]), "return": float(v)} for i, v in ret.items()]
    res = alpha_test(monthly, fac)
    full = res["full"]
    assert full["months"] == len(idx) - 1                     # first month dropped
    assert full["alpha_monthly"] == pytest.approx(0.005, abs=0.0015) and full["t_alpha"] > 5
    assert full["betas"]["Mom"] == pytest.approx(0.6, abs=0.05)
    assert res["first_half"]["end"] < res["second_half"]["start"]
    assert res["first_half"]["months"] + res["second_half"]["months"] == full["months"]


# ---------------------------------------------------------------- Norgate (simulated package)
def fake_norgate(cal):
    sess = pd.to_datetime(cal.sessions[:400])
    n = len(sess)
    raw = np.r_[np.full(200, 100.0), np.full(n - 200, 50.0)] * (1 + np.arange(n) * 0.001)  # 2:1 split at day 200
    factor = np.r_[np.full(200, 0.5), np.ones(n - 200)]                                      # capital adjustment
    div = np.zeros(n)
    div[300] = 0.25

    def frame(close, with_div):
        d = {"Open": close, "High": close, "Low": close, "Close": close, "Volume": np.full(n, 1e6)}
        if with_div:
            d["Dividend"] = div
        return pd.DataFrame(d, index=sess)

    old_end = 250                                               # OLD delisted after session 250

    def price_timeseries(sym, stock_price_adjustment_setting, padding_setting, start_date, end_date, timeseriesformat):
        if sym == "OLD-202003":
            return frame(np.full(n, 20.0), True).iloc[:old_end + 1]
        if sym in ("SPY", "XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC"):
            return frame(300 * (1 + np.arange(n) * 0.0005), True)
        if stock_price_adjustment_setting == "CAPITAL":
            return frame(raw * factor, False)
        return frame(raw, True)

    def index_constituent_timeseries(sym, indexname, padding_setting, start_date, timeseriesformat):
        flag = np.zeros(n, dtype=int)
        if sym == "AAA":
            flag[100:] = 1                                       # joins the index at session 100
        if sym == "OLD-202003":
            flag[:old_end + 1] = 1
        return pd.DataFrame({"Index Constituent": flag}, index=sess)

    return types.SimpleNamespace(
        StockPriceAdjustmentType=types.SimpleNamespace(NONE="NONE", CAPITAL="CAPITAL", TOTALRETURN="TOTALRETURN"),
        PaddingType=types.SimpleNamespace(NONE="NONE", ALLMARKETDAYS="ALLMARKETDAYS"),
        watchlist_symbols=lambda name: ["AAA", "OLD-202003"] if name == "Russell 1000 Current & Past" else [],
        price_timeseries=price_timeseries,
        index_constituent_timeseries=index_constituent_timeseries,
        security_name=lambda s: f"{s} Inc",
        last_quoted_date=lambda s: sess[old_end] if s == "OLD-202003" else sess[-1] + pd.Timedelta(days=4000),
    )


def test_norgate_mapping_splits_dividends_membership_and_delistings(tmp_path):
    cal = weekday_calendar("2019-01-01", 420)
    prov = NorgateProvider(nd=fake_norgate(cal), history_start="2019-01-01")
    inst = {i.symbol: i for i in prov.list_instruments()}
    assert inst["OLD-202003"].delist_date == cal.sessions[250] and not inst["OLD-202003"].active
    assert inst["SPY"].is_benchmark and inst["AAA"].asset_type == "common_stock"
    acts = prov.fetch_corporate_actions(["AAA"], "2019-01-01", cal.sessions[399])
    split = acts[acts["action_type"] == "split"]
    assert list(split["ex_date"]) == [cal.sessions[200]] and split["ratio"].iloc[0] == pytest.approx(2.0)
    assert acts[acts["action_type"] == "cash_dividend"]["amount"].tolist() == [0.25]
    mem = prov.index_membership(["AAA", "OLD-202003"]).set_index("symbol")
    assert mem.loc["AAA", "start"] == cal.sessions[100] and pd.isna(mem.loc["AAA", "end"])
    assert mem.loc["OLD-202003", "end"] == cal.sessions[250]

    db = Database(tmp_path / "ng.db")
    run_import(db, prov, cal, start="2019-01-01", end=cal.sessions[399])
    panel = get_panel(db, "norgate", cal, "SPY")
    cfg = StrategyConfig(signal="momentum_12_1", min_adv_usd=0, min_names_per_side=1, max_names_per_side=1,
                         lookback_sessions=20, skip_sessions=2, min_history_sessions=20, htb_exclude_pct=0,
                         sector_neutral=False, short_min_price=0)
    early = compute_signals(panel, 60, cfg).table.set_index("symbol")
    assert early.loc["AAA", "reason"] == "not_in_index"      # before joining the index: point-in-time excluded
    later = compute_signals(panel, 150, cfg).table.set_index("symbol")
    assert later.loc["AAA", "reason"] != "not_in_index"
    assert "OLD-202003" not in set(compute_signals(panel, 300, cfg).table["symbol"])  # delisted: gone after its last day


def test_norgate_without_package_explains():
    import importlib.util

    if importlib.util.find_spec("norgatedata") is not None:
        pytest.skip("norgatedata installed")
    from app.data.provider import ProviderError

    with pytest.raises(ProviderError, match="Norgate"):
        NorgateProvider()
