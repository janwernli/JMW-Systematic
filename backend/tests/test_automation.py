"""Fully automatic paper-trading cycle against a simulated Alpaca PAPER broker."""

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.automation import DailyCycle, set_setting, submission_mode
from app.broker.alpaca_paper import AlpacaPaperBroker, BrokerError
from app.config import Settings
from app.services import AppContext

from .helpers import FixtureProvider, rich_fixture

NY = ZoneInfo("America/New_York")


class FakeBroker:
    def __init__(self, equity=100_000.0, positions=None, not_etb=()):
        self.equity = equity
        self.pos = dict(positions or {})          # symbol -> (qty, avg_entry)
        self.orders: dict[str, dict] = {}
        self.submits: list[dict] = []
        self.not_etb = set(not_etb)

    def account(self):
        return {"equity": str(self.equity), "cash": str(self.equity), "long_market_value": "0", "short_market_value": "0",
                "buying_power": str(4 * self.equity), "regt_buying_power": str(2 * self.equity), "multiplier": "4",
                "shorting_enabled": True, "status": "ACTIVE"}

    def positions(self):
        return [{"symbol": s, "qty": q, "avg_entry_price": e, "market_value": q * e, "current_price": e,
                 "unrealized_pl": 0.0} for s, (q, e) in self.pos.items()]

    def clock(self):
        return {"is_open": False}

    def portfolio_history(self):
        return []

    def asset(self, symbol):
        return {"symbol": symbol, "shortable": True, "easy_to_borrow": symbol not in self.not_etb}

    def order_by_client_id(self, cid):
        return self.orders.get(cid)

    def submit_order(self, symbol, qty, side, time_in_force, client_order_id):
        if client_order_id in self.orders:
            return self.orders[client_order_id]
        o = {"id": f"b{len(self.orders)}", "status": "accepted", "symbol": symbol, "qty": str(qty), "side": side,
             "time_in_force": time_in_force, "client_order_id": client_order_id, "filled_qty": "0"}
        self.orders[client_order_id] = o
        self.submits.append(o)
        return o


def month_end_fixture():
    cal, prov, series, sectors = rich_fixture()
    m = max(cal.index(s) for s in cal.month_end_sessions("2019-01-01", cal.sessions[1040]))
    cut = {k: {kk: vv[: m + 1] for kk, vv in d.items()} for k, d in series.items()}
    prov2 = FixtureProvider(cal, cut, benchmark="SPY", sectors=sectors, etfs={s for s in series if s.startswith("XL")})
    return cal, prov2, cal.sessions[m]


def make_ctx(tmp_path, broker, enabled=True, mode="auto"):
    """Most tests exercise sending; the approve-mode default is covered in test_approval.py."""
    cal, prov, latest = month_end_fixture()
    s = Settings(_env_file=None, DATABASE_PATH=str(tmp_path / "auto.db"), BROKER_TRADING_ENABLED=str(enabled).lower(),
                 SEC_USER_AGENT="", LOG_LEVEL="WARNING", LOG_FORMAT="text")
    ctx = AppContext(s, calendar=cal, provider=prov, broker=broker)
    if mode is not None:
        set_setting(ctx.db, "rebalance_mode", mode)
    fill = cal.next_session(latest)
    now = datetime.fromisoformat(fill).replace(hour=8, tzinfo=NY).astimezone(UTC)   # 08:00 ET on the fill day
    return ctx, cal, latest, fill, now


def targets(ctx):
    plan = ctx.db.query_one("SELECT * FROM rebalance_plans ORDER BY id DESC LIMIT 1")
    rows = ctx.db.query("SELECT symbol, target_weight FROM signal_rows WHERE set_id=? AND selected=1",
                        (plan["signal_set_id"],))
    return plan, {r["symbol"]: r["target_weight"] for r in rows}


