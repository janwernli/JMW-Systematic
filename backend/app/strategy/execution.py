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
  5. Whatever cash remains is residual cash.

Long-short (v2) targets are signed. Execution order: reduce longs, open shorts (both
raise cash), cover shorts, then buy longs by rank. Short sales fill at open x (1 - slippage),
covers at open x (1 + slippage). In long-only mode steps 2-3 are empty, so v1 is unchanged.
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
    effect: str = "open_long"   # open_long | close_long | open_short | close_short

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

    targets: signed weights of NAV (positive = long, negative = short).
    prices: execution reference price per symbol (the fill session's open for real
    fills, or the signal close for pre-trade *estimates*). None = unavailable.

    Order of execution: (1) reduce/exit longs, (2) open/increase shorts (both raise cash),
    (3) cover/reduce shorts, (4) buy longs in rank order, limited by available cash.
    """
    shares = {s: q for s, q in shares.items() if q != 0}
    stale: list[str] = []
    parts = [cash]
    for s, q in sorted(shares.items()):
        p = prices.get(s)
        if p is None or not p > 0:
            p = fallback_marks.get(s, 0.0)
            stale.append(s)
        parts.append(q * p)
    nav = r2(math.fsum(parts))  # exact, order-independent sum (paper ledger and backtest must agree to the cent)

    fills: list[Fill] = []
    unfilled: list[dict] = []
    target_sh: dict[str, int] = {}
    comm_mult = 1 + costs.commission_bps / 10_000
    for s, w in targets.items():
        p = prices.get(s)
        if p is None or not p > 0:
            target_sh[s] = shares.get(s, 0)
            unfilled.append({"symbol": s, "side": ("buy" if w >= 0 else "sell") if shares.get(s, 0) == 0 else "adjust",
                             "reason": "no_open_price", "detail": "No opening price at the fill session; not traded."})
            continue
        if w >= 0:
            budget = w * nav - costs.commission_per_order
            target_sh[s] = max(int(math.floor(budget / (costs.buy_price(p) * comm_mult) + 1e-9)), 0)
        else:
            target_sh[s] = -max(int(math.floor(-w * nav / costs.sell_price(p) + 1e-9)), 0)

    new_shares = dict(shares)
    cash_now = cash

    def missing(s: str, side: str, what: str) -> None:
        unfilled.append({"symbol": s, "side": side, "reason": "no_open_price",
                         "detail": f"No opening price at the fill session; {what}."})

    # ---- (1) reduce / exit longs --------------------------------------------------------------
    for s in sorted(shares):
        cur = shares[s]
        goal = max(target_sh.get(s, 0), 0)
        if cur <= 0 or goal >= cur:
            continue
        p = prices.get(s)
        if p is None or not p > 0:
            missing(s, "sell", "position kept")
            continue
        q = cur - goal
        fp = costs.sell_price(p)
        gross = r2(q * fp)
        f = Fill(s, "sell", q, p, fp, gross, r2(q * (p - fp)), costs.commission(gross),
                 "exit" if goal == 0 else "trim", "close_long")
        fills.append(f)
        cash_now = r2(cash_now + f.cash_delta)
        new_shares[s] = goal

    # ---- (2) open / increase shorts (weakest momentum first) -------------------------------------
    short_order = [s for s in rank_order if target_sh.get(s, 0) < 0]
    short_order += sorted(s for s, q in target_sh.items() if q < 0 and s not in short_order)
    for s in short_order:
        cur = new_shares.get(s, 0)
        tgt = target_sh[s]
        if cur > 0 or tgt >= cur:
            continue
        p = prices[s]
        q = cur - tgt
        fp = costs.sell_price(p)
        gross = r2(q * fp)
        f = Fill(s, "sell", q, p, fp, gross, r2(q * (p - fp)), costs.commission(gross),
                 "short" if cur == 0 else "add_short", "open_short")
        fills.append(f)
        cash_now = r2(cash_now + f.cash_delta)
        new_shares[s] = tgt

    def total_cost(q: int, fp: float) -> float:
        g = r2(q * fp)
        return r2(g + costs.commission(g))

    def affordable(want: int, fp: float, s: str, what: str) -> int:
        """Largest whole quantity <= want that cash covers; records partial / skipped fills."""
        if total_cost(want, fp) <= cash_now:
            return want
        q = int(math.floor(max(cash_now - costs.commission_per_order, 0) / (fp * comm_mult)))
        while q > 0 and total_cost(q, fp) > cash_now:
            q -= 1
        if q <= 0:
            unfilled.append({"symbol": s, "side": "buy", "reason": "insufficient_cash",
                             "detail": f"Needed {want} shares to {what}; no whole share affordable with ${cash_now:,.2f}."})
        else:
            unfilled.append({"symbol": s, "side": "buy", "reason": "partial_insufficient_cash",
                             "detail": f"Bought {q} of {want} target shares (cash limit)."})
        return max(q, 0)

    # ---- (3) cover / reduce shorts -----------------------------------------------------------
    for s in sorted(new_shares):
        cur = new_shares[s]
        goal = min(target_sh.get(s, 0), 0)
        if cur >= 0 or goal <= cur:
            continue
        p = prices.get(s)
        if p is None or not p > 0:
            missing(s, "buy", "short kept")
            continue
        fp = costs.buy_price(p)
        q = affordable(goal - cur, fp, s, "cover")
        if q <= 0:
            continue
        gross = r2(q * fp)
        f = Fill(s, "buy", q, p, fp, gross, r2(q * (fp - p)), costs.commission(gross),
                 "cover" if cur + q == 0 else "trim_short", "close_short")
        fills.append(f)
        cash_now = r2(cash_now + f.cash_delta)
        new_shares[s] = cur + q

    # ---- (4) buy longs, best rank first -----------------------------------------------------------
    order = [s for s in rank_order if s in target_sh] + sorted(s for s in target_sh if s not in rank_order)
    for s in order:
        cur = new_shares.get(s, 0)
        tgt = target_sh[s]
        p = prices.get(s)
        if cur < 0 or tgt <= cur or p is None or not p > 0:
            continue
        fp = costs.buy_price(p)
        q = affordable(tgt - cur, fp, s, "buy")
        if q <= 0:
            continue
        gross = r2(q * fp)
        f = Fill(s, "buy", q, p, fp, gross, r2(q * (fp - p)), costs.commission(gross),
                 "entry" if cur == 0 else "add", "open_long")
        fills.append(f)
        cash_now = r2(cash_now + f.cash_delta)
        new_shares[s] = cur + q

    new_shares = {s: q for s, q in new_shares.items() if q != 0}
    return RebalanceExecution(fills, unfilled, target_sh, nav, r2(cash), cash_now, new_shares, stale)


def cover_shorts(shares: dict[str, int], cash: float, symbols: list[str], prices: dict[str, float | None],
                 costs: CostModel) -> RebalanceExecution:
    """Buy to cover the full short position in `symbols` (used by the stop-loss); other holdings untouched."""
    keep = {s: q for s, q in shares.items() if s not in symbols}
    covering = {s: shares[s] for s in symbols if shares.get(s, 0) < 0}
    ex = execute_rebalance(covering, cash, {s: 0.0 for s in covering}, [], {s: prices.get(s) for s in covering},
                           {}, costs)
    ex.shares_after = {**keep, **ex.shares_after}
    for f in ex.fills:
        f.reason = "stop_loss_cover"
    return ex


def update_basis(basis: dict[str, float], shares_before: int, fill: Fill) -> None:
    """Signed cost basis: long = cash paid (incl. costs), short = -(net proceeds). Average-cost method."""
    signed_q = fill.shares if fill.side == "buy" else -fill.shares
    value = fill.gross_value + fill.commission if fill.side == "buy" else -(fill.gross_value - fill.commission)
    b = basis.get(fill.symbol, 0.0)
    after = shares_before + signed_q
    if shares_before == 0 or (shares_before > 0) == (signed_q > 0):
        b = b + value
    else:
        b = b * after / shares_before if after != 0 else 0.0
    if after == 0:
        basis.pop(fill.symbol, None)
    else:
        basis[fill.symbol] = r2(b)


def short_stop_triggers(panel: Panel, t: int, shares: dict[str, int], basis: dict[str, float],
                        stop: float | None) -> list[dict]:
    """Shorts whose close at t is at least `stop` above their average entry price (net proceeds per share)."""
    if not stop:
        return []
    out = []
    for s, q in sorted(shares.items()):
        if q >= 0 or s not in basis:
            continue
        j = panel.sym_index.get(s)
        close = panel.close[t, j] if j is not None else np.nan
        if np.isnan(close):
            continue
        entry = basis[s] / q  # both negative -> positive price per share (split-adjusted automatically)
        if entry > 0 and close >= entry * (1 + stop):
            out.append({"symbol": s, "entry": round(entry, 4), "close": float(close), "move": float(close / entry - 1)})
    return out


def accrual_days(panel: Panel, t: int) -> int:
    """Calendar days since the previous session (weekends/holidays accrue on the next session)."""
    from datetime import date

    if t <= 0:
        return 1
    return (date.fromisoformat(panel.sessions[t]) - date.fromisoformat(panel.sessions[t - 1])).days


def borrow_fee(panel: Panel, t: int, shares: dict[str, int], annual_rate: float) -> tuple[float, float]:
    """Borrow fee accrued at session t's close: |short value| × annual_rate / 360 × calendar days since the
    previous session (ACT/360, as securities-lending fees are quoted)."""
    parts = []
    for s, q in shares.items():
        if q < 0:
            px = panel.mark[t, panel.sym_index[s]]
            if not np.isnan(px):
                parts.append(-q * px)
    short_value = math.fsum(parts)
    return r2(short_value * annual_rate / 360 * accrual_days(panel, t)), short_value


def exposures(panel: Panel, t: int, shares: dict[str, int]) -> tuple[float, float]:
    """(long market value, short market value [negative]) at session t's marks."""
    lv: list[float] = []
    sv: list[float] = []
    for s, q in shares.items():
        px = panel.mark[t, panel.sym_index[s]]
        if np.isnan(px):
            continue
        (lv if q > 0 else sv).append(q * px)
    return r2(math.fsum(lv)), r2(math.fsum(sv))


