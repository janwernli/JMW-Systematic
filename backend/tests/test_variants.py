"""Strategy variants: SPY core + overlay, fixed (130/30) sizing, optional beta neutrality, margin loans and
margin checks, the unified excess-return basis, the comparison report and API."""

import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.backtest import engine as engine_mod
from app.backtest import factors as factors_mod
from app.backtest.compare import rf_exposure, run_report
from app.backtest.engine import margin_requirement, max_debit_fraction, run_backtest
from app.backtest.factors import FACTORS, alpha_test
from app.backtest.variants import VARIANT_BY_KEY, VARIANTS
from app.config import Settings
from app.data.store import run_import
from app.services import AppContext
from app.strategy.config import StrategyConfig
from app.strategy.execution import CostModel, debit_interest, execute_rebalance
from app.strategy.signals import compute_signals

from .helpers import make_panel, rich_fixture, weekday_calendar
from .test_automation import FakeBroker

BASE = dict(min_adv_usd=1e6, min_names_per_side=10, max_names_per_side=30, long_pct=0.2, short_pct=0.2,
            buffer_exit_pct=0.3, htb_exclude_pct=0.0)


@pytest.fixture(scope="module")
def fixture_panel():
    from app.data.panel import build_panel

    cal, prov, series, sectors = rich_fixture()
    syms = list(series)
    panel = build_panel(prov.fetch_bars(syms, "2000", "2100"), prov.fetch_corporate_actions(syms, "2000", "2100"),
                        pd.DataFrame([i.__dict__ for i in prov.list_instruments()]), cal, "SPY")
    return cal, panel


def signals(panel, cfg):
    return compute_signals(panel, len(panel.sessions) - 6, cfg)


# ---------------------------------------------------------------- defaults stay the paper default
def test_defaults_unchanged_and_neutral_10_is_the_default():
    cfg = StrategyConfig()
    assert (cfg.core_beta, cfg.beta_neutral, cfg.sizing) == (0.0, True, "vol_target")
    assert VARIANTS[0].key == "neutral_10" and VARIANTS[0].overrides == {}
    assert VARIANTS[0].config() == StrategyConfig()
    assert [v.name for v in VARIANTS] == ["Neutral 10%", "Neutral 15%", "SPY + overlay", "130/30"]


def test_variant_definitions_match_the_spec():
    n15 = VARIANT_BY_KEY["neutral_15"].config()
    assert (n15.target_vol, n15.max_side_gross, n15.max_total_gross) == (0.15, 1.0, 2.0)
    spy = VARIANT_BY_KEY["spy_overlay"].config()
    assert (spy.core_beta, spy.sizing, spy.fixed_long_gross, spy.max_side_gross, spy.max_total_gross) ==         (1.0, "fixed", 0.30, 0.40, 1.6)
    assert spy.beta_neutral and spy.sector_neutral and spy.crash_guard and spy.signal == "composite"
    assert (spy.short_stop_loss, spy.short_min_price, spy.htb_exclude_pct) == (0.5, 10.0, 0.20)
    assert any("could exceed 160%" in w for w in spy.config_warnings())
    ext = VARIANT_BY_KEY["ext_130_30"].config()
    assert (ext.sizing, ext.fixed_long_gross, ext.fixed_short_gross, ext.beta_neutral, ext.max_total_gross) == \
        ("fixed", 1.3, 0.3, False, 1.6)
    assert not ext.sector_neutral and ext.max_long_weight * ext.min_names_per_side == pytest.approx(1.3)
    assert ext.config_warnings() == []
    with pytest.raises(ValueError):
        StrategyConfig(core_beta=1.0, max_total_gross=1.0)


