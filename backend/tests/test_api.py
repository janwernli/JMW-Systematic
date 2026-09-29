"""End-to-end API flow on the seeded DEMO data set (FastAPI TestClient, temp database)."""

import time

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.backtest.engine import run_backtest
from app.config import Settings
from app.strategy.config import StrategyConfig


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp("api") / "api.db"
    settings = Settings(_env_file=None, DATABASE_PATH=str(db), MARKET_DATA_PROVIDER="demo", DEMO_AUTOSEED="true",
                        LOG_FORMAT="text", LOG_LEVEL="WARNING")
    with TestClient(create_app(settings)) as c:
        yield c


def test_status_and_quality_are_labelled_demo(client):
    s = client.get("/api/status").json()
    assert s["mode"] == "demo" and s["data_label"] == "Demo Data"
    assert s["data_end"] == "2026-09-25" and s["has_portfolio"]
    q = client.get("/api/data/quality").json()
    assert q["has_data"] and not q["is_stale"] and q["eligible_count"] > 50
    assert any("DEMO" in w["message"] for w in q["warnings"])


def test_seeded_portfolio_waits_for_august_decision(client):
    cc = client.get("/api/portfolio/summary").json()
    assert cc["data_label"] == "Paper Simulation"
    assert cc["valuation_session"] == "2026-08-31"
    assert cc["nav"] == pytest.approx(cc["cash"] + cc["invested"], abs=0.02)
    plan = client.get("/api/rebalance/current").json()
    assert plan["status"] == "proposed" and plan["signal_session"] == "2026-08-31"
    assert plan["fill_session"] == "2026-09-01" and plan["can_apply"]
    assert all(c["ok"] for c in plan["checks"])
    adv = client.post("/api/paper/advance", json={}).json()
    assert adv["stopped_reason"] == "decision_required" and adv["processed_count"] == 0


def test_apply_once_then_advance_to_latest(client):
    plan = client.get("/api/rebalance/current").json()
    r = client.post(f"/api/rebalance/plans/{plan['id']}/apply")
    assert r.status_code == 200 and r.json()["status"] == "applied"
    dup = client.post(f"/api/rebalance/plans/{plan['id']}/apply")
    assert dup.status_code == 409
    adv = client.post("/api/paper/advance", json={}).json()
    assert adv["as_of"] == "2026-09-25" and adv["stopped_reason"] == "no_newer_data"
    done = client.get(f"/api/rebalance/plans/{plan['id']}").json()
    assert done["status"] == "executed" and done["fills"]
    assert {f["session"] for f in done["fills"]} == {"2026-09-01"}
    pos = client.get("/api/portfolio/positions").json()
    assert pos["valuation_session"] == "2026-09-25" and 0 < len(pos["rows"]) <= 200
    # long-short default: signed weights (shorts negative) plus cash still sum to NAV
    assert any(r["shares"] < 0 for r in pos["rows"]) and any(r["shares"] > 0 for r in pos["rows"])
    assert sum(r["weight"] for r in pos["rows"]) + pos["cash_weight"] == pytest.approx(1.0, abs=1e-6)


def test_universe_and_stock_detail(client):
    u = client.get("/api/universe").json()
    longs = [r for r in u["rows"] if r["selected"] and r["model_weight"] > 0]
    shorts = [r for r in u["rows"] if r["selected"] and r["model_weight"] < 0]
    assert u["signal_session"] == "2026-09-25" and u["selected_count"] == len(longs) + len(shorts)
    assert 50 <= len(longs) <= 100 and 50 <= len(shorts) <= 100
    assert max(r["rank"] for r in longs) < min(r["rank"] for r in shorts)   # winners long, losers short
    assert all(r["close_raw"] > 10 for r in shorts)                         # short price floor
    top = next(r for r in u["rows"] if r["rank"] == 1)
    d = client.get(f"/api/universe/{top['symbol']}").json()
    sig = d["signal"]
    assert sig["momentum"] == pytest.approx(sig["tr_t21"] / sig["tr_t252"] - 1)
    assert d["prices"][-1]["session"] == "2026-09-25"
    assert client.get("/api/universe/NOPE").status_code == 404


def test_backtest_launch_and_results(client):
    cfg = StrategyConfig(top_n=20, start_date="2019-01-02", end_date="2024-12-31").model_dump()
    run = client.post("/api/research/runs", json={"config": cfg, "name": "t"}).json()
    for _ in range(100):
        r = client.get(f"/api/research/runs/{run['id']}").json()
        if r["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert r["status"] == "completed", r.get("error")
    assert r["metrics"]["cagr_meaningful"] and r["repro"]["deterministic"]
    series = client.get(f"/api/research/runs/{run['id']}/series").json()
    assert series[0]["session"] >= "2019-01-02" and series[-1]["session"] <= "2024-12-31"
    monthly = client.get(f"/api/research/runs/{run['id']}/monthly").json()
    assert len(monthly) == 72
    trades = client.get(f"/api/research/runs/{run['id']}/trades?limit=5").json()
    assert trades["total"] > 0 and len(trades["rows"]) == 5
    # identical inputs -> identical results
    run2 = client.post("/api/research/runs", json={"config": cfg}).json()
    for _ in range(100):
        r2 = client.get(f"/api/research/runs/{run2['id']}").json()
        if r2["status"] == "completed":
            break
        time.sleep(0.1)
    assert r2["metrics"] == r["metrics"]


def test_bad_backtest_config_is_rejected_with_message(client):
    r = client.post("/api/research/runs", json={"config": {"top_n": 0}})
    assert r.status_code == 422 and "top_n" in r.json()["message"]


def test_ledger_and_reproducibility(client):
    assert client.get("/api/ledger/fills").json()["total"] > 0
    cash = client.get("/api/ledger/cash?limit=2000").json()
    assert any(r["kind"] == "initial_deposit" for r in cash["rows"])
    assert client.get("/api/ledger/events").json()["total"] > 0
    rep = client.get("/api/ledger/reproducibility").json()
    assert rep["data_version"].startswith("demo-2026-09-25")


def test_demo_paper_ledger_equals_backtest(client):
    """The seeded paper portfolio (inception 2025-12-31, plans auto-applied) must match a backtest exactly."""
    ctx = client.app.state.ctx
    pid = ctx.ledger.active()["id"]
    paper = {r["session"]: r["nav"] for r in ctx.db.query(
        "SELECT session, nav FROM paper_nav WHERE portfolio_id=? AND session<='2026-08-31' ORDER BY session", (pid,))}
    bt = run_backtest(ctx.panel(), ctx.calendar, StrategyConfig(start_date="2025-12-31", end_date="2026-08-31"))
    assert list(bt.nav.index) == list(paper)
    assert list(bt.nav["nav"]) == list(paper.values())
