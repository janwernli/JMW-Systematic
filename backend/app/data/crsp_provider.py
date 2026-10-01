"""CRSP daily stock file (WRDS CSV export) provider - STUB for survivorship-free research.

Not used for trading. It lets the strategy-variant comparison be rerun on CRSP data (delisted stocks included,
historical share codes / exchanges / SIC codes) once you have a WRDS subscription.

Expected CSV (one row per PERMNO and trading day, exported from crsp.dsf joined with crsp.dsenames):

  required  PERMNO, date, PRC, VOL, RET, DLRET, SHRCD, EXCHCD, SICCD, SHROUT
  needed    OPENPRC   - the engine fills orders at the OPEN; without it the provider refuses to load
            CFACPR    - cumulative price-adjustment factor: separates splits from cash distributions
  optional  TICKER, COMNAM (display only)

Conventions implemented here:
  * symbol = "P<PERMNO>" (stable across ticker changes); TICKER/COMNAM go to the instrument name
  * PRC < 0 means a bid/ask midpoint (no trade): the absolute value is used as the close
  * SHRCD 10/11 = common stock; 73 = ETF (e.g. SPY, PERMNO 84398, used as the benchmark); everything else "other"
  * EXCHCD 1/2/3 = NYSE/AMEX/NASDAQ; SICCD -> 11 sectors with the same mapping as the SEC SIC codes
  * delisting: the last row of a PERMNO before the file's end is its delisting date; DLRET is kept in
    the instrument metadata (TODO: apply (1 + DLRET) to the delisting cash-out instead of the last close)
  * splits: ratio = CFACPR(t-1) / CFACPR(t) when the factor changes
  * cash distributions per share: RET(t) x P(t-1) - (P(t) - P(t-1)) on days without a split (>= 1e-4)

The calendar is XNYS; CRSP dates outside it are dropped by the panel builder.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .provider import (ACTION_COLUMNS, BAR_COLUMNS, InstrumentRecord, MarketDataProvider, ProviderError,
                       ProviderInfo)
from .sectors import sic_to_sector

REQUIRED = ["PERMNO", "date", "PRC", "VOL", "RET", "DLRET", "SHRCD", "EXCHCD", "SICCD", "SHROUT"]
EXCHANGES = {1: "NYSE", 2: "AMEX", 3: "NASDAQ"}


class CrspCsvProvider(MarketDataProvider):
    def __init__(self, path: str | Path, history_start: str = "1990-01-01", benchmark_permno: int = 84398):
        self.path = Path(path)
        self.history_start = history_start
        self.benchmark_symbol = f"P{benchmark_permno}"
        self.info = ProviderInfo(
            key="crsp", name="CRSP daily stock file (WRDS CSV)", feed="crsp-dsf", data_label="End-of-Day Market Data",
            is_demo=False, benchmark_symbol=self.benchmark_symbol, benchmark_return_basis="total_return",
            point_in_time_universe=True,
            coverage_note="CRSP: all NYSE/AMEX/NASDAQ securities incl. delisted ones, as exported.",
            entitlement_note="Requires a WRDS / CRSP subscription; the CSV stays local.",
            adjustment_note="Raw prices; splits from CFACPR, cash distributions implied by RET.",
            survivorship_note="Survivorship-free: delisted securities are included (delisting returns: see README).",
            requires_key=False)
        self._df: pd.DataFrame | None = None

    # ------------------------------------------------------------------ loading
    def frame(self) -> pd.DataFrame:
        if self._df is not None:
            return self._df
        if not self.path.exists():
            raise ProviderError(f"CRSP CSV not found: {self.path} (set CRSP_CSV_PATH).")
        df = pd.read_csv(self.path, dtype={"PERMNO": "int64"}, low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        missing = [c for c in REQUIRED if c not in df.columns]
        if missing:
            raise ProviderError(f"CRSP CSV is missing columns {missing}; required: {REQUIRED}.")
        df["date"] = pd.to_datetime(df["date"].astype(str), format="mixed").dt.strftime("%Y-%m-%d")
        for c in ("PRC", "VOL", "RET", "DLRET", "SHROUT", "OPENPRC", "CFACPR"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")       # CRSP letter codes (e.g. 'C', 'B') -> NaN
        df = df[df["date"] >= self.history_start].sort_values(["PERMNO", "date"]).reset_index(drop=True)
        df["symbol"] = "P" + df["PERMNO"].astype(str)
        df["close"] = df["PRC"].abs()
        self._df = df
        return df

    def _need(self, col: str, why: str) -> None:
        if col not in self.frame().columns:
            raise ProviderError(f"CRSP CSV has no {col} column: {why}. Add {col} to the WRDS export (see README).")

    # ------------------------------------------------------------------ interface
    def list_instruments(self, symbols: list[str] | None = None) -> list[InstrumentRecord]:
        df = self.frame()
        if symbols is not None:
            df = df[df["symbol"].isin(symbols)]
        last_date = self.frame()["date"].max()
        out = []
        for sym, g in df.groupby("symbol", sort=True):
            last = g.iloc[-1]
            shrcd = int(last["SHRCD"]) if pd.notna(last["SHRCD"]) else None
            atype = "common_stock" if shrcd in (10, 11) else "etf" if shrcd == 73 else "other"
            sic = str(int(last["SICCD"])) if pd.notna(last["SICCD"]) and int(last["SICCD"]) > 0 else None
            delisted = g["date"].iloc[-1] < last_date
            dl = g["DLRET"].dropna()
            name = " ".join(str(last[c]) for c in ("TICKER", "COMNAM") if c in g.columns and pd.notna(last[c])) or sym
            out.append(InstrumentRecord(
                symbol=sym, name=name, exchange=EXCHANGES.get(int(last["EXCHCD"])) if pd.notna(last["EXCHCD"]) else None,
                asset_type=atype, asset_type_source="CRSP SHRCD", sector=sic_to_sector(sic) if sic else None,
                sector_source="CRSP SICCD (latest)" if sic else None, is_benchmark=sym == self.benchmark_symbol,
                list_date=g["date"].iloc[0], delist_date=g["date"].iloc[-1] if delisted else None, active=not delisted,
                metadata={"permno": int(g["PERMNO"].iloc[0]), "shrcd": shrcd,
                          "delisting_return": float(dl.iloc[-1]) if len(dl) else None}))
        return out

    def fetch_bars(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        self._need("OPENPRC", "orders fill at the open")
        df = self.frame()
        df = df[df["symbol"].isin(symbols) & (df["date"] >= start) & (df["date"] <= end)]
        bars = pd.DataFrame({"symbol": df["symbol"], "session": df["date"], "open": df["OPENPRC"].abs(),
                             "high": np.nan, "low": np.nan, "close": df["close"], "volume": df["VOL"]})
        return bars[BAR_COLUMNS].reset_index(drop=True)

    def fetch_corporate_actions(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        self._need("CFACPR", "splits cannot be separated from cash distributions")
        df = self.frame()
        rows = []
        for sym, g in df[df["symbol"].isin(symbols)].groupby("symbol"):
            g = g.reset_index(drop=True)
            prev_p, prev_f = g["close"].shift(1), g["CFACPR"].shift(1)
            for i in range(1, len(g)):
                d = g.at[i, "date"]
                if not (start <= d <= end):
                    continue
                f0, f1 = prev_f.iat[i], g.at[i, "CFACPR"]
                if pd.notna(f0) and pd.notna(f1) and f1 > 0 and abs(f0 / f1 - 1) > 1e-9:
                    rows.append({"symbol": sym, "ex_date": d, "action_type": "split", "ratio": float(f0 / f1),
                                 "amount": None})
                    continue
                p0, p1, r = prev_p.iat[i], g.at[i, "close"], g.at[i, "RET"]
                if pd.notna(p0) and pd.notna(p1) and pd.notna(r):
                    div = r * p0 - (p1 - p0)
                    if div >= 1e-4:
                        rows.append({"symbol": sym, "ex_date": d, "action_type": "cash_dividend", "ratio": None,
                                     "amount": round(float(div), 6)})
        return pd.DataFrame(rows, columns=ACTION_COLUMNS)

    def default_history_range(self) -> tuple[str, str]:
        df = self.frame()
        return df["date"].min(), df["date"].max()
