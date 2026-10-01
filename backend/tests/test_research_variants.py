"""Research variants: point-in-time fundamentals + value/momentum score, quarterly rebalancing, trend + satellite,
the extra report columns, and the CRSP CSV provider stub."""

import numpy as np
import pandas as pd
import pytest

from app.backtest.compare import run_report
from app.backtest.engine import run_backtest
from app.backtest.variants import VARIANT_BY_KEY, VARIANTS
from app.data.crsp_provider import CrspCsvProvider
from app.data.fundamentals import PointInTimeFundamentals, extract_facts, fetch_sec_fundamentals
from app.data.panel import build_panel
from app.data.provider import ProviderError
from app.db import Database
from app.strategy.composite import winsor_z
from app.strategy.config import StrategyConfig
from app.strategy.long_short import trend_signal
from app.strategy.signals import compute_signals

from .helpers import make_panel, rich_fixture, weekday_calendar

BASE = dict(min_adv_usd=1e6, min_names_per_side=10, max_names_per_side=30, long_pct=0.2, short_pct=0.2,
            buffer_exit_pct=0.3, htb_exclude_pct=0.0)


class FlatRate:
    carried: set = set()

    def annual(self, s):
        return 0.03


@pytest.fixture(scope="module")
def fixture():
    cal, prov, series, sectors = rich_fixture()
    syms = list(series)
    panel = build_panel(prov.fetch_bars(syms, "2000", "2100"), prov.fetch_corporate_actions(syms, "2000", "2100"),
                        pd.DataFrame([i.__dict__ for i in prov.list_instruments()]), cal, "SPY")
    return cal, panel


def fact(symbol, concept, end, value, filed, start=None, accn=None):
    return {"symbol": symbol, "concept": concept, "start": start, "end": end, "value": value, "filed": filed,
            "accn": accn or f"{symbol}-{filed}"}


# ---------------------------------------------------------------- point-in-time fundamentals
def test_point_in_time_uses_only_filed_facts_and_ttm():
    f = PointInTimeFundamentals(pd.DataFrame([
        fact("A", "equity", "2023-12-31", 500.0, "2024-02-20"),
        fact("A", "equity", "2024-03-31", 520.0, "2024-05-02"),
        fact("A", "shares", "2024-01-31", 100.0, "2024-02-20"),
        fact("A", "net_income", "2023-12-31", 120.0, "2024-02-20", start="2023-01-01"),       # FY2023
        fact("A", "net_income", "2023-03-31", 20.0, "2023-05-01", start="2023-01-01"),        # Q1 2023
        fact("A", "net_income", "2024-03-31", 35.0, "2024-05-02", start="2024-01-01"),        # Q1 2024
    ]))
    # before the Q1-2024 10-Q is filed: FY2023 only, and the year-end balance sheet
    assert f.instant("A", "equity", "2024-04-30") == (500.0, "2023-12-31")
    assert f.ttm_earnings("A", "2024-04-30") == 120.0
    # after it is filed: TTM = FY + YTD - prior YTD = 120 + 35 - 20; the newer balance sheet
    assert f.instant("A", "equity", "2024-05-31") == (520.0, "2024-03-31")
    assert f.ttm_earnings("A", "2024-05-31") == pytest.approx(135.0)
    # nothing is known before the first filing
    assert f.instant("A", "equity", "2024-02-19") is None and f.ttm_earnings("A", "2024-02-19") is None
    # ratios with a 2:1 split after the share count's date: shares double
    r = f.ratios("A", "2024-05-31", close=5.0, split_factor=lambda end: 2.0)
    assert r["mcap"] == pytest.approx(5.0 * 200) and r["bm"] == pytest.approx(520 / 1000)
    assert r["ep"] == pytest.approx(135 / 1000)
    # stale data (> 18 months) is not used
    assert f.instant("A", "equity", "2026-01-31") is None


def test_negative_equity_and_share_classes():
    f = PointInTimeFundamentals(pd.DataFrame([
        fact("B", "equity", "2024-03-31", -50.0, "2024-05-01"),
        fact("B", "shares", "2024-04-20", 60.0, "2024-05-01", accn="x"),     # class A
        fact("B", "shares", "2024-04-20", 40.0, "2024-05-01", accn="x"),     # class B, same filing
    ]))
    assert f.instant("B", "shares", "2024-06-30")[0] == 100.0
    r = f.ratios("B", "2024-06-30", close=10.0, split_factor=lambda end: 1.0)
    assert np.isnan(r["bm"]) and r["mcap"] == 1000.0                      # negative book equity -> no B/M


