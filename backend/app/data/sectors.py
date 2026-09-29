"""Sector classification from SEC EDGAR SIC codes, mapped to 11 GICS-style sectors.

Source: https://www.sec.gov/files/company_tickers.json (ticker -> CIK) and
https://data.sec.gov/submissions/CIK##########.json ("sic", "sicDescription").
SEC requires a descriptive User-Agent with a contact e-mail and at most 10 requests/second.

Caveats (shown in the UI):
  * SIC is the company's *current* code in EDGAR, not point-in-time.
  * SIC -> GICS-style sector is an approximation: e.g. SIC 7370 (computer services) covers
    both software and internet platforms (GICS Communication Services), so Alphabet/Meta land in
    Information Technology.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from ..db import Database, log_event, utcnow

log = logging.getLogger(__name__)

SECTORS = [
    "Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy", "Financials", "Health Care",
    "Industrials", "Information Technology", "Materials", "Real Estate", "Utilities",
]

# (low, high, sector) inclusive SIC ranges; checked in order, first match wins (specific ranges first).
_SIC_RANGES: list[tuple[int, int, str]] = [
    (100, 299, "Consumer Staples"), (700, 799, "Consumer Staples"), (800, 899, "Materials"), (900, 999, "Consumer Staples"),
    (1000, 1099, "Materials"), (1200, 1299, "Energy"), (1300, 1399, "Energy"), (1400, 1499, "Materials"),
    (1520, 1531, "Consumer Discretionary"), (1500, 1799, "Industrials"),
    (2000, 2199, "Consumer Staples"), (2200, 2399, "Consumer Discretionary"),
    (2450, 2452, "Consumer Discretionary"), (2400, 2499, "Materials"), (2500, 2599, "Consumer Discretionary"),
    (2600, 2699, "Materials"), (2700, 2799, "Communication Services"),
    (2830, 2836, "Health Care"), (2840, 2844, "Consumer Staples"), (2800, 2899, "Materials"),
    (2900, 2999, "Energy"), (3011, 3011, "Consumer Discretionary"), (3000, 3099, "Materials"),
    (3100, 3199, "Consumer Discretionary"), (3200, 3399, "Materials"), (3400, 3499, "Industrials"),
    (3570, 3579, "Information Technology"), (3500, 3599, "Industrials"),
    (3630, 3639, "Consumer Discretionary"), (3651, 3652, "Consumer Discretionary"),
    (3660, 3679, "Information Technology"), (3600, 3699, "Industrials"),
    (3710, 3716, "Consumer Discretionary"), (3750, 3751, "Consumer Discretionary"), (3790, 3799, "Consumer Discretionary"),
    (3700, 3799, "Industrials"), (3812, 3812, "Industrials"), (3820, 3829, "Information Technology"),
    (3840, 3851, "Health Care"), (3860, 3861, "Information Technology"), (3870, 3873, "Consumer Discretionary"),
    (3800, 3899, "Industrials"), (3900, 3999, "Consumer Discretionary"),
    (4000, 4799, "Industrials"), (4800, 4899, "Communication Services"),
    (4950, 4959, "Industrials"), (4900, 4999, "Utilities"),
    (5122, 5122, "Health Care"), (5140, 5149, "Consumer Staples"), (5170, 5172, "Energy"), (5000, 5199, "Industrials"),
    (5331, 5331, "Consumer Staples"), (5399, 5399, "Consumer Staples"), (5400, 5499, "Consumer Staples"),
    (5912, 5912, "Consumer Staples"), (5200, 5999, "Consumer Discretionary"),
    (6500, 6553, "Real Estate"), (6798, 6798, "Real Estate"), (6000, 6799, "Financials"),
    (7000, 7099, "Consumer Discretionary"), (7200, 7299, "Consumer Discretionary"),
    (7310, 7319, "Communication Services"), (7370, 7379, "Information Technology"), (7300, 7399, "Industrials"),
    (7500, 7599, "Consumer Discretionary"), (7800, 7899, "Communication Services"), (7900, 7999, "Consumer Discretionary"),
    (8000, 8099, "Health Care"), (8200, 8299, "Consumer Discretionary"), (8731, 8731, "Health Care"),
    (8700, 8799, "Industrials"), (8000, 8999, "Industrials"),
]


def sic_to_sector(sic: int | str | None) -> str | None:
    try:
        code = int(sic)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for lo, hi, sector in _SIC_RANGES:
        if lo <= code <= hi:
            return sector
    return None


def _sec_ticker(symbol: str) -> str:
    return symbol.upper().replace(".", "-")  # BRK.B (Alpaca) -> BRK-B (SEC)


class SecClient:
    def __init__(self, user_agent: str, transport: httpx.BaseTransport | None = None, sleep=time.sleep,
                 min_interval: float = 0.125):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must be set in .env to a name and contact e-mail "
                             "(SEC fair-access policy), e.g. 'MyApp research me@example.com'.")
        self._client = httpx.Client(headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
                                    timeout=httpx.Timeout(30.0), transport=transport)
        self._sleep = sleep
        self._min_interval = min_interval
        self._last = 0.0

    def _get(self, url: str) -> dict | None:
        for attempt in range(5):
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            r = self._client.get(url)
            if r.status_code == 404:
                return None
            if r.status_code in (429, 503):
                self._sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"SEC request failed after retries: {url}")

    def ticker_map(self) -> dict[str, int]:
        data = self._get("https://www.sec.gov/files/company_tickers.json") or {}
        return {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}

    def sic(self, cik: int) -> tuple[str | None, str | None]:
        d = self._get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json") or {}
        return (str(d.get("sic")) if d.get("sic") else None), d.get("sicDescription")


def classify_instruments(db: Database, provider: str, client: SecClient, only_missing: bool = True) -> dict:
    """Fill instruments.sector from EDGAR SIC codes for the provider's common stocks."""
    q = ("SELECT id, symbol FROM instruments WHERE provider=? AND asset_type='common_stock'"
         + (" AND (sector IS NULL OR sector='')" if only_missing else ""))
    rows = db.query(q, (provider,))
    if not rows:
        return {"requested": 0, "classified": 0, "missing": []}
    tmap = client.ticker_map()
    done, missing = 0, []
    for r in rows:
        cik = tmap.get(_sec_ticker(r["symbol"]))
        sic, desc = client.sic(cik) if cik else (None, None)
        sector = sic_to_sector(sic)
        if not sector:
            missing.append(r["symbol"])
            continue
        with db.transaction() as conn:
            meta = conn.execute("SELECT metadata_json FROM instruments WHERE id=?", (r["id"],)).fetchone()[0]
            m = json.loads(meta or "{}")
            m.update(sic=sic, sic_description=desc, cik=cik, sector_fetched_at=utcnow())
            conn.execute("UPDATE instruments SET sector=?, sector_source=?, metadata_json=? WHERE id=?",
                         (sector, f"SEC EDGAR SIC {sic} ({desc}) -> {sector} (approximate mapping, current code)",
                          json.dumps(m), r["id"]))
        done += 1
    with db.transaction() as conn:
        log_event(conn, "warning" if missing else "info", "data_import",
                  f"Sector classification: {done} of {len(rows)} stocks classified from SEC EDGAR SIC codes"
                  + (f"; unclassified: {', '.join(missing[:20])}{'...' if len(missing) > 20 else ''}" if missing else ""),
                  payload={"unclassified": missing})
    return {"requested": len(rows), "classified": done, "missing": missing}
