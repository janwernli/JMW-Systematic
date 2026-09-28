-- 0001_init: core schema for market data, research runs and the internal paper ledger.
-- All session dates are ISO strings (YYYY-MM-DD) of NYSE (XNYS) trading sessions.
-- All timestamps are ISO-8601 UTC strings.

------------------------------------------------------------------------------
-- Market data
------------------------------------------------------------------------------
CREATE TABLE data_imports (
    id                 INTEGER PRIMARY KEY,
    provider           TEXT NOT NULL,
    feed               TEXT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    status             TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'partial')),
    requested_start    TEXT,
    requested_end      TEXT,
    coverage_start     TEXT,
    coverage_end       TEXT,
    symbols_requested  INTEGER DEFAULT 0,
    symbols_loaded     INTEGER DEFAULT 0,
    bars_loaded        INTEGER DEFAULT 0,
    actions_loaded     INTEGER DEFAULT 0,
    warnings_json      TEXT NOT NULL DEFAULT '[]',
    error              TEXT,
    provenance_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE instruments (
    id              INTEGER PRIMARY KEY,
    provider        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    name            TEXT,
    exchange        TEXT,
    -- common_stock | etf | fund | preferred | warrant | unit | right | other
    asset_type      TEXT NOT NULL,
    asset_type_source TEXT,            -- how asset_type was determined (provider field / heuristic)
    sector          TEXT,
    sector_source   TEXT,
    is_benchmark    INTEGER NOT NULL DEFAULT 0,
    list_date       TEXT,
    delist_date     TEXT,              -- last trading session if known to be delisted
    active          INTEGER NOT NULL DEFAULT 1,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    import_id       INTEGER REFERENCES data_imports(id),
    updated_at      TEXT NOT NULL,
    UNIQUE (provider, symbol)
);

-- Raw (unadjusted) daily bars. Adjustments are derived from corporate_actions.
CREATE TABLE bars (
    instrument_id   INTEGER NOT NULL REFERENCES instruments(id),
    session         TEXT NOT NULL,
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL,
    volume          REAL,
    import_id       INTEGER REFERENCES data_imports(id),
    PRIMARY KEY (instrument_id, session)
) WITHOUT ROWID;
CREATE INDEX idx_bars_session ON bars(session);

CREATE TABLE corporate_actions (
    id              INTEGER PRIMARY KEY,
    instrument_id   INTEGER NOT NULL REFERENCES instruments(id),
    ex_date         TEXT NOT NULL,
    action_type     TEXT NOT NULL CHECK (action_type IN ('split', 'cash_dividend')),
    ratio           REAL,   -- split: new shares per old share (2.0 = 2-for-1, 0.1 = 1-for-10)
    amount          REAL,   -- cash dividend per share, USD, on the ex_date share basis
    import_id       INTEGER REFERENCES data_imports(id),
    UNIQUE (instrument_id, ex_date, action_type)
);

