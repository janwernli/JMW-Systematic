-- 0005_rebalance_mode: plan-level approval of Alpaca paper rebalances.
--   app_settings.rebalance_mode = 'approve' (default) | 'auto' replaces rebalance_approval ('manual' | 'auto').
--   Approve mode: month-end rebalance and catch-up orders stay 'planned' until the whole plan is approved on
--   the Trading page. Stop-loss covers are always automatic.

CREATE TABLE broker_plan_decisions (
    plan_id      INTEGER PRIMARY KEY REFERENCES rebalance_plans(id),
    decision     TEXT NOT NULL CHECK (decision IN ('approved', 'declined')),
    decided_at   TEXT NOT NULL,
    order_count  INTEGER,            -- orders shown to the user when deciding
    note         TEXT
);

-- Carry over decisions made under 0004's per-order manual approval.
INSERT OR IGNORE INTO broker_plan_decisions (plan_id, decision, decided_at, order_count, note)
SELECT plan_id, 'approved', MIN(approved_at), COUNT(*), 'migrated from per-order approval'
FROM broker_orders WHERE approved_at IS NOT NULL AND plan_id IS NOT NULL GROUP BY plan_id;
INSERT OR IGNORE INTO broker_plan_decisions (plan_id, decision, decided_at, order_count, note)
SELECT plan_id, 'declined', MIN(updated_at), COUNT(*), 'migrated from per-order decline'
FROM broker_orders WHERE status = 'declined' AND plan_id IS NOT NULL GROUP BY plan_id;

-- New default: approve. (The previous 'auto' was only a default, never an explicit choice.)
INSERT INTO app_settings (key, value, updated_at) VALUES ('rebalance_mode', 'approve', datetime('now'))
ON CONFLICT(key) DO NOTHING;
DELETE FROM app_settings WHERE key = 'rebalance_approval';
