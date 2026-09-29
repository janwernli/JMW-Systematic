"""Alpaca Market Data adapter (optional; requires a free Alpaca account key).

READ-ONLY DATA ACCESS. This module never calls an order, position or account
endpoint. The trading API base URL is used only for the public asset list.

Endpoints used (per Alpaca's public docs):
  GET {data}/v2/stocks/bars           raw daily bars, multi-symbol, paginated via next_page_token
  GET {data}/v1/corporate-actions     forward_split / reverse_split / cash_dividend, paginated
  GET {trading}/v2/assets             asset master (status, exchange, name)

Documented limits & coverage (verify against your own plan -- they may change):
  * Historical stock data from 2016 onward.
  * Free "Basic" plan: ~200 requests/minute; SIP (consolidated) historical data is
    available except for the most recent 15 minutes; the IEX feed covers only a
    small share of volume, which makes dollar-volume filters unreliable -> SIP default.
  * Rate-limit headers: X-RateLimit-Limit / -Remaining / -Reset (epoch seconds).
  * The asset list contains current (active) and some inactive assets, but it is NOT a
    point-in-time security master: delisted names are largely missing. Backtests built
    on it are SURVIVORSHIP-BIASED and are labelled as such everywhere.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from .provider import (
    ACTION_COLUMNS,
    BAR_COLUMNS,
    InstrumentRecord,
    MarketDataProvider,
    ProviderError,
    ProviderInfo,
)

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
BENCHMARK = "SPY"

# Name patterns, checked in order. Alpaca exposes no ETF / share-class field, so the universe filter is a
# documented heuristic aiming at "US common stock" in the CRSP share-code 10/11 sense (no funds, notes, ADRs,
# preferreds, warrants, units, rights or blank-check shells).
_NON_COMMON = [
    (re.compile(r"\bNOTES?\b|DEBENTURE|SUBORDINATED|SYNTHETIC FIXED|\bSTRATS\b|\bDUE \d{4}|\bPERCENT\b|SENIOR NOTES", re.I), "debt"),
    (re.compile(r"\bETF\b|\bETN\b|ETRACS|CURRENCYSHARES|\bINDEX FUND\b|\bSPDR\b|\bISHARES\b|\bPROSHARES\b|\bDIREXION\b", re.I), "etf"),
    (re.compile(r"\bFUND\b|\bINCOME TRUST\b|\bMUNICIPAL\b|CLOSED[- ]END|\bTRUST\b.*\bUNITS?\b", re.I), "fund"),
    (re.compile(r"\bPREFERRED\b|\bPFD\b|%", re.I), "preferred"),
    (re.compile(r"\bWARRANTS?\b|\bWTS?\b", re.I), "warrant"),
    (re.compile(r"\bUNITS?\b", re.I), "unit"),
    (re.compile(r"\bRIGHTS?\b", re.I), "right"),
    (re.compile(r"AMERICAN DEPOSITA|DEPOSITORY SHARE|DEPOSITARY SHARE|\bADRS?\b|\bADS\b", re.I), "adr"),
    (re.compile(r"ACQUISITION CORP|ACQUISITION CO\b|ACQUISITION LTD|MERGER CORP|BLANK CHECK", re.I), "spac"),
]
# A "... Trust" without common-stock or REIT wording is almost always a closed-end fund or grantor trust.
_COMMON_HINT = re.compile(r"COMMON STOCK|COMMON SHARES|ORDINARY SHARES?|SUBORDINATE VOTING|REAL ESTATE INVESTMENT TRUST|\bREIT\b|\bINC\b.*COMMON", re.I)


def classify_asset(symbol: str, name: str) -> str:
    """Heuristic common-stock filter (see _NON_COMMON). Returns an asset_type."""
    name = name or ""
    for pat, kind in _NON_COMMON:
        if pat.search(name):
            return kind
    if re.search(r"\bTRUST\b", name, re.I) and not _COMMON_HINT.search(name):
        return "fund"
    # Suffixes for preferreds / warrants / units / rights. Plain share classes (BRK.B, BF.B) stay common stock.
    if re.search(r"[./-](P|PR|W|WS|U|R)[A-Z]?$", symbol):
        return "other"
    return "common_stock"


class AlpacaProvider(MarketDataProvider):
    def __init__(self, key_id: str, secret: str, feed: str = "sip",
                 data_url: str = "https://data.alpaca.markets", trading_url: str = "https://paper-api.alpaca.markets",
                 history_start: str = "2016-01-01", universe_file: Path | None = None, max_symbols: int = 600,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        if not key_id or not secret:
            raise ProviderError("ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY must be set in .env to use Alpaca data.")
        self._client = httpx.Client(
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"},
            timeout=httpx.Timeout(30.0), transport=transport)
        self.feed = feed
        self.data_url = data_url.rstrip("/")
        self.trading_url = trading_url.rstrip("/")
        self.history_start = history_start
        self.universe_file = universe_file
        self.max_symbols = max_symbols
        self._sleep = sleep
        self._instruments: list[InstrumentRecord] | None = None
        self.selection_note = ""
        self.info = ProviderInfo(
            key="alpaca",
            name="Alpaca Market Data",
            feed=feed,
            data_label="Delayed Market Data",
            is_demo=False,
            benchmark_symbol=BENCHMARK,
            benchmark_return_basis="total_return",
            point_in_time_universe=False,
            coverage_note=("Daily bars from 2016 onward (Alpaca historical coverage). EOD data is imported after the "
                           "close; nothing here is a live quote."),
            entitlement_note=(f"Feed '{feed}'. Free plans: SIP history except the latest 15 minutes, ~200 requests/min. "
                              "IEX-only volume understates liquidity. Check your Alpaca subscription."),
            adjustment_note="Requested adjustment=raw; splits and cash dividends applied from /v1/corporate-actions.",
            survivorship_note=("Universe built from Alpaca's CURRENT asset list (plus configured file). Delisted "
                               "securities are largely absent and membership is not point-in-time: historical results "
                               "are SURVIVORSHIP-BIASED and overstate what was achievable."),
            requires_key=True,
        )

    # ------------------------------------------------------------------ HTTP
    def _get(self, url: str, params: dict) -> dict:
        delay = 1.0
        for attempt in range(8):
            try:
                r = self._client.get(url, params=params)
            except httpx.TransportError as e:
                log.warning("alpaca transport error; retrying", extra={"attempt": attempt, "error": str(e)})
                self._sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 429:
                reset = r.headers.get("X-RateLimit-Reset")
                wait = max(float(reset) - time.time(), 1.0) if reset and reset.isdigit() else delay
                log.warning("alpaca rate limit; backing off", extra={"wait_s": round(wait, 1)})
                self._sleep(min(wait, 60))
                delay = min(delay * 2, 60)
                continue
            if r.status_code >= 500:
                self._sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code in (401, 403):
                raise ProviderError(f"Alpaca rejected the credentials or entitlement ({r.status_code}): {r.text[:200]}")
            if r.status_code >= 400:
                raise ProviderError(f"Alpaca request failed ({r.status_code}) for {url}: {r.text[:300]}")
            remaining = r.headers.get("X-RateLimit-Remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) < 3:
                self._sleep(1.0)
            return r.json()
        raise ProviderError(f"Alpaca request to {url} failed after retries.")

    def _paginate(self, url: str, params: dict):
        token = None
        while True:
            p = dict(params)
            if token:
                p["page_token"] = token
            data = self._get(url, p)
            yield data
            token = data.get("next_page_token")
            if not token:
                break

    # ------------------------------------------------------------------ interface
    def list_instruments(self, symbols: list[str] | None = None) -> list[InstrumentRecord]:
        if self._instruments is not None and symbols is None:
            return self._instruments
        assets = self._get(f"{self.trading_url}/v2/assets", {"asset_class": "us_equity"})
        by_sym = {a["symbol"]: a for a in assets if a.get("exchange") in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS")}
        if symbols is not None:
            wanted = [s for s in symbols if s != BENCHMARK]
            self.selection_note = (f"Frozen universe of {len(wanted)} symbols chosen at the first import "
                                   "(refreshes do not re-select).")
        elif self.universe_file:
            wanted = [s.strip().upper() for s in Path(self.universe_file).read_text().splitlines()
                      if s.strip() and not s.startswith("#")]
            self.selection_note = f"Universe from file {self.universe_file} ({len(wanted)} symbols)."
        else:
            candidates = sorted(s for s, a in by_sym.items() if a.get("status") == "active" and a.get("tradable")
                                and classify_asset(s, a.get("name", "")) == "common_stock")
            wanted = self._top_by_recent_dollar_volume(candidates, self.max_symbols)
            self.selection_note = (f"Universe = top {len(wanted)} of {len(candidates)} active common stocks by RECENT "
                                   "dollar volume. This selection uses today's information (look-ahead/survivorship bias).")
        out: list[InstrumentRecord] = []
        for s in dict.fromkeys(wanted + [BENCHMARK]):
            a = by_sym.get(s, {"symbol": s, "name": s, "status": "unknown"})
            kind = "etf" if s == BENCHMARK else classify_asset(s, a.get("name", ""))
            out.append(InstrumentRecord(
                symbol=s, name=a.get("name") or s, exchange=a.get("exchange"), asset_type=kind,
                asset_type_source="heuristic: Alpaca asset name/symbol pattern", sector=None, sector_source=None,
                is_benchmark=s == BENCHMARK, active=a.get("status") == "active",
                metadata={"alpaca_status": a.get("status"), "tradable": a.get("tradable"),
                          "selection": self.selection_note},
            ))
        self._instruments = out
        return out

    def _top_by_recent_dollar_volume(self, symbols: list[str], n: int) -> list[str]:
        if n <= 0 or len(symbols) <= n:
            return symbols
        end = self.latest_completed_session()
        start = (pd.Timestamp(end) - pd.Timedelta(days=45)).date().isoformat()
        bars = self.fetch_bars(symbols, start, end)
        if bars.empty:
            return symbols[:n]
        dv = (bars["close"] * bars["volume"]).groupby(bars["symbol"]).mean().sort_values(ascending=False)
        return list(dv.index[:n])

    def fetch_bars(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        rows: list[tuple] = []
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            params = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": start, "end": _bar_end(end),
                      "adjustment": "raw", "feed": self.feed, "limit": 10000, "sort": "asc"}
            for page in self._paginate(f"{self.data_url}/v2/stocks/bars", params):
                for sym, bars in (page.get("bars") or {}).items():
                    for b in bars:
                        session = _session_of(b["t"])
                        rows.append((sym, session, b.get("o"), b.get("h"), b.get("l"), b.get("c"), b.get("v")))
        df = pd.DataFrame(rows, columns=BAR_COLUMNS)
        return df

    def fetch_corporate_actions(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        rows: list[tuple] = []
        for i in range(0, len(symbols), 100):
          for w_start, w_end in _year_windows(start, end):  # keep each request's date range modest
            params = {"symbols": ",".join(symbols[i:i + 100]), "types": "forward_split,reverse_split,cash_dividend",
                      "start": w_start, "end": w_end, "limit": 1000, "sort": "asc"}
            for page in self._paginate(f"{self.data_url}/v1/corporate-actions", params):
                ca = page.get("corporate_actions") or {}
                for kind in ("forward_splits", "reverse_splits"):
                    for a in ca.get(kind, []) or []:
                        old, new = float(a.get("old_rate") or 0), float(a.get("new_rate") or 0)
                        if old > 0 and new > 0:
                            rows.append((a["symbol"], a["ex_date"], "split", new / old, None))
                for a in ca.get("cash_dividends", []) or []:
                    rate = float(a.get("rate") or 0)
                    if rate > 0 and not a.get("foreign"):
                        rows.append((a["symbol"], a["ex_date"], "cash_dividend", None, rate))
        df = pd.DataFrame(rows, columns=ACTION_COLUMNS)
        # Multiple cash dividends on one ex-date (e.g. regular + special) are summed.
        if not df.empty:
            div = df[df["action_type"] == "cash_dividend"].groupby(["symbol", "ex_date"], as_index=False)["amount"].sum()
            div["action_type"], div["ratio"] = "cash_dividend", None
            spl = df[df["action_type"] == "split"].groupby(["symbol", "ex_date"], as_index=False)["ratio"].prod()
            spl["action_type"], spl["amount"] = "split", None
            df = pd.concat([spl, div], ignore_index=True)[ACTION_COLUMNS]
        return df

    def default_history_range(self) -> tuple[str, str]:
        return self.history_start, self.latest_completed_session()

    @staticmethod
    def latest_completed_session() -> str:
        """Never request the current, still-forming daily bar (and respect the free plan's 15-minute delay)."""
        from ..calendar import xnys_calendar

        return xnys_calendar().latest_completed_session(datetime.now(UTC)) or date.today().isoformat()


def _bar_end(end: str) -> str:
    """Daily bars are stamped 00:00 New York time; a bare date would be read as 00:00 UTC and could drop
    that session's bar. Use noon UTC of the end date: after the bar's stamp, and in the past for completed sessions."""
    return f"{end}T12:00:00Z" if len(end) == 10 else end


def _year_windows(start: str, end: str) -> list[tuple[str, str]]:
    out = []
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    while s <= e:
        w = min(s + pd.DateOffset(years=1) - pd.Timedelta(days=1), e)
        out.append((s.date().isoformat(), w.date().isoformat()))
        s = w + pd.Timedelta(days=1)
    return out


def _session_of(ts: str) -> str:
    """Alpaca daily bar timestamps are the session date at 00:00 New York time, expressed in UTC."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(NY).date().isoformat()