# ---------------------------------------------------------------------------------------------
# Corporate actions, delistings and valuation on the panel
# ---------------------------------------------------------------------------------------------
@dataclass
class CashEvent:
    kind: str       # dividend | cash_in_lieu | delisting_cashout | borrow_fee
    symbol: str
    amount: float
    note: str


def apply_corporate_actions(panel: Panel, t: int, shares: dict[str, int]) -> tuple[dict[str, int], list[CashEvent]]:
    """Pre-open processing of ex-date events at session t for current holdings.

    Splits: shares *= ratio; fractional shares are paid out as cash-in-lieu at the
    previous mark restated for the split (known before the open, no look-ahead).
    Cash dividends: shares (post-split) * amount credited on the ex-date.
    Dividend *pay-date* lag is not modeled (documented assumption).
    Shorts: share counts are negative, so split fractions are paid (cash-in-lieu owed) and
    dividends are debited (the short seller owes the manufactured dividend).
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
            whole = int(math.floor(raw + 1e-9)) if raw >= 0 else -int(math.floor(-raw + 1e-9))  # toward zero
            frac = raw - whole
            out[s] = whole
            if abs(frac) > 1e-9:
                ref = panel.mark[t - 1, j] / ratio if t > 0 and not np.isnan(panel.mark[t - 1, j]) else 0.0
                events.append(CashEvent("cash_in_lieu", s, r2(frac * ref),
                                        f"{frac:.4f} fractional shares after {ratio:g}:1 split at {ref:.4f}"))
        d = panel.dividend[t, j]
        if d > 0 and out[s] != 0:
            note = "ex-date credit" if out[s] > 0 else "short: dividend owed"
            events.append(CashEvent("dividend", s, r2(out[s] * d), f"{out[s]} sh x ${d:.4f} ({note})"))
    return {s: q for s, q in out.items() if q != 0}, events


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
        verb = "cashed out" if out[s] > 0 else "covered"
        events.append(CashEvent("delisting_cashout", s, r2(out[s] * px),
                                f"{out[s]} sh {verb} at last close {px:.4f} (delisting assumption)"))
        del out[s]
    return out, events


def mark_to_market(panel: Panel, t: int, shares: dict[str, int]) -> tuple[float, int, dict[str, tuple[float, bool]]]:
    """Positions value at session t's close; returns (value, stale_count, {symbol: (price, stale)})."""
    parts: list[float] = []
    stale = 0
    marks: dict[str, tuple[float, bool]] = {}
    for s, q in sorted(shares.items()):
        j = panel.sym_index[s]
        px = panel.mark[t, j]
        is_stale = bool(panel.stale[t, j])
        if np.isnan(px):
            px, is_stale = 0.0, True
        stale += is_stale
        marks[s] = (float(px), is_stale)
        parts.append(q * px)
    return r2(math.fsum(parts)), stale, marks


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


def cash_interest(panel: Panel, t: int, cash: float, annual_rate: float) -> float:
    """Interest credited at session t's close on positive cash: cash x rate / 360 x calendar days since the
    previous session (ACT/360, same convention as the borrow fee)."""
    if cash <= 0 or annual_rate <= 0:
        return 0.0
    return r2(cash * annual_rate / 360 * accrual_days(panel, t))
