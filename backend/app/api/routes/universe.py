"""Universe & rankings, stock detail."""

from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query

from ...strategy.config import StrategyConfig
from ...strategy.execution import marks_at
from ...strategy.signals import REASONS, compute_signals
from ...services import AppContext
from ..deps import get_ctx
from ..schemas import StockDetail, UniverseResponse

router = APIRouter(prefix="/universe", tags=["universe"])


def _nan(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


def latest_plan(ctx: AppContext, pid: int) -> dict | None:
    return ctx.db.query_one("SELECT * FROM rebalance_plans WHERE portfolio_id=? AND status IN "
                            "('proposed','applied','executed') ORDER BY signal_session DESC LIMIT 1", (pid,))


@router.get("", response_model=UniverseResponse)
def universe(session: str | None = Query(None, description="Signal session (default: latest stored session)"),
             ctx: AppContext = Depends(get_ctx)):
    panel = ctx.panel()
    port = ctx.ledger.active()
    cfg = ctx.ledger.config_of(port) if port else StrategyConfig()
    s = session or panel.sessions[-1]
    if s not in panel.sess_index:
        raise HTTPException(400, f"{s} is not a stored trading session ({panel.sessions[0]}..{panel.sessions[-1]}).")
    t = panel.sess_index[s]
    shares: dict[str, int] = ctx.ledger.positions(port["id"]) if port else {}
    held_long = {x for x, q in shares.items() if q > 0}
    held_short = {x for x, q in shares.items() if q < 0}
    nav = None
    marks: dict[str, float] = {}
    if port:
        last = ctx.db.query_one("SELECT nav, session FROM paper_nav WHERE portfolio_id=? ORDER BY session DESC LIMIT 1",
                                (port["id"],))
        nav = last["nav"] if last else None
        if last and last["session"] in panel.sess_index:
            marks = marks_at(panel, panel.sess_index[last["session"]], shares)
    sig = compute_signals(panel, t, cfg, held_long, held_short)
    plan = latest_plan(ctx, port["id"]) if port else None
    plan_w: dict[str, float] = {}
    if plan and plan["signal_set_id"]:
        plan_w = {r["symbol"]: r["target_weight"] for r in ctx.db.query(
            "SELECT symbol, target_weight FROM signal_rows WHERE set_id=? AND selected=1", (plan["signal_set_id"],))}

    inst = panel.instruments
    rows = []
    for r in sig.table.itertuples(index=False):
        sym = r.symbol
        q = shares.get(sym, 0)
        rows.append({
            "symbol": sym, "name": inst.at[sym, "name"] if "name" in inst.columns else None,
            "sector": r.sector if isinstance(r.sector, str) else None,
            "asset_type": r.asset_type, "close_raw": r.close_raw, "adv20": r.adv20, "momentum": r.momentum,
            "rank": None if str(r.rank) == "<NA>" else int(r.rank), "eligible": bool(r.eligible), "reason": r.reason,
            "reason_text": REASONS.get(r.reason, r.reason), "selected": bool(r.selected),
            "side": r.side if isinstance(r.side, str) else None,
            "score": None if r.score != r.score else float(r.score),
            "resid_mom": None if r.resid_mom != r.resid_mom else float(r.resid_mom),
            "sector_mom": None if r.sector_mom != r.sector_mom else float(r.sector_mom),
            "fip": None if r.fip != r.fip else float(r.fip),
            "composite": None if r.composite != r.composite else float(r.composite),
            "percentile": None if r.percentile != r.percentile else float(r.percentile),
            "vol": None if r.vol != r.vol else float(r.vol), "beta": None if r.beta != r.beta else float(r.beta),
            "model_weight": float(r.target_weight), "plan_target_weight": plan_w.get(sym, 0.0) if plan else None,
            "position_shares": q, "current_weight": (q * marks.get(sym, 0.0) / nav) if nav else 0.0,
        })
    counts = sig.table["reason"].value_counts().to_dict()
    frozen = plan is not None and plan["signal_session"] == s
    return {
        "data_label": ctx.data_label(), "signal_session": s, "is_month_end": ctx.calendar.is_month_end(s),
        "basis_note": ("Recomputed from stored data for the frozen plan's signal session." if frozen else
                       "Indicative ranking recomputed after the close of this session with the paper config "
                       "(current holdings feed the rank buffer). Only month-end signals drive rebalances."),
        "plan_signal_session": plan["signal_session"] if plan else None,
        "portfolio_as_of": port["as_of_session"] if port else None, "config": cfg,
        "universe_count": sig.universe_count, "eligible_count": sig.eligible_count,
        "selected_count": sig.selected_count, "coverage": sig.coverage, "blocked_reason": sig.blocked_reason,
        "reason_counts": {str(k): int(v) for k, v in counts.items()}, "rows": rows,
        "diagnostics": sig.diagnostics,
    }


@router.get("/{symbol}", response_model=StockDetail)
def stock_detail(symbol: str, session: str | None = None, ctx: AppContext = Depends(get_ctx)):
    panel = ctx.panel()
    symbol = symbol.upper()
    if symbol not in panel.sym_index:
        raise HTTPException(404, f"Unknown symbol {symbol}")
    port = ctx.ledger.active()
    cfg = ctx.ledger.config_of(port) if port else StrategyConfig()
    s = session or panel.sessions[-1]
    if s not in panel.sess_index:
        raise HTTPException(400, f"{s} is not a stored trading session")
    t = panel.sess_index[s]
    j = panel.sym_index[symbol]
    sig = compute_signals(panel, t, cfg)
    row = sig.table.set_index("symbol").loc[symbol] if symbol in set(sig.table["symbol"]) else None
    t21, t252 = t - cfg.skip_sessions, t - cfg.lookback_sessions
    inst_row = ctx.db.query_one("SELECT * FROM instruments WHERE provider=? AND symbol=?",
                                (ctx.require_provider().info.key, symbol)) or {}
    has = ~np.isnan(panel.close[:, j])
    first_tr = panel.tr[has, j][0] if has.any() else np.nan
    prices = [{
        "session": panel.sessions[i], "open": _nan(panel.open[i, j]), "high": _nan(panel.high[i, j]),
        "low": _nan(panel.low[i, j]), "close": _nan(panel.close[i, j]), "volume": _nan(panel.volume[i, j]),
        "tr_index": _nan(panel.tr[i, j] / first_tr * 100) if not np.isnan(first_tr) else None,
    } for i in np.flatnonzero(has) if i <= t]
    acts = ctx.db.query(
        "SELECT a.ex_date, a.action_type, a.ratio, a.amount FROM corporate_actions a JOIN instruments i ON "
        "i.id=a.instrument_id WHERE i.provider=? AND i.symbol=? AND a.ex_date<=? ORDER BY a.ex_date",
        (ctx.require_provider().info.key, symbol, s))
    reason = row["reason"] if row is not None else "not_in_universe"
    signal = {
        "signal_session": s,
        "session_t21": panel.sessions[t21] if t21 >= 0 else None,
        "session_t252": panel.sessions[t252] if t252 >= 0 else None,
        "close_t": _nan(panel.close[t, j]),
        "close_t21_raw": _nan(panel.close[t21, j]) if t21 >= 0 else None,
        "close_t252_raw": _nan(panel.close[t252, j]) if t252 >= 0 else None,
        "tr_t21": _nan(panel.tr[t21, j]) if t21 >= 0 else None,
        "tr_t252": _nan(panel.tr[t252, j]) if t252 >= 0 else None,
        "momentum": row["momentum"] if row is not None else None,
        "adv20": row["adv20"] if row is not None else None,
        "valid_history": int(row["valid_history"]) if row is not None else 0,
        "eligible": bool(row["eligible"]) if row is not None else False,
        "reason": reason, "reason_text": REASONS.get(reason, "Not listed at this session"),
        "rank": None if row is None or str(row["rank"]) == "<NA>" else int(row["rank"]),
        "formula": (f"momentum = TR({panel.sessions[t21] if t21 >= 0 else 't-21'}) / "
                    f"TR({panel.sessions[t252] if t252 >= 0 else 't-252'}) - 1, where TR is the causal total-return "
                    "index built from raw closes, splits and cash dividends."),
    }
    return {
        "data_label": ctx.data_label(), "symbol": symbol, "name": inst_row.get("name"), "sector": inst_row.get("sector"),
        "sector_source": inst_row.get("sector_source"), "asset_type": inst_row.get("asset_type", "other"),
        "asset_type_source": inst_row.get("asset_type_source"), "exchange": inst_row.get("exchange"),
        "list_date": inst_row.get("list_date"), "delist_date": inst_row.get("delist_date"),
        "signal": signal, "prices": prices, "corporate_actions": acts,
        "position_shares": (ctx.ledger.positions(port["id"]).get(symbol, 0) if port else 0),
    }
