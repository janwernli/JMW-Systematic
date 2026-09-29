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
  5b. Sector neutrality (applied AFTER the crash guard, so the limits hold for the book that is traded;
     with the guard on, this caps how net-long the book can get): a small quadratic program moves the weights as little as possible
     (relative squared deviation from the inverse-vol weights) so that |long - short| per sector
     <= `max_sector_net`, keeping each side's gross, beta neutrality and per-name caps. If it is
     infeasible, the unconstrained weights are kept and the failure is reported.
  5c. Hard-to-borrow stand-in: the least liquid `htb_exclude_pct` of eligible stocks by
     `htb_adv_window`-session dollar volume cannot be shorted (live trading also checks
     Alpaca's easy-to-borrow flag at order time).
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
    out = np.ones(N)  # insufficient history -> the shrinkage prior beta = 1
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
    htb_threshold = None
    if cfg.htb_exclude_pct > 0 and "adv60" in table.columns:
        adv60 = dict(zip(table["symbol"], table["adv60"]))
        vals = np.array([adv60.get(x) for x in elig["symbol"] if adv60.get(x) is not None and adv60.get(x) == adv60.get(x)],
                        dtype=float)
        if vals.size:
            htb_threshold = float(np.quantile(vals, cfg.htb_exclude_pct))
            before = len(shortable)
            shortable = [x for x in shortable if (adv60.get(x) or 0) >= htb_threshold]
            diag["htb_excluded"] = before - len(shortable)
    diag["htb_adv_threshold"] = htb_threshold
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

    # ---- sector neutrality (after the crash guard, so it sees the final short book) -----------------
    sector_of = dict(zip(table["symbol"], table["sector"])) if "sector" in table.columns else {}
    sector_info = {"enabled": cfg.sector_neutral, "applied": False, "max_abs_net": None, "status": "disabled"}
    if cfg.sector_neutral:
        secL = [sector_of.get(x) if isinstance(sector_of.get(x), str) else None for x in longs]
        secS = [sector_of.get(x) if isinstance(sector_of.get(x), str) else None for x in shorts]
        wL, wS, sector_info = sector_neutralize(wL, wS, bL_i, bS_i, secL, secS, cfg.max_long_weight,
                                                cfg.max_short_weight, cfg.max_sector_net)
        if sector_info["status"] not in ("ok", "already_neutral"):
            diag["notes"].append(f"Sector neutrality: {sector_info['status']}")

    # Sector nets of the FINAL weights (after crash guard and neutralization), always reported.
    final_net: dict[str, float] = {}
    for sym, w in zip(longs, wL):
        sec = sector_of.get(sym) if isinstance(sector_of.get(sym), str) else "Unclassified"
        final_net[sec] = final_net.get(sec, 0.0) + float(w)
    for sym, w in zip(shorts, wS):
        sec = sector_of.get(sym) if isinstance(sector_of.get(sym), str) else "Unclassified"
        final_net[sec] = final_net.get(sec, 0.0) - float(w)
    classified = {k: v for k, v in final_net.items() if k != "Unclassified"}
    sector_info["final_sector_net"] = final_net
    sector_info["final_max_abs_net"] = max((abs(v) for v in classified.values()), default=None)

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
        cap_capacity=[capL_tot, capS_tot], binding=binding, crash_guard=crash, sector_neutrality=sector_info,
        max_long_weight=float(wL.max()), max_short_weight=float(wS.max()),
    )
    return diag


