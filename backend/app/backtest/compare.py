"""Side-by-side report of completed backtest runs (used by the strategy-variant comparison).

Definitions (also shown in the UI):
  excess return      r_d - RF_d x x_d, with RF_d = annual Ken French RF / 252 and x_d = 1 if cash earns RF,
                     else x_d = 1 - max(cash / NAV, 0) held into day d: RF is charged on all capital except
                     idle, non-interest-bearing cash. With cash >= 0 that is the net exposure (a neutral book
                     ~0, SPY core / 130/30 ~1); a margin loan (cash < 0) is already charged RF + spread inside
                     r, so x = 1 there. Market-neutral and invested books are compared on the same basis.
  Sharpe             mean(excess) x 252 / (stdev(excess) x sqrt(252))
  Investor Sharpe    the same with r - RF on the FULL NAV (what an investor holding this account earns over
                     T-bills; idle cash that earns nothing counts against it)
  Sortino            mean(excess) x 252 / (sqrt(mean(min(excess, 0)^2)) x sqrt(252))
  max drawdown       with peak, trough and recovery (first close back at the peak NAV) dates
  worst month        lowest calendar-month return
  beta / correlation daily, vs. SPY total return
  alpha              FF5 + momentum, Newey-West t-stat, on the same excess return (monthly)
  turnover           one-way, annualized: sum over rebalances of (buys + sells) / 2 / NAV, per year
  total costs        slippage + commissions + borrow fees + margin-loan interest, USD
  costs % NAV / yr   total costs / average NAV / years
  halves             the run split at its middle session (performance) / middle month (alpha)
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .factors import RateSource, alpha_test
from .metrics import monthly_returns


def rf_exposure(nav: pd.DataFrame, cash_interest: bool) -> pd.Series:
    """Fraction of NAV to charge RF on at each close (see module docstring)."""
    if cash_interest:
        return pd.Series(1.0, index=nav.index)
    return (1.0 - (nav["cash"].astype(float) / nav["nav"]).clip(lower=0.0)).fillna(0.0)


def excess_returns(nav: pd.DataFrame, rf: RateSource, cash_interest: bool) -> pd.Series:
    r = nav["nav"].pct_change()
    held = rf_exposure(nav, cash_interest).shift(1)
    rf_d = pd.Series([rf.annual(s) / 252 for s in nav.index], index=nav.index)
    return (r - rf_d * held).dropna()


def _perf(nav: pd.DataFrame, rf: RateSource, cash_interest: bool) -> dict:
    s = nav["nav"]
    days = (pd.Timestamp(s.index[-1]) - pd.Timestamp(s.index[0])).days
    r = s.pct_change().dropna()
    ex = excess_returns(nav, rf, cash_interest)
    inv = (r - pd.Series([rf.annual(x) / 252 for x in s.index], index=s.index)).dropna()
    inv_sd = float(inv.std(ddof=1)) if len(inv) > 2 else 0.0
    vol = float(r.std(ddof=1) * math.sqrt(252)) if len(r) > 2 else None
    ex_sd = float(ex.std(ddof=1)) if len(ex) > 2 else 0.0
    down = float(np.sqrt(np.mean(np.minimum(ex.to_numpy(), 0.0) ** 2))) if len(ex) else 0.0
    dd = s / s.cummax() - 1
    trough = dd.idxmin()
    peak = s.loc[:trough].idxmax()
    after = s.loc[trough:]
    rec = after[after >= s[peak]]
    return {
        "start": s.index[0], "end": s.index[-1],
        "total_return": float(s.iloc[-1] / s.iloc[0] - 1),
        "cagr": float((s.iloc[-1] / s.iloc[0]) ** (365.25 / days) - 1) if days >= 365 else None,
        "ann_vol": vol,
        "sharpe": float(ex.mean() * 252 / (ex_sd * math.sqrt(252))) if ex_sd > 0 else None,
        "investor_sharpe": float(inv.mean() * 252 / (inv_sd * math.sqrt(252))) if inv_sd > 0 else None,
        "sortino": float(ex.mean() * 252 / (down * math.sqrt(252))) if down > 0 else None,
        "max_drawdown": float(dd.min()), "max_dd_peak": peak, "max_dd_trough": trough,
        "max_dd_recovery": rec.index[0] if len(rec) else None,
    }


def run_report(nav: pd.DataFrame, metrics: dict, cash_interest: bool, factors: pd.DataFrame | None,
               initial_capital: float) -> dict:
    """nav: session-indexed nav, benchmark_nav, long_value, short_value (from backtest_nav)."""
    rf = RateSource(factors) if factors is not None else _ZeroRate()
    full = _perf(nav, rf, cash_interest)
    mid = len(nav) // 2
    halves = [_perf(nav.iloc[:mid + 1], rf, cash_interest), _perf(nav.iloc[mid:], rf, cash_interest)]

    months = monthly_returns(nav["nav"], initial_capital)
    worst = min(months, key=lambda m: m["return"]) if months else None
    held = rf_exposure(nav, cash_interest).shift(1).dropna()
    by_month = held.groupby(lambda d: d[:4] + d[5:7]).mean().to_dict()
    alpha = alpha_test(months, factors, excess=cash_interest, net_exposure=None if cash_interest else by_month) \
        if factors is not None else None

    def a(fit):
        return None if not fit else {"alpha_annual": fit["alpha_annual"], "t_alpha": fit["t_alpha"],
                                     "start": fit["start"], "end": fit["end"], "months": fit["months"],
                                     "beta_mkt": fit["betas"].get("Mkt-RF"), "beta_mom": fit["betas"].get("Mom")}

    costs = sum(metrics.get(k) or 0.0 for k in ("total_costs", "borrow_fees", "margin_interest"))
    years = (pd.Timestamp(nav.index[-1]) - pd.Timestamp(nav.index[0])).days / 365.25
    cost_pct = costs / float(nav["nav"].mean()) / years if years > 0 else None
    return {
        **full,
        "worst_month": None if worst is None else {"year": worst["year"], "month": worst["month"],
                                                   "value": worst["return"]},
        "beta": metrics.get("realized_beta"), "correlation": metrics.get("correlation"),
        "alpha": a(alpha["full"]) if alpha else None,
        "alpha_model": alpha["model"] if alpha else None,
        "avg_turnover": metrics.get("avg_turnover"), "annualized_turnover": metrics.get("annualized_turnover"),
        "total_costs": costs, "cost_pct_nav_per_year": cost_pct, "slippage_commission": metrics.get("total_costs"), "borrow_fees": metrics.get("borrow_fees"),
        "margin_interest": metrics.get("margin_interest"), "min_cash_weight": metrics.get("min_cash_weight"),
        "cost_drag": metrics.get("cost_drag"),
        "avg_long_gross": metrics.get("avg_long_gross"), "avg_short_gross": metrics.get("avg_short_gross"),
        "avg_gross_exposure": metrics.get("avg_gross_exposure"), "avg_net_exposure": metrics.get("avg_net_exposure"),
        "max_gross_exposure": metrics.get("max_gross_exposure"),
        "halves": [{**h, "alpha": a(alpha[k]) if alpha else None}
                   for h, k in zip(halves, ("first_half", "second_half"))],
        "margin": metrics.get("margin"),
        "rf_note": ("Excess returns subtract RF on the whole NAV (cash earns RF)." if cash_interest else
                    "Cash earns no interest, so excess returns subtract RF on the capital not held as idle cash."),
    }


class _ZeroRate:
    carried: set = set()

    def annual(self, session: str) -> float:
        return 0.0
