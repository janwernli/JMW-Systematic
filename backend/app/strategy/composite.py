"""Composite momentum signal (all inputs at or before the signal session t).

Components, each winsorized at mean ± k·σ and z-scored across the eligible cross-section:

  1. Residual momentum (default weight 60%)
       Monthly total returns at month-end sessions. For each stock, OLS over the last
       `residual_window_months` months (ending at the signal month) of
           r_stock = a + b_m · r_market + b_s · r_sector_ETF + e
       (market only if the sector or its ETF is unavailable in the window).
       Score = Σ e over the formation months t-11 … t-1 (the most recent month is skipped,
       mirroring 12-1) divided by the standard deviation of those residuals.
       (Blitz, Huij & Martens 2011; Gutierrez & Pirinsky 2007.)
  2. Sector-demeaned 12-1 momentum (25%)
       Plain 12-1 minus the mean 12-1 of eligible stocks in the same sector.
  3. Frog-in-the-pan / information discreteness (15%)
       ID = sgn(PRET) · (%negative days − %positive days) over the 12-1 formation window
       (Da, Gurun & Warachka 2014). Smooth ("continuous") paths have low ID. The directional
       score used for ranking is sgn(PRET) · (−ID) = %positive − %negative days, so smooth
       winners rank high (long) and smooth losers rank low (short).

Composite = Σ w_k z_k / Σ w_k over the components available for that stock.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.panel import Panel
from .config import StrategyConfig

SECTOR_ETF = {
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}


def _ffill(a: np.ndarray) -> np.ndarray:
    """Forward-fill NaNs along axis 0."""
    out = a.copy()
    for i in range(1, out.shape[0]):
        m = np.isnan(out[i])
        out[i, m] = out[i - 1, m]
    return out


def month_ends_up_to(panel: Panel, t: int) -> list[int]:
    """Session indices of month-ends strictly before t, followed by t itself (the signal month-end)."""
    cache = getattr(panel, "_month_end_idx", None)
    if cache is None:
        s = panel.sessions
        cache = [i for i in range(len(s) - 1) if s[i][:7] != s[i + 1][:7]]
        panel._month_end_idx = cache  # type: ignore[attr-defined]
    return [i for i in cache if i < t] + [t]


def _tr_filled(panel: Panel) -> np.ndarray:
    tf = getattr(panel, "_tr_ffill", None)
    if tf is None:
        tf = _ffill(panel.tr)
        panel._tr_ffill = tf  # type: ignore[attr-defined]
    return tf


def residual_momentum(panel: Panel, t: int, cols: list[int], sectors: list[str | None], window: int,
                      bench_col: int | None) -> np.ndarray:
    out = np.full(len(cols), np.nan)
    me = month_ends_up_to(panel, t)
    if bench_col is None or len(me) < window + 1:
        return out
    idx = me[-(window + 1):]
    tf = _tr_filled(panel)[idx]
    with np.errstate(invalid="ignore", divide="ignore"):
        R = tf[1:] / tf[:-1] - 1.0          # (window, N) monthly total returns
    mkt = R[:, bench_col]
    etf_cols = {sec: panel.sym_index.get(etf) for sec, etf in SECTOR_ETF.items()}
    form = slice(window - 12, window - 1)  # 11 formation months, skipping the most recent
    for k, (j, sec) in enumerate(zip(cols, sectors)):
        y = R[:, j]
        xs = [np.ones(window), mkt]
        sc = etf_cols.get(sec) if sec else None
        if sc is not None and np.isfinite(R[:, sc]).all():
            xs.append(R[:, sc])
        X = np.column_stack(xs)
        ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if ok.sum() < max(24, X.shape[1] + 10):
            continue
        beta, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        e = y - X @ beta
        ef = e[form]
        ef = ef[np.isfinite(ef)]
        if len(ef) < 8:
            continue
        sd = np.std(ef, ddof=1)
        if sd > 0:
            out[k] = ef.sum() / sd
    return out


def fip_score(panel: Panel, t: int, cols: list[int], skip: int, lookback: int) -> np.ndarray:
    """sgn(PRET) · (−ID) = %positive − %negative daily returns over (t-lookback, t-skip]."""
    lo, hi = t - lookback, t - skip
    if lo < 0:
        return np.full(len(cols), np.nan)
    tr = panel.tr[lo: hi + 1][:, cols]
    with np.errstate(invalid="ignore", divide="ignore"):
        r = tr[1:] / tr[:-1] - 1.0
    n = np.sum(np.isfinite(r), axis=0)
    pos = np.sum(r > 0, axis=0)
    neg = np.sum(r < 0, axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        score = (pos - neg) / n
    score[n < (hi - lo) * 0.8] = np.nan
    return score


def winsor_z(x: np.ndarray, k: float) -> np.ndarray:
    """Clip to mean ± k·std, then z-score (computed on finite values)."""
    out = np.full_like(x, np.nan, dtype=float)
    ok = np.isfinite(x)
    if ok.sum() < 3:
        return out
    mu, sd = x[ok].mean(), x[ok].std(ddof=1)
    if not sd > 0:
        out[ok] = 0.0
        return out
    c = np.clip(x[ok], mu - k * sd, mu + k * sd)
    sd2 = c.std(ddof=1)
    out[ok] = (c - c.mean()) / sd2 if sd2 > 0 else 0.0
    return out


def add_composite(panel: Panel, t: int, cfg: StrategyConfig, table: pd.DataFrame) -> dict:
    """Adds component columns + `composite` for eligible rows; returns coverage diagnostics."""
    table["sector"] = [panel.instruments.at[s, "sector"] if isinstance(panel.instruments.at[s, "sector"], str) else None
                       for s in table["symbol"]]
    for c in ("resid_mom", "sector_mom", "fip", "composite"):
        table[c] = np.nan
    el = table.index[table["eligible"]]
    if len(el) == 0:
        return {}
    syms = list(table.loc[el, "symbol"])
    cols = [panel.sym_index[s] for s in syms]
    secs = list(table.loc[el, "sector"])
    bench_col = panel.sym_index.get(cfg.benchmark_symbol or panel.benchmark or "")

    resid = residual_momentum(panel, t, cols, secs, cfg.residual_window_months, bench_col)
    mom = table.loc[el, "momentum"].astype(float).to_numpy()
    sec_key = pd.Series([s or "Unclassified" for s in secs])
    sec_mom = mom - pd.Series(mom).groupby(sec_key).transform("mean").to_numpy()
    fip = fip_score(panel, t, cols, cfg.skip_sessions, cfg.lookback_sessions)

    comps = [(resid, cfg.w_residual), (sec_mom, cfg.w_sector_demeaned), (fip, cfg.w_fip)]
    num = np.zeros(len(el))
    den = np.zeros(len(el))
    for raw, w in comps:
        z = winsor_z(raw, cfg.winsor_sigma)
        ok = np.isfinite(z)
        num[ok] += w * z[ok]
        den[ok] += w
    with np.errstate(invalid="ignore", divide="ignore"):
        comp = np.where(den > 0, num / den, np.nan)
    table.loc[el, "resid_mom"] = resid
    table.loc[el, "sector_mom"] = sec_mom
    table.loc[el, "fip"] = fip
    table.loc[el, "composite"] = comp
    return {
        "signal": "composite",
        "weights": {"residual": cfg.w_residual, "sector_demeaned": cfg.w_sector_demeaned, "fip": cfg.w_fip},
        "coverage": {"residual": int(np.isfinite(resid).sum()), "sector_demeaned": int(np.isfinite(sec_mom).sum()),
                     "fip": int(np.isfinite(fip).sum()), "eligible": len(el)},
        "unclassified_sector": int(sum(1 for s in secs if not s)),
    }


def sector_neutral_z(x: np.ndarray, sectors: list[str | None], k: float) -> np.ndarray:
    """Winsorize at mean +/- k sd, subtract the sector mean, then z-score across names (finite values only)."""
    out = np.full(len(x), np.nan)
    ok = np.isfinite(x)
    if ok.sum() < 3:
        return out
    mu, sd = x[ok].mean(), x[ok].std(ddof=1)
    c = np.where(ok, np.clip(x, mu - k * sd, mu + k * sd) if sd > 0 else x, np.nan)
    key = pd.Series([s or "Unclassified" for s in sectors])
    dem = c - pd.Series(c).groupby(key).transform("mean").to_numpy()
    sd2 = np.nanstd(dem[ok], ddof=1)
    out[ok] = dem[ok] / sd2 if sd2 > 0 else 0.0
    return out


def add_value_momentum(panel: Panel, t: int, cfg: StrategyConfig, table: pd.DataFrame) -> dict:
    """value_momentum score = (1 - w_value) x composite z + w_value x value z, for eligible rows.

    value z = z-score of the mean of the sector-neutral, winsorized z-scores of book-to-market and earnings
    yield, from point-in-time fundamentals (filed <= signal date). Names without value data keep the composite
    z only (coverage reported)."""
    for c in ("bm", "ep", "value_z", "vm_score"):
        table[c] = np.nan
    el = table.index[table["eligible"]]
    if len(el) == 0:
        return {}
    mom_z = winsor_z(table.loc[el, "composite"].astype(float).to_numpy(), cfg.winsor_sigma)
    fund = panel.fundamentals
    asof = panel.sessions[t]
    syms = list(table.loc[el, "symbol"])
    bm = np.full(len(el), np.nan)
    ep = np.full(len(el), np.nan)
    if fund is not None and len(fund):
        sessions = panel.sessions
        for k, sym in enumerate(syms):
            j = panel.sym_index[sym]
            splits = panel.split_ratio[:, j]

            def factor(end: str, j=j, splits=splits) -> float:
                # splits on sessions after the share count's date, up to and including t
                i0 = int(np.searchsorted(sessions, end, side="right"))
                return float(np.prod(splits[i0:t + 1])) if i0 <= t else 1.0

            r = fund.ratios(sym, asof, float(panel.close[t, j]), factor)
            bm[k], ep[k] = r["bm"], r["ep"]
    secs = list(table.loc[el, "sector"]) if "sector" in table.columns else [None] * len(el)
    zb = sector_neutral_z(bm, secs, cfg.winsor_sigma)
    ze = sector_neutral_z(ep, secs, cfg.winsor_sigma)
    both = np.vstack([zb, ze])
    n_ok = np.isfinite(both).sum(axis=0)
    with np.errstate(invalid="ignore"):
        raw_val = np.where(n_ok > 0, np.nansum(both, axis=0) / np.maximum(n_ok, 1), np.nan)
    val_z = winsor_z(raw_val, cfg.winsor_sigma)
    w = cfg.w_value
    score = np.where(np.isfinite(val_z), (1 - w) * mom_z + w * val_z, mom_z)
    table.loc[el, "bm"] = bm
    table.loc[el, "ep"] = ep
    table.loc[el, "value_z"] = val_z
    table.loc[el, "vm_score"] = score
    return {"w_value": w, "fundamentals_loaded": fund is not None and len(fund) > 0, "eligible": len(el),
            "coverage": {"book_to_market": int(np.isfinite(bm).sum()), "earnings_yield": int(np.isfinite(ep).sum()),
                         "value": int(np.isfinite(val_z).sum())},
            "note": None if fund is not None and len(fund) else
            "No point-in-time fundamentals loaded (run `python -m app fundamentals`): ranking uses momentum only."}