def test_month_end_rebalance_sent_as_opening_auction_orders_sized_from_equity(tmp_path):
    fb = FakeBroker(not_etb={"S001"})
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb)
    res = DailyCycle(ctx, fb, now_fn=lambda: now).run(trigger="test")
    assert res["status"] in ("ok", "warning"), res
    plan, tw = targets(ctx)
    assert plan["signal_session"] == latest and plan["status"] == "applied"
    panel = ctx.panel()
    t = panel.sess_index[latest]
    assert fb.submits and all(o["time_in_force"] == "opg" for o in fb.submits)
    for o in fb.submits:
        w = tw[o["symbol"]]
        close = panel.close[t, panel.sym_index[o["symbol"]]]
        assert int(o["qty"]) == abs(math.trunc(w * 100_000 / close))
        assert o["side"] == ("buy" if w > 0 else "sell")
    rows = {r["symbol"]: r for r in ctx.db.query("SELECT * FROM broker_orders")}
    if "S001" in tw and tw["S001"] < 0:
        assert rows["S001"]["status"] == "skipped" and "easy-to-borrow" in rows["S001"]["status_reason"]
    assert all(r["intended_session"] == fill for r in rows.values())
    # a second run the same morning sends nothing new (orders still working at the broker)
    n = len(fb.submits)
    DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(minutes=5)).run(trigger="test")
    assert len(fb.submits) == n


def test_dry_run_and_kill_switches_send_nothing(tmp_path):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb)
    DailyCycle(ctx, fb, now_fn=lambda: now).run(dry_run=True)
    assert fb.submits == [] and ctx.db.scalar("SELECT COUNT(*) FROM broker_orders WHERE status='planned'") > 0
    set_setting(ctx.db, "automation_enabled", "false")
    DailyCycle(ctx, fb, now_fn=lambda: now).run()
    assert fb.submits == []
    set_setting(ctx.db, "automation_enabled", "true")
    DailyCycle(ctx, fb, now_fn=lambda: now).run()                  # planned drafts are now sent under the same ids
    assert fb.submits and ctx.db.scalar("SELECT COUNT(*) FROM broker_orders WHERE status='planned'") == 0


def test_env_flag_off_never_trades(tmp_path):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb, enabled=False)
    DailyCycle(ctx, fb, now_fn=lambda: now).run()
    assert fb.submits == []


def test_stale_data_blocks_orders(tmp_path):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb)
    res = DailyCycle(ctx, fb, now_fn=lambda: now + timedelta(days=20)).run()
    assert res["status"] == "error" and fb.submits == []
    assert any(s["name"] == "orders" and s["status"] == "skipped" for s in res["steps"])


def test_stop_loss_and_flip_on_actual_positions(tmp_path):
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb)
    DailyCycle(ctx, fb, now_fn=lambda: now).run(dry_run=True)
    plan, tw = targets(ctx)
    panel = ctx.panel()
    t = panel.sess_index[latest]
    close = lambda s: float(panel.close[t, panel.sym_index[s]])  # noqa: E731
    long_t = next(s for s, w in tw.items() if w > 0)
    short_t = next(s for s, w in tw.items() if w < 0)
    # actual paper account: a squeezed short in the long target (close is 60% above entry) and a long in a short target
    fb.pos = {long_t: (-10, close(long_t) / 1.6), short_t: (25, close(short_t))}
    later = now + timedelta(minutes=10)
    DailyCycle(ctx, fb, now_fn=lambda: later).run()
    sent = {o["client_order_id"]: o for o in fb.submits}
    stop = next(o for c, o in sent.items() if c.startswith("stop-"))
    assert stop["symbol"] == long_t and stop["side"] == "buy" and stop["qty"] == "10"
    flip = next(o for o in fb.submits if o["symbol"] == short_t)
    assert flip["side"] == "sell" and flip["qty"] == "25"            # close the long only; short opens next run
    assert not any(o["symbol"] == long_t and not o["client_order_id"].startswith("stop-") for o in fb.submits)


@pytest.mark.parametrize("et,fill,is_open,expected", [
    ("2026-09-30 18:00", "2026-10-01", False, None),          # 09:28-19:00 ET: OPG would be rejected
    ("2026-09-30 19:30", "2026-10-01", False, "opg"),
    ("2026-10-01 09:00", "2026-10-01", False, "opg"),
    ("2026-10-01 09:29", "2026-10-01", False, None),          # after the auction cutoff, market not yet open
    ("2026-10-01 09:45", "2026-10-01", True, "day"),          # late: regular-hours market order
    ("2026-10-02 10:00", "2026-10-01", True, None),           # fill day passed
])
def test_submission_window(et, fill, is_open, expected):
    now = datetime.fromisoformat(et).replace(tzinfo=NY)
    assert submission_mode(now, fill, is_open)[0] == expected


def test_broker_refuses_live_endpoint():
    with pytest.raises(BrokerError, match="PAPER"):
        AlpacaPaperBroker("k", "s", "https://api.alpaca.markets")
    AlpacaPaperBroker("k", "s", "https://paper-api.alpaca.markets")
