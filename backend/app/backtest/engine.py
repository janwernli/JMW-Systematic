"""Event-ordered monthly-rebalance backtest.

Per session t, strictly in this order:
  1. PRE-OPEN   apply ex-date splits / cash dividends to holdings (shorts pay dividends)
  2. OPEN       (a) cover shorts whose stop-loss triggered at the previous close
                (b) execute the pending rebalance formed at the previous session's close
                at this session's open +/- slippage
  3. CLOSE      close out holdings whose final trading session is t (delisting)
  4. CLOSE      charge borrow fees on short market value; mark to the close; record NAV
                (cash + long value + short value, shorts negative)
  5. CLOSE      check short stop-losses (fills at the next open)
  6. AFTER CLOSE if t is the last session of its month: compute and freeze signals
                using data <= t; the resulting target is executed at step 2 of t+1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from ..calendar import TradingCalendar
from ..data.panel import Panel
from ..strategy.config import StrategyConfig
from ..strategy.execution import (
    CashEvent,
    CostModel,
    Fill,
    apply_corporate_actions,
    borrow_fee,
    cover_shorts,
    delisting_cashouts,
    execute_rebalance,
    exposures,
    short_stop_triggers,
    update_basis,
    mark_to_market,
    open_prices,
    preopen_marks,
    r2,
)
from ..strategy.signals import SignalResult, compute_signals


@dataclass
class RebalanceRecord:
    signal_session: str
    fill_session: str | None
    signals: SignalResult
    status: str                   # executed | blocked | not_executed_end_of_data
    nav_at_open: float | None = None
    buy_value: float = 0.0
    sell_value: float = 0.0
    turnover: float = 0.0
    slippage_cost: float = 0.0
    commission: float = 0.0
    cash_after: float | None = None
    unfilled: list[dict] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)


@dataclass
class StopEvent:
    trigger_session: str
    fill_session: str | None
    symbol: str
    entry: float
    close: float
    fills: list[Fill] = field(default_factory=list)
    unfilled: list[dict] = field(default_factory=list)


@dataclass
class BacktestResult:
    config: StrategyConfig
    nav: pd.DataFrame             # session-indexed daily records
    rebalances: list[RebalanceRecord]
    cash_events: list[tuple[str, CashEvent]]
    warnings: list[str]
    benchmark_symbol: str | None
    start_session: str
    end_session: str
    stops: list[StopEvent] = field(default_factory=list)

    @property
    def fills(self) -> list[tuple[str, Fill]]:
        out = [(r.fill_session, f) for r in self.rebalances for f in r.fills]
        out += [(e.fill_session, f) for e in self.stops for f in e.fills]
        return sorted(out, key=lambda x: x[0] or "")


def earliest_start(panel: Panel, cfg: StrategyConfig) -> str:
    """First session at which a full lookback exists in the loaded data."""
    return panel.sessions[min(cfg.lookback_sessions, len(panel.sessions) - 1)]


def run_backtest(
    panel: Panel,
    calendar: TradingCalendar,
    cfg: StrategyConfig,
    progress: Callable[[float, str], None] | None = None,
) -> BacktestResult:
    costs = CostModel(cfg.slippage_bps, cfg.commission_per_order, cfg.commission_bps)
    start = cfg.start_date or earliest_start(panel, cfg)
    end = cfg.end_date or panel.sessions[-1]
    idx = [i for i, s in enumerate(panel.sessions) if start <= s <= end]
    if len(idx) < 2:
        raise ValueError(f"Not enough sessions with data between {start} and {end}.")
    t0, t_end = idx[0], idx[-1]
    warnings: list[str] = []

    bsym = cfg.benchmark_symbol or panel.benchmark
    bj = panel.sym_index.get(bsym) if bsym else None
    if bj is None:
        warnings.append(f"Benchmark '{bsym}' not available; benchmark comparison omitted.")
    elif np.isnan(panel.tr[t0, bj]):
        warnings.append(f"Benchmark '{bsym}' has no bar at the start session; comparison omitted.")
        bj = None

    cash = r2(cfg.initial_capital)
    shares: dict[str, int] = {}
    basis: dict[str, float] = {}
    cum_costs = 0.0
    pending: RebalanceRecord | None = None
    rebalances: list[RebalanceRecord] = []
    cash_events: list[tuple[str, CashEvent]] = []
    stops: list[StopEvent] = []
    pending_stops: list[StopEvent] = []
    stopped_since_signal: set[str] = set()
    rows = []
    bench_last = np.nan

    def record_fills(before: dict[str, int], fills: list[Fill]) -> None:
        running = dict(before)
        for f in fills:
            update_basis(basis, running.get(f.symbol, 0), f)
            running[f.symbol] = running.get(f.symbol, 0) + (f.shares if f.side == "buy" else -f.shares)

    for k, t in enumerate(idx):
        s = panel.sessions[t]
        # 1. pre-open corporate actions
        if shares:
            shares, evs = apply_corporate_actions(panel, t, shares)
            for e in evs:
                cash = r2(cash + e.amount)
                cash_events.append((s, e))
            for sym in list(basis):
                if sym not in shares:
                    basis.pop(sym)

        # 2a. open: stop-loss covers triggered at the previous close
        if pending_stops:
            syms = [e.symbol for e in pending_stops if shares.get(e.symbol, 0) < 0]
            ex = cover_shorts(shares, cash, syms, open_prices(panel, t, syms), costs)
            record_fills(shares, ex.fills)
            shares, cash = ex.shares_after, ex.cash_after
            cum_costs += ex.slippage_cost + ex.commission
            by_sym = {f.symbol: f for f in ex.fills}
            retry = []
            for e in pending_stops:
                e.fill_session = s if e.symbol in by_sym else None
                e.fills = [by_sym[e.symbol]] if e.symbol in by_sym else []
                e.unfilled = [u for u in ex.unfilled if u["symbol"] == e.symbol]
                if e.symbol not in by_sym and shares.get(e.symbol, 0) < 0:
                    retry.append(StopEvent(e.trigger_session, None, e.symbol, e.entry, e.close))  # no open: retry
                stops.append(e)
            pending_stops = retry

        # 2b. open: execute pending rebalance
        if pending is not None and pending.fill_session == s:
            symbols = set(shares) | set(pending.signals.target_weights())
            ex = execute_rebalance(
                shares, cash, pending.signals.target_weights(), list(pending.signals.selected["symbol"]),
                open_prices(panel, t, symbols), preopen_marks(panel, t, symbols), costs,
            )
            record_fills(shares, ex.fills)
            shares, cash = ex.shares_after, ex.cash_after
            cum_costs += ex.slippage_cost + ex.commission
            pending.status = "executed"
            pending.nav_at_open = ex.nav_at_open
            pending.buy_value, pending.sell_value = ex.buy_value, ex.sell_value
            pending.turnover = ex.turnover
            pending.slippage_cost, pending.commission = ex.slippage_cost, ex.commission
            pending.cash_after = ex.cash_after
            pending.unfilled = ex.unfilled
            pending.fills = ex.fills
            pending = None

        # 3. delisting close-outs at the close
        if shares:
            shares, evs = delisting_cashouts(panel, t, shares)
            for e in evs:
                cash = r2(cash + e.amount)
                cash_events.append((s, e))
                basis.pop(e.symbol, None)

        # 4. borrow fees, then mark to market
        fee, _ = borrow_fee(panel, t, shares, cfg.borrow_fee_annual)
        if fee:
            cash = r2(cash - fee)
            cum_costs += fee
            cash_events.append((s, CashEvent("borrow_fee", "", -fee, "borrow fee on short market value")))
        pos_value, stale_n, _ = mark_to_market(panel, t, shares)
        long_v, short_v = exposures(panel, t, shares)
        nav = r2(cash + pos_value)
        if bj is not None:
            v = panel.tr[t, bj]
            bench_last = v if not np.isnan(v) else bench_last
        rows.append({
            "session": s, "nav": nav, "gross_nav": r2(nav + cum_costs), "cash": cash,
            "positions_value": pos_value, "long_value": long_v, "short_value": short_v,
            "benchmark_nav": (cfg.initial_capital * bench_last / panel.tr[t0, bj]) if bj is not None else None,
            "positions": len(shares), "stale_marks": stale_n,
        })

        # 5. short stop-loss checks on the close (fill at the next open)
        queued = {e.symbol for e in pending_stops}
        stop = cfg.short_stop_loss
        for trig in short_stop_triggers(panel, t, shares, basis, stop):
            if trig["symbol"] not in queued:
                pending_stops.append(StopEvent(s, None, trig["symbol"], trig["entry"], trig["close"]))
                stopped_since_signal.add(trig["symbol"])

        # 6. after close: month-end signal
        if calendar.is_month_end(s):
            held_long = {x for x, q in shares.items() if q > 0}
            held_short = {x for x, q in shares.items() if q < 0 and x not in stopped_since_signal}
            sig = compute_signals(panel, t, cfg, held_long, held_short, set(stopped_since_signal))
            stopped_since_signal = set()
            fill = calendar.next_session(s)
            rec = RebalanceRecord(s, fill, sig, status="pending")
            warm = sig.universe_count - (sig.table["reason"] == "no_bar_at_signal").sum()
            if sig.blocked_reason and sig.eligible_count == 0 and (sig.table["reason"] == "insufficient_history").sum() == warm:
                rec.status = "warmup"
            elif sig.blocked_reason:
                rec.status = "blocked"
                warnings.append(f"{s}: rebalance blocked -- {sig.blocked_reason}")
            elif t == t_end or fill is None or fill not in panel.sess_index or fill > end:
                rec.status = "not_executed_end_of_data"
            else:
                pending = rec
            rebalances.append(rec)

        if progress and (k % 50 == 0 or k == len(idx) - 1):
            progress((k + 1) / len(idx), s)

    nav_df = pd.DataFrame(rows).set_index("session")
    return BacktestResult(cfg, nav_df, rebalances, cash_events, warnings, bsym if bj is not None else None,
                          panel.sessions[t0], panel.sessions[t_end], stops + pending_stops)
