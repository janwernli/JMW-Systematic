"""12-1 momentum signal formation, eligibility and ranking at one signal session.

Only information at or before the signal session's close is read: every array
access below is at row index <= t.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..data.panel import Panel
from .config import StrategyConfig
from .long_short import build_books

REASONS = {
    "eligible": "Eligible",
    "excluded_asset_type": "Excluded instrument type (not a common stock)",
    "benchmark": "Benchmark instrument (not tradable in strategy)",
    "no_bar_at_signal": "No bar at the signal session (halted / missing data)",
    "insufficient_history": "Fewer valid bars than the minimum history requirement",
    "missing_lookback_price": "Missing price at the t-21 or t-252 lookback session",
    "price_below_min": "Raw close at or below the minimum price",
    "missing_liquidity_data": "Incomplete dollar-volume window",
    "adv_below_min": "Average daily dollar volume below the minimum",
    "delisting_at_signal": "Delisting on the signal session",
}


@dataclass
class SignalResult:
    session: str
    t: int
    table: pd.DataFrame          # one row per candidate instrument
    universe_count: int          # point-in-time common stocks listed at t
    eligible_count: int
    selected_count: int
    coverage: float              # share of universe with a bar at t
    blocked_reason: str | None   # set when data is insufficient to rebalance
    diagnostics: dict = field(default_factory=dict)   # long-short sizing details (empty for long-only)

    @property
    def selected(self) -> pd.DataFrame:
        """Targets in execution priority: longs by rank (best first), then shorts (worst first)."""
        sel = self.table[self.table["selected"]]
        longs = sel[sel["target_weight"] > 0].sort_values("rank")
        shorts = sel[sel["target_weight"] < 0].sort_values("rank", ascending=False)
        return pd.concat([longs, shorts])

    def target_weights(self) -> dict[str, float]:
        """Signed target weights (fraction of NAV): positive = long, negative = short."""
        sel = self.selected
        return dict(zip(sel["symbol"], sel["target_weight"]))


def compute_signals(panel: Panel, t: int, cfg: StrategyConfig, held_long: set[str] | None = None,
                    held_short: set[str] | None = None, blocked_shorts: set[str] | None = None) -> SignalResult:
    """Eligibility + momentum ranking, then v1 (top-N equal weight) or v2 (long-short books) targets.

    held_long / held_short feed the v2 rank buffer; blocked_shorts = names stopped out since the last signal.
    """
    session = panel.sessions[t]
    inst = panel.instruments
    t21, t252 = t - cfg.skip_sessions, t - cfg.lookback_sessions

    # Point-in-time candidate set: listed on/before t and not delisted before t.
    listed = panel.list_idx <= t
    not_gone = (panel.delist_idx < 0) & (panel.delist_idx != -2) | (panel.delist_idx >= t)
    cand = np.flatnonzero(listed & not_gone)

    close_t = panel.close[t]
    # ADV over the `adv_window` sessions ending at t; NaN if any bar in the window is missing.
    w0 = t - cfg.adv_window + 1
    if w0 >= 0:
        dv = panel.close[w0:t + 1] * panel.volume[w0:t + 1]
        adv = np.where(np.isnan(dv).any(axis=0), np.nan, np.nanmean(np.nan_to_num(dv, nan=0.0), axis=0))
    else:
        adv = np.full(len(panel.symbols), np.nan)
    tr21 = panel.tr[t21] if t21 >= 0 else np.full(len(panel.symbols), np.nan)
    tr252 = panel.tr[t252] if t252 >= 0 else np.full(len(panel.symbols), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        mom = tr21 / tr252 - 1.0
    history = panel.valid_count[t - 1] if t >= 1 else np.zeros(len(panel.symbols), dtype=np.int32)

    rows = []
    universe = 0
    with_bar = 0
    for j in cand:
        sym = panel.symbols[j]
        atype = inst.at[sym, "asset_type"]
        is_bench = bool(inst.at[sym, "is_benchmark"])
        if is_bench:
            reason = "benchmark"
        elif atype != "common_stock":
            reason = "excluded_asset_type"
        else:
            universe += 1
            has_bar = not np.isnan(close_t[j])
            with_bar += has_bar
            if not has_bar:
                reason = "no_bar_at_signal"
            elif t252 < 0 or history[j] < cfg.min_history_sessions:
                reason = "insufficient_history"
            elif np.isnan(tr21[j]) or np.isnan(tr252[j]):
                reason = "missing_lookback_price"
            elif panel.delist_idx[j] == t:
                reason = "delisting_at_signal"
            elif not close_t[j] > cfg.min_price:
                reason = "price_below_min"
            elif np.isnan(adv[j]):
                reason = "missing_liquidity_data"
            elif not adv[j] >= cfg.min_adv_usd:
                reason = "adv_below_min"
            else:
                reason = "eligible"
        rows.append({
            "symbol": sym,
            "asset_type": atype,
            "close_raw": None if np.isnan(close_t[j]) else float(close_t[j]),
            "adv20": None if np.isnan(adv[j]) else float(adv[j]),
            "session_t21": panel.sessions[t21] if t21 >= 0 else None,
            "session_t252": panel.sessions[t252] if t252 >= 0 else None,
            "tr_t21": None if np.isnan(tr21[j]) else float(tr21[j]),
            "tr_t252": None if np.isnan(tr252[j]) else float(tr252[j]),
            "valid_history": int(history[j]),
            "momentum": None if np.isnan(mom[j]) else float(mom[j]),
            "eligible": reason == "eligible",
            "reason": reason,
        })
    table = pd.DataFrame(rows, columns=[
        "symbol", "asset_type", "close_raw", "adv20", "session_t21", "session_t252", "tr_t21", "tr_t252",
        "valid_history", "momentum", "eligible", "reason",
    ])
    table["rank"] = pd.array([pd.NA] * len(table), dtype="Int64")
    table["selected"] = False
    table["target_weight"] = 0.0

    elig = table[table["eligible"]]
    # Deterministic ordering: momentum desc, then ADV desc, then symbol asc.
    ranked = elig.sort_values(["momentum", "adv20", "symbol"], ascending=[False, False, True], kind="mergesort")
    table.loc[ranked.index, "rank"] = np.arange(1, len(ranked) + 1)
    diagnostics: dict = {}
    table["side"] = None
    table["percentile"] = np.nan
    table["vol"] = np.nan
    table["beta"] = np.nan
    if cfg.is_long_short:
        diagnostics = build_books(panel, t, cfg, table, held_long or set(), held_short or set(), blocked_shorts or set())
        n_sel = int(table["selected"].sum())
    else:
        n_sel = min(cfg.top_n, len(ranked))
        sel_idx = ranked.index[:n_sel]
        table.loc[sel_idx, "selected"] = True
        table["side"] = None
        if n_sel:
            table.loc[sel_idx, "target_weight"] = 1.0 / n_sel
            table.loc[sel_idx, "side"] = "long"

    coverage = with_bar / universe if universe else 0.0
    blocked = None
    if universe == 0:
        blocked = "No common stocks in the point-in-time universe at the signal session."
    elif coverage < cfg.min_session_coverage:
        blocked = (f"Only {coverage:.0%} of the universe has a bar at {session} "
                   f"(minimum {cfg.min_session_coverage:.0%}); data is stale or incomplete.")
    elif n_sel == 0:
        blocked = "No eligible securities at the signal session."
    elif cfg.is_long_short and not (diagnostics.get("long_names") and diagnostics.get("short_names")):
        blocked = "Long-short books could not be formed: " + "; ".join(diagnostics.get("notes", []))

    return SignalResult(
        session=session, t=t, table=table.sort_values(["rank", "symbol"], na_position="last").reset_index(drop=True),
        universe_count=universe, eligible_count=len(ranked), selected_count=n_sel,
        coverage=coverage, blocked_reason=blocked, diagnostics=diagnostics,
    )
