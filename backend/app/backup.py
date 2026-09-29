"""Consistent SQLite backups via the online backup API (safe while the server and timers are running).

    python -m app backup [--dir ~/backups] [--keep 14]

Writes <dir>/momentum-YYYY-MM-DD.db (a same-day re-run replaces that day's file atomically), verifies it
with PRAGMA quick_check and keeps the newest `keep` dated backups.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import date
from pathlib import Path

NAME = re.compile(r"^momentum-(\d{4}-\d{2}-\d{2})\.db$")


def backup_database(db_path: Path, backup_dir: Path, keep: int = 14, today: date | None = None) -> dict:
    db_path, backup_dir = Path(db_path), Path(backup_dir).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    if keep < 1:
        raise ValueError("keep must be >= 1")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (today or date.today()).isoformat()
    target = backup_dir / f"momentum-{stamp}.db"
    tmp = backup_dir / f".momentum-{stamp}.db.partial"
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        with dst:
            src.backup(dst, pages=4096)   # copies in steps; readers/writers are not blocked for long
        check = dst.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"backup failed quick_check: {check}")
    finally:
        dst.close()
        src.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    dated = sorted((p for p in backup_dir.iterdir() if NAME.match(p.name)), key=lambda p: p.name, reverse=True)
    removed = []
    for old in dated[keep:]:
        old.unlink()
        removed.append(old.name)
    return {"backup": str(target), "bytes": target.stat().st_size, "kept": [p.name for p in dated[:keep]],
            "removed": removed}
