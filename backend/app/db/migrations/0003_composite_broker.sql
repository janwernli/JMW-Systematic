-- 0003_composite_broker: composite signal columns, point-in-time index membership, Alpaca paper-trading
-- tables, automation log/settings; removes the retired synthetic demo and long-only artifacts.

------------------------------------------------------------------------------
-- Signal rows: composite components, sector, liquidity screen
------------------------------------------------------------------------------
ALTER TABLE signal_rows ADD COLUMN adv60 REAL;
ALTER TABLE signal_rows ADD COLUMN sector TEXT;
ALTER TABLE signal_rows ADD COLUMN resid_mom REAL;
ALTER TABLE signal_rows ADD COLUMN sector_mom REAL;
ALTER TABLE signal_rows ADD COLUMN fip REAL;
ALTER TABLE signal_rows ADD COLUMN composite REAL;
ALTER TABLE signal_rows ADD COLUMN score REAL;

------------------------------------------------------------------------------
-- Point-in-time index membership (e.g. Norgate Russell 1000 incl. delisted names)
------------------------------------------------------------------------------
CREATE TABLE universe_membership (
    instrument_id   INTEGER NOT NULL REFERENCES instruments(id),
    index_name      TEXT NOT NULL,
    start_session   TEXT NOT NULL,
    end_session     TEXT,                 -- NULL = still a member
    import_id       INTEGER REFERENCES data_imports(id),
    PRIMARY KEY (instrument_id, index_name, start_session)
) WITHOUT ROWID;

------------------------------------------------------------------------------
-- Alpaca PAPER account mirror (the source of truth for the traded portfolio)
------------------------------------------------------------------------------
CREATE TABLE broker_orders (
    id               INTEGER PRIMARY KEY,
    client_order_id  TEXT NOT NULL UNIQUE,     -- deterministic: prevents duplicate submission
    broker_order_id  TEXT,
    origin           TEXT NOT NULL CHECK (origin IN ('rebalance', 'stop_loss', 'catch_up')),
    plan_id          INTEGER REFERENCES rebalance_plans(id),
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    position_effect  TEXT,
    qty              INTEGER NOT NULL CHECK (qty > 0),
    order_type       TEXT NOT NULL,
    time_in_force    TEXT NOT NULL,
    intended_session TEXT NOT NULL,
    ref_price        REAL,
    status           TEXT NOT NULL,            -- planned | skipped | submitted | Alpaca status (new, filled, ...)
    status_reason    TEXT,
    filled_qty       INTEGER NOT NULL DEFAULT 0,
    filled_avg_price REAL,
    filled_at        TEXT,
    submitted_at     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    raw_json         TEXT
);
CREATE INDEX idx_broker_orders_session ON broker_orders(intended_session);