def test_extract_facts_and_fetch_store(tmp_path):
    payload = {"facts": {
        "us-gaap": {"StockholdersEquity": {"units": {"USD": [
                        {"end": "2024-03-31", "val": 10, "filed": "2024-05-01", "form": "10-Q", "accn": "a"}]}},
                    "NetIncomeLoss": {"units": {"USD": [
                        {"start": "2024-01-01", "end": "2024-03-31", "val": 3, "filed": "2024-05-01", "accn": "a"},
                        {"end": "2024-03-31", "val": 3, "filed": "2024-05-01", "accn": "a"}]}}},   # no start: dropped
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                    {"end": "2024-04-20", "val": 7, "filed": "2024-05-01", "accn": "a"}]}}}}}
    rows = extract_facts(payload)
    assert sorted(r["concept"] for r in rows) == ["equity", "net_income", "shares"]
    assert {r["source_tag"] for r in rows} >= {"dei:EntityCommonStockSharesOutstanding", "us-gaap:StockholdersEquity"}

    class FakeSec:
        def ticker_map(self):
            return {"AAA": 1, "BRK-B": 2}

        def companyfacts(self, cik):
            return payload if cik == 1 else {"facts": {}}

    db = Database(tmp_path / "f.db")
    stats = fetch_sec_fundamentals(db, "fixture", FakeSec(), ["AAA", "BRK.B", "ZZZ"])
    assert (stats["ok"], stats["no_facts"], stats["no_cik"]) == (1, 1, 1) and stats["facts"] == 3
    fetch_sec_fundamentals(db, "fixture", FakeSec(), ["AAA"])                   # refetch replaces, no duplicates
    assert db.scalar("SELECT COUNT(*) FROM fundamental_facts WHERE symbol='AAA'") == 3
    assert len(PointInTimeFundamentals.load(db, "fixture")) == 1


# ---------------------------------------------------------------- value + momentum score
def test_value_momentum_blends_point_in_time_value(fixture):
    cal, panel = fixture
    t = len(panel.sessions) - 6
    asof = panel.sessions[t]
    later = (pd.Timestamp(asof) + pd.Timedelta(days=10)).date().isoformat()
    syms = [s for s in panel.symbols if s.startswith("S")][:40]
    rows = []
    for k, s in enumerate(syms):
        filed = asof if k < 30 else later                                    # 10 names: filed AFTER the signal
        rows += [fact(s, "equity", asof, 100.0 + 10 * k, filed), fact(s, "shares", asof, 10.0, filed),
                 fact(s, "net_income", asof, 5.0 + k, filed, start=(pd.Timestamp(asof) - pd.Timedelta(days=364)).date().isoformat())]
    panel.fundamentals = PointInTimeFundamentals(pd.DataFrame(rows))
    try:
        cfg = StrategyConfig(**BASE, signal="value_momentum", w_value=0.5)
        sig = compute_signals(panel, t, cfg)
        tab = sig.table[sig.table["eligible"]].set_index("symbol")
        cov = sig.diagnostics["value"]["coverage"]
        assert cov["value"] == len([s for s in syms[:30] if s in tab.index]) and cov["value"] > 0
        mom_z = pd.Series(winsor_z(tab["composite"].astype(float).to_numpy(), cfg.winsor_sigma), index=tab.index)
        for s in tab.index:
            if s in syms[:30]:
                assert tab.at[s, "vm_score"] == pytest.approx(0.5 * mom_z[s] + 0.5 * tab.at[s, "value_z"])
            else:                                                          # incl. names filed after the signal
                assert np.isnan(tab.at[s, "value_z"]) and tab.at[s, "vm_score"] == pytest.approx(mom_z[s])
        ranked = tab.sort_values("rank")
        assert list(ranked["vm_score"]) == sorted(ranked["vm_score"], reverse=True)   # ranked by the blended score
    finally:
        panel.fundamentals = None


def test_value_momentum_without_fundamentals_falls_back_to_momentum(fixture):
    cal, panel = fixture
    sig = compute_signals(panel, len(panel.sessions) - 6, StrategyConfig(**BASE, signal="value_momentum"))
    assert sig.diagnostics["value"]["fundamentals_loaded"] is False and "momentum only" in sig.diagnostics["value"]["note"]


