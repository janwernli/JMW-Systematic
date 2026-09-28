"""SQLite access: connection factory, versioned migrations and small helpers."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply any not-yet-applied `NNNN_name.sql` files in order. Idempotent."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    done: list[str] = []
    for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(file.name.split("_", 1)[0])
        if version in applied:
            continue
        sql = file.read_text(encoding="utf-8")
        # executescript commits implicitly; wrap in an explicit transaction for atomicity.
        conn.executescript(
            "BEGIN;\n" + sql + f"\nINSERT INTO schema_migrations VALUES ({version}, '{file.stem}', '{utcnow()}');\nCOMMIT;"
        )
        log.info("applied migration", extra={"migration": file.stem})
        done.append(file.stem)
    return done


class Database:
    """Thread-aware wrapper: one connection per thread, shared file.

    Writes are serialized with a process-wide lock so that multi-statement
    ledger transactions never interleave.
    """

    def __init__(self, path: Path | str):
        self.path = path
        self._local = threading.local()
        self.write_lock = threading.RLock()
        with self.write_lock:
            apply_migrations(self.conn)

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = _connect(self.path)
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.write_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None


def log_event(
    conn: sqlite3.Connection,
    level: str,
    category: str,
    message: str,
    *,
    portfolio_id: int | None = None,
    run_id: int | None = None,
    session: str | None = None,
    payload: dict | None = None,
) -> None:
    """Append an immutable audit record (and mirror it to the structured log)."""
    conn.execute(
        "INSERT INTO system_events (ts, level, category, message, portfolio_id, run_id, session, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (utcnow(), level, category, message, portfolio_id, run_id, session, json.dumps(payload or {}, default=str)),
    )
    getattr(log, "warning" if level == "warning" else level)(
        message, extra={"category": category, "portfolio_id": portfolio_id, "run_id": run_id, "session": session}
    )
