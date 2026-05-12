CREATE TABLE IF NOT EXISTS portfolio_news_analysis (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                      TEXT NOT NULL,
    sentiment_score             REAL,
    sentiment_label             TEXT,
    summary                     TEXT,
    impact_level                INTEGER,
    key_themes                  TEXT,
    is_stock_specific           INTEGER,
    article_count               INTEGER,
    sector                      TEXT,
    is_fallback                 INTEGER,
    latest_article_url          TEXT,
    latest_article_published_utc REAL,
    analyzed_at                 TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pna_ticker ON portfolio_news_analysis (ticker);

CREATE INDEX IF NOT EXISTS idx_pna_ticker_analyzed_at
    ON portfolio_news_analysis (ticker, analyzed_at);

CREATE TABLE IF NOT EXISTS portfolio_news_seen_articles (
    ticker          TEXT NOT NULL,
    article_url     TEXT NOT NULL,
    published_utc   REAL,
    title           TEXT,
    PRIMARY KEY (ticker, article_url)
);

CREATE INDEX IF NOT EXISTS idx_pnsa_ticker
    ON portfolio_news_seen_articles (ticker);

CREATE TABLE IF NOT EXISTS portfolio_options_analysis (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                  TEXT NOT NULL,
    expiry                  TEXT NOT NULL,
    opt_type                TEXT NOT NULL,
    spot_price              REAL,
    directional_bias        TEXT,
    bias_strength           TEXT,
    confidence              TEXT,
    iv_analysis             TEXT,
    pcr_analysis            TEXT,
    max_pain_analysis       TEXT,
    gamma_exposure_analysis TEXT,
    key_levels              TEXT,
    unusual_activity        TEXT,
    risk_factors            TEXT,
    summary                 TEXT,
    metrics_json            TEXT,
    analyzed_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_poa_ticker
    ON portfolio_options_analysis (ticker);

CREATE INDEX IF NOT EXISTS idx_poa_lookup
    ON portfolio_options_analysis (ticker, expiry, opt_type, analyzed_at);