def sector_neutralize(wL: np.ndarray, wS: np.ndarray, bL: np.ndarray, bS: np.ndarray, secL: list[str | None],
                      secS: list[str | None], capL: float, capS: float, max_net: float):
    """Closest weights (relative squared deviation) with |net| <= max_net per classified sector.

    Attempt 1 keeps each side's gross, the book's beta and per-name caps. If that is infeasible (checked
    exactly with an LP), attempt 2 lets each side's gross shrink (beta neutrality and caps still hold): the
    sector constraint takes priority over reaching the vol target, and the reduction is reported.
    Unclassified names are unconstrained. On failure the input weights are returned unchanged.
    """
    from scipy.optimize import linprog, minimize

    sectors = sorted({x for x in secL + secS if x})
    nL, nS = len(wL), len(wS)

    def nets(a: np.ndarray, b: np.ndarray) -> dict[str, float]:
        return {sec: float(sum(w for w, x in zip(a, secL) if x == sec) - sum(w for w, x in zip(b, secS) if x == sec))
                for sec in sectors}

    before = nets(wL, wS)
    info = {"enabled": True, "applied": False, "max_net": max_net, "sectors": len(sectors),
            "unclassified": int(sum(1 for x in secL + secS if not x)),
            "max_abs_net_before": max((abs(v) for v in before.values()), default=0.0), "gross_reduced": False}
    if not sectors:
        info.update(status="no sector data", sector_net=before, max_abs_net=None)
        return wL, wS, info
    if info["max_abs_net_before"] <= max_net + 1e-12:
        info.update(status="already_neutral", sector_net=before, max_abs_net=info["max_abs_net_before"])
        return wL, wS, info

    k = 100.0  # optimise in percent-of-NAV units (better conditioning)
    x0 = np.concatenate([wL, wS]) * k
    S = np.zeros((len(sectors), nL + nS))
    for i, sec in enumerate(sectors):
        S[i, :nL] = [1.0 if x == sec else 0.0 for x in secL]
        S[i, nL:] = [-1.0 if x == sec else 0.0 for x in secS]
    G = np.vstack([np.r_[np.ones(nL), np.zeros(nS)], np.r_[np.zeros(nL), np.ones(nS)]])
    B = np.r_[bL, -bS][None, :]
    g0, beta0 = G @ x0, B @ x0
    bounds = [(0.0, capL * k)] * nL + [(0.0, capS * k)] * nS
    m = max_net * k
    A_ub_sec = np.vstack([S, -S])
    b_ub_sec = np.full(2 * len(sectors), m)

    attempts = [("keep gross", np.vstack([G, B]), np.r_[g0, beta0], None, None),
                ("reduce gross", B, beta0, G, g0)]
    for label, A_eq, b_eq, A_extra, b_extra in attempts:
        A_ub = A_ub_sec if A_extra is None else np.vstack([A_ub_sec, A_extra])
        b_ub = b_ub_sec if b_extra is None else np.r_[b_ub_sec, b_extra]
        lp = linprog(np.zeros(nL + nS), A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
        if lp.status != 0:
            continue
        cons = [{"type": "eq", "fun": lambda x, A=A_eq, b=b_eq: A @ x - b, "jac": lambda x, A=A_eq: A},
                {"type": "ineq", "fun": lambda x, A=A_ub, b=b_ub: b - A @ x, "jac": lambda x, A=A_ub: -A}]
        start = np.clip(lp.x, 1e-9, None) if label == "reduce gross" else x0
        res = minimize(lambda x: float(np.sum((x - x0) ** 2 / x0)), start, jac=lambda x: 2 * (x - x0) / x0,
                       method="SLSQP", bounds=bounds, constraints=cons, options={"maxiter": 1000, "ftol": 1e-12})
        x = np.clip(res.x if res.success else lp.x, 0, None)
        ok = (np.all(np.abs(S @ x) <= m + 1e-6) and np.allclose(A_eq @ x, b_eq, atol=1e-6)
              and np.all(x[:nL] <= capL * k + 1e-7) and np.all(x[nL:] <= capS * k + 1e-7))
        if not ok:
            continue
        x = x / k
        after = nets(x[:nL], x[nL:])
        reduced = label == "reduce gross"
        info.update(applied=True, status="ok" if not reduced else "ok (gross reduced to meet sector limits)",
                    sector_net=after, max_abs_net=max(abs(v) for v in after.values()), gross_reduced=reduced,
                    solver="SLSQP" if res.success else "LP vertex (QP did not converge)",
                    turnover_vs_unconstrained=float(np.abs(x * k - x0).sum() / k))
        return x[:nL], x[nL:], info
    info.update(status="infeasible even with reduced gross; unconstrained weights kept", sector_net=before,
                max_abs_net=info["max_abs_net_before"])
    return wL, wS, info
