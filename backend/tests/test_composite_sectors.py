"""Composite signal components, SEC SIC sector mapping/classification, sector neutrality, HTB screen, config warning."""

import json
import warnings

import httpx
import numpy as np
import pandas as pd
import pytest

from app.data.panel import build_panel
from app.data.sectors import SecClient, classify_instruments, sic_to_sector
from app.data.store import run_import
from app.db import Database
from app.strategy.composite import fip_score, residual_momentum, winsor_z
from app.strategy.config import ConfigWarning, StrategyConfig
from app.strategy.long_short import sector_neutralize
from app.strategy.signals import compute_signals

from .helpers import make_panel, rich_fixture, weekday_calendar


# ---------------------------------------------------------------- components
def test_winsor_z_clips_outliers_and_standardizes():
    rng = np.random.default_rng(0)
    x = np.r_[rng.normal(0, 1, 200), 50.0, np.nan]
    z = winsor_z(x, 3.0)
    ok = np.isfinite(z)
    assert np.isnan(z[-1]) and ok.sum() == 201
    assert z[ok].mean() == pytest.approx(0, abs=1e-12) and z[ok].std(ddof=1) == pytest.approx(1)
    finite = x[np.isfinite(x)]
    mu, sd = finite.mean(), finite.std(ddof=1)
    clipped = np.clip(finite, mu - 3 * sd, mu + 3 * sd)          # single pass: sd includes the outlier
    assert z[200] == pytest.approx((mu + 3 * sd - clipped.mean()) / clipped.std(ddof=1))
    assert z[200] < (50 - finite.mean()) / finite.std(ddof=1) * 10  # clipped, not the raw outlier
    assert np.argsort(z[:200]).tolist() == np.argsort(x[:200]).tolist()


def test_fip_is_share_of_up_days_minus_down_days():
    cal = weekday_calendar("2020-01-01", 300)
    t = 280
    lo, hi = t - 252, t - 21                               # 231 daily returns in the formation window
    r = np.full(300, 0.001)
    r[lo + 1: lo + 1 + 60] = -0.002                        # 60 down days, 171 up days inside the window
    close = 100 * np.cumprod(1 + r)
    p = make_panel(cal, {"A": {"close": close}})
    (score,) = fip_score(p, t, [p.sym_index["A"]], 21, 252)
    assert score == pytest.approx((171 - 60) / 231)       # = sgn(PRET) * (-ID)


def test_residual_momentum_rewards_idiosyncratic_drift_not_beta():
    cal, prov, series, sectors = rich_fixture(n_stocks=4, seed=3)
    rng = np.random.default_rng(9)
    mkt = np.diff(np.log(series["SPY"]["close"]), prepend=np.log(series["SPY"]["close"][0]))
    xlk = np.diff(np.log(series["XLK"]["close"]), prepend=np.log(series["XLK"]["close"][0]))
    T = len(mkt)
    beta_only = 100 * np.exp(np.cumsum(1.8 * mkt + 0.5 * (xlk - mkt) + rng.normal(0, 0.004, T)))
    t_idx = [cal.index(x) for x in cal.month_end_sessions("2022-01-01", "2022-12-31")][-1]
    drift = np.zeros(T)
    drift[t_idx - 252: t_idx - 21] = 0.0015                  # idiosyncratic trend only in the formation window
    alpha = 100 * np.exp(np.cumsum(0.9 * mkt + drift + rng.normal(0, 0.004, T)))
    s2 = {"SPY": series["SPY"], "XLK": series["XLK"], "BETA": {"close": beta_only}, "ALPHA": {"close": alpha}}
    inst = pd.DataFrame([{"symbol": k, "asset_type": "etf" if k in ("SPY", "XLK") else "common_stock",
                          "is_benchmark": int(k == "SPY"), "sector": "Information Technology", "delist_date": None,
                          "name": k} for k in s2])
    from .helpers import bars_frame
    p = build_panel(bars_frame(cal, s2), pd.DataFrame(columns=["symbol", "ex_date", "action_type", "ratio", "amount"]),
                    inst, cal, "SPY")
    t = p.sess_index[cal.month_end_sessions("2022-01-01", "2022-12-31")[-1]]
    res = residual_momentum(p, t, [p.sym_index["BETA"], p.sym_index["ALPHA"]], ["Information Technology"] * 2, 36,
                            p.sym_index["SPY"])
    assert res[1] > 3 and abs(res[0]) < res[1] / 2       # recent idiosyncratic trend scores high; beta alone does not


