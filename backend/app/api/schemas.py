"""Typed API contracts. The frontend's TypeScript types are generated from these via OpenAPI."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..strategy.config import StrategyConfig

DataLabel = Literal["Delayed Market Data", "End-of-Day Market Data", "Backtest", "Paper Simulation", "Alpaca Paper"]


class ApiError(BaseModel):
    error: str
    message: str


# ------------------------------------------------------------------ system / data
class ProviderInfoModel(BaseModel):
    key: str
    name: str
    feed: str
    data_label: str
    is_demo: bool
    benchmark_symbol: str
    benchmark_return_basis: str
    point_in_time_universe: bool
    coverage_note: str
    entitlement_note: str
    adjustment_note: str
    survivorship_note: str
    requires_key: bool


class MarketClock(BaseModel):
    now_utc: str
    now_new_york: str
    status: Literal["open", "closed", "pre_open", "after_close"]
    session_today: str | None
    next_open_utc: str | None
    next_close_utc: str | None
    calendar: str
    source: str


class SystemStatus(BaseModel):
    app_version: str
    mode: Literal["live"]
    data_label: str
    provider: ProviderInfoModel | None
    provider_error: str | None
    market_clock: MarketClock
    data_version: str | None
    data_start: str | None
    data_end: str | None
    has_portfolio: bool
    portfolio_as_of: str | None
    server_time_utc: str
    bind_host: str


class DataImport(BaseModel):
    id: int
    provider: str
    feed: str | None
    started_at: str
    finished_at: str | None
    status: str
    requested_start: str | None
    requested_end: str | None
    coverage_start: str | None
    coverage_end: str | None
    symbols_requested: int | None
    symbols_loaded: int | None
    bars_loaded: int | None
    actions_loaded: int | None
    warnings: list[str]
    error: str | None


class QualityWarning(BaseModel):
    level: Literal["info", "warning", "error"]
    message: str


class StaleSymbol(BaseModel):
    symbol: str
    last_bar: str


class DataQuality(BaseModel):
    provider: ProviderInfoModel
    has_data: bool
    latest_import: DataImport | None
    latest_successful_import: DataImport | None
    coverage_start: str | None
    coverage_end: str | None
    expected_latest_session: str | None
    is_stale: bool
    stale_reason: str | None
    instruments: int
    universe_count: int
    eligible_count: int | None = None
    total_bars: int | None = None
    missing_bars: int
    missing_opens: int
    stale_symbols: list[StaleSymbol]
    latest_session_coverage: float
    benchmark_symbol: str | None = None
    warnings: list[QualityWarning]
    generated_at: str


# ------------------------------------------------------------------ universe
class UniverseRow(BaseModel):
    symbol: str
    name: str | None
    sector: str | None
    asset_type: str
    close_raw: float | None
    adv20: float | None
    momentum: float | None
    rank: int | None
    eligible: bool
    reason: str
    reason_text: str
    selected: bool
    score: float | None = None
    resid_mom: float | None = None
    sector_mom: float | None = None
    fip: float | None = None
    composite: float | None = None
    side: str | None = Field(None, description="long | short | None in this live ranking")
    percentile: float | None = None
    composite: float | None = None
    sector: str | None = None
    vol: float | None = None
    beta: float | None = None
    model_weight: float = Field(description="Signed target weight in this live ranking (negative = short)")
    plan_target_weight: float | None = Field(description="Target weight in the latest frozen paper plan")
    position_shares: int
    current_weight: float


class UniverseResponse(BaseModel):
    data_label: str
    signal_session: str
    is_month_end: bool
    basis_note: str
    plan_signal_session: str | None
    portfolio_as_of: str | None
    config: StrategyConfig
    universe_count: int
    eligible_count: int
    selected_count: int
    coverage: float
    blocked_reason: str | None
    reason_counts: dict[str, int]
    diagnostics: dict = {}
    rows: list[UniverseRow]


class PricePoint(BaseModel):
    session: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    tr_index: float | None


class CorporateActionModel(BaseModel):
    ex_date: str
    action_type: str
    ratio: float | None
    amount: float | None


class SignalInputs(BaseModel):
    signal_session: str
    session_t21: str | None
    session_t252: str | None
    close_t: float | None
    close_t21_raw: float | None
    close_t252_raw: float | None
    tr_t21: float | None
    tr_t252: float | None
    momentum: float | None
    adv20: float | None
    valid_history: int
    eligible: bool
    reason: str
    reason_text: str
    rank: int | None
    formula: str


class StockDetail(BaseModel):
    data_label: str
    symbol: str
    name: str | None
    sector: str | None
    sector_source: str | None
    asset_type: str
    asset_type_source: str | None
    exchange: str | None
    list_date: str | None
    delist_date: str | None
    signal: SignalInputs
    prices: list[PricePoint]
    corporate_actions: list[CorporateActionModel]
    position_shares: int


# ------------------------------------------------------------------ portfolio
class PortfolioMeta(BaseModel):
    id: int
    name: str
    provider: str
    status: str
    initial_capital: float
    cash: float
    inception_session: str
    as_of_session: str
    config_id: int
    cum_costs: float
    created_at: str
    notes: str | None


class NavPoint(BaseModel):
    session: str
    nav: float
    gross_nav: float
    cash: float
    benchmark_nav: float | None
    drawdown: float


class Mover(BaseModel):
    symbol: str
    return_1d: float | None
    pnl_1d: float
    weight: float


class FillModel(BaseModel):
    id: int
    session: str
    symbol: str
    side: Literal["buy", "sell"]
    shares: int
    ref_price: float
    fill_price: float
    gross_value: float
    slippage_cost: float
    commission: float
    plan_id: int | None = None
    order_id: int | None = None
    reason: str | None = None
    created_at: str | None = None
    position_effect: str | None = None


class EventModel(BaseModel):
    id: int
    ts: str
    level: Literal["info", "warning", "error"]
    category: str
    message: str
    portfolio_id: int | None
    run_id: int | None
    session: str | None
    payload: dict


class NextRebalance(BaseModel):
    signal_session: str | None
    fill_session: str | None
    pending_plan_id: int | None
    pending_plan_status: str | None
    blocked_reason: str | None


class CommandCenter(BaseModel):
    data_label: str
    portfolio: PortfolioMeta | None
    valuation_session: str | None
    nav: float | None
    cash: float | None
    invested: float | None
    positions: int
    daily_return: float | None
    daily_pnl: float | None
    inception_return: float | None
    gross_inception_return: float | None
    benchmark_symbol: str | None
    benchmark_return_basis: str | None
    benchmark_inception_return: float | None
    return_difference: float | None
    drawdown: float | None
    max_drawdown: float | None
    mode: str | None = None
    long_gross: float | None = None
    short_gross: float | None = None
    net_exposure: float | None = None
    next_rebalance: NextRebalance
    nav_series: list[NavPoint]
    top_movers: list[Mover]
    recent_fills: list[FillModel]
    alerts: list[EventModel]
    data_is_stale: bool
    data_stale_reason: str | None
    data_end: str | None


class PositionRow(BaseModel):
    symbol: str
    side: str
    name: str | None
    sector: str | None
    shares: int
    mark_price: float
    mark_session: str
    stale_mark: bool
    market_value: float
    weight: float
    target_weight: float | None
    drift: float | None
    cost_basis: float
    unrealized_pnl: float
    unrealized_pct: float | None
    contribution: float = Field(description="Unrealized P&L / NAV")
    return_1d: float | None
    opened_session: str | None


class SectorExposure(BaseModel):
    sector: str
    weight: float
    long_weight: float = 0.0
    short_weight: float = 0.0
    positions: int


class Concentration(BaseModel):
    top5_weight: float
    top10_weight: float
    largest_symbol: str | None
    largest_weight: float
    hhi: float
    effective_n: float | None


class PositionsResponse(BaseModel):
    data_label: str
    valuation_session: str | None
    valuation_note: str
    nav: float
    cash: float
    cash_weight: float
    rows: list[PositionRow]
    long_gross: float
    short_gross: float
    net_exposure: float
    mode: str
    sector_exposure: list[SectorExposure] | None
    sector_note: str
    concentration: Concentration
    plan_signal_session: str | None


# ------------------------------------------------------------------ paper actions
class PaperInitRequest(BaseModel):
    inception_session: str | None = Field(None, description="Session at whose close cash is funded; default latest data session")
    config: StrategyConfig | None = None
    name: str | None = None


class PaperAdvanceRequest(BaseModel):
    until: str | None = Field(None, description="Last session to process (default: latest stored data)")
    max_sessions: int | None = Field(None, ge=1, le=10_000)


class AdvanceResponse(BaseModel):
    processed: list[str]
    processed_count: int
    as_of: str
    stopped_reason: str
    stopped_explanation: str
    pending_plan_id: int | None


class PaperConfigResponse(BaseModel):
    config_id: int
    config: StrategyConfig
    history: list[dict]


# ------------------------------------------------------------------ rebalance
class RuleCheck(BaseModel):
    rule: str
    ok: bool
    detail: str


class PlanOrderModel(BaseModel):
    id: int
    symbol: str
    side: Literal["buy", "sell"]
    current_shares: int
    target_shares: int
    est_shares: int
    est_price: float
    est_value: float
    est_cost: float
    current_weight: float
    target_weight: float
    note: str | None
    order_status: str | None = None
    filled_shares: int | None = None
    order_reason: str | None = None


class PlanTarget(BaseModel):
    symbol: str
    rank: int
    momentum: float
    close_raw: float
    adv20: float
    target_weight: float
    current_weight: float
    side: str | None = None
    vol: float | None = None
    beta: float | None = None
    percentile: float | None = None


class PlanSummary(BaseModel):
    id: int
    signal_session: str
    fill_session: str
    status: Literal["proposed", "applied", "executed", "skipped", "blocked"]
    block_reason: str | None
    created_at: str
    decided_at: str | None
    executed_at: str | None
    selected_count: int
    est_turnover: float | None
    realized_turnover: float | None


class PlanDetail(PlanSummary):
    data_label: str
    data_version: str
    config: StrategyConfig
    estimate: dict
    execution: dict | None
    checks: list[RuleCheck]
    orders: list[PlanOrderModel]
    targets: list[PlanTarget]
    fills: list[FillModel]
    can_apply: bool
    apply_disabled_reason: str | None
    portfolio_as_of: str


# ------------------------------------------------------------------ research
class ResearchDefaults(BaseModel):
    data_label: str
    config: StrategyConfig
    config_warnings: list[str] = []
    earliest_start: str
    latest_end: str
    benchmark_symbol: str | None
    survivorship_warning: str | None


class RunRequest(BaseModel):
    config: StrategyConfig
    name: str | None = None


class RunSummary(BaseModel):
    id: int
    name: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: float
    progress_note: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    provider: str
    data_label: str
    data_version: str
    config: StrategyConfig
    headline: dict | None


class RunDetail(RunSummary):
    metrics: dict | None
    assumptions: list[dict]
    warnings: list[str]
    repro: dict | None


class RunSeriesPoint(BaseModel):
    session: str
    nav: float
    gross_nav: float
    benchmark_nav: float | None
    drawdown: float
    benchmark_drawdown: float | None
    cash_weight: float
    positions: int
    long_gross: float | None = None
    short_gross: float | None = None
    net_exposure: float | None = None
    rolling_return: float | None
    rolling_vol: float | None
    rolling_bench_return: float | None
    rolling_difference: float | None


class MonthlyReturn(BaseModel):
    year: int
    month: int
    strategy: float
    benchmark: float | None


class RunRebalance(BaseModel):
    id: int
    signal_session: str
    fill_session: str | None
    status: str
    universe_count: int
    eligible_count: int
    selected_count: int
    nav_at_open: float | None
    turnover: float | None
    slippage_cost: float | None
    commission: float | None
    cash_after: float | None
    unfilled: list[dict]
    diagnostics: dict | None = None


class Page(BaseModel):
    total: int
    limit: int
    offset: int


class FillsPage(Page):
    rows: list[FillModel]


class OrderModel(BaseModel):
    id: int
    plan_id: int | None
    origin: str = "plan"
    trigger_session: str | None = None
    symbol: str
    intended_side: str
    target_weight: float
    est_shares: int
    fill_session: str
    status: str
    status_reason: str | None
    filled_shares: int
    created_at: str
    updated_at: str


class OrdersPage(Page):
    rows: list[OrderModel]


class CashTx(BaseModel):
    id: int
    session: str
    kind: str
    symbol: str | None
    amount: float
    balance_after: float
    fill_id: int | None
    note: str | None
    created_at: str


class CashPage(Page):
    rows: list[CashTx]


class EventsPage(Page):
    rows: list[EventModel]


class Reproducibility(BaseModel):
    app_version: str
    git_commit: str | None
    python: str
    packages: dict[str, str]
    database_path: str
    schema_migrations: list[dict]
    provider: str
    data_version: str | None
    portfolio_configs: list[dict]
    plan_data_versions: list[dict]