------------------------------------------------------------------------------
-- Strategy configuration & signals
------------------------------------------------------------------------------
CREATE TABLE strategy_configs (
    id              INTEGER PRIMARY KEY,
    config_hash     TEXT NOT NULL UNIQUE,
    config_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE signal_sets (
    id              INTEGER PRIMARY KEY,
    context         TEXT NOT NULL CHECK (context IN ('backtest', 'paper')),
    run_id          INTEGER,
    portfolio_id    INTEGER,
    signal_session  TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    data_version    TEXT NOT NULL,
    universe_count  INTEGER NOT NULL,
    eligible_count  INTEGER NOT NULL,
    selected_count  INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_signal_sets_ctx ON signal_sets(context, run_id, portfolio_id, signal_session);

CREATE TABLE signal_rows (
    set_id          INTEGER NOT NULL REFERENCES signal_sets(id),
    symbol          TEXT NOT NULL,
    close_raw       REAL,
    adv20           REAL,
    session_t21     TEXT,
    session_t252    TEXT,
    tr_t21          REAL,
    tr_t252         REAL,
    valid_history   INTEGER,
    momentum        REAL,
    eligible        INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    rank            INTEGER,
    selected        INTEGER NOT NULL,
    target_weight   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (set_id, symbol)
) WITHOUT ROWID;

------------------------------------------------------------------------------
-- Backtests
------------------------------------------------------------------------------
CREATE TABLE backtest_runs (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    progress         REAL NOT NULL DEFAULT 0,
    progress_note    TEXT,
    config_id        INTEGER NOT NULL REFERENCES strategy_configs(id),
    config_json      TEXT NOT NULL,
    provider         TEXT NOT NULL,
    data_label       TEXT NOT NULL,
    data_version     TEXT NOT NULL,
    data_import_id   INTEGER,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    error            TEXT,
    metrics_json     TEXT,
    assumptions_json TEXT,
    warnings_json    TEXT,
    repro_json       TEXT
);

CREATE TABLE backtest_nav (
    run_id          INTEGER NOT NULL REFERENCES backtest_runs(id),
    session         TEXT NOT NULL,
    nav             REAL NOT NULL,
    gross_nav       REAL NOT NULL,
    cash            REAL NOT NULL,
    positions_value REAL NOT NULL,
    benchmark_nav   REAL,
    positions       INTEGER NOT NULL,
    stale_marks     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, session)
) WITHOUT ROWID;

CREATE TABLE backtest_rebalances (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES backtest_runs(id),
    signal_session  TEXT NOT NULL,
    fill_session    TEXT,
    signal_set_id   INTEGER REFERENCES signal_sets(id),
    universe_count  INTEGER NOT NULL,
    eligible_count  INTEGER NOT NULL,
    selected_count  INTEGER NOT NULL,
    nav_at_open     REAL,
    buy_value       REAL,
    sell_value      REAL,
    turnover        REAL,
    slippage_cost   REAL,
    commission      REAL,
    cash_after      REAL,
    unfilled_json   TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL
);
CREATE INDEX idx_bt_reb_run ON backtest_rebalances(run_id);

CREATE TABLE backtest_fills (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES backtest_runs(id),
    rebalance_id    INTEGER REFERENCES backtest_rebalances(id),
    session         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    shares          INTEGER NOT NULL,
    ref_price       REAL NOT NULL,
    fill_price      REAL NOT NULL,
    gross_value     REAL NOT NULL,
    slippage_cost   REAL NOT NULL,
    commission      REAL NOT NULL,
    reason          TEXT NOT NULL
);
CREATE INDEX idx_bt_fills_run ON backtest_fills(run_id);

CREATE TABLE backtest_cash_events (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES backtest_runs(id),
    session         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    symbol          TEXT,
    amount          REAL NOT NULL,
    note            TEXT
);
CREATE INDEX idx_bt_cash_run ON backtest_cash_events(run_id);

------------------------------------------------------------------------------
-- Internal virtual (paper) portfolio. No broker is involved.
------------------------------------------------------------------------------
CREATE TABLE paper_portfolios (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    provider         TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    initial_capital  REAL NOT NULL,
    cash             REAL NOT NULL,
    inception_session TEXT NOT NULL,
    as_of_session    TEXT NOT NULL,
    config_id        INTEGER NOT NULL REFERENCES strategy_configs(id),
    cum_costs        REAL NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    archived_at      TEXT,
    notes            TEXT
);
CREATE UNIQUE INDEX idx_one_active_portfolio ON paper_portfolios(provider) WHERE status = 'active';

CREATE TABLE rebalance_plans (
    id              INTEGER PRIMARY KEY,
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    signal_session  TEXT NOT NULL,
    fill_session    TEXT NOT NULL,
    signal_set_id   INTEGER REFERENCES signal_sets(id),
    config_id       INTEGER NOT NULL REFERENCES strategy_configs(id),
    data_version    TEXT NOT NULL,
    -- proposed -> applied -> executed ; proposed -> skipped ; blocked (never applicable)
    status          TEXT NOT NULL CHECK (status IN ('proposed', 'applied', 'executed', 'skipped', 'blocked')),
    block_reason    TEXT,
    estimate_json   TEXT NOT NULL,
    checks_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    decision_note   TEXT,
    executed_at     TEXT,
    execution_json  TEXT,
    UNIQUE (portfolio_id, signal_session)
);

CREATE TABLE plan_orders (
    id              INTEGER PRIMARY KEY,
    plan_id         INTEGER NOT NULL REFERENCES rebalance_plans(id),
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    current_shares  INTEGER NOT NULL,
    target_shares   INTEGER NOT NULL,
    est_shares      INTEGER NOT NULL,
    est_price       REAL NOT NULL,
    est_value       REAL NOT NULL,
    est_cost        REAL NOT NULL,
    current_weight  REAL NOT NULL,
    target_weight   REAL NOT NULL,
    note            TEXT
);
CREATE INDEX idx_plan_orders_plan ON plan_orders(plan_id);

CREATE TABLE paper_orders (
    id              INTEGER PRIMARY KEY,
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    plan_id         INTEGER NOT NULL REFERENCES rebalance_plans(id),
    symbol          TEXT NOT NULL,
    intended_side   TEXT NOT NULL CHECK (intended_side IN ('buy', 'sell')),
    target_weight   REAL NOT NULL,
    est_shares      INTEGER NOT NULL,
    fill_session    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'filled', 'partially_filled', 'unfilled', 'no_trade')),
    status_reason   TEXT,
    filled_shares   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (plan_id, symbol)
);

