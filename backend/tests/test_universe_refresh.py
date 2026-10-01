"""Live universe refresh at month-end: top N by trailing 60-session dollar volume as of the signal date (no future
data), held names kept until exited, new symbols backfilled before signals, changes logged, idempotent."""

from datetime import UTC, datetime

import numpy as np
import pytest

from app.automation import DailyCycle
from app.config import Settings
from app.data.provider import top_by_dollar_volume
from app.services import AppContext
from app.strategy.config import StrategyConfig
from app.strategy.signals import compute_signals

from .helpers import FixtureProvider, weekday_calendar
from .test_automation import NY, FakeBroker

CFG = dict(signal="momentum_12_1", min_names_per_side=1, max_names_per_side=2, long_pct=0.2, short_pct=0.2,
           buffer_exit_pct=0.3, lookback_sessions=60, skip_sessions=5, min_history_sessions=60, adv_window=5,
           min_adv_usd=0, min_price=1, short_min_price=0, htb_exclude_pct=0, sector_neutral=False, crash_guard=False,
           vol_lookback_sessions=20, beta_lookback_sessions=60, max_long_weight=1, max_short_weight=1)


def build(cal, m, extra_after=20):
    """10 stocks; month-end at session index m. Volumes (shares) are set so the top 6 as of m is C..H."""
    T = m + 1 + extra_after
    rng = np.random.default_rng(1)
    idx = np.arange(T)
    vols = {
        "A": np.where(idx < m - 80, 2e6, 1e3), "B": np.where(idx < m - 80, 2e6, 1e3),   # dry up before the window
        "C": np.full(T, 1e6), "D": np.full(T, 1e6), "E": np.full(T, 1e6), "F": np.full(T, 1e6),
        "G": np.where(idx < m - 70, 1e3, 5e6), "H": np.where(idx < m - 70, 1e3, 5e6),   # become liquid
        "I": np.where(idx <= m, 1e3, 9e9),                                             # liquid only AFTER m
        "J": np.full(T, 1e3),
    }
    series = {"SPY": {"close": 300 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, T))), "volume": np.full(T, 5e7)}}
    for s, v in vols.items():
        series[s] = {"close": np.round(50 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, T))), 4), "volume": v}
    for d in series.values():
        d["open"] = np.round(d["close"] * 0.999, 4)
    return series


def month_end_index(cal, near):
    return max(i for i in range(near) if cal.sessions[i + 1][:7] != cal.sessions[i][:7])


@pytest.fixture
def setup(tmp_path):
    cal = weekday_calendar("2019-01-01", 420)
    m = month_end_index(cal, 330)
    prov = FixtureProvider(cal, build(cal, m), benchmark="SPY", universe=list("ABCDEF"), reselect=True)
    s = Settings(_env_file=None, DATABASE_PATH=str(tmp_path / "u.db"), SEC_USER_AGENT="", LOG_LEVEL="WARNING",
                 LOG_FORMAT="text")
    ctx = AppContext(s, calendar=cal, provider=prov, broker=None)
    ctx.refresh_data()
    return ctx, cal, m


def symbols_with_bars(ctx):
    return {r["symbol"] for r in ctx.db.query(
        "SELECT DISTINCT i.symbol FROM instruments i JOIN bars b ON b.instrument_id=i.id")}


def test_top_by_dollar_volume_uses_only_data_up_to_asof(setup):
    ctx, cal, m = setup
    bars = ctx.provider._bars
    d = cal.sessions[m]
    top = top_by_dollar_volume(bars[bars["symbol"] != "SPY"], d, 6)
    assert set(top) == set("CDEFGH") and "I" not in top          # I's volume spike is after the month-end
    later = top_by_dollar_volume(bars[bars["symbol"] != "SPY"], cal.sessions[m + 15], 6)
    assert "I" in later                                           # ... and does count once it is in the past