# ---------------------------------------------------------------- book construction
def test_spy_core_is_exempt_from_neutrality_and_counts_toward_gross(fixture_panel):
    cal, panel = fixture_panel
    cfg = StrategyConfig(**BASE, core_beta=1.0, target_vol=0.08, max_total_gross=2.0)
    sig = signals(panel, cfg)
    tw = sig.target_weights()
    d = sig.diagnostics
    assert tw["SPY"] == 1.0 and sig.selected.iloc[0]["symbol"] == "SPY"     # the core is bought first
    assert d["core"] == {"symbol": "SPY", "weight": 1.0, "requested": 1.0}
    assert d["overlay_gross_cap"] == pytest.approx(1.0)
    assert d["long_gross"] + d["short_gross"] <= 1.0 + 1e-9                    # overlay within 2.0 - core
    assert d["total_gross"] == pytest.approx(1.0 + d["long_gross"] + d["short_gross"])
    assert abs(d["ex_ante_net_beta"]) < 1e-9                                 # the OVERLAY is beta-neutral
    assert "SPY" not in {s for s, w in tw.items() if w < 0}
    assert sum(d["sector_neutrality"]["final_sector_net"].values()) == pytest.approx(d["net_exposure"])  # no SPY
    plain = signals(panel, StrategyConfig(**BASE, target_vol=0.08, max_total_gross=1.0))
    ov = {s: w for s, w in tw.items() if s != "SPY"}
    assert ov == pytest.approx(plain.target_weights())                        # same overlay as without the core


def test_fixed_sizing_130_30_style_and_gross_cap_scaling(fixture_panel):
    cal, panel = fixture_panel
    cfg = StrategyConfig(**BASE, sizing="fixed", fixed_long_gross=0.45, fixed_short_gross=0.15, beta_neutral=False,
                         sector_neutral=False, max_total_gross=1.0, max_long_weight=0.05, crash_guard=False)
    d = signals(panel, cfg).diagnostics
    assert d["long_gross"] == pytest.approx(0.45) and d["short_gross"] == pytest.approx(0.15)
    assert d["beta_ratio"] == 1.0 and d["sizing"] == "fixed"
    capped = StrategyConfig(**{**cfg.model_dump(), "max_total_gross": 0.4})
    d2 = signals(panel, capped).diagnostics
    assert d2["long_gross"] + d2["short_gross"] == pytest.approx(0.4)
    assert d2["long_gross"] / d2["short_gross"] == pytest.approx(3.0)
    assert any("scaled" in b for b in d2["binding"])


def test_beta_neutral_off_gives_equal_dollar_sides(fixture_panel):
    cal, panel = fixture_panel
    d = signals(panel, StrategyConfig(**BASE, beta_neutral=False, sector_neutral=False, crash_guard=False)).diagnostics
    assert d["beta_ratio"] == 1.0 and d["long_gross"] == pytest.approx(d["short_gross"])


# ---------------------------------------------------------------- margin loans
def test_margin_loan_only_when_the_book_needs_it():
    assert max_debit_fraction({"A": 0.6, "B": -0.6}) == 0.0                  # neutral: fully self-financed
    assert max_debit_fraction({"SPY": 1.0, "A": 0.5, "B": -0.45}) == pytest.approx(0.06)
    assert max_debit_fraction({"A": 1.3, "B": -0.3}) == pytest.approx(0.01)   # 130/30: only a rounding buffer
    prices = {"SPY": 400.0, "A": 100.0, "B": 50.0}
    targets = {"SPY": 1.0, "A": 0.5, "B": -0.45}                             # SPY core + a net-long overlay
    order = ["SPY", "A", "B"]
    no_loan = execute_rebalance({}, 100_000.0, targets, order, prices, {}, CostModel(10, 0, 0))
    loan = execute_rebalance({}, 100_000.0, targets, order, prices, {}, CostModel(10, 0, 0),
                             max_debit_frac=max_debit_fraction(targets))
    assert [u["symbol"] for u in no_loan.unfilled] == ["A"] and no_loan.cash_after >= 0   # longs cut short
    assert not loan.unfilled and loan.cash_after == pytest.approx(100_000 + 900 * 49.95 - 249 * 400.4 - 499 * 100.1)
    assert loan.shares_after["A"] == 499 and loan.shares_after["SPY"] == 249


def test_debit_interest_act_360():
    cal = weekday_calendar("2024-01-01", 10)
    p = make_panel(cal, {"A": {"close": np.full(10, 10.0)}})
    t = p.sessions.index("2024-01-08")                                        # Monday: 3 calendar days
    assert debit_interest(p, t, -36_000.0, 0.05) == pytest.approx(36_000 * 0.05 / 360 * 3)
    assert debit_interest(p, t, 1_000.0, 0.05) == 0.0


