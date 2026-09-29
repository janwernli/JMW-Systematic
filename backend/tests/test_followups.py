"""Cash interest + alpha consistency, Norgate GICS sectors, crash guard before sector neutrality, manual approval."""

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.automation import DailyCycle, set_setting
from app.backtest.engine import run_backtest
from app.backtest.factors import FACTORS, alpha_test
from app.data.norgate_provider import NorgateProvider, normalize_gics_sector
from app.data.panel import build_panel
from app.data.store import data_version, get_panel, run_import
from app.db import Database
from app.ledger.paper import PaperLedger
from app.strategy.config import StrategyConfig
from app.strategy.execution import cash_interest
from app.strategy.signals import compute_signals

from .helpers import FixtureProvider, make_panel, rich_fixture, weekday_calendar
from .test_automation import FakeBroker, make_ctx, targets
from .test_factors_norgate import fake_norgate


class FlatRate:
    def __init__(self, annual):
        self.a = annual

    def annual(self, session):
        return self.a


# ---------------------------------------------------------------- cash interest
def test_cash_interest_is_act_360_on_positive_cash_only():
    cal = weekday_calendar("2021-01-04", 10)                     # Monday
    p = make_panel(cal, {"A": {"close": np.full(10, 10.0)}})
    assert cash_interest(p, 1, 100_000, 0.036) == 10.0         # 100,000 x 3.6% / 360 x 1 day
    assert cash_interest(p, 5, 100_000, 0.036) == 30.0         # Friday -> Monday: 3 calendar days
    assert cash_interest(p, 1, -500, 0.036) == 0.0


def test_backtest_credits_interest_and_ledger_matches(tmp_path):
    cal = weekday_calendar("2020-01-01", 130)
    rng = np.random.default_rng(4)
    series = {s: {"close": np.round(30 * np.exp(rng.normal(0.0004 * (k - 2), 0.02, 130).cumsum()), 4)}
              for k, s in enumerate("ABCDE")}
    for d in series.values():
        d["open"] = np.round(d["close"] * 0.999, 4)
    cfg = StrategyConfig(signal="momentum_12_1", htb_exclude_pct=0, sector_neutral=False, crash_guard=False,
                         short_min_price=0, min_names_per_side=1, max_names_per_side=1, lookback_sessions=10,
                         skip_sessions=2, min_history_sessions=10, adv_window=3, min_adv_usd=0, min_price=1,
                         vol_lookback_sessions=20, beta_lookback_sessions=60, max_long_weight=1, max_short_weight=1,
                         cash_interest=True)
    prov = FixtureProvider(cal, series)
    db = Database(tmp_path / "i.db")
    run_import(db, prov, cal)
    panel = get_panel(db, prov.info.key, cal, None)
    with pytest.raises(ValueError, match="risk-free"):
        run_backtest(panel, cal, cfg)
    res = run_backtest(panel, cal, cfg.model_copy(update={"start_date": "2020-01-31"}), rf=FlatRate(0.05))
    interest = [e for _, e in res.cash_events if e.kind == "interest"]
    assert interest and all(e.amount > 0 for e in interest)
    led = PaperLedger(db, cal, prov.info.key, lambda: get_panel(db, prov.info.key, cal, None),
                      lambda: data_version(db, prov.info.key), rf_fn=lambda: FlatRate(0.05))
    led.initialize(cfg, inception_session="2020-01-31")
    led.advance(auto_apply=True)
    paper = [r["nav"] for r in db.query("SELECT nav FROM paper_nav ORDER BY session")]
    assert paper == list(res.nav["nav"])                         # model ledger == backtest, with interest
    assert db.scalar("SELECT COUNT(*) FROM cash_transactions WHERE kind='interest'") == len(interest)


def test_alpha_regression_matches_cash_treatment():
    rng = np.random.default_rng(2)
    idx = [f"{y}{m:02d}" for y in range(2016, 2026) for m in range(1, 13)]
    fac = pd.DataFrame(rng.normal(0, 0.03, (len(idx), 6)), index=idx, columns=FACTORS)
    fac["RF"] = 0.004
    ret = 0.003 + 0.5 * fac["Mom"] + rng.normal(0, 0.004, len(idx))
    monthly = [{"year": int(i[:4]), "month": int(i[4:]), "return": float(v)} for i, v in ret.items()]
    raw = alpha_test(monthly, fac, excess=False)
    exc = alpha_test(monthly, fac, excess=True)
    assert not raw["excess_returns"] and exc["excess_returns"]
    assert raw["full"]["alpha_monthly"] - exc["full"]["alpha_monthly"] == pytest.approx(0.004, abs=1e-9)
    assert "not subtracted" in raw["model"] and "- RF" in exc["model"]


