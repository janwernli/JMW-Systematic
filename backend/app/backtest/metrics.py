"""Performance statistics. Formulas (also shown in the UI):

  daily return          r_t = NAV_t / NAV_{t-1} - 1
  total return          NAV_end / NAV_start - 1
  CAGR                  (NAV_end / NAV_start) ** (365.25 / calendar_days) - 1   -- only if >= 365 days
  annualized volatility stdev(r_t, ddof=1) * sqrt(252)
  return/vol ratio      mean(r_t) * 252 / annualized volatility   (risk-free rate assumed 0)
  drawdown              NAV_t / max(NAV_0..t) - 1 ; max drawdown = min(drawdown)
  one-way turnover      (buys + sells) / 2 / NAV at the rebalance open
  return difference     strategy total return - benchmark total return (a simple difference,
                        NOT a statistically estimated alpha)
  gross NAV             net NAV + cumulative transaction costs (slippage + commissions), not compounded
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

MIN_DAYS_FOR_CAGR = 365


def _series_stats(nav: pd.Series) -> dict:
    nav = nav.dropna()
    if len(nav) < 2:
        return {"total_return": None, "cagr": None, "ann_vol": None, "ret_vol_ratio": None,
                "max_drawdown": None, "max_dd_peak": None, "max_dd_trough": None}
    rets = nav.pct_change().dropna()
    total = nav.iloc[-1] / nav.iloc[0] - 1
    days = (pd.Timestamp(nav.index[-1]) - pd.Timestamp(nav.index[0])).days
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (365.25 / days) - 1 if days >= MIN_DAYS_FOR_CAGR else None
    vol = float(rets.std(ddof=1) * math.sqrt(252)) if len(rets) > 2 else None
    ratio = float(rets.mean() * 252 / vol) if vol else None
    dd = nav / nav.cummax() - 1
    trough = dd.idxmin()
    peak = nav.loc[:trough].idxmax()
    return {
        "total_return": float(total), "cagr": None if cagr is None else float(cagr), "ann_vol": vol,
        "ret_vol_ratio": ratio, "max_drawdown": float(dd.min()), "max_dd_peak": peak, "max_dd_trough": trough,
    }


def drawdown(nav: pd.Series) -> pd.Series:
    return nav / nav.cummax() - 1


def compute_metrics(nav_df: pd.DataFrame, rebalances: list[dict], initial_capital: float) -> dict:
    """nav_df: index=session, columns nav, gross_nav, benchmark_nav. rebalances: dicts with turnover etc."""
    start, end = nav_df.index[0], nav_df.index[-1]
    days = (pd.Timestamp(end) - pd.Timestamp(start)).days
    net = _series_stats(nav_df["nav"])
    gross = _series_stats(nav_df["gross_nav"])
    bench = _series_stats(nav_df["benchmark_nav"]) if nav_df["benchmark_nav"].notna().any() else None
    executed = [r for r in rebalances if r.get("status") == "executed"]
    turnovers = [r["turnover"] for r in executed if r.get("turnover") is not None]
    years = days / 365.25 if days else 0
    total_costs = float(sum(r.get("slippage_cost", 0) + r.get("commission", 0) for r in executed))
    out = {
        "start_session": start,
        "end_session": end,
        "sessions": int(len(nav_df)),
        "calendar_days": int(days),
        "cagr_meaningful": days >= MIN_DAYS_FOR_CAGR,
        "initial_capital": initial_capital,
        "final_nav": float(nav_df["nav"].iloc[-1]),
        "net": net,
        "gross": gross,
        "benchmark": bench,
        "return_difference": (net["total_return"] - bench["total_return"]) if bench and bench["total_return"] is not None else None,
        "cagr_difference": (net["cagr"] - bench["cagr"]) if bench and net["cagr"] is not None and bench["cagr"] is not None else None,
        "rebalances_executed": len(executed),
        "rebalances_blocked": sum(1 for r in rebalances if r.get("status") == "blocked"),
        "avg_turnover": float(np.mean(turnovers)) if turnovers else None,
        "annualized_turnover": float(sum(turnovers) / years) if years >= 1 and turnovers else None,
        "total_costs": total_costs,
        "cost_drag": (gross["total_return"] - net["total_return"]) if gross["total_return"] is not None else None,
        "avg_positions": float(nav_df["positions"].mean()),
        "avg_cash_weight": float((nav_df["cash"] / nav_df["nav"]).mean()),
    }
    return out


def monthly_returns(nav: pd.Series, first_base: float | None = None) -> list[dict]:
    """Month return = last NAV of month / last NAV of prior month - 1 (first month vs. start NAV)."""
    s = nav.dropna()
    if s.empty:
        return []
    months = pd.Series(s.values, index=pd.to_datetime(s.index)).groupby(lambda d: (d.year, d.month)).last()
    out = []
    prev = first_base if first_base is not None else float(s.iloc[0])
    for (y, m), v in months.items():
        out.append({"year": int(y), "month": int(m), "return": float(v / prev - 1)})
        prev = float(v)
    return out


def rolling_metrics(nav_df: pd.DataFrame, window: int = 252, vol_window: int = 63) -> pd.DataFrame:
    r = nav_df["nav"].pct_change()
    out = pd.DataFrame(index=nav_df.index)
    out["rolling_return"] = nav_df["nav"] / nav_df["nav"].shift(window) - 1
    out["rolling_vol"] = r.rolling(vol_window).std(ddof=1) * math.sqrt(252)
    if nav_df["benchmark_nav"].notna().any():
        b = nav_df["benchmark_nav"].astype(float)
        out["rolling_bench_return"] = b / b.shift(window) - 1
        out["rolling_difference"] = out["rolling_return"] - out["rolling_bench_return"]
    else:
        out["rolling_bench_return"] = np.nan
        out["rolling_difference"] = np.nan
    return out
