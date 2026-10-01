"""Switching the paper strategy: config versioning, replan-now reconciliation (neutral book -> SPY + overlay),
dry run changes nothing, approve vs auto mode, the model ledger's margin loan, and the spy_overlay sanity backtest."""

import sqlite3
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import paper_strategy
from app.__main__ import main
from app.automation import DailyCycle, account_margin_flags, set_setting
from app.backtest.engine import run_backtest
from app.backtest.variants import VARIANT_BY_KEY
from app.config import REPO_ROOT
from app.data.store import data_version, get_panel, run_import
from app.db import Database
from app.ledger.paper import PaperLedger
from app.paper_strategy import SwitchError, active_variant, apply, preview
from app.strategy.config import StrategyConfig

from .helpers import rich_fixture
from .test_automation import FakeBroker, make_ctx, targets


class FlatRate:
    carried: set = set()

    def __init__(self, a):
        self.a = a

    def annual(self, s):
        return self.a


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(paper_strategy, "send", lambda settings, msg, **kw: out.append((kw.get("title"), msg)))
    return out


def snapshot(db: Database) -> dict:
    """Every table's full contents (to prove a dry run changes nothing)."""
    tables = [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")]
    return {t: db.query(f"SELECT * FROM {t}") for t in tables}


def fill_everything(ctx, fb):
    """The broker fills every working order at its reference price (as Alpaca would at the open)."""
    for o in ctx.db.query("SELECT * FROM broker_orders WHERE status IN ('accepted','new','submitted')"):
        q = o["qty"] if o["side"] == "buy" else -o["qty"]
        cur, _ = fb.pos.get(o["symbol"], (0, o["ref_price"]))
        if cur + q:
            fb.pos[o["symbol"]] = (cur + q, o["ref_price"])
        else:
            fb.pos.pop(o["symbol"], None)
        fb.orders[o["client_order_id"]]["status"] = "filled"
    ctx.db.conn.execute("UPDATE broker_orders SET status='filled', filled_qty=qty WHERE status IN "
                        "('accepted','new','submitted')")
    ctx.db.conn.commit()


def neutral_book(tmp_path, mode="auto"):
    """A context whose Alpaca account holds the default (Neutral 10%) month-end book."""
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb, mode="auto")
    DailyCycle(ctx, fb, now_fn=lambda: now).run()
    assert fb.submits, "the neutral month-end book should have been sent"
    fill_everything(ctx, fb)
    set_setting(ctx.db, "rebalance_mode", mode)
    return fb, ctx, cal, latest, fill, now


# ---------------------------------------------------------------- versioning
def test_config_switch_creates_a_new_version_logs_and_notifies(tmp_path, sent):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb)
    DailyCycle(ctx, fb, now_fn=lambda: now).run(dry_run=True)
    port = ctx.ledger.active()
    assert active_variant(ctx) == {"key": "neutral_10", "name": "Neutral 10%"}
    n_configs = ctx.db.scalar("SELECT COUNT(*) FROM strategy_configs")
    res = apply(ctx, "spy_overlay", replan_now=False, now=now)
    after = ctx.ledger.active()
    assert res["changed"] and after["config_id"] != port["config_id"]
    assert ctx.db.scalar("SELECT COUNT(*) FROM strategy_configs") == n_configs + 1      # a new config version
    cfg = ctx.ledger.config_of(after)
    assert cfg.core_beta == 1.0 and cfg.initial_capital == ctx.ledger.config_of(port).initial_capital
    assert active_variant(ctx) == {"key": "spy_overlay", "name": "SPY + overlay"}
    assert ctx.db.scalar("SELECT COUNT(*) FROM system_events WHERE message='Paper strategy changed to SPY + overlay'") == 1
    assert sent and sent[-1][0] == "Paper strategy changed to SPY + overlay"
    again = apply(ctx, "spy_overlay", replan_now=False, now=now)                            # idempotent
    assert not again["changed"] and ctx.db.scalar("SELECT COUNT(*) FROM strategy_configs") == n_configs + 1
    with pytest.raises(SwitchError):
        preview(ctx, "no_such_variant", False)