def test_composite_ranks_and_sector_demeaning():
    cal, prov, series, sectors = rich_fixture()
    inst = pd.DataFrame([i.to_dict() for i in prov.list_instruments()])
    p = build_panel(prov.fetch_bars(list(series), "2000", "2100"), prov.fetch_corporate_actions(list(series), "2000", "2100"),
                    inst, cal, "SPY")
    t = p.sess_index[cal.month_end_sessions("2021-06-01", "2022-06-30")[-1]]
    sig = compute_signals(p, t, StrategyConfig(min_adv_usd=1e6, min_names_per_side=5, max_names_per_side=10))
    tab = sig.table[sig.table["eligible"]]
    assert tab["composite"].notna().all() and tab["resid_mom"].notna().all()
    # sector-demeaned momentum sums to zero within each sector
    assert tab.groupby("sector")["sector_mom"].sum().abs().max() < 1e-9
    # ranking follows the composite, not plain 12-1
    assert (tab.sort_values("rank")["composite"].diff().dropna() <= 1e-12).all()
    plain = compute_signals(p, t, StrategyConfig(signal="momentum_12_1", min_adv_usd=1e6, min_names_per_side=5,
                                                 max_names_per_side=10)).table
    assert (plain[plain["eligible"]].sort_values("rank")["momentum"].diff().dropna() <= 1e-12).all()


# ---------------------------------------------------------------- HTB screen + sector neutrality
def test_htb_screen_excludes_least_liquid_from_shorts_only():
    cal, prov, series, sectors = rich_fixture()
    inst = pd.DataFrame([i.to_dict() for i in prov.list_instruments()])
    p = build_panel(prov.fetch_bars(list(series), "2000", "2100"), prov.fetch_corporate_actions(list(series), "2000", "2100"),
                    inst, cal, "SPY")
    t = p.sess_index[cal.month_end_sessions("2021-06-01", "2022-06-30")[-1]]
    sig = compute_signals(p, t, StrategyConfig(min_adv_usd=1e6, min_names_per_side=5, max_names_per_side=10,
                                               sector_neutral=False))
    tab = sig.table[sig.table["eligible"]]
    thr = sig.diagnostics["htb_adv_threshold"]
    assert thr == pytest.approx(np.quantile(tab["adv60"], 0.20))
    assert (tab[tab["side"] == "short"]["adv60"] >= thr).all()
    assert sig.diagnostics["htb_excluded"] > 0


def test_sector_neutralize_holds_limits_gross_and_beta():
    rng = np.random.default_rng(1)
    nL = nS = 30
    secs = ["A", "B", "C"]
    secL = [secs[0]] * 15 + [secs[1]] * 10 + [secs[2]] * 5          # longs concentrated in A
    secS = [secs[2]] * 15 + [secs[1]] * 10 + [secs[0]] * 5          # shorts concentrated in C
    wL, wS = np.full(nL, 0.6 / nL), np.full(nS, 0.6 / nS)
    bL, bS = rng.uniform(0.8, 1.2, nL), rng.uniform(0.8, 1.2, nS)
    wS = wS * (wL @ bL) / (wS @ bS)                                  # beta-neutral start
    xL, xS, info = sector_neutralize(wL, wS, bL, bS, secL, secS, 0.05, 0.05, 0.02)
    assert info["applied"] and info["max_abs_net"] <= 0.02 + 1e-7
    assert xL.sum() == pytest.approx(wL.sum()) and xS.sum() == pytest.approx(wS.sum())
    assert xL @ bL - xS @ bS == pytest.approx(0, abs=1e-8)
    assert xL.max() <= 0.05 + 1e-9 and xS.max() <= 0.05 + 1e-9


