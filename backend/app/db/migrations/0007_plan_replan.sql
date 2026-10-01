-- 0007_plan_replan: switching the paper strategy mid-month.
--   rebalance_plans.kind: 'month_end' (formed at a month-end close) | 'replan' (rebuilt from the latest month-end
--   signal with a new config, `python -m app paper-config --replan-now`). Uniqueness of (portfolio, signal session)
--   now applies to month-end plans only, and an unexecuted plan can be 'superseded' by a replan.
--   Table rebuilt (SQLite cannot alter CHECK constraints); data and immutability triggers preserved.

CREATE TABLE rebalance_plans_new (
    id              INTEGER PRIMARY KEY,
    portfolio_id    INTEGER NOT NULL REFERENCES paper_portfolios(id),
    signal_session  TEXT NOT NULL,
    fill_session    TEXT NOT NULL,
    signal_set_id   INTEGER REFERENCES signal_sets(id),
    config_id       INTEGER NOT NULL REFERENCES strategy_configs(id),
    data_version    TEXT NOT NULL,
    -- proposed -> applied -> executed ; proposed -> skipped ; blocked ; proposed|applied -> superseded
    status          TEXT NOT NULL CHECK (status IN ('proposed', 'applied', 'executed', 'skipped', 'blocked', 'superseded')),
    block_reason    TEXT,
    estimate_json   TEXT NOT NULL,
    checks_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    decision_note   TEXT,
    executed_at     TEXT,
    execution_json  TEXT,
    kind            TEXT NOT NULL DEFAULT 'month_end' CHECK (kind IN ('month_end', 'replan')),
    replaces_json   TEXT                          -- replan: ids of the plans it superseded
);
INSERT INTO rebalance_plans_new (id, portfolio_id, signal_session, fill_session, signal_set_id, config_id, data_version,
    status, block_reason, estimate_json, checks_json, created_at, decided_at, decision_note, executed_at, execution_json)
SELECT id, portfolio_id, signal_session, fill_session, signal_set_id, config_id, data_version, status, block_reason,
       estimate_json, checks_json, created_at, decided_at, decision_note, executed_at, execution_json
FROM rebalance_plans;
DROP TABLE rebalance_plans;
ALTER TABLE rebalance_plans_new RENAME TO rebalance_plans;
CREATE UNIQUE INDEX ux_plans_month_end ON rebalance_plans(portfolio_id, signal_session) WHERE kind = 'month_end';
CREATE INDEX idx_plans_portfolio ON rebalance_plans(portfolio_id, signal_session, id);

-- Plans: proposed -> applied|skipped|superseded ; applied -> executed|superseded. Nothing else.
CREATE TRIGGER plans_transition BEFORE UPDATE OF status ON rebalance_plans
WHEN NOT (
    (OLD.status = 'proposed' AND NEW.status IN ('applied', 'skipped', 'superseded')) OR
    (OLD.status = 'applied'  AND NEW.status IN ('executed', 'superseded'))
)
BEGIN SELECT RAISE(ABORT, 'illegal rebalance plan status transition'); END;
CREATE TRIGGER plans_no_delete BEFORE DELETE ON rebalance_plans
BEGIN SELECT RAISE(ABORT, 'rebalance_plans are immutable'); END;