# ---------------------------------------------------------------- dry run
def test_dry_run_changes_nothing(tmp_path, monkeypatch, capsys, sent):
    fb, ctx, cal, latest, fill, now = neutral_book(tmp_path)
    before = snapshot(ctx.db)
    monkeypatch.setattr("app.services.AppContext", lambda settings: ctx)
    assert main(["paper-config", "--variant", "spy_overlay", "--replan-now", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN: nothing was changed." in out and "Target book" in out and "SPY 100.0%" in out
    assert "est. overlay beta" in out and "Orders (" in out
    pv = preview(ctx, "spy_overlay", True, now=now)
    assert snapshot(ctx.db) == before and sent == []                                       # nothing written or sent
    assert pv["book"]["spy_weight"] == 1.0 and pv["book"]["total_gross"] <= 1.6 + 1e-9
    assert abs(pv["book"]["overlay_beta"]) < 0.05
    assert pv["orders"][0]["symbol"] == "SPY" and pv["orders"][0]["side"] == "buy"          # SPY listed first


# ---------------------------------------------------------------- replan now: neutral book -> SPY + overlay
def test_replan_now_reconciles_neutral_book_to_spy_overlay(tmp_path, sent):
    fb, ctx, cal, latest, fill, now = neutral_book(tmp_path, mode="auto")
    old_plan, old_targets = targets(ctx)
    held = dict(fb.pos)
    res = apply(ctx, "spy_overlay", replan_now=True, now=now)
    new_id = res["plan_id"]
    plans = {r["id"]: r for r in ctx.db.query("SELECT * FROM rebalance_plans")}
    assert plans[old_plan["id"]]["status"] == "superseded"
    new = plans[new_id]
    assert new["kind"] == "replan" and new["status"] == "applied"
    assert new["signal_session"] == old_plan["signal_session"]          # the month-end signal, not a mid-month one
    assert new["fill_session"] == cal.next_session(ctx.ledger.active()["as_of_session"])
    ranks = lambda sid: dict(ctx.db.conn.execute(   # noqa: E731
        "SELECT symbol, rank FROM signal_rows WHERE set_id=? AND rank IS NOT NULL", (sid,)).fetchall())
    assert ranks(new["signal_set_id"]) == ranks(old_plan["signal_set_id"])   # same month-end ranking
    _, tw = targets(ctx)
    assert tw["SPY"] == 1.0 and sum(tw.values()) == pytest.approx(1.0 + sum(w for s, w in tw.items() if s != "SPY"))

    fb.submits.clear()
    DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=5)).run()
    sent_orders = {o["symbol"]: o for o in fb.submits}
    assert all(o["client_order_id"].startswith(f"p{new_id}-") for o in fb.submits)          # only the new plan trades
    spy = sent_orders["SPY"]
    assert spy["side"] == "buy" and int(spy["qty"]) > 0                                     # buy the SPY core
    for sym, (qty, _) in held.items():
        if sym not in tw:                                                                  # dropped names are closed
            assert sent_orders[sym]["side"] == ("sell" if qty > 0 else "buy") and int(sent_orders[sym]["qty"]) == abs(qty)
    assert all(o["time_in_force"] == "opg" for o in fb.submits)


def test_replan_respects_approve_mode(tmp_path, sent):
    fb, ctx, cal, latest, fill, now = neutral_book(tmp_path, mode="approve")
    res = apply(ctx, "spy_overlay", replan_now=True, now=now)
    assert "approve it on the Trading page" in res["message"]
    fb.submits.clear()
    DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=5)).run()
    assert fb.submits == []                                                                # held for approval
    waiting = ctx.db.query("SELECT * FROM broker_orders WHERE status='planned' AND status_reason LIKE 'awaiting approval%'")
    assert waiting and {o["plan_id"] for o in waiting} == {res["plan_id"]}
    from app.api.routes.broker import decide_plan
    decide_plan(ctx, res["plan_id"], "approved", len(waiting))
    DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=10)).run()
    assert {o["client_order_id"] for o in waiting} <= {o["client_order_id"] for o in fb.submits}


def test_replan_blocked_by_working_orders_changes_nothing(tmp_path, sent):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb, mode="auto")
    DailyCycle(ctx, fb, now_fn=lambda: now).run()                       # orders sent, still working (not filled)
    cfg_id = ctx.ledger.active()["config_id"]
    with pytest.raises(SwitchError, match="still working at Alpaca"):
        apply(ctx, "spy_overlay", replan_now=True, now=now)
    assert ctx.ledger.active()["config_id"] == cfg_id and sent == []


def test_no_replan_needed_when_the_month_end_is_not_processed_yet(tmp_path, sent):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb)
    ctx.refresh_data()
    ctx.ledger.initialize(StrategyConfig(), inception_session=cal.sessions[cal.index(latest) - 5])
    pv = preview(ctx, "spy_overlay", True, now=now)
    assert pv["replan"]["action"] == "not_needed"
    res = apply(ctx, "spy_overlay", True, now=now)
    assert res["plan_id"] is None and ctx.ledger.config_of(ctx.ledger.active()).core_beta == 1.0
    DailyCycle(ctx, fb, now_fn=lambda: now).run()                       # the month-end plan uses the new config
    plan, tw = targets(ctx)
    assert plan["kind"] == "month_end" and tw.get("SPY") == 1.0


