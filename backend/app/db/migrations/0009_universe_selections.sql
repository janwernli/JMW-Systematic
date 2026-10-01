-- 0009_universe_selections: monthly re-selection of the live Alpaca universe.
-- At each month-end signal the universe is re-chosen as the top ALPACA_MAX_SYMBOLS common stocks by trailing
-- 60-session dollar volume as of that date (held positions are kept until exited). Membership intervals go to
-- universe_membership (index_name 'alpaca_top_adv60'); this table logs each selection (one per month-end).

CREATE TABLE universe_selections (
    provider       TEXT NOT NULL,
    session        TEXT NOT NULL,               -- month-end signal session the selection is as of
    created_at     TEXT NOT NULL,
    size           INTEGER NOT NULL,            -- members after the selection (incl. kept held names)
    added_json     TEXT NOT NULL,
    removed_json   TEXT NOT NULL,
    kept_held_json TEXT NOT NULL,               -- held names outside the top N, kept until exited
    seeded         INTEGER NOT NULL DEFAULT 0,  -- 1 = first selection: existing universe seeded from history start
    PRIMARY KEY (provider, session)
);
