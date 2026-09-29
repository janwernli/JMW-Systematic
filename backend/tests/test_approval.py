"""rebalance_mode: plan-level approval of month-end rebalances (default 'approve'); stop-losses stay automatic."""

import shutil
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.db as dbmod
from app.api.main import create_app
from app.api.routes import broker as broker_routes
from app.automation import DailyCycle, get_setting, rebalance_mode, set_setting

from .test_automation import FakeBroker, make_ctx, targets


@pytest.fixture
def no_background_cycle(monkeypatch):
    """Approving normally starts a cycle in a thread; the tests drive cycles explicitly instead."""
    calls = []
    monkeypatch.setattr(broker_routes, "run_now", lambda req, ctx: calls.append(req) or {"started": True})
    return calls


def squeeze_a_short(ctx, fb, latest):
    """Give the account a short that closed >= 1.5x its entry, so the cycle must cover it."""
    plan, tw = targets(ctx)
    panel = ctx.panel()
    t = panel.sess_index[latest]
    sym = next(s for s, w in tw.items() if w > 0)
    fb.pos = {sym: (-10, float(panel.close[t, panel.sym_index[sym]]) / 1.6)}
    return sym


def test_default_mode_is_approve_and_holds_rebalances_but_not_stops(tmp_path):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb, mode=None)      # fresh DB, no explicit setting
    assert get_setting(ctx.db, "rebalance_mode", "?") == "approve" and rebalance_mode(ctx.db) == "approve"
    DailyCycle(ctx, fb, now_fn=lambda: now).run(dry_run=True)             # learn the targets
    squeeze_a_short(ctx, fb, latest)
    res = DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=5)).run()
    assert [o["client_order_id"][:5] for o in fb.submits] == ["stop-"]    # only the stop-loss cover went out
    waiting = ctx.db.query("SELECT * FROM broker_orders WHERE status='planned' AND status_reason LIKE 'awaiting approval%'")
    assert waiting and {o["origin"] for o in waiting} == {"rebalance"}
    orders_step = next(s for s in res["steps"] if s["name"] == "orders")
    assert orders_step["data"]["awaiting"] == len(waiting) and orders_step["data"]["sent"] == 1
    assert "awaiting your approval" in orders_step["detail"]


def test_approving_the_plan_sends_every_order_and_later_catch_ups(tmp_path, no_background_cycle):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb, mode="approve")
    DailyCycle(ctx, fb, now_fn=lambda: now).run()
    assert fb.submits == []
    with TestClient(create_app(ctx.settings, ctx=ctx)) as c:
        ov = c.get("/api/broker/overview").json()
        assert ov["rebalance_mode"] == "approve"
        ap = ov["awaiting_plan"]
        assert ap and ap["orders"] == len(ov["awaiting_approval"]) > 0
        assert ap["buy_notional"] > 0 and ap["sell_notional"] > 0
        # the reviewed count must match the current list
        r = c.post(f"/api/broker/plans/{ap['plan_id']}/approve", json={"expected_orders": ap["orders"] + 1})
        assert r.status_code == 409 and "changed" in r.json()["message"]
        r = c.post(f"/api/broker/plans/{ap['plan_id']}/approve", json={"expected_orders": ap["orders"]})
        assert r.status_code == 200 and r.json()["approved"] == ap["orders"]
        assert len(no_background_cycle) == 1 and no_background_cycle[0].dry_run is False
        # one decision per plan
        assert c.post(f"/api/broker/plans/{ap['plan_id']}/decline", json={}).status_code == 409
        assert c.get("/api/broker/overview").json()["awaiting_plan"] is None
    DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=5)).run()
    sent = {o["client_order_id"] for o in fb.submits}
    assert {o["client_order_id"] for o in ov["awaiting_approval"]} <= sent
    # a catch-up for the same (approved) plan needs no second approval
    plan, _ = targets(ctx)
    cid = next(iter(sent))
    fb.orders[cid]["status"] = "canceled"
    ctx.db.conn.execute("UPDATE broker_orders SET status='canceled' WHERE client_order_id=?", (cid,))
    DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=10)).run()
    catch_up = ctx.db.query("SELECT * FROM broker_orders WHERE origin='catch_up'")
    assert catch_up and all(o["approved_at"] and o["status"] != "planned" for o in catch_up)


