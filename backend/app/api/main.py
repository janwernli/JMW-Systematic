"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import REPO_ROOT, Settings, get_settings
from ..data.provider import ProviderError
from ..ledger.paper import LedgerError
from ..logging_setup import configure_logging
from ..services import AppContext
from .routes import broker, ledger, portfolio, rebalance, research, system, universe

log = logging.getLogger("app.api")
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if ctx is None:
            configure_logging(settings.log_level, settings.log_format)
        context = ctx or AppContext(settings)
        app.state.ctx = context
        context.recover_interrupted_runs()
        log.info("startup complete", extra={"provider": context.provider.info.key if context.provider else None})
        yield
        context.executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(
        title="Momentum Terminal API",
        version=__version__,
        description="US equities long-short momentum: research, model ledger and Alpaca PAPER trading automation.",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        if request.url.path.startswith("/api"):
            log.info("request", extra={"method": request.method, "path": request.url.path,
                                       "status": response.status_code,
                                       "ms": round((time.perf_counter() - t0) * 1000, 1)})
        return response

    @app.exception_handler(LedgerError)
    async def ledger_error(_: Request, exc: LedgerError):
        status = 404 if exc.code in ("not_found", "no_portfolio") else 409
        return JSONResponse(status_code=status, content={"error": exc.code, "message": str(exc)})

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        code = {400: "invalid_request", 404: "not_found", 409: "conflict"}.get(exc.status_code, "http_error")
        return JSONResponse(status_code=exc.status_code, content={"error": code, "message": str(exc.detail)})

    @app.exception_handler(ProviderError)
    async def provider_error(_: Request, exc: ProviderError):
        return JSONResponse(status_code=503, content={"error": "data_unavailable", "message": str(exc)})

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"error": "invalid_request", "message": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        msgs = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors())
        return JSONResponse(status_code=422, content={"error": "validation_error", "message": msgs})

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content={"error": "internal_error",
                                                      "message": f"{type(exc).__name__}: {exc}"})

    for r in (system.router, universe.router, portfolio.router, rebalance.router, research.router, ledger.router,
              broker.router):
        app.include_router(r, prefix="/api")

    # Serve the production frontend build (npm run build) from the same localhost origin.
    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            f = FRONTEND_DIST / path
            if path and f.is_file() and FRONTEND_DIST in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


def export_openapi(path: Path) -> None:
    import json

    app = create_app()
    path.write_text(json.dumps(app.openapi(), indent=2), encoding="utf-8")