CREATE TABLE paper_fills (
    id              INTEGER PRIMARY KEY,
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    order_id        INTEGER REFERENCES paper_orders(id),
    plan_id         INTEGER REFERENCES rebalance_plans(id),
    session         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    shares          INTEGER NOT NULL CHECK (shares > 0),
    ref_price       REAL NOT NULL,
    fill_price      REAL NOT NULL,
    gross_value     REAL NOT NULL,
    slippage_cost   REAL NOT NULL,
    commission      REAL NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE cash_transactions (
    id              INTEGER PRIMARY KEY,
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    session         TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('initial_deposit', 'buy', 'sell', 'commission',
                                                   'dividend', 'cash_in_lieu', 'delisting_cashout')),
    symbol          TEXT,
    amount          REAL NOT NULL,
    balance_after   REAL NOT NULL,
    fill_id         INTEGER REFERENCES paper_fills(id),
    note            TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_cash_tx_portfolio ON cash_transactions(portfolio_id, id);

-- Current holdings (derived state, updated in the same DB transaction as fills/actions).
CREATE TABLE positions (
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    symbol          TEXT NOT NULL,
    shares          INTEGER NOT NULL CHECK (shares >= 0),
    cost_basis      REAL NOT NULL,
    opened_session  TEXT NOT NULL,
    updated_session TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, symbol)
) WITHOUT ROWID;

CREATE TABLE position_snapshots (
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    session         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    shares          INTEGER NOT NULL,
    mark_price      REAL NOT NULL,
    mark_session    TEXT NOT NULL,
    market_value    REAL NOT NULL,
    cost_basis      REAL NOT NULL,
    PRIMARY KEY (portfolio_id, session, symbol)
) WITHOUT ROWID;

CREATE TABLE paper_nav (
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    session         TEXT NOT NULL,
    cash            REAL NOT NULL,
    positions_value REAL NOT NULL,
    nav             REAL NOT NULL,
    gross_nav       REAL NOT NULL,
    benchmark_index REAL,
    positions       INTEGER NOT NULL,
    stale_marks     INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, session)
) WITHOUT ROWID;