CREATE TABLE broker_equity (
    session          TEXT PRIMARY KEY,         -- daily equity from Alpaca portfolio history
    equity           REAL NOT NULL,
    profit_loss      REAL,
    updated_at       TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE broker_positions (
    as_of            TEXT NOT NULL,            -- sync timestamp (UTC)
    symbol           TEXT NOT NULL,
    qty              INTEGER NOT NULL,
    avg_entry_price  REAL,
    market_value     REAL,
    current_price    REAL,
    unrealized_pl    REAL,
    PRIMARY KEY (as_of, symbol)
) WITHOUT ROWID;

CREATE TABLE broker_account_snapshots (
    as_of            TEXT PRIMARY KEY,
    equity           REAL, cash REAL, long_market_value REAL, short_market_value REAL,
    buying_power     REAL, regt_buying_power REAL, multiplier REAL, shorting_enabled INTEGER,
    status           TEXT, raw_json TEXT
) WITHOUT ROWID;

CREATE TABLE automation_runs (
    id               INTEGER PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    trigger          TEXT NOT NULL,            -- schedule | manual | cli
    dry_run          INTEGER NOT NULL,
    status           TEXT NOT NULL,            -- running | ok | warning | error | skipped
    summary          TEXT,
    steps_json       TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE app_settings (
    key              TEXT PRIMARY KEY,
    value            TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
INSERT INTO app_settings (key, value, updated_at) VALUES ('automation_enabled', 'true', datetime('now'));

------------------------------------------------------------------------------
-- Remove retired demo / long-only artifacts (triggers dropped and recreated around the purge)
------------------------------------------------------------------------------
DROP TRIGGER paper_fills_no_delete;
DROP TRIGGER cash_tx_no_delete;
DROP TRIGGER paper_nav_no_delete;
DROP TRIGGER pos_snap_no_delete;
DROP TRIGGER plan_orders_no_delete;
DROP TRIGGER plans_no_delete;
DROP TRIGGER paper_orders_no_delete;
DROP TRIGGER events_no_delete;

CREATE TEMP TABLE _demo_pf AS SELECT id FROM paper_portfolios WHERE provider = 'demo';
DELETE FROM cash_transactions WHERE portfolio_id IN (SELECT id FROM _demo_pf);
DELETE FROM paper_fills WHERE portfolio_id IN (SELECT id FROM _demo_pf);
DELETE FROM paper_orders WHERE portfolio_id IN (SELECT id FROM _demo_pf);
DELETE FROM plan_orders WHERE plan_id IN (SELECT id FROM rebalance_plans WHERE portfolio_id IN (SELECT id FROM _demo_pf));
DELETE FROM rebalance_plans WHERE portfolio_id IN (SELECT id FROM _demo_pf);
DELETE FROM position_snapshots WHERE portfolio_id IN (SELECT id FROM _demo_pf);
DELETE FROM paper_nav WHERE portfolio_id IN (SELECT id FROM _demo_pf);
DELETE FROM positions WHERE portfolio_id IN (SELECT id FROM _demo_pf);
DELETE FROM system_events WHERE portfolio_id IN (SELECT id FROM _demo_pf);
DELETE FROM signal_rows WHERE set_id IN (SELECT id FROM signal_sets WHERE portfolio_id IN (SELECT id FROM _demo_pf));
DELETE FROM signal_sets WHERE portfolio_id IN (SELECT id FROM _demo_pf);
DELETE FROM paper_portfolios WHERE id IN (SELECT id FROM _demo_pf);

CREATE TEMP TABLE _old_runs AS SELECT id FROM backtest_runs
    WHERE provider = 'demo' OR config_json LIKE '%"mode":"long_only"%';
DELETE FROM backtest_fills WHERE run_id IN (SELECT id FROM _old_runs);
DELETE FROM backtest_cash_events WHERE run_id IN (SELECT id FROM _old_runs);
DELETE FROM backtest_rebalances WHERE run_id IN (SELECT id FROM _old_runs);
DELETE FROM backtest_nav WHERE run_id IN (SELECT id FROM _old_runs);
DELETE FROM signal_rows WHERE set_id IN (SELECT id FROM signal_sets WHERE run_id IN (SELECT id FROM _old_runs));
DELETE FROM signal_sets WHERE run_id IN (SELECT id FROM _old_runs);
DELETE FROM system_events WHERE run_id IN (SELECT id FROM _old_runs);
DELETE FROM backtest_runs WHERE id IN (SELECT id FROM _old_runs);

DELETE FROM corporate_actions WHERE instrument_id IN (SELECT id FROM instruments WHERE provider = 'demo');
DELETE FROM bars WHERE instrument_id IN (SELECT id FROM instruments WHERE provider = 'demo');
DELETE FROM instruments WHERE provider = 'demo';
DELETE FROM data_imports WHERE provider = 'demo';
DROP TABLE _demo_pf;
DROP TABLE _old_runs;

CREATE TRIGGER paper_fills_no_delete BEFORE DELETE ON paper_fills
BEGIN SELECT RAISE(ABORT, 'paper_fills are immutable'); END;
CREATE TRIGGER cash_tx_no_delete BEFORE DELETE ON cash_transactions
BEGIN SELECT RAISE(ABORT, 'cash_transactions are immutable'); END;
CREATE TRIGGER paper_nav_no_delete BEFORE DELETE ON paper_nav
BEGIN SELECT RAISE(ABORT, 'paper_nav rows are immutable'); END;
CREATE TRIGGER pos_snap_no_delete BEFORE DELETE ON position_snapshots
BEGIN SELECT RAISE(ABORT, 'position_snapshots are immutable'); END;
CREATE TRIGGER plan_orders_no_delete BEFORE DELETE ON plan_orders
BEGIN SELECT RAISE(ABORT, 'plan_orders are immutable'); END;
CREATE TRIGGER plans_no_delete BEFORE DELETE ON rebalance_plans
BEGIN SELECT RAISE(ABORT, 'rebalance_plans are immutable'); END;
CREATE TRIGGER paper_orders_no_delete BEFORE DELETE ON paper_orders
BEGIN SELECT RAISE(ABORT, 'paper_orders are immutable'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON system_events
BEGIN SELECT RAISE(ABORT, 'system_events are immutable'); END;
