"""Factor-model alpha test: Fama-French 5 factors + momentum (Kenneth R. French Data Library).

  r_strategy,m − RF_m = α + β_mkt·(Mkt−RF) + β_smb·SMB + β_hml·HML + β_rmw·RMW + β_cma·CMA + β_mom·Mom + ε_m

Monthly factor returns (percent in the source files) are downloaded from
  https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_CSV.zip
  https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_CSV.zip
and cached under data/factors (refreshed weekly). The library publishes with a lag of about 1-2 months,
so the most recent months of a backtest may be excluded; the months used are reported.

Standard errors: Newey-West HAC with Bartlett weights and L = floor(4·(T/100)^(2/9)) lags.
The first backtest month is dropped (it is partial / pre-investment).
"""

from __future__ import annotations

import io
import math
import re
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
FILES = {"ff5": "F-F_Research_Data_5_Factors_2x3_CSV.zip", "mom": "F-F_Momentum_Factor_CSV.zip"}
FACTORS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"]
_MONTH_ROW = re.compile(r"^\s*(\d{6})\s*,(.*)$")


def parse_french_csv(text: str) -> pd.DataFrame:
    """Parse the MONTHLY block of a Ken French CSV (the first run of YYYYMM rows after a header row)."""
    lines = text.splitlines()
    header = None
    rows = []
    for i, line in enumerate(lines):
        m = _MONTH_ROW.match(line)
        if m:
            if header is None:
                header = [h.strip() for h in lines[i - 1].split(",")][1:]
            rows.append([m.group(1)] + [float(x) for x in m.group(2).split(",")])
        elif rows:
            break  # end of the monthly block (annual block follows)
    if header is None:
        raise ValueError("no monthly block found in factor file")
    df = pd.DataFrame(rows, columns=["yyyymm"] + header).set_index("yyyymm")
    df.columns = [c.strip() for c in df.columns]
    return df / 100.0


def load_factors(cache_dir: Path, transport: httpx.BaseTransport | None = None, max_age_days: float = 7) -> pd.DataFrame:
    cache_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for key, fname in FILES.items():
        path = cache_dir / fname
        if not path.exists() or time.time() - path.stat().st_mtime > max_age_days * 86400:
            with httpx.Client(timeout=60, transport=transport, follow_redirects=True) as c:
                r = c.get(BASE + fname)
                r.raise_for_status()
                path.write_bytes(r.content)
        with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as z:
            text = z.read(z.namelist()[0]).decode("latin-1")
        frames.append(parse_french_csv(text))
    ff = frames[0].join(frames[1], how="inner")
    ff = ff.rename(columns={c: "Mom" for c in ff.columns if c.lower().startswith("mom")})
    return ff[FACTORS + ["RF"]]


def newey_west_ols(y: np.ndarray, X: np.ndarray, lags: int | None = None) -> dict:
    T, k = X.shape
    lags = int(math.floor(4 * (T / 100) ** (2 / 9))) if lags is None else lags
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    e = y - X @ beta
    u = X * e[:, None]
    S = u.T @ u
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1)
        G = u[L:].T @ u[:-L]
        S += w * (G + G.T)
    cov = XtX_inv @ S @ XtX_inv
    se = np.sqrt(np.diag(cov))
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return {"beta": beta, "se": se, "t": beta / se, "r2": 1 - float((e ** 2).sum()) / ss_tot if ss_tot else None,
            "lags": lags, "n": T}


class RateSource:
    """Annualized risk-free rate per session from the monthly Ken French RF (monthly RF x 12).

    Months not yet published (library lag) use the latest available month and are recorded in `carried`."""

    def __init__(self, factors: pd.DataFrame):
        rf = factors["RF"].dropna()
        self.by_month = {k: float(v) * 12 for k, v in rf.items()}
        self.last_month = max(self.by_month)
        self.carried: set[str] = set()

    def annual(self, session: str) -> float:
        key = session[:4] + session[5:7]
        if key in self.by_month:
            return self.by_month[key]
        if key > self.last_month:
            self.carried.add(key)
            return self.by_month[self.last_month]
        return 0.0


def alpha_test(monthly: list[dict], factors: pd.DataFrame, excess: bool = False) -> dict:
    """monthly: [{'year','month','return'}] strategy returns (decimal).

    excess=True (cash earns RF in the backtest): dependent variable r - RF.
    excess=False (cash earns nothing): dependent variable r, because subtracting RF would charge the strategy for
    interest it never received."""
    s = pd.Series({f"{m['year']:04d}{m['month']:02d}": m["return"] for m in monthly}).iloc[1:]  # drop first month
    df = pd.DataFrame({"r": s}).join(factors, how="inner").dropna()
    notes = []
    if len(s) and len(df) < len(s):
        notes.append(f"{len(s) - len(df)} strategy month(s) not yet in the factor library (publication lag) were excluded.")

    def fit(sub: pd.DataFrame) -> dict | None:
        if len(sub) < 24:
            return None
        y = (sub["r"] - sub["RF"]).to_numpy() if excess else sub["r"].to_numpy()
        X = np.column_stack([np.ones(len(sub))] + [sub[f].to_numpy() for f in FACTORS])
        r = newey_west_ols(y, X)
        return {
            "months": len(sub), "start": sub.index[0], "end": sub.index[-1],
            "alpha_monthly": float(r["beta"][0]), "alpha_annual": float(r["beta"][0] * 12),
            "t_alpha": float(r["t"][0]),
            "betas": {f: float(b) for f, b in zip(FACTORS, r["beta"][1:])},
            "t_betas": {f: float(t) for f, t in zip(FACTORS, r["t"][1:])},
            "r2": r["r2"], "nw_lags": r["lags"],
        }

    half = len(df) // 2
    return {
        "model": "Fama-French 5 factors (2x3) + momentum; dependent variable = "
                 + ("strategy return - RF (cash earns RF in this backtest)" if excess
                    else "strategy return (cash earns no interest in this backtest, so RF is not subtracted)")
                 + "; Newey-West t-stats",
        "excess_returns": excess,
        "source": "Kenneth R. French Data Library (" + ", ".join(FILES.values()) + ")",
        "full": fit(df), "first_half": fit(df.iloc[:half]), "second_half": fit(df.iloc[half:]),
        "notes": notes + (["Each half needs at least 24 months; shorter samples are not estimated."] if half < 24 else []),
    }