------------------------------------------------------------------------------
-- Audit trail
------------------------------------------------------------------------------
CREATE TABLE system_events (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,
    level           TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    category        TEXT NOT NULL,
    message         TEXT NOT NULL,
    portfolio_id    INTEGER,
    run_id          INTEGER,
    session         TEXT,
    payload_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_events_ts ON system_events(id DESC);

------------------------------------------------------------------------------
-- Immutability guards: append-only ledger records.
------------------------------------------------------------------------------
CREATE TRIGGER paper_fills_no_update BEFORE UPDATE ON paper_fills
BEGIN SELECT RAISE(ABORT, 'paper_fills are immutable'); END;
CREATE TRIGGER paper_fills_no_delete BEFORE DELETE ON paper_fills
BEGIN SELECT RAISE(ABORT, 'paper_fills are immutable'); END;

CREATE TRIGGER cash_tx_no_update BEFORE UPDATE ON cash_transactions
BEGIN SELECT RAISE(ABORT, 'cash_transactions are immutable'); END;
CREATE TRIGGER cash_tx_no_delete BEFORE DELETE ON cash_transactions
BEGIN SELECT RAISE(ABORT, 'cash_transactions are immutable'); END;

CREATE TRIGGER paper_nav_no_update BEFORE UPDATE ON paper_nav
BEGIN SELECT RAISE(ABORT, 'paper_nav rows are immutable'); END;
CREATE TRIGGER paper_nav_no_delete BEFORE DELETE ON paper_nav
BEGIN SELECT RAISE(ABORT, 'paper_nav rows are immutable'); END;

CREATE TRIGGER pos_snap_no_update BEFORE UPDATE ON position_snapshots
BEGIN SELECT RAISE(ABORT, 'position_snapshots are immutable'); END;
CREATE TRIGGER pos_snap_no_delete BEFORE DELETE ON position_snapshots
BEGIN SELECT RAISE(ABORT, 'position_snapshots are immutable'); END;

CREATE TRIGGER plan_orders_no_update BEFORE UPDATE ON plan_orders
BEGIN SELECT RAISE(ABORT, 'plan_orders are immutable'); END;
CREATE TRIGGER plan_orders_no_delete BEFORE DELETE ON plan_orders
BEGIN SELECT RAISE(ABORT, 'plan_orders are immutable'); END;

CREATE TRIGGER signal_rows_no_update BEFORE UPDATE ON signal_rows
BEGIN SELECT RAISE(ABORT, 'signal_rows are immutable'); END;

CREATE TRIGGER events_no_update BEFORE UPDATE ON system_events
BEGIN SELECT RAISE(ABORT, 'system_events are immutable'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON system_events
BEGIN SELECT RAISE(ABORT, 'system_events are immutable'); END;

-- Orders may only leave the 'pending' state once.
CREATE TRIGGER paper_orders_terminal BEFORE UPDATE ON paper_orders
WHEN OLD.status <> 'pending'
BEGIN SELECT RAISE(ABORT, 'paper order already in a terminal state'); END;
CREATE TRIGGER paper_orders_no_delete BEFORE DELETE ON paper_orders
BEGIN SELECT RAISE(ABORT, 'paper_orders are immutable'); END;

-- Plans: proposed -> applied|skipped ; applied -> executed. Nothing else.
CREATE TRIGGER plans_transition BEFORE UPDATE OF status ON rebalance_plans
WHEN NOT (
    (OLD.status = 'proposed' AND NEW.status IN ('applied', 'skipped')) OR
    (OLD.status = 'applied'  AND NEW.status = 'executed')
)
BEGIN SELECT RAISE(ABORT, 'illegal rebalance plan status transition'); END;
CREATE TRIGGER plans_no_delete BEFORE DELETE ON rebalance_plans
BEGIN SELECT RAISE(ABORT, 'rebalance_plans are immutable'); END;
