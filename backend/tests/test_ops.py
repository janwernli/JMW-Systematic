"""VM operations: ntfy notifications, SQLite backups with rotation, the migrate command, deploy files."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from app import notify
from app.__main__ import main
from app.automation import DailyCycle
from app.backup import backup_database
from app.config import REPO_ROOT, Settings
from app.db import Database

from .test_automation import FakeBroker, make_ctx


def settings(**kw):
    return Settings(_env_file=None, **kw)


class Capture:
    def __init__(self, status=200):
        self.requests: list[httpx.Request] = []
        self.status = status

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        return httpx.Response(self.status)


# ---------------------------------------------------------------- ntfy
def test_notifications_are_off_without_topic():
    cap = Capture()
    assert notify.send(settings(), "hello", transport=cap.transport()) is False
    assert cap.requests == []


def test_send_posts_to_topic_with_headers_and_token():
    cap = Capture()
    s = settings(NTFY_TOPIC="jmw-abc123", NTFY_SERVER="https://ntfy.example.org/", NTFY_TOKEN="tk_secret")
    assert notify.send(s, "Equity $1 — ok", title="JMW daily ok", priority="low", tags=("chart",),
                       transport=cap.transport())
    (r,) = cap.requests
    assert str(r.url) == "https://ntfy.example.org/jmw-abc123" and r.method == "POST"
    assert r.headers["Title"] == "JMW daily ok" and r.headers["Priority"] == "low" and r.headers["Tags"] == "chart"
    assert r.headers["Authorization"] == "Bearer tk_secret"
    assert r.content.decode("utf-8") == "Equity $1 — ok"


def test_send_never_raises_on_failure():
    assert notify.send(settings(NTFY_TOPIC="t"), "x", transport=Capture(status=500).transport()) is False

    def boom(request):
        raise httpx.ConnectError("offline")
    assert notify.send(settings(NTFY_TOPIC="t"), "x", transport=httpx.MockTransport(boom)) is False


def result(status="ok", fresh=True, sent=3, planned=0, skipped=1, awaiting=0, extra_steps=()):
    return {"run_id": 42, "status": status, "steps": [
        {"name": "data", "status": "ok" if fresh else "error", "detail": "import ok" if fresh else "STALE",
         "data": {"latest": "2026-10-01", "expected": "2026-10-01" if fresh else "2026-10-02", "fresh": fresh}},
        {"name": "sync", "status": "ok", "detail": "", "data": {"equity": 101234.5, "longs": 59, "shorts": 50}},
        {"name": "orders", "status": "ok", "detail": "",
         "data": {"sent": sent, "planned": planned, "skipped": skipped, "awaiting": awaiting}},
        *extra_steps]}


def test_daily_summary_contents():
    title, msg, prio, _ = notify.daily_summary(result(), "schedule")
    assert title == "JMW daily ok" and prio == "default"
    assert "Orders: 3 sent, 0 planned, 1 skipped" in msg
    assert "Equity $101,234.50 | 59 long / 50 short" in msg and "Run #42 (schedule)" in msg


def test_daily_summary_alerts_on_stale_data_errors_and_awaiting_approval():
    title, msg, prio, _ = notify.daily_summary(result(status="error", fresh=False, sent=0))
    assert title == "JMW daily STALE DATA" and prio == "high" and "STALE (expected 2026-10-02)" in msg
    boom = {"name": "fatal", "status": "error", "detail": "RuntimeError: x"}
    title, msg, prio, _ = notify.daily_summary(result(status="error", extra_steps=[boom]))
    assert title == "JMW daily ERROR" and prio == "high" and "ERROR fatal: RuntimeError: x" in msg
    title, msg, prio, _ = notify.daily_summary(result(sent=1, awaiting=96), dry_run=True)
    assert title == "JMW daily DRY RUN - plan awaiting approval" and prio == "high"
    assert "96 awaiting approval" in msg and "Trading page" in msg


def test_daily_cycle_sends_a_summary(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("app.automation.notify_daily",
                        lambda s, res, trigger, dry_run: sent.append((res, trigger, dry_run)))
    fb = FakeBroker()
    ctx, cal, latest, fill, now = make_ctx(tmp_path, fb)
    res = DailyCycle(ctx, fb, now_fn=lambda: now).run(trigger="schedule")
    assert len(sent) == 1 and sent[0][0]["run_id"] == res["run_id"] and sent[0][1] == "schedule"
    sync = next(s for s in res["steps"] if s["name"] == "sync")
    assert sync["data"] == {"equity": 100_000.0, "longs": 0, "shorts": 0}


# ---------------------------------------------------------------- backup
def make_db(path: Path):
    db = Database(path)
    with db.transaction() as conn:
        conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('probe', 'x', 'now')")
    return db


def test_backup_is_consistent_dated_and_rotated(tmp_path):
    db = make_db(tmp_path / "momentum.db")
    out = tmp_path / "backups"
    out.mkdir()
    (out / "unrelated.txt").write_text("keep me")
    for i in range(20):
        backup_database(db.path, out, keep=14, today=date(2026, 1, 1) + timedelta(days=i))
    names = sorted(p.name for p in out.iterdir())
    dated = [n for n in names if n.startswith("momentum-")]
    assert len(dated) == 14 and dated[0] == "momentum-2026-01-07.db" and dated[-1] == "momentum-2026-01-20.db"
    assert "unrelated.txt" in names and not any(n.endswith(".partial") for n in names)
    con = sqlite3.connect(out / dated[-1])
    assert con.execute("SELECT value FROM app_settings WHERE key='probe'").fetchone() == ("x",)
    assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] >= 5


def test_backup_same_day_replaces_and_works_while_db_is_open(tmp_path):
    db = make_db(tmp_path / "momentum.db")
    with db.transaction() as conn:           # an open writer connection (WAL) does not block the backup
        conn.execute("UPDATE app_settings SET value='y' WHERE key='probe'")
    out = tmp_path / "b"
    backup_database(db.path, out, today=date(2026, 3, 1))
    with db.transaction() as conn:
        conn.execute("UPDATE app_settings SET value='z' WHERE key='probe'")
    res = backup_database(db.path, out, today=date(2026, 3, 1))
    assert res["removed"] == [] and len(list(out.glob("momentum-*.db"))) == 1
    assert sqlite3.connect(res["backup"]).execute("SELECT value FROM app_settings WHERE key='probe'").fetchone() == ("z",)


def test_backup_rejects_missing_db_and_bad_keep(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "nope.db", tmp_path / "b")
    make_db(tmp_path / "m.db")
    with pytest.raises(ValueError):
        backup_database(tmp_path / "m.db", tmp_path / "b", keep=0)


def test_backup_and_migrate_commands(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cli.db"
    monkeypatch.setattr("app.config.get_settings", lambda: settings(DATABASE_PATH=str(path)))
    assert main(["migrate"]) == 0
    assert "applied 0001_init" in capsys.readouterr().out
    assert main(["migrate"]) == 0
    assert "schema up to date" in capsys.readouterr().out
    assert main(["backup", "--dir", str(tmp_path / "bk"), "--keep", "3"]) == 0
    assert len(list((tmp_path / "bk").glob("momentum-*.db"))) == 1
    alerts = []
    monkeypatch.setattr("app.notify.alert", lambda s, what, detail: alerts.append(what))
    path.unlink()
    assert main(["backup", "--dir", str(tmp_path / "bk")]) == 1 and alerts == ["backup"]


# ---------------------------------------------------------------- deploy files
DEPLOY = REPO_ROOT / "deploy"


def test_backend_unit_binds_localhost_only_with_one_worker():
    unit = (DEPLOY / "systemd/jmw-backend.service").read_text(encoding="utf-8")
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "--host 127.0.0.1" in exec_line and "--port 8765" in exec_line and "--workers 1" in exec_line
    directives = "\n".join(line for line in unit.splitlines() if not line.startswith("#"))
    assert "0.0.0.0" not in directives and "Restart=always" in directives and "MemoryMax=" in directives


def test_daily_timer_runs_weekdays_new_york_time():
    timer = (DEPLOY / "systemd/jmw-daily.timer").read_text(encoding="utf-8")
    cal = [line.split("=", 1)[1] for line in timer.splitlines() if line.startswith("OnCalendar=")]
    assert cal == ["Mon..Fri *-*-* 08:00:00 America/New_York", "Mon..Fri *-*-* 17:30:00 America/New_York"]
    assert "Persistent=true" in timer
    svc = (DEPLOY / "systemd/jmw-daily.service").read_text(encoding="utf-8")
    assert "-m app daily --trigger schedule" in svc


def test_backup_timer_and_scripts_present():
    timer = (DEPLOY / "systemd/jmw-backup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=" in timer and "Persistent=true" in timer
    assert "-m app backup" in (DEPLOY / "systemd/jmw-backup.service").read_text(encoding="utf-8")
    for script in ("setup.sh", "update.sh"):
        text = (DEPLOY / script).read_bytes()
        assert text.startswith(b"#!/usr/bin/env bash") and b"\r\n" not in text and b"set -euo pipefail" in text


def test_env_example_lists_vm_variables_with_safe_defaults():
    env = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in env.splitlines() if line and not line.startswith("#") and "=" in line)
    for key in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "ALPACA_DATA_FEED", "SEC_USER_AGENT",
                "BROKER_TRADING_ENABLED", "NTFY_TOPIC", "APP_HOST", "APP_PORT", "MARKET_DATA_PROVIDER"):
        assert key in values, key
    assert values["BROKER_TRADING_ENABLED"] == "false" and values["ALPACA_DATA_FEED"] == "sip"
    assert values["APP_HOST"] == "127.0.0.1" and values["ALPACA_API_SECRET_KEY"] == "" and values["NTFY_TOPIC"] == ""