def test_refresh_adds_removes_keeps_held_and_backfills(setup):
    ctx, cal, m = setup
    d, prev = cal.sessions[m], cal.sessions[m - 1]
    assert symbols_with_bars(ctx) == set("ABCDEF") | {"SPY"}
    with ctx.db.transaction() as conn:                           # B is held in the Alpaca account
        conn.execute("INSERT INTO broker_positions (as_of, symbol, qty) VALUES ('2030-01-01T00:00:00Z', 'B', 10)")
    v0 = ctx.data_version()
    res = ctx.refresh_universe(d, n=6)
    assert res["added"] == ["G", "H"] and res["removed"] == ["A"] and res["kept_held"] == ["B"]
    assert res["seeded"] and res["size"] == 7 and set(res["backfilled"]) == {"G", "H"}
    assert {"G", "H"} <= symbols_with_bars(ctx) and "I" not in symbols_with_bars(ctx)
    first = ctx.db.scalar("SELECT MIN(start_session) FROM universe_membership")
    assert ctx.db.scalar("SELECT MIN(session) FROM bars b JOIN instruments i ON i.id=b.instrument_id "
                         "WHERE i.symbol='G'") == cal.sessions[0]                         # full history backfilled
    mem = {(r["symbol"], r["start_session"]): r["end_session"] for r in ctx.db.query(
        "SELECT i.symbol, m.start_session, m.end_session FROM universe_membership m JOIN instruments i "
        "ON i.id=m.instrument_id")}
    assert mem[("A", first)] == prev and mem[("B", first)] is None and mem[("C", first)] is None
    assert mem[("G", d)] is None and mem[("H", d)] is None
    # the cached panel is rebuilt with the new membership (point in time)
    assert ctx.data_version() != v0
    panel = ctx.panel()
    col = panel.sym_index
    t = panel.sess_index[d]
    assert panel.membership[t, col["G"]] and not panel.membership[t - 1, col["G"]]
    assert not panel.membership[t, col["A"]] and panel.membership[t - 1, col["A"]]
    assert panel.membership[:t, [col[s] for s in "ABCDEF"]].all()          # history unchanged for the old universe
    sig = compute_signals(panel, t, StrategyConfig(**CFG))
    reason = dict(zip(sig.table["symbol"], sig.table["reason"]))
    assert reason["A"] == "not_in_index" and reason["G"] == "eligible" and reason["B"] == "eligible"
    # logged with the symbol lists
    msg = ctx.db.scalar("SELECT message FROM system_events WHERE category='universe' ORDER BY id DESC LIMIT 1")
    assert f"Universe refresh as of {d}" in msg and "added 2 ['G', 'H']" in msg and "removed 1 ['A']" in msg
    assert "kept 1 held" in msg
    # idempotent for the same month-end
    again = ctx.refresh_universe(d, n=6)
    assert again["already_done"] and ctx.db.scalar("SELECT COUNT(*) FROM universe_selections") == 1


def test_next_month_drops_exited_names(setup):
    ctx, cal, m = setup
    with ctx.db.transaction() as conn:
        conn.execute("INSERT INTO broker_positions (as_of, symbol, qty) VALUES ('2030-01-01T00:00:00Z', 'B', 10)")
    ctx.refresh_universe(cal.sessions[m], n=6)
    with ctx.db.transaction() as conn:                           # B exited
        conn.execute("INSERT INTO broker_positions (as_of, symbol, qty) VALUES ('2030-02-01T00:00:00Z', 'C', 5)")
    nxt = month_end_index(cal, m + 25)
    res = ctx.refresh_universe(cal.sessions[nxt], n=6)
    assert "B" in res["removed"] and not res["seeded"]
    assert "I" in res["added"]                                   # its liquidity is now in the trailing window


def test_daily_cycle_refreshes_universe_before_the_month_end_plan(tmp_path):
    cal = weekday_calendar("2019-01-01", 420)
    m = month_end_index(cal, 330)
    prov = FixtureProvider(cal, build(cal, m, extra_after=0), benchmark="SPY", universe=list("ABCDEF"), reselect=True)
    s = Settings(_env_file=None, DATABASE_PATH=str(tmp_path / "d.db"), SEC_USER_AGENT="", LOG_LEVEL="WARNING",
                 LOG_FORMAT="text", BROKER_TRADING_ENABLED="false")
    ctx = AppContext(s, calendar=cal, provider=prov, broker=FakeBroker())
    ctx.settings.alpaca_max_symbols = 6
    fill = cal.next_session(cal.sessions[m])
    now = datetime.fromisoformat(fill).replace(hour=8, tzinfo=NY).astimezone(UTC)
    res = DailyCycle(ctx, ctx.broker, now_fn=lambda: now).run(dry_run=True)
    names = [st["name"] for st in res["steps"]]
    assert names.index("universe") < names.index("model")       # before the month-end plan is formed
    uni = next(st for st in res["steps"] if st["name"] == "universe")
    assert uni["data"]["added"] == ["G", "H"] and "A" in uni["data"]["removed"]
    plan = ctx.db.query_one("SELECT * FROM rebalance_plans ORDER BY id DESC LIMIT 1")
    rows = {r["symbol"]: r for r in ctx.db.query("SELECT symbol, eligible, reason, selected FROM signal_rows "
                                                  "WHERE set_id=?", (plan["signal_set_id"],))}
    assert rows["A"]["reason"] == "not_in_index" and not rows["A"]["selected"]
    assert rows["G"]["eligible"] == 1
    # a second run the same day does not re-select
    res2 = DailyCycle(ctx, ctx.broker, now_fn=lambda: now).run(dry_run=True)
    assert "universe" not in [st["name"] for st in res2["steps"]]   # ledger already processed the month-end


def test_fixed_universe_providers_skip_reselection(tmp_path):
    cal = weekday_calendar("2019-01-01", 420)
    m = month_end_index(cal, 330)
    prov = FixtureProvider(cal, build(cal, m), benchmark="SPY")          # reselect=False
    ctx = AppContext(Settings(_env_file=None, DATABASE_PATH=str(tmp_path / "f.db"), SEC_USER_AGENT=""),
                     calendar=cal, provider=prov)
    ctx.refresh_data()
    assert ctx.refresh_universe(cal.sessions[m]) is None
    assert ctx.db.scalar("SELECT COUNT(*) FROM universe_membership") == 0       # nothing seeded
