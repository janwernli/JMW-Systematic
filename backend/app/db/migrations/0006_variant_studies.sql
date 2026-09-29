-- 0006_variant_studies: one row per side-by-side comparison of the pre-defined strategy variants
-- (the four backtest runs share the same data version and period).

CREATE TABLE variant_studies (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    data_version  TEXT NOT NULL,
    runs_json     TEXT NOT NULL      -- [{"key": "neutral_10", "run_id": 12}, ...] in VARIANTS order
);
