"""Entry points.

    python -m app                    run the API server (localhost by default)
    python -m app export-openapi F   write the OpenAPI schema (used to generate frontend types)
    python -m app import-data        import/refresh market data from the configured provider
    python -m app advance [--until YYYY-MM-DD]   advance the virtual portfolio
    python -m app daily [--dry-run] [--trigger T] run the automated daily cycle (Alpaca paper)
    python -m app migrate            apply pending database migrations and exit
    python -m app paper-config --variant KEY [--replan-now] [--dry-run]   switch the paper strategy
    python -m app backup [--dir D] [--keep N]   consistent SQLite backup (default ~/backups, keep 14)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("serve", help="run the API server (default)")
    run.add_argument("--reload", action="store_true")
    exp = sub.add_parser("export-openapi")
    exp.add_argument("path", type=Path)
    imp = sub.add_parser("import-data")
    imp.add_argument("--full", action="store_true", help="re-fetch full history for the (frozen) universe")
    adv = sub.add_parser("advance")
    adv.add_argument("--until", default=None)
    daily = sub.add_parser("daily", help="run the automated daily cycle (data, sync, model, stops, rebalance)")
    daily.add_argument("--dry-run", action="store_true", help="compute orders but never send them")
    daily.add_argument("--trigger", default="cli")
    sub.add_parser("sectors", help="(re)classify sectors from SEC EDGAR SIC codes")
    sub.add_parser("migrate", help="apply pending database migrations and exit")
    pc = sub.add_parser("paper-config", help="switch the paper strategy (model ledger + Alpaca paper) to a variant")
    pc.add_argument("--variant", required=True, help="neutral_10 | neutral_15 | spy_overlay | ext_130_30")
    pc.add_argument("--replan-now", action="store_true",
                    help="rebuild the latest month-end plan with the new config; trades at the next 08:00 ET run")
    pc.add_argument("--dry-run", action="store_true", help="print the target book and orders; change nothing")
    bak = sub.add_parser("backup", help="consistent SQLite backup (online backup API) with rotation")
    bak.add_argument("--dir", type=Path, default=Path.home() / "backups")
    bak.add_argument("--keep", type=int, default=14)
    args = parser.parse_args(argv)

    from .config import get_settings
    from .ledger.paper import LedgerError

    settings = get_settings()
    if args.cmd == "export-openapi":
        from .api.main import export_openapi

        export_openapi(args.path)
        print(f"wrote {args.path}")
        return 0
    if args.cmd == "migrate":
        from .db import Database

        before = _applied(settings.database_path)
        Database(settings.database_path).close()
        after = _applied(settings.database_path)
        new = sorted(after - before)
        print(f"{settings.database_path}: " + (f"applied {', '.join(new)}" if new else "schema up to date")
              + f" ({len(after)} migrations)")
        return 0
    if args.cmd == "backup":
        from .backup import backup_database
        from .notify import alert

        try:
            res = backup_database(settings.database_path, args.dir, keep=args.keep)
        except Exception as e:  # noqa: BLE001
            alert(settings, "backup", f"{type(e).__name__}: {e}")
            print(f"backup failed: {e}", file=sys.stderr)
            return 1
        print(f"backup {res['backup']} ({res['bytes'] / 1e6:.1f} MB); keeping {len(res['kept'])}"
              + (f"; removed {', '.join(res['removed'])}" if res["removed"] else ""))
        return 0
    if args.cmd == "paper-config":
        from .logging_setup import configure_logging
        from .paper_strategy import SwitchError, apply, format_preview, preview
        from .services import AppContext

        import warnings

        from .strategy.config import ConfigWarning

        warnings.simplefilter("ignore", ConfigWarning)   # printed by format_preview instead
        configure_logging("WARNING", "text")
        ctx = AppContext(settings)
        try:
            pv = preview(ctx, args.variant, args.replan_now)
            print(format_preview(pv))
            if args.dry_run:
                print("\nDRY RUN: nothing was changed.")
                return 0
            res = apply(ctx, args.variant, args.replan_now)
        except (SwitchError, LedgerError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print("\n" + res["message"])
        return 0
    if args.cmd in ("import-data", "advance", "daily", "sectors"):
        from .logging_setup import configure_logging
        from .services import AppContext

        configure_logging(settings.log_level, "text")
        if args.cmd == "daily":
            from .notify import alert

            try:
                ctx = AppContext(settings)
                res = ctx.daily_cycle(trigger=args.trigger, dry_run=args.dry_run)
            except Exception as e:  # noqa: BLE001 - the cycle itself records and notifies its own errors
                alert(settings, "daily cycle", f"could not run: {type(e).__name__}: {e}")
                raise
            print(f"daily cycle #{res['run_id']}: {res['status']}")
            for st in res["steps"]:
                print(f"  [{st['status']:>7}] {st['name']}: {st['detail']}")
            return 0 if res["status"] != "error" else 1
        ctx = AppContext(settings)
        if args.cmd == "sectors":
            print(ctx.fill_sectors(only_missing=False) or "SEC_USER_AGENT not configured")
            return 0
        if args.cmd == "import-data":
            res = ctx.refresh_data(full=args.full)
            print(f"import #{res['id']}: {res['status']}, {res['bars_loaded']} bars, coverage "
                  f"{res['coverage_start']}..{res['coverage_end']}")
        else:
            r = ctx.ledger.advance(until=args.until)
            print(f"processed {len(r.processed)} sessions; as of {r.as_of}; stopped: {r.stopped_reason}")
        return 0

    import socket

    import uvicorn

    with socket.socket() as sock:
        if sock.connect_ex((settings.host, settings.port)) == 0:
            print(f"ERROR: port {settings.port} on {settings.host} is already in use by another program. "
                  f"Set a free port in .env, e.g. APP_PORT=8766 (the frontend proxy reads the same value).",
                  file=sys.stderr)
            return 2

    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding to {settings.host} exposes the app beyond this computer.", file=sys.stderr)
    uvicorn.run("app.api.main:create_app", factory=True, host=settings.host, port=settings.port,
                reload=getattr(args, "reload", False), log_config=None)
    return 0


def _applied(db_path: Path) -> set[str]:
    import sqlite3

    if not Path(db_path).exists():
        return set()
    con = sqlite3.connect(db_path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM schema_migrations")}
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
