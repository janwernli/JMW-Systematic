"""Point-in-time fundamentals for the value signal.

Source (Alpaca provider): SEC EDGAR XBRL companyfacts (https://data.sec.gov/api/xbrl/companyfacts/). We keep
three concepts with their FILING dates:

  equity      us-gaap:StockholdersEquity (fallback: ...IncludingPortionAttributableToNoncontrollingInterest)
  net_income  us-gaap:NetIncomeLoss (fallback: us-gaap:ProfitLoss), duration facts
  shares      dei:EntityCommonStockSharesOutstanding (cover page; share classes summed per filing),
              fallback us-gaap:CommonStockSharesOutstanding

At a signal date d only facts with filed <= d are used (no look-ahead):

  book equity     latest period end among facts filed <= d (end within 18 months of d)
  shares          same; adjusted for splits between the period end and d (panel split ratios)
  market cap      raw close at d x adjusted shares
  TTM earnings    latest fiscal year (duration ~1y), or, if a later interim period exists,
                  FY + YTD - prior-year YTD (same duration, ending ~1 year earlier); fallback FY alone
  book-to-market  equity / market cap  (NaN when equity <= 0)
  earnings yield  TTM earnings / market cap

Companyfacts excludes dimensional facts, so a few multi-class issuers may lack cover-page shares; they fall back
to us-gaap shares or are left without value data (coverage is reported). Compustat via WRDS can replace this
module later behind the same `PointInTimeFundamentals` interface.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..db import Database, utcnow

log = logging.getLogger(__name__)

TAGS = {
    "equity": [("us-gaap", "StockholdersEquity"),
               ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")],
    "net_income": [("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss")],
    "shares": [("dei", "EntityCommonStockSharesOutstanding"), ("us-gaap", "CommonStockSharesOutstanding")],
}
UNITS = {"equity": "USD", "net_income": "USD", "shares": "shares"}
MAX_AGE_DAYS = 548          # ~18 months: older balance-sheet / share data is treated as missing


def sec_ticker(symbol: str) -> str:
    """Alpaca 'BRK.B' -> SEC 'BRK-B'."""
    return symbol.upper().replace(".", "-")


def extract_facts(companyfacts: dict) -> list[dict]:
    """The facts we keep from one companyfacts payload (first tag per concept that has data)."""
    facts = companyfacts.get("facts", {})
    out = []
    for concept, candidates in TAGS.items():
        for ns, tag in candidates:
            rows = facts.get(ns, {}).get(tag, {}).get("units", {}).get(UNITS[concept])
            if not rows:
                continue
            for r in rows:
                if r.get("val") is None or not r.get("end") or not r.get("filed"):
                    continue
                if concept == "net_income" and not r.get("start"):
                    continue
                out.append({"concept": concept, "source_tag": f"{ns}:{tag}", "start": r.get("start"), "end": r["end"],
                            "value": float(r["val"]), "filed": r["filed"], "form": r.get("form"), "fy": r.get("fy"),
                            "fp": r.get("fp"), "accn": r.get("accn")})
            break
    return out


def fetch_sec_fundamentals(db: Database, provider: str, client, symbols: list[str] | None = None,
                           progress=None) -> dict:
    """Download companyfacts for the provider's common stocks and store the facts (replaces per symbol)."""
    if symbols is None:
        symbols = [r["symbol"] for r in db.query(
            "SELECT symbol FROM instruments WHERE provider=? AND asset_type='common_stock' ORDER BY symbol", (provider,))]
    ciks = client.ticker_map()
    stats = {"symbols": len(symbols), "ok": 0, "no_cik": 0, "no_facts": 0, "error": 0, "facts": 0}
    for i, sym in enumerate(symbols):
        cik = ciks.get(sec_ticker(sym))
        status, rows, note = "no_cik", [], None
        if cik is not None:
            try:
                data = client.companyfacts(cik)
                rows = extract_facts(data) if data else []
                status = "ok" if rows else "no_facts"
            except Exception as e:  # noqa: BLE001 - one bad company must not stop the run
                status, note = "error", str(e)[:300]
        with db.transaction() as conn:
            if status in ("ok", "no_facts"):
                conn.execute("DELETE FROM fundamental_facts WHERE provider=? AND symbol=?", (provider, sym))
                conn.executemany(
                    "INSERT INTO fundamental_facts (provider, symbol, cik, concept, source_tag, start, end, value, filed,"
                    " form, fy, fp, accn) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(provider, sym, cik, r["concept"], r["source_tag"], r["start"], r["end"], r["value"], r["filed"],
                      r["form"], r["fy"], r["fp"], r["accn"]) for r in rows])
            conn.execute("INSERT INTO fundamental_fetches (provider, symbol, cik, fetched_at, status, facts, note) "
                         "VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider, symbol) DO UPDATE SET cik=excluded.cik, "
                         "fetched_at=excluded.fetched_at, status=excluded.status, facts=excluded.facts, note=excluded.note",
                         (provider, sym, cik, utcnow(), status, len(rows), note))
        stats[status] += 1
        stats["facts"] += len(rows)
        if progress:
            progress(i + 1, len(symbols), sym, status)
    return stats


