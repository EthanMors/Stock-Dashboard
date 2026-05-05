CREATE TABLE IF NOT EXISTS wsb_posts (
    post_id         TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    title           TEXT,
    body            TEXT,
    author          TEXT,
    score           INTEGER DEFAULT 0,
    num_comments    INTEGER DEFAULT 0,
    created_utc     REAL,
    url             TEXT,
    permalink       TEXT,
    fetched_at      TEXT,
    sentiment_score REAL,
    sentiment_label TEXT,
    analyzed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_wsb_ticker ON wsb_posts (ticker);
