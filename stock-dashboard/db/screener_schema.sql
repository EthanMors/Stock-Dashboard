CREATE TABLE IF NOT EXISTS screener_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_key    TEXT NOT NULL,
    results_json  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sr_screen_key
    ON screener_results (screen_key, fetched_at);

CREATE TABLE IF NOT EXISTS screener_analysis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    risk_score      INTEGER,
    upside_thesis   TEXT,
    key_risks       TEXT,
    red_flags       TEXT,
    verdict         TEXT,
    raw_metrics     TEXT,
    analyzed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sa_ticker
    ON screener_analysis (ticker);

CREATE INDEX IF NOT EXISTS idx_sa_ticker_analyzed_at
    ON screener_analysis (ticker, analyzed_at);


CREATE TABLE IF NOT EXISTS screener_fundamentals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    fundamentals_json TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sf_ticker
    ON screener_fundamentals (ticker);

CREATE INDEX IF NOT EXISTS idx_sf_ticker_fetched
    ON screener_fundamentals (ticker, fetched_at);