# ---------------------------------------------------------------------------------------------
# Point-in-time computation
# ---------------------------------------------------------------------------------------------
def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


class PointInTimeFundamentals:
    """Facts per symbol and concept, queried as of a signal date (filed <= date)."""

    def __init__(self, facts: pd.DataFrame):
        self.by: dict[tuple[str, str], pd.DataFrame] = {}
        if facts.empty:
            return
        f = facts.copy()
        for (sym, concept), g in f.groupby(["symbol", "concept"]):
            self.by[(sym, concept)] = g.sort_values(["filed", "end"]).reset_index(drop=True)

    def __len__(self) -> int:
        return len({s for s, _ in self.by})

    @classmethod
    def load(cls, db: Database, provider: str) -> "PointInTimeFundamentals":
        df = pd.read_sql_query("SELECT symbol, concept, start, end, value, filed, accn FROM fundamental_facts "
                               "WHERE provider=?", db.conn, params=(provider,))
        return cls(df)

    def _known(self, sym: str, concept: str, asof: str) -> pd.DataFrame | None:
        g = self.by.get((sym, concept))
        if g is None:
            return None
        g = g[g["filed"] <= asof]
        return g if len(g) else None

    def instant(self, sym: str, concept: str, asof: str) -> tuple[float, str] | None:
        """(value, period end) of the latest-ending instant fact known at `asof`; share classes summed per filing."""
        g = self._known(sym, concept, asof)
        if g is None:
            return None
        g = g[g["end"] <= asof]
        if not len(g):
            return None
        end = g["end"].max()
        if (_d(asof) - _d(end)).days > MAX_AGE_DAYS:
            return None
        rows = g[g["end"] == end]
        latest = rows[rows["filed"] == rows["filed"].max()]
        if concept == "shares":
            val = float(latest.drop_duplicates(["accn", "value"])["value"].sum())   # share classes in one filing
        else:
            val = float(latest["value"].iloc[-1])
        return val, end

    def ttm_earnings(self, sym: str, asof: str) -> float | None:
        g = self._known(sym, "net_income", asof)
        if g is None:
            return None
        g = g[g["end"] <= asof].copy()
        if not len(g):
            return None
        g["days"] = [(_d(e) - _d(s)).days for s, e in zip(g["start"], g["end"])]
        # one value per (start, end): the latest filing known at `asof` (restatements included)
        g = g.sort_values("filed").drop_duplicates(["start", "end"], keep="last")
        annual = g[(g["days"] >= 350) & (g["days"] <= 380)]
        if not len(annual):
            return None
        fy = annual.loc[annual["end"].idxmax()]
        if (_d(asof) - _d(fy["end"])).days > MAX_AGE_DAYS:
            return None
        interim = g[(g["days"] < 350) & (g["end"] > fy["end"])]
        if len(interim):
            # longest year-to-date period that starts right after the fiscal year
            ytd = interim[[abs((_d(s) - _d(fy["end"])).days - 1) <= 7 for s in interim["start"]]]
            if len(ytd):
                cur = ytd.loc[ytd["end"].idxmax()]
                prev_end = _d(cur["end"]) - timedelta(days=365)
                prior = g[[abs((_d(e) - prev_end).days) <= 10 and abs(d - cur["days"]) <= 10
                           for e, d in zip(g["end"], g["days"])]]
                if len(prior):
                    return float(fy["value"] + cur["value"] - prior["value"].iloc[-1])
        return float(fy["value"])

    def ratios(self, sym: str, asof: str, close: float, split_factor) -> dict:
        """book-to-market and earnings yield at `asof` given the raw close and a split-adjustment function."""
        out = {"bm": np.nan, "ep": np.nan, "mcap": np.nan}
        sh = self.instant(sym, "shares", asof)
        if sh is None or not (close and close > 0):
            return out
        shares = sh[0] * split_factor(sh[1])
        if not shares > 0:
            return out
        mcap = close * shares
        out["mcap"] = mcap
        eq = self.instant(sym, "equity", asof)
        if eq is not None and eq[0] > 0:
            out["bm"] = eq[0] / mcap
        ttm = self.ttm_earnings(sym, asof)
        if ttm is not None:
            out["ep"] = ttm / mcap
        return out
