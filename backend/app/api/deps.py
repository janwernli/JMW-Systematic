"""Shared API helpers."""

from __future__ import annotations

import json

from fastapi import Request

from ..services import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def loads(s: str | None, default=None):
    if s is None:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def event_row(r: dict) -> dict:
    r = dict(r)
    r["payload"] = loads(r.pop("payload_json", "{}"), {})
    return r


def import_row(r: dict | None) -> dict | None:
    if r is None:
        return None
    r = dict(r)
    r["warnings"] = loads(r.pop("warnings_json", "[]"), [])
    r.pop("provenance_json", None)
    return r
