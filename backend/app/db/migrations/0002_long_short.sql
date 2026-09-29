-- 0002_long_short: support short positions, stop-loss orders, borrow fees and exposure tracking.
-- Tables whose CHECK constraints change are rebuilt (SQLite's documented 12-step procedure);
-- existing rows are copied unchanged and immutability triggers are recreated.

------------------------------------------------------------------------------
-- positions: shares may now be negative (short positions)
------------------------------------------------------------------------------
CREATE TABLE positions_new (
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    symbol          TEXT NOT NULL,
    shares          INTEGER NOT NULL CHECK (shares <> 0),
    cost_basis      REAL NOT NULL,          -- signed: long = cost paid, short = -(net proceeds)
    opened_session  TEXT NOT NULL,
    updated_session TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, symbol)
) WITHOUT ROWID;
INSERT INTO positions_new SELECT * FROM positions WHERE shares <> 0;
DROP TABLE positions;
ALTER TABLE positions_new RENAME TO positions;

------------------------------------------------------------------------------
-- cash_transactions: new kind 'borrow_fee'
------------------------------------------------------------------------------
CREATE TABLE cash_transactions_new (
    id              INTEGER PRIMARY KEY,
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    session         TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('initial_deposit', 'buy', 'sell', 'commission', 'dividend',
                                                   'cash_in_lieu', 'delisting_cashout', 'borrow_fee')),
    symbol          TEXT,
    amount          REAL NOT NULL,
    balance_after   REAL NOT NULL,
    fill_id         INTEGER REFERENCES paper_fills(id),
    note            TEXT,
    created_at      TEXT NOT NULL
);
INSERT INTO cash_transactions_new SELECT * FROM cash_transactions;
DROP TABLE cash_transactions;
ALTER TABLE cash_transactions_new RENAME TO cash_transactions;
CREATE INDEX idx_cash_tx_portfolio ON cash_transactions(portfolio_id, id);
CREATE TRIGGER cash_tx_no_update BEFORE UPDATE ON cash_transactions
BEGIN SELECT RAISE(ABORT, 'cash_transactions are immutable'); END;
CREATE TRIGGER cash_tx_no_delete BEFORE DELETE ON cash_transactions
BEGIN SELECT RAISE(ABORT, 'cash_transactions are immutable'); END;

------------------------------------------------------------------------------
-- paper_orders: plan_id optional (stop-loss covers), origin + trigger session
------------------------------------------------------------------------------
CREATE TABLE paper_orders_new (
    id              INTEGER PRIMARY KEY,
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    plan_id         INTEGER REFERENCES rebalance_plans(id),
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
    origin          TEXT NOT NULL DEFAULT 'plan' CHECK (origin IN ('plan', 'stop_loss')),
    trigger_session TEXT,
    UNIQUE (plan_id, symbol)
);
INSERT INTO paper_orders_new (id, portfolio_id, plan_id, symbol, intended_side, target_weight, est_shares, fill_session,
                              status, status_reason, filled_shares, created_at, updated_at)
SELECT id, portfolio_id, plan_id, symbol, intended_side, target_weight, est_shares, fill_session,
       status, status_reason, filled_shares, created_at, updated_at FROM paper_orders;
DROP TABLE paper_orders;
ALTER TABLE paper_orders_new RENAME TO paper_orders;
CREATE INDEX idx_paper_orders_stop ON paper_orders(portfolio_id, origin, trigger_session);
CREATE TRIGGER paper_orders_terminal BEFORE UPDATE ON paper_orders
WHEN OLD.status <> 'pending'
BEGIN SELECT RAISE(ABORT, 'paper order already in a terminal state'); END;
CREATE TRIGGER paper_orders_no_delete BEFORE DELETE ON paper_orders
BEGIN SELECT RAISE(ABORT, 'paper_orders are immutable'); END;

------------------------------------------------------------------------------
-- New columns (ADD COLUMN is not an UPDATE, so immutability triggers are unaffected)
------------------------------------------------------------------------------
ALTER TABLE paper_fills ADD COLUMN position_effect TEXT;      -- open_long | close_long | open_short | close_short
ALTER TABLE backtest_fills ADD COLUMN position_effect TEXT;
ALTER TABLE paper_nav ADD COLUMN long_value REAL;
ALTER TABLE paper_nav ADD COLUMN short_value REAL;              -- negative market value of shorts
ALTER TABLE backtest_nav ADD COLUMN long_value REAL;
ALTER TABLE backtest_nav ADD COLUMN short_value REAL;
ALTER TABLE backtest_rebalances ADD COLUMN diagnostics_json TEXT;
ALTER TABLE signal_sets ADD COLUMN diagnostics_json TEXT;
ALTER TABLE signal_rows ADD COLUMN side TEXT;                  -- long | short | NULL
ALTER TABLE signal_rows ADD COLUMN percentile REAL;            -- 0 = best momentum, 1 = worst
ALTER TABLE signal_rows ADD COLUMN vol REAL;                   -- annualized realized vol
ALTER TABLE signal_rows ADD COLUMN beta REAL;                  -- shrunk beta vs. benchmark