def test_leveraged_backtest_charges_margin_interest_and_neutral_does_not(fixture_panel):
    cal, panel = fixture_panel

    class Flat:
        carried: set = set()

        def annual(self, s):
            return 0.02

    lev = run_backtest(panel, cal, StrategyConfig(**BASE, core_beta=1.0, target_vol=0.08, max_total_gross=2.0), rf=Flat())
    neu = run_backtest(panel, cal, StrategyConfig(**BASE), rf=Flat())
    kinds = lambda r: {e.kind for _, e in r.cash_events}   # noqa: E731
    if (lev.nav["cash"] < 0).any():
        assert "margin_interest" in kinds(lev)
    assert "margin_interest" not in kinds(neu) and (neu.nav["cash"] >= 0).all()
    assert not any(u["reason"] == "insufficient_cash" for r in lev.rebalances for u in r.unfilled)


# ---------------------------------------------------------------- margin checks
def test_margin_requirement_rules():
    cal = weekday_calendar("2024-01-01", 3)
    p = make_panel(cal, {"CHEAP": {"close": np.full(3, 3.0)}, "DEAR": {"close": np.full(3, 100.0)},
                         "LONG": {"close": np.full(3, 50.0)}})
    short_req, long_req = margin_requirement(p, 2, {"CHEAP": -1000, "DEAR": -100, "LONG": 200})
    assert short_req == pytest.approx(1000 * 5.0 + 0.30 * 100 * 100)          # $5/share floor vs 30%
    assert long_req == pytest.approx(0.25 * 200 * 50)


def test_engine_flags_gross_and_maintenance_breaches(fixture_panel, monkeypatch):
    cal, panel = fixture_panel
    cfg = StrategyConfig(**BASE, start_date=panel.sessions[400])
    clean = run_backtest(panel, cal, cfg)
    assert clean.margin_flags == [] and (clean.nav["gross_exposure"] <= 2.0).all()
    monkeypatch.setitem(engine_mod.MARGIN, "max_gross", 0.05)                # absurd limits: every invested
    monkeypatch.setitem(engine_mod.MARGIN, "short_maint_pct", 20.0)          # day must breach both
    res = run_backtest(panel, cal, cfg)
    invested = res.nav[res.nav["positions"] > 0]
    flagged = {f["session"]: f for f in res.margin_flags}
    assert set(invested.index) <= set(flagged)
    f = flagged[invested.index[-1]]
    assert f["breaches"] == ["gross", "maintenance"] and f["gross_exposure"] > 0.05
    assert f["short_requirement"] + f["long_requirement"] > f["equity"]


# ---------------------------------------------------------------- excess-return basis
def test_rf_exposure_basis():
    nav = pd.DataFrame({"nav": [100.0, 100.0, 100.0], "cash": [100.0, 5.0, -20.0]}, index=["a", "b", "c"])
    assert rf_exposure(nav, False).tolist() == pytest.approx([0.0, 0.95, 1.0])  # idle cash is not charged RF
    assert rf_exposure(nav, True).tolist() == [1.0, 1.0, 1.0]


def test_alpha_rf_on_net_exposure_matches_the_two_extremes():
    rng = np.random.default_rng(3)
    idx = [f"{y}{m:02d}" for y in range(2016, 2026) for m in range(1, 13)]
    fac = pd.DataFrame(rng.normal(0, 0.03, (len(idx), 6)), index=idx, columns=FACTORS)
    fac["RF"] = 0.003
    ret = 0.002 + 0.9 * fac["Mkt-RF"] + rng.normal(0, 0.004, len(idx))
    monthly = [{"year": int(i[:4]), "month": int(i[4:]), "return": float(v)} for i, v in ret.items()]
    raw = alpha_test(monthly, fac)
    exc = alpha_test(monthly, fac, excess=True)
    full = alpha_test(monthly, fac, net_exposure={i: 1.0 for i in idx})
    none = alpha_test(monthly, fac, net_exposure={i: 0.0 for i in idx})
    assert full["full"]["alpha_monthly"] == pytest.approx(exc["full"]["alpha_monthly"])
    assert none["full"]["alpha_monthly"] == pytest.approx(raw["full"]["alpha_monthly"])
    assert full["rf_on_net_exposure"] and "idle cash" in full["model"]