# ---------------------------------------------------------------- Norgate GICS
def test_norgate_gics_sectors_including_delisted(tmp_path):
    cal = weekday_calendar("2019-01-01", 420)
    nd = fake_norgate(cal)
    gics = {"AAA": "Information Technology", "OLD-202003": "Telecommunication Services"}  # legacy name
    nd.classification_at_level = lambda sym, scheme, rtype, level: gics.get(sym) if (scheme, rtype, level) == ("GICS", "Name", 1) else None
    prov = NorgateProvider(nd=nd, history_start="2019-01-01")
    inst = {i.symbol: i for i in prov.list_instruments()}
    assert inst["OLD-202003"].delist_date is not None                           # delisted ...
    assert inst["OLD-202003"].sector == "Communication Services"                # ... and still gets a sector
    assert inst["AAA"].sector_source == "Norgate GICS level 1 (sector)"
    assert inst["SPY"].sector is None                                            # reference ETFs are unclassified
    db = Database(tmp_path / "g.db")
    run_import(db, prov, cal, start="2019-01-01", end=cal.sessions[399])
    stored = {r["symbol"]: r["sector"] for r in db.query("SELECT symbol, sector FROM instruments")}
    assert stored["OLD-202003"] == "Communication Services" and stored["AAA"] == "Information Technology"


def test_gics_name_normalization():
    assert normalize_gics_sector("Telecommunication Services") == "Communication Services"
    assert normalize_gics_sector("Health Care") == "Health Care"
    assert normalize_gics_sector("Not A Sector") is None and normalize_gics_sector(None) is None


def test_sec_edgar_is_not_used_for_norgate(tmp_path):
    from app.config import Settings
    from app.services import AppContext

    cal = weekday_calendar("2019-01-01", 420)
    prov = NorgateProvider(nd=fake_norgate(cal), history_start="2019-01-01")
    ctx = AppContext(Settings(_env_file=None, DATABASE_PATH=str(tmp_path / "n.db"), SEC_USER_AGENT="x y@z.com"),
                     calendar=cal, provider=prov)
    assert ctx.fill_sectors() is None


# ---------------------------------------------------------------- crash guard before sector neutrality
def test_crash_guard_applied_before_sector_neutralization():
    cal, prov, series, sectors = rich_fixture(seed=5)
    crash = -0.004 + 0.03 * np.where(np.arange(len(series["SPY"]["close"])) % 2 == 0, 1.0, -1.0)
    series["SPY"] = {"close": np.round(300 * np.exp(np.cumsum(crash)), 4), "volume": np.full(len(crash), 5e7)}
    inst = pd.DataFrame([i.to_dict() for i in prov.list_instruments()])
    from .helpers import bars_frame
    p = build_panel(bars_frame(cal, series), pd.DataFrame(columns=["symbol", "ex_date", "action_type", "ratio", "amount"]),
                    inst, cal, "SPY")
    t = p.sess_index[cal.month_end_sessions("2021-06-01", "2022-06-30")[-1]]
    cfg = StrategyConfig(min_adv_usd=1e6, min_names_per_side=5, max_names_per_side=10, crash_market_lookback_sessions=252)
    sig = compute_signals(p, t, cfg)
    d = sig.diagnostics
    assert d["crash_guard"]["active"]
    sn = d["sector_neutrality"]
    # the reported final nets are computed from the final (post-guard, post-QP) weights ...
    w = sig.table.set_index("symbol")
    final = w[w["selected"]].groupby("sector")["target_weight"].sum()
    for sec, v in final.items():
        assert sn["final_sector_net"][sec] == pytest.approx(v, abs=1e-12)
    # ... and respect the sector limit (the QP ran on the guard-scaled short book)
    assert sn["status"] in ("ok", "already_neutral", "ok (gross reduced to meet sector limits)")
    assert sn["final_max_abs_net"] <= cfg.max_sector_net + 1e-7


# ---------------------------------------------------------------- manual approval
def test_manual_approval_holds_rebalances_but_not_stops(tmp_path):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb)
    set_setting(ctx.db, "rebalance_approval", "manual")
    DailyCycle(ctx, fb, now_fn=lambda: now).run(dry_run=True)       # learn the targets
    plan, tw = targets(ctx)
    panel = ctx.panel()
    t = panel.sess_index[latest]
    squeezed = next(s for s, w in tw.items() if w > 0)
    fb.pos = {squeezed: (-10, float(panel.close[t, panel.sym_index[squeezed]]) / 1.6)}
    DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=5)).run()
    assert [o["client_order_id"][:5] for o in fb.submits] == ["stop-"]           # only the stop-loss went out
    waiting = ctx.db.query("SELECT * FROM broker_orders WHERE status='planned' AND status_reason LIKE 'awaiting approval%'")
    assert waiting and all(o["origin"] == "rebalance" for o in waiting)
    # approve -> the next run sends them under the same client ids
    ctx.db.conn.execute("UPDATE broker_orders SET approved_at='2026-01-01' WHERE status='planned'")
    DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=10)).run()
    sent = {o["client_order_id"] for o in fb.submits}
    assert {o["client_order_id"] for o in waiting} <= sent


def test_declined_orders_are_never_sent(tmp_path):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb)
    set_setting(ctx.db, "rebalance_approval", "manual")
    DailyCycle(ctx, fb, now_fn=lambda: now).run()
    n = ctx.db.conn.execute("UPDATE broker_orders SET status='declined' WHERE status='planned'").rowcount
    assert n > 0
    set_setting(ctx.db, "rebalance_approval", "auto")               # even switching back to auto ...
    DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=5)).run()
    assert fb.submits == []                                          # ... declined orders are not regenerated
