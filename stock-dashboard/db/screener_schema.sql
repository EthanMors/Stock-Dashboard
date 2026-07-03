CREATE TABLE IF NOT EXISTS screener_runs (
    run_id       TEXT PRIMARY KEY,
    run_at       TEXT NOT NULL,
    industry     TEXT NOT NULL,
    weights_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screener_runs_run_at ON screener_runs (run_at);

CREATE TABLE IF NOT EXISTS screener_picks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    industry       TEXT NOT NULL,
    boom_score     REAL,
    stage_reached  TEXT NOT NULL,
    boom_potential TEXT,
    conviction     TEXT,
    entry_price    REAL,
    alert_price    REAL,
    picked_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screener_picks_ticker    ON screener_picks (ticker);
CREATE INDEX IF NOT EXISTS idx_screener_picks_picked_at ON screener_picks (picked_at);
CREATE INDEX IF NOT EXISTS idx_screener_picks_run_id    ON screener_picks (run_id);
