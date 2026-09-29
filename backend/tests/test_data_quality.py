"""Stale / insufficient data handling: detection, blocked rebalances, no silent processing."""

import dataclasses
from datetime import UTC, datetime

import numpy as np
import pytest

from app.data.store import data_version, get_panel, quality_report, run_import
from app.db import Database
from app.ledger.paper import LedgerError, PaperLedger
from app.strategy.config import StrategyConfig

from .helpers import FixtureProvider, weekday_calendar

CAL = weekday_calendar("2020-01-01", 60)
CFG = StrategyConfig(signal="momentum_12_1", htb_exclude_pct=0.0, sector_neutral=False, crash_guard=False, borrow_fee_annual=0.0, short_min_price=0, min_names_per_side=1, max_names_per_side=1, vol_lookback_sessions=20, beta_lookback_sessions=60, max_long_weight=1.0, max_short_weight=1.0, lookback_sessions=10, skip_sessions=2, min_history_sessions=10, adv_window=3, min_adv_usd=0,
                     min_price=1, slippage_bps=0)


def provider(missing_at: str | None = None, live: bool = False):
    series = {s: {"close": 20 + np.arange(60) * (0.1 + 0.05 * k)} for k, s in enumerate(["AAA", "BBB", "CCC", "DDD"])}
    if missing_at:  # 3 of 4 stocks have no bar at this session -> 25% coverage
        for s in ("AAA", "BBB", "CCC"):
            series[s]["close"][CAL.index(missing_at)] = np.nan
    p = FixtureProvider(CAL, series)
    if live:
        p.info = dataclasses.replace(p.info, key="fixture_live", is_demo=False, data_label="Delayed Market Data",
                                     point_in_time_universe=False, survivorship_note="test survivorship")
    return p


def ledger(db, prov):
    return PaperLedger(db, CAL, prov.info.key, lambda: get_panel(db, prov.info.key, CAL, None),
                       lambda: data_version(db, prov.info.key), is_demo=prov.info.is_demo)


def test_live_data_behind_calendar_is_flagged_stale(tmp_path):
    db = Database(tmp_path / "q.db")
    prov = provider(live=True)
    run_import(db, prov, CAL)
    long_cal = weekday_calendar("2020-01-01", 120)  # exchange keeps trading after our data ends
    q = quality_report(db, prov, long_cal, now=datetime(2020, 4, 1, 23, 0, tzinfo=UTC))
    assert q["expected_latest_session"] == "2020-04-01"
    assert q["is_stale"] and "6 session(s) behind" in q["stale_reason"]
    assert any("SURVIVORSHIP" in w["message"] for w in q["warnings"])
    fresh = quality_report(db, prov, long_cal, now=datetime(2020, 3, 24, 23, 0, tzinfo=UTC))
    assert fresh["coverage_end"] == "2020-03-24" and not fresh["is_stale"]


def test_insufficient_coverage_blocks_rebalance(tmp_path):
    db = Database(tmp_path / "b.db")
    prov = provider(missing_at="2020-02-28")
    run_import(db, prov, CAL)
    led = ledger(db, prov)
    led.initialize(CFG, inception_session="2020-01-31")
    led.advance(auto_apply=True, until="2020-03-05")
    plan = db.query_one("SELECT * FROM rebalance_plans WHERE signal_session='2020-02-28'")
    assert plan["status"] == "blocked" and "25%" in plan["block_reason"]
    with pytest.raises(LedgerError):
        led.apply_plan(plan["id"])
    # Nothing traded on the fill session of the blocked plan.
    assert db.scalar("SELECT COUNT(*) FROM paper_fills WHERE session='2020-03-02'") == 0


def test_advance_stops_at_session_without_any_bars(tmp_path):
    db = Database(tmp_path / "s.db")
    prov = provider()
    prov._bars = prov._bars[prov._bars["session"] != "2020-02-12"]  # an entire session missing from the feed
    run_import(db, prov, CAL)
    led = ledger(db, prov)
    led.initialize(CFG, inception_session="2020-02-05")
    res = led.advance()
    assert res.stopped_reason == "session_without_data" and res.as_of == "2020-02-11"


def test_alpaca_without_key_starts_and_reports_error(tmp_path):
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.config import Settings

    s = Settings(_env_file=None, DATABASE_PATH=str(tmp_path / "a.db"), MARKET_DATA_PROVIDER="alpaca",
                 LOG_LEVEL="WARNING", LOG_FORMAT="text")
    with TestClient(create_app(s)) as c:
        st = c.get("/api/status").json()
        assert st["provider"] is None and "ALPACA_API_KEY_ID" in st["provider_error"]
        assert st["mode"] == "live" and st["data_label"] == "Delayed Market Data"
        r = c.get("/api/portfolio/summary")
        assert r.status_code == 503 and r.json()["error"] == "data_unavailable"
