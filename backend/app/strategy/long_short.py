"""v2 long-short book construction and sizing (all inputs at or before the signal session t).

Steps (documented in the UI and README):
  1. Rank eligible stocks by 12-1 momentum (rank 1 = strongest). n = eligible count.
     percentile = (rank - 1) / n  (0 = best).
  2. Books with a buffer:
       long  = held longs still in the top `buffer_exit_pct`  ∪  new names in the top `long_pct`
       short = held shorts still in the bottom `buffer_exit_pct` ∪ new names in the bottom `short_pct`
     Shorts additionally need raw close > `short_min_price` and must not have been stopped out
     since the previous signal. Each book is filled with the next ranks up to `min_names_per_side`
     and trimmed to the best `max_names_per_side` (never more than half the eligible names).
  3. Within each side, weights ∝ 1 / realized vol (trailing `vol_lookback_sessions`), with per-name caps
     (`max_long_weight`, `max_short_weight`) enforced by water-filling.
  4. Beta neutrality: short gross = long gross × β_long / β_short (β shrunk toward 1).
  5. Volatility target: long gross = target_vol / σ(unit portfolio), where σ is the realized vol of the
     beta-neutral portfolio's trailing daily returns; bounded by min/max gross per side, the total
     gross cap, and per-name cap capacity. If neutrality and the minimum gross conflict, the minimum
     gross is relaxed (caps and neutrality take priority) and the conflict is reported.
  6. Crash guard: if the benchmark's trailing 24-month total return < 0 AND its 6-month realized vol
     > `crash_market_vol_threshold`, the short book is multiplied by `crash_short_scale`.
"""

from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd

from ..data.panel import Panel
from .config import StrategyConfig


