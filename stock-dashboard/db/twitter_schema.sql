CREATE TABLE IF NOT EXISTS twitter_tweets (
    tweet_id        TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    text            TEXT,
    author_username TEXT,
    author_name     TEXT,
    reply_count     INTEGER DEFAULT 0,
    retweet_count   INTEGER DEFAULT 0,
    like_count      INTEGER DEFAULT 0,
    quote_count     INTEGER DEFAULT 0,
    created_at      TEXT,
    url             TEXT,
    fetched_at      TEXT,
    sentiment_score REAL,
    sentiment_label TEXT,
    analyzed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_twitter_tweets_ticker ON twitter_tweets (ticker);

CREATE TABLE IF NOT EXISTS twitter_ticker_summaries (
    ticker          TEXT PRIMARY KEY,
    sentiment_score REAL,
    sentiment_label TEXT,
    summary         TEXT,
    hype_level      INTEGER DEFAULT 0,
    tweet_ids       TEXT,
    analyzed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_twitter_summaries_ticker ON twitter_ticker_summaries (ticker);