def test_sector_neutralize_reduces_gross_when_caps_pin_shorts():
    wL = np.array([0.02, 0.02, 0.02])
    wS = np.array([0.015, 0.015, 0.015, 0.015])                      # all shorts pinned at the 1.5% cap
    xL, xS, info = sector_neutralize(wL, wS, np.ones(3), np.ones(4) * 1.5, ["A", "A", "B"], ["C", "C", "B", "B"],
                                     0.02, 0.015, 0.02)
    assert info["applied"] and info["gross_reduced"] and info["max_abs_net"] <= 0.02 + 1e-7


# ---------------------------------------------------------------- config warning
def test_config_warns_when_short_caps_cannot_reach_gross():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cfg = StrategyConfig(min_names_per_side=40, max_short_weight=0.015, max_side_gross=0.75)
    assert any(issubclass(x.category, ConfigWarning) for x in w)
    assert "capacity is 60.0%" in cfg.config_warnings()[0]
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        StrategyConfig()                                             # defaults: 50 x 1.5% = 75% -> no warning
    assert not [x for x in w if issubclass(x.category, ConfigWarning)]


def test_legacy_long_only_keys_are_ignored_when_loading_old_configs():
    cfg = StrategyConfig.model_validate_json('{"mode": "long_only", "top_n": 50, "min_price": 7}')
    assert cfg.min_price == 7 and not hasattr(cfg, "top_n")


# ---------------------------------------------------------------- SEC SIC sectors
@pytest.mark.parametrize("sic,sector", [
    (3674, "Information Technology"), (7372, "Information Technology"), (2834, "Health Care"), (6022, "Financials"),
    (6798, "Real Estate"), (4911, "Utilities"), (1311, "Energy"), (2911, "Energy"), (5331, "Consumer Staples"),
    (3711, "Consumer Discretionary"), (4813, "Communication Services"), (3721, "Industrials"), (2821, "Materials"),
    (9995, None), ("", None),
])
def test_sic_mapping(sic, sector):
    assert sic_to_sector(sic) == sector


def test_sec_client_classifies_instruments(tmp_path):
    cal, prov, series, sectors = rich_fixture(n_stocks=3)
    prov._inst = [i.__class__(**{**i.to_dict(), "sector": None, "sector_source": None}) for i in prov._inst]
    db = Database(tmp_path / "sec.db")
    run_import(db, prov, cal)
    seen = {}

    def handler(req: httpx.Request):
        seen["ua"] = req.headers["User-Agent"]
        if req.url.path.endswith("company_tickers.json"):
            return httpx.Response(200, json={"0": {"cik_str": 1, "ticker": "S000"}, "1": {"cik_str": 2, "ticker": "S001"}})
        cik = int(req.url.path.split("CIK")[1].split(".")[0])
        return httpx.Response(200, json={"sic": {1: "3674", 2: "2834"}[cik], "sicDescription": "desc"})

    client = SecClient("JMW-Systematic research test@example.com", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    res = classify_instruments(db, prov.info.key, client)
    assert res["classified"] == 2 and res["missing"] == ["S002"]
    got = {r["symbol"]: r["sector"] for r in db.query("SELECT symbol, sector FROM instruments WHERE asset_type='common_stock'")}
    assert got == {"S000": "Information Technology", "S001": "Health Care", "S002": None}
    meta = json.loads(db.scalar("SELECT metadata_json FROM instruments WHERE symbol='S000'"))
    assert meta["sic"] == "3674" and seen["ua"].startswith("JMW-Systematic")
    # a later provider refresh must not wipe the SEC sector
    run_import(db, prov, cal)
    assert db.scalar("SELECT sector FROM instruments WHERE symbol='S000'") == "Information Technology"


def test_sec_client_requires_contact_user_agent():
    with pytest.raises(ValueError):
        SecClient("no-email-here")