# ---------------------------------------------------------------- model ledger = backtest with SPY core + margin loan
def test_model_ledger_matches_backtest_with_spy_core_and_margin_loan(tmp_path):
    cal, prov, series, sectors = rich_fixture()
    db = Database(tmp_path / "l.db")
    run_import(db, prov, cal)
    panel = get_panel(db, prov.info.key, cal, "SPY")
    cfg = StrategyConfig(min_adv_usd=1e6, min_names_per_side=10, max_names_per_side=30, long_pct=0.2, short_pct=0.2,
                         htb_exclude_pct=0.0, crash_guard=False, core_beta=1.0, sizing="fixed", fixed_long_gross=0.30,
                         fixed_short_gross=0.15, beta_neutral=False, min_side_gross=0.15, max_side_gross=0.4,
                         max_total_gross=1.6, sector_neutral=False)
    start = [s for s in cal.month_end_sessions(panel.sessions[300], panel.sessions[-1]) if s in panel.sess_index][0]
    res = run_backtest(panel, cal, cfg.model_copy(update={"start_date": start}), rf=FlatRate(0.03))
    assert any(e.kind == "margin_interest" for _, e in res.cash_events)       # net 115%: a real margin loan
    led = PaperLedger(db, cal, prov.info.key, lambda: get_panel(db, prov.info.key, cal, "SPY"),
                      lambda: data_version(db, prov.info.key), rf_fn=lambda: FlatRate(0.03))
    led.initialize(cfg, inception_session=start)
    led.advance(auto_apply=True)
    paper = [r["nav"] for r in db.query("SELECT nav FROM paper_nav ORDER BY session")]
    assert paper == list(res.nav["nav"])                                    # to the cent
    assert db.scalar("SELECT COUNT(*) FROM cash_transactions WHERE note LIKE 'margin loan interest%'") == \
        sum(1 for _, e in res.cash_events if e.kind == "margin_interest")


# ---------------------------------------------------------------- margin checks on the real account
def test_account_margin_flags():
    ok = {"equity": "100000", "long_market_value": "130000", "short_market_value": "-30000", "maintenance_margin": "40000"}
    assert account_margin_flags(ok, 1.6) == []
    drift = {**ok, "long_market_value": "140000", "short_market_value": "-30000"}
    assert "above the strategy cap" in account_margin_flags(drift, 1.6)[0]
    reg_t = {**ok, "long_market_value": "170000", "short_market_value": "-40000"}
    assert account_margin_flags(reg_t, 1.6)[0].startswith("MARGIN: gross")
    maint = {**ok, "maintenance_margin": "120000"}
    assert any("below maintenance" in f for f in account_margin_flags(maint, 1.6))


# ---------------------------------------------------------------- spy_overlay sanity backtests (no tuning)
def _sanity(res, cfg):
    assert res.margin_flags == []
    gross = res.nav["gross_exposure"]
    assert gross.max() < 2.0
    executed = [r for r in res.rebalances if r.status == "executed"]
    assert executed and all(r.signals.diagnostics["total_gross"] <= cfg.max_total_gross + 1e-9 for r in executed)


def test_spy_overlay_backtest_sanity_on_fixture():
    from app.data.panel import build_panel

    cal, prov, series, sectors = rich_fixture()
    syms = list(series)
    panel = build_panel(prov.fetch_bars(syms, "2000", "2100"), prov.fetch_corporate_actions(syms, "2000", "2100"),
                        pd.DataFrame([i.__dict__ for i in prov.list_instruments()]), cal, "SPY")
    cfg = VARIANT_BY_KEY["spy_overlay"].config().model_copy(update={
        "min_adv_usd": 1e6, "min_names_per_side": 10, "max_names_per_side": 30, "long_pct": 0.2, "short_pct": 0.2,
        "htb_exclude_pct": 0.0})                           # the fixture has 60 stocks; the variant itself is unchanged
    res = run_backtest(panel, cal, cfg, rf=FlatRate(0.02))
    _sanity(res, cfg)
    assert res.nav["nav"].iloc[-1] > 0


REAL_DB = REPO_ROOT / "data" / "momentum.db"


@pytest.mark.skipif(not REAL_DB.exists(), reason="no local market data (CI): fixture sanity test covers the code path")
def test_spy_overlay_backtest_sanity_on_available_data(tmp_path):
    """Runs the exact spy_overlay variant once on the locally stored data (read-only copy). No tuning."""
    import shutil

    from app.calendar import xnys_calendar

    copy = tmp_path / "real.db"
    src = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    db = Database(copy)
    provider = db.scalar("SELECT provider FROM instruments GROUP BY provider ORDER BY COUNT(*) DESC LIMIT 1")
    bench = db.scalar("SELECT symbol FROM instruments WHERE is_benchmark=1 AND provider=?", (provider,))
    cal = xnys_calendar()
    panel = get_panel(db, provider, cal, bench)
    cfg = VARIANT_BY_KEY["spy_overlay"].config()
    res = run_backtest(panel, cal, cfg, rf=FlatRate(0.02))
    _sanity(res, cfg)
    shutil.rmtree(tmp_path, ignore_errors=True)
