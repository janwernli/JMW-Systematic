"""Entry points.

    python -m app                    run the API server (localhost by default)
    python -m app export-openapi F   write the OpenAPI schema (used to generate frontend types)
    python -m app import-data        import/refresh market data from the configured provider
    python -m app advance [--until YYYY-MM-DD]   advance the virtual portfolio
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
    sub.add_parser("import-data")
    adv = sub.add_parser("advance")
    adv.add_argument("--until", default=None)
    args = parser.parse_args(argv)

    from .config import get_settings

    settings = get_settings()
    if args.cmd == "export-openapi":
        from .api.main import export_openapi

        export_openapi(args.path)
        print(f"wrote {args.path}")
        return 0
    if args.cmd in ("import-data", "advance"):
        from .logging_setup import configure_logging
        from .services import AppContext

        configure_logging(settings.log_level, "text")
        ctx = AppContext(settings)
        if args.cmd == "import-data":
            from .data.store import run_import

            res = run_import(ctx.db, ctx.require_provider(), ctx.calendar)
            print(f"import #{res['id']}: {res['status']}, {res['bars_loaded']} bars, coverage "
                  f"{res['coverage_start']}..{res['coverage_end']}")
        else:
            r = ctx.ledger.advance(until=args.until)
            print(f"processed {len(r.processed)} sessions; as of {r.as_of}; stopped: {r.stopped_reason}")
        return 0

    import uvicorn

    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding to {settings.host} exposes the app beyond this computer.", file=sys.stderr)
    uvicorn.run("app.api.main:create_app", factory=True, host=settings.host, port=settings.port,
                reload=getattr(args, "reload", False), log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
