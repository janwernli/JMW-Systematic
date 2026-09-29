"""Alpaca PAPER trading adapter.

Safety:
  * Refuses any trading host other than Alpaca's paper endpoint (paper-api.alpaca.markets).
  * Every order carries a deterministic client_order_id; a duplicate-ID rejection is treated as
    "already submitted" and the existing order is fetched instead (never double-submits).
  * Keys are read from server-side settings only.

Order timing (Alpaca docs): time_in_force="opg" orders are accepted from 19:00 ET on the previous
day until 09:28 ET and execute only in the opening auction; unfilled ones are cancelled after the open.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)
PAPER_HOSTS = {"paper-api.alpaca.markets"}


class BrokerError(RuntimeError):
    pass


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class AlpacaPaperBroker:
    def __init__(self, key_id: str, secret: str, base_url: str = "https://paper-api.alpaca.markets",
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        host = urlparse(base_url).hostname or ""
        if host not in PAPER_HOSTS:
            raise BrokerError(f"Refusing to trade against '{host}': only the Alpaca PAPER endpoint is allowed.")
        if not key_id or not secret:
            raise BrokerError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set.")
        self.base = base_url.rstrip("/")
        self._client = httpx.Client(headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
                                    timeout=httpx.Timeout(30.0), transport=transport)
        self._sleep = sleep

    # ------------------------------------------------------------------ HTTP
    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        delay = 1.0
        for attempt in range(6):
            try:
                r = self._client.request(method, f"{self.base}{path}", **kw)
            except httpx.TransportError as e:
                log.warning("alpaca broker transport error", extra={"attempt": attempt, "error": str(e)})
                self._sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                self._sleep(delay)
                delay = min(delay * 2, 30)
                continue
            return r
        raise BrokerError(f"Alpaca {method} {path} failed after retries.")

    def _json(self, method: str, path: str, **kw):
        r = self._req(method, path, **kw)
        if r.status_code >= 400:
            raise BrokerError(f"Alpaca {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    # ------------------------------------------------------------------ reads
    def account(self) -> dict:
        return self._json("GET", "/v2/account")

    def clock(self) -> dict:
        return self._json("GET", "/v2/clock")

    def positions(self) -> list[dict]:
        out = []
        for p in self._json("GET", "/v2/positions"):
            qty = int(round(float(p["qty"])))
            if p.get("side") == "short" and qty > 0:
                qty = -qty
            out.append({"symbol": p["symbol"], "qty": qty, "avg_entry_price": _f(p.get("avg_entry_price")),
                        "market_value": _f(p.get("market_value")), "current_price": _f(p.get("current_price")),
                        "unrealized_pl": _f(p.get("unrealized_pl"))})
        return out

    def asset(self, symbol: str) -> dict:
        return self._json("GET", f"/v2/assets/{symbol}")

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        r = self._req("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise BrokerError(f"order lookup {client_order_id} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def portfolio_history(self, period: str = "1A", timeframe: str = "1D") -> list[dict]:
        d = self._json("GET", "/v2/account/portfolio/history", params={"period": period, "timeframe": timeframe})
        out = []
        for ts, eq, pl in zip(d.get("timestamp") or [], d.get("equity") or [], d.get("profit_loss") or []):
            if eq is None:
                continue
            out.append({"ts": ts, "equity": float(eq), "profit_loss": _f(pl)})
        return out

    # ------------------------------------------------------------------ orders
    def submit_order(self, symbol: str, qty: int, side: str, time_in_force: str, client_order_id: str) -> dict:
        body = {"symbol": symbol, "qty": str(int(qty)), "side": side, "type": "market",
                "time_in_force": time_in_force, "client_order_id": client_order_id}
        r = self._req("POST", "/v2/orders", json=body)
        if r.status_code in (200, 201):
            return r.json()
        text = r.text[:400]
        if r.status_code == 422 and "client_order_id" in text.lower():
            existing = self.order_by_client_id(client_order_id)
            if existing:
                return existing  # idempotent: already submitted earlier
        raise BrokerError(f"order {client_order_id} rejected ({r.status_code}): {text}")