def _daily_returns(panel: Panel, t: int, window: int) -> np.ndarray:
    """Total-return daily returns for sessions (t-window, t], shape (window, N). NaN where missing."""
    lo = max(t - window, 0)
    tr = panel.tr[lo: t + 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        r = tr[1:] / tr[:-1] - 1.0
    return r


def realized_vol(panel: Panel, t: int, window: int, min_frac: float = 0.8) -> np.ndarray:
    r = _daily_returns(panel, t, window)
    n = np.sum(~np.isnan(r), axis=0)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN columns (not yet listed) -> NaN, filtered below
        v = np.nanstd(r, axis=0, ddof=1) * math.sqrt(252)
    v[n < max(int(window * min_frac), 10)] = np.nan
    v[v <= 0] = np.nan
    return v


def betas(panel: Panel, t: int, window: int, bench_col: int | None, shrink: float) -> np.ndarray:
    N = len(panel.symbols)
    if bench_col is None:
        return np.ones(N)
    r = _daily_returns(panel, t, window)
    m = r[:, bench_col]
    out = np.full(N, np.nan)
    ok_m = ~np.isnan(m)
    for j in range(N):
        ok = ok_m & ~np.isnan(r[:, j])
        if ok.sum() < max(int(window * 0.6), 20):
            continue
        x, y = m[ok], r[ok, j]
        var = np.var(x, ddof=1)
        if var > 0:
            out[j] = np.cov(x, y, ddof=1)[0, 1] / var
    return (1 - shrink) * out + shrink * 1.0


def waterfill(raw: np.ndarray, total: float, cap: float) -> np.ndarray:
    """Scale raw (positive) weights to sum to `total` with each weight <= cap (excess redistributed)."""
    raw = np.asarray(raw, dtype=float)
    if raw.size == 0 or total <= 0:
        return np.zeros_like(raw)
    if raw.size * cap <= total + 1e-12:
        return np.full_like(raw, cap)
    w = np.zeros_like(raw)
    free = np.ones(raw.size, dtype=bool)
    remaining = total
    for _ in range(raw.size + 1):
        share = raw[free] / raw[free].sum() * remaining
        over = share > cap + 1e-15
        if not over.any():
            w[free] = share
            break
        idx = np.flatnonzero(free)[over]
        w[idx] = cap
        free[idx] = False
        remaining = total - w[~free].sum()
    return w


def build_books(panel: Panel, t: int, cfg: StrategyConfig, table: pd.DataFrame, held_long: set[str],
                held_short: set[str], blocked_shorts: set[str]) -> dict:
    """Mutates `table` (side, selected, target_weight, percentile, vol, beta) and returns diagnostics."""
    elig = table[table["eligible"]].sort_values("rank")
    n = len(elig)
    diag: dict = {"eligible": n, "notes": []}
    table["side"] = None
    table["percentile"] = np.nan
    table["vol"] = np.nan
    table["beta"] = np.nan
    if n < 2:
        diag["notes"].append("Fewer than two eligible stocks: no long-short books.")
        return diag

    j_of = panel.sym_index
    vol = realized_vol(panel, t, cfg.vol_lookback_sessions)
    bench_col = panel.sym_index.get(cfg.benchmark_symbol or panel.benchmark or "")
    beta = betas(panel, t, cfg.beta_lookback_sessions, bench_col, cfg.beta_shrink)
    ranks = {s: int(r) for s, r in zip(elig["symbol"], elig["rank"])}
    pct = {s: (r - 1) / n for s, r in ranks.items()}
    idx = table.index[table["eligible"]]
    table.loc[idx, "percentile"] = [pct[s] for s in table.loc[idx, "symbol"]]
    table.loc[idx, "vol"] = [vol[j_of[s]] for s in table.loc[idx, "symbol"]]
    table.loc[idx, "beta"] = [beta[j_of[s]] for s in table.loc[idx, "symbol"]]

    usable = [s for s in elig["symbol"] if not np.isnan(vol[j_of[s]]) and not np.isnan(beta[j_of[s]])]
    close = dict(zip(table["symbol"], table["close_raw"]))
    shortable = [s for s in usable if (close.get(s) or 0) > cfg.short_min_price and s not in blocked_shorts]
    side_cap = min(cfg.max_names_per_side, n // 2)
    side_min = min(cfg.min_names_per_side, side_cap)

    def book(cands: list[str], held: set[str], entry_pct: float, keep_pct: float, dist) -> list[str]:
        # cands ordered from most to least attractive for this side; dist(s) = distance from that extreme
        keep = [s for s in cands if s in held and dist(s) < keep_pct]
        new = [s for s in cands if dist(s) < entry_pct]
        chosen = sorted(set(keep) | set(new), key=cands.index)
        for s in cands:  # fill to the minimum with the next ranks
            if len(chosen) >= side_min:
                break
            if s not in chosen:
                chosen.append(s)
        return sorted(chosen, key=cands.index)[:side_cap]

    longs = book(usable, held_long, cfg.long_pct, cfg.buffer_exit_pct, lambda s: pct[s])
    shortable_set, long_set = set(shortable), set(longs)
    short_cands = [s for s in reversed(usable) if s in shortable_set and s not in long_set]
    # distance from the bottom: the worst-ranked stock has distance 0
    shorts = book(short_cands, held_short, cfg.short_pct, cfg.buffer_exit_pct, lambda s: 1 - pct[s] - 1 / n)
    diag.update(long_names=len(longs), short_names=len(shorts), shortable=len(shortable),
                blocked_shorts=sorted(blocked_shorts))
    if not longs or not shorts:
        diag["notes"].append("One side is empty: rebalance blocked.")
        return diag

    rawL = np.array([1 / vol[j_of[s]] for s in longs])
    rawS = np.array([1 / vol[j_of[s]] for s in shorts])
    bL_i = np.array([beta[j_of[s]] for s in longs])
    bS_i = np.array([beta[j_of[s]] for s in shorts])
    R = _daily_returns(panel, t, cfg.vol_lookback_sessions)
    RL = np.nan_to_num(R[:, [j_of[s] for s in longs]])
    RS = np.nan_to_num(R[:, [j_of[s] for s in shorts]])
    capL_tot = len(longs) * cfg.max_long_weight
    capS_tot = len(shorts) * cfg.max_short_weight

    GL = GS = (cfg.min_side_gross + cfg.max_side_gross) / 2
    for _ in range(4):
        wL = waterfill(rawL, min(GL, capL_tot), cfg.max_long_weight)
        wS = waterfill(rawS, min(GS, capS_tot), cfg.max_short_weight)
        betaL = float(wL @ bL_i / wL.sum())
        betaS = float(wS @ bS_i / wS.sum())
        ratio = betaL / betaS if betaS > 0 else 1.0
        unit = RL @ (wL / wL.sum()) - ratio * (RS @ (wS / wS.sum()))
        sig_unit = float(np.std(unit, ddof=1) * math.sqrt(252)) if len(unit) > 2 else float("nan")
        g_star = cfg.target_vol / sig_unit if sig_unit and sig_unit > 0 else cfg.min_side_gross
        hi = min(cfg.max_side_gross, cfg.max_side_gross / ratio, cfg.max_total_gross / (1 + ratio),
                 capL_tot, capS_tot / ratio)
        lo = max(cfg.min_side_gross, cfg.min_side_gross / ratio)
        feasible = lo <= hi
        # Minimum gross is relaxed only when it conflicts with caps/neutrality; the vol target stays a ceiling.
        GL = min(max(g_star, lo), hi) if feasible else min(g_star, hi)
        GS = ratio * GL

    wL = waterfill(rawL, GL, cfg.max_long_weight)
    wS = waterfill(rawS, GS, cfg.max_short_weight)
    binding = []
    if not feasible:
        binding.append("minimum side gross relaxed to keep beta neutrality within per-name caps / gross limits")
        diag["notes"].append(binding[-1])
    if abs(GL - hi) < 1e-9 and g_star > hi:
        binding.append("vol target capped by gross / per-name capacity")
    if feasible and abs(GL - lo) < 1e-9 and g_star < lo:
        binding.append("vol target below minimum gross (minimum applied)")

    # ---- crash guard -------------------------------------------------------------------------------
    crash = {"enabled": cfg.crash_guard, "active": False, "market_return": None, "market_vol": None}
    if cfg.crash_guard and bench_col is not None:
        t0 = t - cfg.crash_market_lookback_sessions
        mret = None
        if t0 >= 0 and not np.isnan(panel.tr[t0, bench_col]) and not np.isnan(panel.tr[t, bench_col]):
            mret = float(panel.tr[t, bench_col] / panel.tr[t0, bench_col] - 1)
        mvol = realized_vol(panel, t, cfg.vol_lookback_sessions)[bench_col]
        mvol = None if np.isnan(mvol) else float(mvol)
        crash.update(market_return=mret, market_vol=mvol)
        if mret is not None and mvol is not None and mret < 0 and mvol > cfg.crash_market_vol_threshold:
            crash["active"] = True
            wS = wS * cfg.crash_short_scale
            diag["notes"].append(f"Crash guard ON: market 24m return {mret:.1%} < 0 and 6m vol {mvol:.1%} > "
                                 f"{cfg.crash_market_vol_threshold:.0%}; short book × {cfg.crash_short_scale:g}.")
    elif cfg.crash_guard:
        diag["notes"].append("Crash guard inactive: benchmark data unavailable.")

    weights = {s: float(w) for s, w in zip(longs, wL)} | {s: -float(w) for s, w in zip(shorts, wS)}
    port = RL @ wL - RS @ wS
    net_beta = float(wL @ bL_i - wS @ bS_i)
    sel_idx = table.index[table["symbol"].isin(weights)]
    table.loc[sel_idx, "selected"] = True
    table.loc[sel_idx, "target_weight"] = [weights[s] for s in table.loc[sel_idx, "symbol"]]
    table.loc[sel_idx, "side"] = ["long" if weights[s] > 0 else "short" for s in table.loc[sel_idx, "symbol"]]
    diag.update(
        long_gross=float(wL.sum()), short_gross=float(wS.sum()), net_exposure=float(wL.sum() - wS.sum()),
        beta_long=betaL, beta_short=betaS, beta_ratio=ratio, ex_ante_net_beta=net_beta,
        unit_vol=sig_unit, vol_target=cfg.target_vol, ex_ante_vol=float(np.std(port, ddof=1) * math.sqrt(252)),
        gross_bounds=[cfg.min_side_gross, cfg.max_side_gross], total_gross_cap=cfg.max_total_gross,
        cap_capacity=[capL_tot, capS_tot], binding=binding, crash_guard=crash,
        max_long_weight=float(wL.max()), max_short_weight=float(wS.max()),
    )
    return diag
