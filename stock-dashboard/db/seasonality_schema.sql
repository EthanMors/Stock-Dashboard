-- seasonality.db — seasonality, macro regime, and sector rotation
--
-- Owned by data/seasonality_cache.py. Three concerns:
--   1. Cached Gemini regime/adaptation analyses (expensive Pro calls)
--   2. Point-in-time regime snapshots so regime shifts can be detected
--   3. Cached seasonality stat blobs (yfinance history is slow to refetch)

CREATE TABLE IF NOT EXISTS seasonality_analysis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_key   TEXT NOT NULL,   -- sorted, comma-joined tickers the analysis covered
    regime_label    TEXT,            -- e.g. "Risk-On / Easing"
    regime_summary  TEXT,
    confidence      TEXT,            -- "high"|"medium"|"low"
    month_ahead     TEXT,            -- name of the month the outlook covers
    seasonal_outlook TEXT,
    macro_outlook   TEXT,
    catalyst_watch  TEXT,            -- JSON array of {date, event, why_it_matters}
    position_actions TEXT,           -- JSON array of {ticker, action, rationale, urgency}
    sector_actions  TEXT,            -- JSON array of {sector, etf, action, rationale, conviction}
    risk_factors    TEXT,            -- JSON array of strings
    inputs_json     TEXT,            -- the computed payload handed to Gemini
    analyzed_at     TEXT NOT NULL    -- ISO UTC "YYYY-MM-DDTHH:MM:SS"
);

CREATE INDEX IF NOT EXISTS idx_seasonality_analysis_key
    ON seasonality_analysis (portfolio_key, analyzed_at DESC);

CREATE TABLE IF NOT EXISTS regime_snapshot (
    snapshot_date   TEXT PRIMARY KEY,  -- "YYYY-MM-DD", one per day
    regime_label    TEXT,
    risk_score      REAL,              -- [-100, 100] composite risk appetite
    rate_score      REAL,              -- [-100, 100] positive = falling rates / easing
    growth_score    REAL,              -- [-100, 100] positive = pro-growth
    inflation_score REAL,              -- [-100, 100] positive = inflationary
    indicators_json TEXT,              -- JSON dict of raw indicator readings
    recorded_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seasonality_stats_cache (
    cache_key       TEXT PRIMARY KEY,  -- "<TICKER>|<lookback_years>"
    stats_json      TEXT NOT NULL,
    cached_at       TEXT NOT NULL      -- ISO UTC; 24h TTL
);