# ---------------------------------------------------------------- quarterly
def test_quarterly_rebalances_only_at_quarter_ends(fixture):
    cal, panel = fixture
    cfg = StrategyConfig(**{**BASE, "buffer_exit_pct": 0.40}, rebalance_frequency="quarterly")
    res = run_backtest(panel, cal, cfg)
    months = {int(r.signal_session[5:7]) for r in res.rebalances}
    assert months <= {3, 6, 9, 12} and len(res.rebalances) >= 4
    monthly = run_backtest(panel, cal, StrategyConfig(**BASE))
    assert len(monthly.rebalances) > 2.5 * len(res.rebalances)
    assert VARIANT_BY_KEY["neutral_10_quarterly"].config().buffer_exit_pct == 0.40


# ---------------------------------------------------------------- trend + satellite
def test_trend_signal_on_and_off():
    cal = weekday_calendar("2020-01-01", 330)
    up = np.linspace(100, 200, 330)
    down = np.linspace(200, 100, 330)
    p = make_panel(cal, {"UP": {"close": up}, "DN": {"close": down}})
    t = max(i for i, s in enumerate(p.sessions) if i + 1 < len(p.sessions) and p.sessions[i + 1][:7] != s[:7])
    on = trend_signal(p, t, p.sym_index["UP"], 10)
    off = trend_signal(p, t, p.sym_index["DN"], 10)
    assert on["on"] is True and on["close"] > on["sma"]
    assert off["on"] is False and off["close"] < off["sma"]
    assert trend_signal(p, 30, p.sym_index["UP"], 10)["on"] is None          # fewer than 10 month-ends


def test_trend_satellite_book_and_backtest(fixture):
    cal, panel = fixture
    cfg = VARIANT_BY_KEY["trend_satellite"].config().model_copy(update={k: v for k, v in BASE.items()
                                                                       if k not in ("short_pct",)})
    sig = compute_signals(panel, len(panel.sessions) - 6, cfg)
    tw, d = sig.target_weights(), sig.diagnostics
    assert all(w > 0 for w in tw.values())                                 # no shorts
    sat = {s: w for s, w in tw.items() if s != "SPY"}
    # 20% book, or less when the per-name 1% cap binds (the 60-stock fixture's top 20% is only 12 names)
    assert sum(sat.values()) == pytest.approx(min(0.20, 0.01 * len(sat))) and max(sat.values()) <= 0.01 + 1e-12
    if len(sat) < 20:
        assert any("limited" in b for b in d["binding"])
    trend = d["core"]["trend"]
    assert tw.get("SPY", 0.0) == (0.8 if trend["on"] is not False else 0.0)
    res = run_backtest(panel, cal, cfg, rf=FlatRate())
    assert (res.nav["cash"] >= -0.01).all()                                # no margin
    assert (res.nav["short_value"] == 0).all()
    assert any(e.kind == "interest" for _, e in res.cash_events)            # idle cash earns RF
    offs = [r for r in res.rebalances if r.status == "executed" and r.signals.diagnostics["core"]["weight"] == 0]
    ons = [r for r in res.rebalances if r.status == "executed" and r.signals.diagnostics["core"]["weight"] == 0.8]
    assert ons and res.margin_flags == []
    if offs:                                                                # trend off -> SPY sold to cash
        assert "SPY" not in offs[0].signals.target_weights()


# ---------------------------------------------------------------- report columns
def test_investor_sharpe_and_cost_pct():
    sessions = [d.date().isoformat() for d in pd.bdate_range("2020-01-01", periods=520)]
    r = 0.0006 + 0.004 * np.sin(np.arange(520))
    nav = 100_000 * np.cumprod(1 + r)
    df = pd.DataFrame({"nav": nav, "cash": nav, "benchmark_nav": nav, "long_value": 0.0, "short_value": 0.0},
                      index=sessions)
    rep = run_report(df, {"total_costs": 1000.0, "borrow_fees": 0.0, "margin_interest": 0.0}, False, None, 100_000)
    rets = pd.Series(nav).pct_change().dropna()
    assert rep["investor_sharpe"] == pytest.approx(rets.mean() * 252 / (rets.std(ddof=1) * np.sqrt(252)))  # RF = 0
    years = (pd.Timestamp(sessions[-1]) - pd.Timestamp(sessions[0])).days / 365.25
    assert rep["cost_pct_nav_per_year"] == pytest.approx(1000.0 / nav.mean() / years)


