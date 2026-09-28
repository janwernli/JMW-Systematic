"""Order sizing, simulated fills, transaction costs and portfolio accounting.

This module is shared verbatim by the backtester and the internal paper ledger,
so both follow exactly the same rules.

Rebalance algorithm (fills at the fill session's OPEN):
  1. NAV_open = cash + sum(shares * open) (a holding without an open is valued
     at its latest mark and flagged).
  2. For each target symbol with an open price:
         target_shares = floor((w * NAV_open - commission_estimate) / buy_fill_price)
     where buy_fill_price = open * (1 + slippage). Targets without an open are
     NOT traded (no substitution of another price) and reported as unfilled.
  3. SELLS FIRST (symbol order): full exits of holdings no longer targeted, then
     trims of targeted holdings above target. sell_fill_price = open * (1 - slippage).
  4. BUYS SECOND, in rank order (best first): buy up to target_shares; if cash is
     short, buy the largest affordable whole-share quantity; otherwise skip.
  5. Whatever cash remains is residual cash. No leverage, no short positions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..data.panel import Panel


def r2(x: float) -> float:
    """Round money to cents (half away from zero, stable across platforms)."""
    return math.floor(abs(x) * 100 + 0.5 + 1e-9) / 100 * (1 if x >= 0 else -1)


def r4(x: float) -> float:
    return math.floor(abs(x) * 10_000 + 0.5 + 1e-9) / 10_000 * (1 if x >= 0 else -1)


@dataclass(frozen=True)
class CostModel:
    slippage_bps: float = 10.0
    commission_per_order: float = 0.0
    commission_bps: float = 0.0

    def buy_price(self, ref: float) -> float:
        return ref if self.slippage_bps == 0 else r4(ref * (1 + self.slippage_bps / 10_000))

    def sell_price(self, ref: float) -> float:
        return ref if self.slippage_bps == 0 else r4(ref * (1 - self.slippage_bps / 10_000))

    def commission(self, value: float) -> float:
        return r2(self.commission_per_order + abs(value) * self.commission_bps / 10_000)


@dataclass
class Fill:
    symbol: str
    side: str
    shares: int
    ref_price: float
    fill_price: float
    gross_value: float
    slippage_cost: float
    commission: float
    reason: str

    @property
    def cash_delta(self) -> float:
        if self.side == "buy":
            return -r2(self.gross_value + self.commission)
        return r2(self.gross_value - self.commission)


@dataclass
class RebalanceExecution:
    fills: list[Fill]
    unfilled: list[dict]
    target_shares: dict[str, int]
    nav_at_open: float
    cash_before: float
    cash_after: float
    shares_after: dict[str, int]
    stale_valuations: list[str] = field(default_factory=list)

    @property
    def buy_value(self) -> float:
        return r2(sum(f.gross_value for f in self.fills if f.side == "buy"))

    @property
    def sell_value(self) -> float:
        return r2(sum(f.gross_value for f in self.fills if f.side == "sell"))

    @property
    def slippage_cost(self) -> float:
        return r2(sum(f.slippage_cost for f in self.fills))

    @property
    def commission(self) -> float:
        return r2(sum(f.commission for f in self.fills))

    @property
    def turnover(self) -> float:
        """One-way turnover: (buys + sells) / 2 / NAV at the open."""
        return (self.buy_value + self.sell_value) / 2 / self.nav_at_open if self.nav_at_open > 0 else 0.0


def execute_rebalance(
    shares: dict[str, int],
    cash: float,
    targets: dict[str, float],
    rank_order: list[str],
    prices: dict[str, float | None],
    fallback_marks: dict[str, float],
    costs: CostModel,
) -> RebalanceExecution:
    """Pure function: returns fills and resulting holdings; does not mutate inputs.

    prices: execution reference price per symbol (the fill session's open for real
    fills, or the signal close for pre-trade *estimates*). None = unavailable.
    """
    shares = {s: q for s, q in shares.items() if q > 0}
    stale: list[str] = []
    nav = cash
    for s, q in shares.items():
        p = prices.get(s)
        if p is None or not p > 0:
            p = fallback_marks.get(s, 0.0)
            stale.append(s)
        nav += q * p
    nav = r2(nav)

    fills: list[Fill] = []
    unfilled: list[dict] = []
    target_sh: dict[str, int] = {}
    for s, w in targets.items():
        p = prices.get(s)
        if p is None or not p > 0:
            target_sh[s] = shares.get(s, 0)
            unfilled.append({"symbol": s, "side": "buy" if shares.get(s, 0) == 0 else "adjust",
                             "reason": "no_open_price", "detail": "No opening price at the fill session; not traded."})
            continue
        bp = costs.buy_price(p)
        budget = w * nav - costs.commission_per_order
        target_sh[s] = max(int(math.floor(budget / (bp * (1 + costs.commission_bps / 10_000)) + 1e-9)), 0)

    new_shares = dict(shares)
    cash_now = cash

    # ---- sells first --------------------------------------------------------------------
    for s in sorted(shares):
        cur = shares[s]
        tgt = target_sh.get(s, 0)
        if tgt >= cur:
            continue
        p = prices.get(s)
        if p is None or not p > 0:
            unfilled.append({"symbol": s, "side": "sell", "reason": "no_open_price",
                             "detail": "No opening price at the fill session; position kept."})
            continue
        q = cur - tgt
        fp = costs.sell_price(p)
        gross = r2(q * fp)
        f = Fill(s, "sell", q, p, fp, gross, r2(q * (p - fp)), costs.commission(gross),
                 "exit" if tgt == 0 else "trim")
        fills.append(f)
        cash_now = r2(cash_now + f.cash_delta)
        new_shares[s] = tgt

    # ---- buys second, best rank first -----------------------------------------------------
    order = [s for s in rank_order if s in target_sh] + sorted(s for s in target_sh if s not in rank_order)
    for s in order:
        cur = new_shares.get(s, 0)
        tgt = target_sh[s]
        p = prices.get(s)
        if tgt <= cur or p is None or not p > 0:
            continue
        fp = costs.buy_price(p)
        want = tgt - cur

        def total_cost(q: int) -> float:
            g = r2(q * fp)
            return r2(g + costs.commission(g))

        q = want
        if total_cost(q) > cash_now:
            q = int(math.floor(max(cash_now - costs.commission_per_order, 0) / (fp * (1 + costs.commission_bps / 10_000))))
            while q > 0 and total_cost(q) > cash_now:
                q -= 1
            if q <= 0:
                unfilled.append({"symbol": s, "side": "buy", "reason": "insufficient_cash",
                                 "detail": f"Needed {want} shares; no whole share affordable with ${cash_now:,.2f}."})
                continue
            unfilled.append({"symbol": s, "side": "buy", "reason": "partial_insufficient_cash",
                             "detail": f"Bought {q} of {want} target shares (cash limit)."})
        gross = r2(q * fp)
        f = Fill(s, "buy", q, p, fp, gross, r2(q * (fp - p)), costs.commission(gross),
                 "entry" if cur == 0 else "add")
        fills.append(f)
        cash_now = r2(cash_now + f.cash_delta)
        new_shares[s] = cur + q

    new_shares = {s: q for s, q in new_shares.items() if q > 0}
    return RebalanceExecution(fills, unfilled, target_sh, nav, r2(cash), cash_now, new_shares, stale)


# ---------------------------------------------------------------------------------------------
# Corporate actions, delistings and valuation on the panel
# ---------------------------------------------------------------------------------------------
@dataclass
class CashEvent:
    kind: str       # dividend | cash_in_lieu | delisting_cashout
    symbol: str
    amount: float
    note: str


def apply_corporate_actions(panel: Panel, t: int, shares: dict[str, int]) -> tuple[dict[str, int], list[CashEvent]]:
    """Pre-open processing of ex-date events at session t for current holdings.

    Splits: shares *= ratio; fractional shares are paid out as cash-in-lieu at the
    previous mark restated for the split (known before the open, no look-ahead).
    Cash dividends: shares (post-split) * amount credited on the ex-date.
    Dividend *pay-date* lag is not modeled (documented assumption).
    """
    out = dict(shares)
    events: list[CashEvent] = []
    for s in sorted(shares):
        j = panel.sym_index.get(s)
        if j is None:
            continue
        ratio = panel.split_ratio[t, j]
        if ratio != 1.0:
            raw = out[s] * ratio
            whole = int(math.floor(raw + 1e-9))
            frac = raw - whole
            out[s] = whole
            if frac > 1e-9:
                ref = panel.mark[t - 1, j] / ratio if t > 0 and not np.isnan(panel.mark[t - 1, j]) else 0.0
                events.append(CashEvent("cash_in_lieu", s, r2(frac * ref),
                                        f"{frac:.4f} fractional shares after {ratio:g}:1 split at {ref:.4f}"))
        d = panel.dividend[t, j]
        if d > 0 and out[s] > 0:
            events.append(CashEvent("dividend", s, r2(out[s] * d), f"{out[s]} sh x ${d:.4f} (ex-date credit)"))
    return {s: q for s, q in out.items() if q > 0}, events


def delisting_cashouts(panel: Panel, t: int, shares: dict[str, int]) -> tuple[dict[str, int], list[CashEvent]]:
    """At the close of a holding's final trading session, convert it to cash at that close.

    Assumption (documented): exit at the last available close, no slippage. Real
    delisting proceeds can be materially lower.
    """
    out = dict(shares)
    events: list[CashEvent] = []
    for s in sorted(shares):
        j = panel.sym_index.get(s)
        if j is None or panel.delist_idx[j] != t:
            continue
        px = panel.mark[t, j]
        events.append(CashEvent("delisting_cashout", s, r2(out[s] * px),
                                f"{out[s]} sh cashed out at last close {px:.4f} (delisting assumption)"))
        del out[s]
    return out, events


def mark_to_market(panel: Panel, t: int, shares: dict[str, int]) -> tuple[float, int, dict[str, tuple[float, bool]]]:
    """Positions value at session t's close; returns (value, stale_count, {symbol: (price, stale)})."""
    total = 0.0
    stale = 0
    marks: dict[str, tuple[float, bool]] = {}
    for s, q in shares.items():
        j = panel.sym_index[s]
        px = panel.mark[t, j]
        is_stale = bool(panel.stale[t, j])
        if np.isnan(px):
            px, is_stale = 0.0, True
        stale += is_stale
        marks[s] = (float(px), is_stale)
        total += q * px
    return r2(total), stale, marks


