-- 0008_fundamentals: point-in-time fundamentals for the value signal (research variant "Value + momentum").
-- One row per reported XBRL fact we use, with its FILING date: a fact is usable at a signal date only if
-- filed <= signal date. Source today: SEC EDGAR companyfacts (Alpaca provider); later Compustat via WRDS.

CREATE TABLE fundamental_facts (
    id          INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL,                 -- market-data provider whose symbols these are
    symbol      TEXT NOT NULL,
    cik         INTEGER,
    concept     TEXT NOT NULL CHECK (concept IN ('equity', 'net_income', 'shares')),
    source_tag  TEXT NOT NULL,                 -- e.g. us-gaap:StockholdersEquity
    start       TEXT,                          -- duration facts (net income) only
    end         TEXT NOT NULL,                 -- period end (instant date for equity / shares)
    value       REAL NOT NULL,
    filed       TEXT NOT NULL,                 -- filing date: the point-in-time key
    form        TEXT,
    fy          INTEGER,
    fp          TEXT,
    accn        TEXT,
    source      TEXT NOT NULL DEFAULT 'sec_companyfacts'
);
CREATE INDEX idx_fund_symbol ON fundamental_facts(provider, symbol, concept, filed);

CREATE TABLE fundamental_fetches (
    provider    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    cik         INTEGER,
    fetched_at  TEXT NOT NULL,
    status      TEXT NOT NULL,                 -- ok | no_cik | no_facts | error
    facts       INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    PRIMARY KEY (provider, symbol)
);
