CREATE TABLE IF NOT EXISTS macro_news_analysis (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_category       TEXT NOT NULL,
    article_url         TEXT NOT NULL UNIQUE,
    title               TEXT,
    published_utc       REAL,
    source              TEXT,
    sentiment_score     REAL,
    sentiment_label     TEXT,
    summary             TEXT,
    impact_level        INTEGER,
    key_themes          TEXT,
    market_impact_type  TEXT,
    affected_sectors    TEXT,
    macro_category      TEXT,
    analyzed_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mna_feed_category ON macro_news_analysis (feed_category);
CREATE INDEX IF NOT EXISTS idx_mna_analyzed_at   ON macro_news_analysis (analyzed_at);
CREATE INDEX IF NOT EXISTS idx_mna_impact_type   ON macro_news_analysis (market_impact_type);
