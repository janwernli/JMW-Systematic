"""End-to-end API flow on a realistic fixture with a simulated Alpaca paper broker (FastAPI TestClient)."""

import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.backtest import factors as factors_mod
from app.backtest.engine import run_backtest
from app.config import Settings
from app.data.store import run_import
from app.services import AppContext
from app.strategy.config import StrategyConfig

from .helpers import rich_fixture
from .test_automation import FakeBroker


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cal, prov, series, sectors = rich_fixture()
    db = tmp_path_factory.mktemp("api") / "api.db"
    settings = Settings(_env_file=None, DATABASE_PATH=str(db), LOG_FORMAT="text", LOG_LEVEL="WARNING", SEC_USER_AGENT="")
    ctx = AppContext(settings, calendar=cal, provider=prov, broker=FakeBroker())
    run_import(ctx.db, prov, cal)
    with TestClient(create_app(settings, ctx=ctx)) as c:
        c.cal = cal
        yield c


def wait_run(client, run_id):
    for _ in range(300):
        r = client.get(f"/api/research/runs/{run_id}").json()
        if r["status"] in ("completed", "failed"):
            return r
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_status_and_quality(client):
    s = client.get("/api/status").json()
    assert s["mode"] == "live" and s["data_end"]
    q = client.get("/api/data/quality").json()
    assert q["has_data"] and q["eligible_count"] > 20
    d = client.get("/api/research/defaults").json()
    assert d["config"]["signal"] == "composite" and d["config"]["max_short_weight"] == 0.015
    assert d["config_warnings"] == []


def test_model_portfolio_plan_and_universe(client):
    cal = client.cal
    inception = cal.month_end_sessions("2021-06-01", "2021-12-31")[-1]
    r = client.post("/api/paper/init", json={"inception_session": inception, "config": {"min_adv_usd": 1e6}})
    assert r.status_code == 200, r.text
    plan = client.get("/api/rebalance/current").json()
    assert plan["status"] == "proposed" and plan["signal_session"] == inception
    rules = {c["rule"]: c for c in plan["checks"]}
    assert rules["Beta-neutral (ex-ante)"]["ok"] and rules["Gross exposure within cap"]["ok"]
    sn = plan["estimate"]["diagnostics"]["sector_neutrality"]
    assert sn["enabled"] and (sn["max_abs_net"] is None or sn["max_abs_net"] <= 0.02 + 1e-6 or not sn["applied"])
    assert client.post(f"/api/rebalance/plans/{plan['id']}/apply").status_code == 200
    adv = client.post("/api/paper/advance", json={"max_sessions": 5}).json()
    assert adv["processed_count"] == 5
    u = client.get("/api/universe").json()
    el = [r for r in u["rows"] if r["eligible"]]
    assert all(r["composite"] is not None for r in el) and all(r["sector"] for r in el)
    longs = [r for r in el if r["side"] == "long"]
    shorts = [r for r in el if r["side"] == "short"]
    assert longs and shorts and max(r["rank"] for r in longs) < min(r["rank"] for r in shorts)


def test_backtest_alpha_and_plain_12_1_comparison(client, monkeypatch):
    idx = [f"{y}{m:02d}" for y in range(2018, 2024) for m in range(1, 13)]
    rng = np.random.default_rng(0)
    fac = pd.DataFrame(rng.normal(0, 0.03, (len(idx), 6)), index=idx, columns=factors_mod.FACTORS)
    fac["RF"] = 0.001
    monkeypatch.setattr(factors_mod, "load_factors", lambda *a, **k: fac)
    runs = [client.post("/api/research/runs", json={"config": {"min_adv_usd": 1e6, "signal": sig}}).json()["id"]
            for sig in ("composite", "momentum_12_1")]
    done = [wait_run(client, i) for i in runs]
    assert all(r["status"] == "completed" for r in done), [r["error"] for r in done]
    assert done[0]["config"]["signal"] == "composite" and done[1]["config"]["signal"] == "momentum_12_1"
    alpha = client.get(f"/api/research/runs/{runs[0]}/alpha").json()
    assert alpha["full"]["months"] >= 24 and "t_alpha" in alpha["full"] and set(alpha["full"]["betas"]) == set(factors_mod.FACTORS)
    if alpha["full"]["months"] >= 48:
        assert alpha["first_half"] is not None and alpha["second_half"] is not None
    else:  # each half needs >= 24 months; shorter halves are reported as not estimable
        assert alpha["first_half"] is None and any("24 months" in n for n in alpha["notes"])


def test_broker_overview_and_automation_controls(client):
    assert client.put("/api/automation/enabled", json={"enabled": False}).json()["automation_enabled"] is False
    r = client.post("/api/automation/run", json={"dry_run": True}).json()
    assert r["started"]
    for _ in range(300):
        runs = client.get("/api/automation/runs").json()
        if runs and runs[0]["status"] != "running":
            break
        time.sleep(0.1)
    assert runs[0]["dry_run"] and runs[0]["trigger"] == "manual"
    ov = client.get("/api/broker/overview").json()
    assert ov["connected"] and ov["account"]["equity"] == 100_000 and ov["automation_enabled"] is False
    assert ov["trading_enabled_env"] is False
    assert client.get("/api/broker/orders").json()["total"] >= 0


def test_bad_config_is_rejected_with_message(client):
    r = client.post("/api/research/runs", json={"config": {"min_names_per_side": 0}})
    assert r.status_code == 422 and "min_names_per_side" in r.json()["message"]


def test_model_ledger_equals_backtest(client):
    """The model ledger (plans applied) must equal a backtest over the same window, to the cent."""
    ctx = client.app.state.ctx
    port = ctx.ledger.active()
    paper = {r["session"]: r["nav"] for r in ctx.db.query("SELECT session, nav FROM paper_nav WHERE portfolio_id=? "
                                                         "ORDER BY session", (port["id"],))}
    cfg = ctx.ledger.config_of(port).model_copy(update={"start_date": port["inception_session"],
                                                       "end_date": port["as_of_session"]})
    bt = run_backtest(ctx.panel(), ctx.calendar, cfg)
    assert list(bt.nav.index) == list(paper) and list(bt.nav["nav"]) == list(paper.values())


def test_portfolio_screens_respond(client):
    s = client.get("/api/portfolio/summary")
    assert s.status_code == 200 and s.json()["long_gross"] is not None
    p = client.get("/api/portfolio/positions")
    assert p.status_code == 200 and p.json()["rows"]
    assert any(r["side"] == "short" for r in p.json()["rows"])
    for path in ("/api/rebalance/plans", "/api/ledger/fills", "/api/ledger/orders", "/api/ledger/cash",
                 "/api/ledger/events", "/api/ledger/reproducibility", "/api/data/imports"):
        assert client.get(path).status_code == 200, path