def test_variant_list_and_definitions():
    keys = [v.key for v in VARIANTS]
    assert keys[:4] == ["neutral_10", "neutral_15", "spy_overlay", "ext_130_30"]
    assert keys[4:] == ["value_momentum", "neutral_10_quarterly", "trend_satellite"]
    vm = VARIANT_BY_KEY["value_momentum"].config()
    assert vm.signal == "value_momentum" and vm.w_value == 0.5 and vm.sector_neutral and vm.target_vol == 0.10
    ts = VARIANT_BY_KEY["trend_satellite"].config()
    assert (ts.core_beta, ts.trend_filter, ts.trend_sma_months, ts.long_only, ts.fixed_long_gross, ts.max_long_weight,
            ts.allow_margin, ts.cash_interest, ts.rebalance_frequency) == (0.8, True, 10, True, 0.2, 0.01, False, True,
                                                                           "monthly")


# ---------------------------------------------------------------- CRSP stub
CRSP_CSV = """PERMNO,date,PRC,VOL,RET,DLRET,SHRCD,EXCHCD,SICCD,SHROUT,OPENPRC,CFACPR,TICKER
10001,2020-01-02,10.00,1000,0.0,,11,1,3571,500,9.9,2,AAA
10001,2020-01-03,-10.50,1200,0.05,,11,1,3571,500,10.1,2,AAA
10001,2020-01-06,5.30,900,0.0095238,,11,1,3571,1000,5.2,1,AAA
10001,2020-01-07,5.40,800,0.0377358,,11,1,3571,1000,5.3,1,AAA
20002,2020-01-02,20.00,500,0.0,,11,3,6021,100,20.0,1,BBB
20002,2020-01-03,18.00,700,-0.1,-0.30,11,3,6021,100,19.0,1,BBB
84398,2020-01-02,320.0,9000,0.0,,73,4,6726,900,319,1,SPY
84398,2020-01-03,321.0,9000,0.003125,,73,4,6726,900,320,1,SPY
84398,2020-01-06,322.0,9000,0.003115,,73,4,6726,900,321,1,SPY
84398,2020-01-07,323.0,9000,0.0031,,73,4,6726,900,322,1,SPY
"""


def test_crsp_csv_provider(tmp_path):
    path = tmp_path / "crsp.csv"
    path.write_text(CRSP_CSV)
    prov = CrspCsvProvider(path, history_start="2020-01-01")
    inst = {i.symbol: i for i in prov.list_instruments()}
    assert inst["P10001"].asset_type == "common_stock" and inst["P10001"].exchange == "NYSE"
    assert inst["P84398"].is_benchmark and inst["P84398"].asset_type == "etf"
    assert inst["P20002"].delist_date == "2020-01-03" and not inst["P20002"].active
    assert inst["P20002"].metadata["delisting_return"] == pytest.approx(-0.30)
    assert inst["P10001"].sector == "Information Technology" and prov.info.point_in_time_universe
    bars = prov.fetch_bars(["P10001"], "2020-01-01", "2020-12-31").set_index("session")
    assert bars.at["2020-01-03", "close"] == 10.50                               # negative PRC = bid/ask midpoint
    acts = prov.fetch_corporate_actions(["P10001"], "2020-01-01", "2020-12-31")
    split = acts[acts["action_type"] == "split"]
    assert list(split["ex_date"]) == ["2020-01-06"] and split["ratio"].iloc[0] == pytest.approx(2.0)
    div = acts[acts["action_type"] == "cash_dividend"]
    # 01-03: RET 5% on 10.00 -> 10.50 is all price (no dividend); 01-07: RET 3.77% on 5.30 -> 5.40 implies $0.10
    assert list(div["ex_date"]) == ["2020-01-07"] and div["amount"].iloc[0] == pytest.approx(0.10, abs=1e-5)
    assert prov.default_history_range() == ("2020-01-02", "2020-01-07")


def test_crsp_requires_open_prices_and_columns(tmp_path):
    path = tmp_path / "noopen.csv"
    path.write_text("\n".join(",".join(c for i, c in enumerate(line.split(",")) if i not in (10, 11))
                              for line in CRSP_CSV.strip().splitlines()))
    prov = CrspCsvProvider(path, history_start="2020-01-01")
    assert prov.list_instruments()                                            # loads
    with pytest.raises(ProviderError, match="OPENPRC"):
        prov.fetch_bars(["P10001"], "2020-01-01", "2020-12-31")
    with pytest.raises(ProviderError, match="CFACPR"):
        prov.fetch_corporate_actions(["P10001"], "2020-01-01", "2020-12-31")
    bad = tmp_path / "bad.csv"
    bad.write_text("PERMNO,date,PRC\n1,2020-01-02,10\n")
    with pytest.raises(ProviderError, match="missing columns"):
        CrspCsvProvider(bad).list_instruments()
