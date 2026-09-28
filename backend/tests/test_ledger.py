"""Paper ledger: persistence across restart, duplicate prevention, immutability, config versioning,
and exact agreement with the backtester."""

import sqlite3

import numpy as np
import pytest

from app.backtest.engine import run_backtest
from app.data.store import data_version, get_panel, run_import
from app.db import Database
from app.ledger.paper import LedgerError, PaperLedger
from app.strategy.config import StrategyConfig

from .helpers import FixtureProvider, weekday_calendar

CAL = weekday_calendar("2020-01-01", 130)
CFG = StrategyConfig(lookback_sessions=10, skip_sessions=2, min_history_sessions=10, adv_window=3, min_adv_usd=0,
                     min_price=1, slippage_bps=10, top_n=2, initial_capital=100_000)


def fixture_provider():
    rng = np.random.default_rng(42)
    series = {s: {"close": np.round(30 * np.exp(rng.normal(0.0005 * k, 0.02, 130).cumsum()), 4)}
              for k, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"])}
    series["BMK"] = {"close": np.round(100 * np.exp(rng.normal(0.0003, 0.01, 130).cumsum()), 4)}
    for d in series.values():
        d["open"] = np.round(d["close"] * 0.998, 4)
    actions = [("BBB", CAL.sessions[70], "split", 2.0, None), ("CCC", CAL.sessions[75], "cash_dividend", None, 0.3)]
    return FixtureProvider(CAL, series, actions, benchmark="BMK")


def make_ledger(db: Database, prov) -> PaperLedger:
    return PaperLedger(db, CAL, prov.info.key, lambda: get_panel(db, prov.info.key, CAL, "BMK"),
                       lambda: data_version(db, prov.info.key), is_demo=True)


@pytest.fixture()
def setup(tmp_path):
    prov = fixture_provider()
    path = tmp_path / "ledger.db"
    db = Database(path)
    run_import(db, prov, CAL)
    return path, db, prov


def test_advance_stops_for_decision_and_prevents_duplicates(setup):
    _, db, prov = setup
    led = make_ledger(db, prov)
    led.initialize(CFG, inception_session="2020-01-31")
    plan = led.pending_plan(led.active()["id"])
    assert plan["status"] == "proposed" and plan["fill_session"] == "2020-02-03"
    res = led.advance()
    assert res.stopped_reason == "decision_required" and res.processed == []
    led.apply_plan(plan["id"])
    with pytest.raises(LedgerError) as e:
        led.apply_plan(plan["id"])
    assert e.value.code == "duplicate"
    res = led.advance(until="2020-02-10")
    assert res.processed[0] == "2020-02-03"
    fills = db.query("SELECT * FROM paper_fills WHERE plan_id=?", (plan["id"],))
    assert {f["session"] for f in fills} == {"2020-02-03"} and len(fills) == 2
    # Too late to apply an old plan once the fill session has passed
    with pytest.raises(LedgerError):
        led.apply_plan(plan["id"])


def test_persistence_across_restart_and_immutability(setup):
    path, db, prov = setup
    led = make_ledger(db, prov)
    led.initialize(CFG, inception_session="2020-01-31")
    led.advance(auto_apply=True, until="2020-04-15")
    before = {
        "port": db.query_one("SELECT id, cash, as_of_session, cum_costs FROM paper_portfolios WHERE status='active'"),
        "pos": db.query("SELECT symbol, shares, cost_basis FROM positions ORDER BY symbol"),
        "nav": db.query("SELECT session, nav FROM paper_nav ORDER BY session"),
        "fills": db.query("SELECT * FROM paper_fills ORDER BY id"),
    }
    db.close()

    db2 = Database(path)  # "restart": fresh connection, migrations re-checked (idempotent)
    led2 = make_ledger(db2, prov)
    assert db2.query_one("SELECT id, cash, as_of_session, cum_costs FROM paper_portfolios WHERE status='active'") == before["port"]
    assert db2.query("SELECT symbol, shares, cost_basis FROM positions ORDER BY symbol") == before["pos"]
    assert db2.query("SELECT session, nav FROM paper_nav ORDER BY session") == before["nav"]
    # Continue where we left off.
    res = led2.advance(auto_apply=True, until="2020-05-15")
    assert res.processed[0] == "2020-04-16"
    assert db2.query("SELECT * FROM paper_fills WHERE id<=? ORDER BY id", (before["fills"][-1]["id"],)) == before["fills"]
    # Ledger rows are append-only at the database level.
    with pytest.raises(sqlite3.DatabaseError):
        db2.conn.execute("UPDATE paper_fills SET shares=shares+1")
    with pytest.raises(sqlite3.DatabaseError):
        db2.conn.execute("DELETE FROM cash_transactions")
    with pytest.raises(sqlite3.DatabaseError):
        db2.conn.execute("UPDATE paper_nav SET nav=0")


def test_config_change_affects_only_future_plans(setup):
    _, db, prov = setup
    led = make_ledger(db, prov)
    led.initialize(CFG, inception_session="2020-01-31")
    led.advance(auto_apply=True, until="2020-03-10")
    fills_before = db.query("SELECT * FROM paper_fills ORDER BY id")
    led.update_config(CFG.model_copy(update={"top_n": 1}))
    led.advance(auto_apply=True, until="2020-04-10")
    assert db.query("SELECT * FROM paper_fills WHERE id<=? ORDER BY id", (fills_before[-1]["id"],)) == fills_before
    mar = db.query_one("SELECT signal_set_id FROM rebalance_plans WHERE signal_session='2020-03-31'")
    assert db.scalar("SELECT COUNT(*) FROM signal_rows WHERE set_id=? AND selected=1", (mar["signal_set_id"],)) == 1


def test_cash_ledger_reconciles(setup):
    _, db, prov = setup
    led = make_ledger(db, prov)
    led.initialize(CFG, inception_session="2020-01-31")
    led.advance(auto_apply=True)
    port = led.active()
    total = db.scalar("SELECT ROUND(SUM(amount), 2) FROM cash_transactions WHERE portfolio_id=?", (port["id"],))
    assert total == pytest.approx(port["cash"], abs=0.005)
    last = db.query_one("SELECT balance_after FROM cash_transactions WHERE portfolio_id=? ORDER BY id DESC LIMIT 1",
                        (port["id"],))
    assert last["balance_after"] == pytest.approx(port["cash"])
    kinds = {r["kind"] for r in db.query("SELECT DISTINCT kind FROM cash_transactions")}
    assert {"initial_deposit", "buy", "sell"} <= kinds


def test_paper_ledger_matches_backtest_exactly(setup):
    _, db, prov = setup
    led = make_ledger(db, prov)
    led.initialize(CFG, inception_session="2020-01-31")
    led.advance(auto_apply=True)
    paper = {r["session"]: r["nav"] for r in db.query("SELECT session, nav FROM paper_nav ORDER BY session")}
    panel = get_panel(db, prov.info.key, CAL, "BMK")
    bt = run_backtest(panel, CAL, CFG.model_copy(update={"start_date": "2020-01-31"}))
    assert list(paper) == list(bt.nav.index)
    assert list(paper.values()) == list(bt.nav["nav"])