def test_declined_plan_is_never_sent_even_in_auto_mode(tmp_path, no_background_cycle):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb, mode="approve")
    DailyCycle(ctx, fb, now_fn=lambda: now).run()
    with TestClient(create_app(ctx.settings, ctx=ctx)) as c:
        ap = c.get("/api/broker/overview").json()["awaiting_plan"]
        r = c.post(f"/api/broker/plans/{ap['plan_id']}/decline", json={"expected_orders": ap["orders"]})
        assert r.status_code == 200 and r.json()["declined"] == ap["orders"]
        assert no_background_cycle == []                                 # declining sends nothing
        assert c.put("/api/automation/rebalance-mode", json={"mode": "auto"}).json() == {"rebalance_mode": "auto"}
        assert c.put("/api/automation/rebalance-mode", json={"mode": "manual"}).status_code == 422
    DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=5)).run()
    assert fb.submits == []                                              # the declined plan is not regenerated
    assert ctx.db.scalar("SELECT COUNT(*) FROM broker_orders WHERE status='planned'") == 0


def test_auto_mode_sends_without_approval(tmp_path):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb, mode="auto")
    DailyCycle(ctx, fb, now_fn=lambda: now).run()
    assert fb.submits and all(o["client_order_id"].startswith("p") for o in fb.submits)
    assert ctx.db.scalar("SELECT COUNT(*) FROM broker_orders WHERE status_reason LIKE 'awaiting approval%'") == 0


def test_unknown_stored_mode_falls_back_to_approve(tmp_path):
    fb = FakeBroker()
    ctx, *_ = make_ctx(tmp_path, fb, mode="manual")      # e.g. a stale value from 0004
    assert rebalance_mode(ctx.db) == "approve"


def test_migration_0005_renames_setting_and_carries_decisions(tmp_path, monkeypatch):
    old = tmp_path / "upto_0004"
    old.mkdir()
    for f in sorted(dbmod.MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name[:4]) <= 4:
            shutil.copy(f, old / f.name)
    conn = sqlite3.connect(tmp_path / "m.db")
    with monkeypatch.context() as m:
        m.setattr(dbmod, "MIGRATIONS_DIR", old)
        dbmod.apply_migrations(conn)                      # a database as it was before this change
    assert conn.execute("SELECT value FROM app_settings WHERE key='rebalance_approval'").fetchone() == ("auto",)
    conn.execute("PRAGMA foreign_keys = OFF")             # decisions made under 0004 (plan rows not needed here)
    for cid, plan, status, approved in (("a", 7, "filled", "2026-01-05T12:00:00Z"), ("b", 8, "declined", None),
                                        ("c", 9, "planned", None)):
        conn.execute("INSERT INTO broker_orders (client_order_id, origin, plan_id, symbol, side, qty, order_type,"
                     " time_in_force, intended_session, status, approved_at, created_at, updated_at) VALUES"
                     " (?, 'rebalance', ?, 'AAA', 'buy', 1, 'market', 'opg', '2026-01-06', ?, ?, 'x', 'y')",
                     (cid, plan, status, approved))
    conn.commit()
    sql = (dbmod.MIGRATIONS_DIR / "0005_rebalance_mode.sql").read_text(encoding="utf-8")
    conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
    assert conn.execute("SELECT value FROM app_settings WHERE key='rebalance_mode'").fetchone() == ("approve",)
    assert conn.execute("SELECT COUNT(*) FROM app_settings WHERE key='rebalance_approval'").fetchone() == (0,)
    decisions = dict(conn.execute("SELECT plan_id, decision FROM broker_plan_decisions").fetchall())
    assert decisions == {7: "approved", 8: "declined"}    # plan 9 is still undecided