def open_prices(panel: Panel, t: int, symbols: set[str] | list[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for s in symbols:
        j = panel.sym_index.get(s)
        v = panel.open[t, j] if j is not None else np.nan
        out[s] = None if np.isnan(v) else float(v)
    return out


def close_prices(panel: Panel, t: int, symbols: set[str] | list[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for s in symbols:
        j = panel.sym_index.get(s)
        v = panel.close[t, j] if j is not None else np.nan
        out[s] = None if np.isnan(v) else float(v)
    return out


def marks_at(panel: Panel, t: int, symbols) -> dict[str, float]:
    out = {}
    for s in symbols:
        j = panel.sym_index.get(s)
        if j is not None and t >= 0 and not np.isnan(panel.mark[t, j]):
            out[s] = float(panel.mark[t, j])
    return out


def preopen_marks(panel: Panel, t: int, symbols) -> dict[str, float]:
    """Previous mark restated for today's ex-date events: the best pre-open valuation."""
    out = {}
    if t <= 0:
        return out
    for s in symbols:
        j = panel.sym_index.get(s)
        if j is not None and not np.isnan(panel.mark[t - 1, j]):
            out[s] = float(panel.mark[t - 1, j] / panel.split_ratio[t, j] - panel.dividend[t, j])
    return out