# ---------------------------------------------------------------- report
def test_run_report_statistics():
    sessions = [d.date().isoformat() for d in pd.bdate_range("2020-01-01", periods=600)]
    rng = np.random.default_rng(5)
    r = 0.0015 + 0.001 * np.sin(np.arange(600))                              # always positive ...
    r[100:120] = -0.02                                                       # ... except one clear drawdown
    nav = 100_000 * np.cumprod(1 + r)
    df = pd.DataFrame({"nav": nav, "cash": nav, "benchmark_nav": nav, "long_value": 0.0, "short_value": 0.0},
                      index=sessions)
    idx = sorted({s[:4] + s[5:7] for s in sessions})
    fac = pd.DataFrame(rng.normal(0, 0.03, (len(idx), 6)), index=idx, columns=FACTORS)
    fac["RF"] = 0.001
    rep = run_report(df, {"total_costs": 10.0, "borrow_fees": 5.0, "margin_interest": 1.0}, False, fac, 100_000)
    rets = pd.Series(nav).pct_change().dropna()
    assert rep["sharpe"] == pytest.approx(rets.mean() * 252 / (rets.std(ddof=1) * np.sqrt(252)))  # all cash: no RF
    assert rep["max_dd_trough"] == sessions[119] and rep["max_dd_peak"] <= sessions[100]
    assert rep["max_dd_recovery"] is None or rep["max_dd_recovery"] > sessions[119]
    assert rep["total_costs"] == 16.0 and rep["worst_month"]["value"] < 0
    assert len(rep["halves"]) == 2 and rep["halves"][0]["end"] == rep["halves"][1]["start"]
    assert rep["sortino"] > 0 and rep["alpha"] is not None


# ---------------------------------------------------------------- API
def test_variant_study_api_runs_all_four_on_the_same_period(tmp_path, monkeypatch):
    idx = [f"{y}{m:02d}" for y in range(2018, 2025) for m in range(1, 13)]
    rng = np.random.default_rng(0)
    fac = pd.DataFrame(rng.normal(0, 0.03, (len(idx), 6)), index=idx, columns=FACTORS)
    fac["RF"] = 0.001
    monkeypatch.setattr(factors_mod, "load_factors", lambda *a, **k: fac)
    cal, prov, series, sectors = rich_fixture()
    settings = Settings(_env_file=None, DATABASE_PATH=str(tmp_path / "v.db"), LOG_FORMAT="text", LOG_LEVEL="WARNING",
                        SEC_USER_AGENT="")
    ctx = AppContext(settings, calendar=cal, provider=prov, broker=FakeBroker())
    run_import(ctx.db, prov, cal)
    with TestClient(create_app(settings, ctx=ctx)) as c:
        ov = c.get("/api/research/variants").json()
        assert [v["key"] for v in ov["variants"]] == [v.key for v in VARIANTS] and ov["paper_default"] == "No model portfolio"
        study = c.post("/api/research/variants/studies", json={}).json()
        assert len(study["runs"]) == 4
        for _ in range(600):
            res = c.get(f"/api/research/variants/studies/{study['id']}").json()
            if res["study"]["complete"]:
                break
            time.sleep(0.2)
        assert all(r["status"] == "completed" for r in res["study"]["runs"]), res["study"]["runs"]
        cfgs = [c.get(f"/api/research/runs/{r['run_id']}").json()["config"] for r in res["study"]["runs"]]
        assert len({(x["start_date"], x["end_date"]) for x in cfgs}) == 1          # identical period
        reports = {r["key"]: r["report"] for r in res["results"]}
        for rep in reports.values():
            for k in ("cagr", "ann_vol", "sharpe", "sortino", "max_drawdown", "max_dd_peak", "worst_month", "beta",
                      "correlation", "alpha", "annualized_turnover", "total_costs", "halves", "margin"):
                assert k in rep
            assert len(rep["halves"]) == 2 and rep["margin"]["limits"]["max_gross"] == 2.0
        assert reports["spy_overlay"]["avg_net_exposure"] > 0.8 > abs(reports["neutral_10"]["avg_net_exposure"])
        pt = res["series"][-1]
        assert set(pt["navs"]) == set(reports) and pt["benchmark"] is not None and set(pt["drawdowns"]) == set(reports)
        assert c.post("/api/research/variants/studies", json={"start_date": "2100-01-01"}).status_code == 400
