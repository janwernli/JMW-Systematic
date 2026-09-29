-- 0004_interest_approval: optional cash interest (RF) in the model ledger; manual approval of paper rebalances.

------------------------------------------------------------------------------
-- cash_transactions: new kind 'interest' (table rebuilt; data and immutability triggers preserved)
------------------------------------------------------------------------------
CREATE TABLE cash_transactions_new (
    id              INTEGER PRIMARY KEY,
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    session         TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('initial_deposit', 'buy', 'sell', 'commission', 'dividend',
                                                   'cash_in_lieu', 'delisting_cashout', 'borrow_fee', 'interest')),
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
-- Manual approval of rebalance orders
------------------------------------------------------------------------------
ALTER TABLE broker_orders ADD COLUMN approved_at TEXT;   -- set when the user approves (manual mode)
INSERT OR IGNORE INTO app_settings (key, value, updated_at) VALUES ('rebalance_approval', 'auto', datetime('now'));
