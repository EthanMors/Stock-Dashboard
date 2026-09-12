CREATE TABLE IF NOT EXISTS screener_ta (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT    NOT NULL,
    ta_json     TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_screener_ta_ticker
    ON screener_ta (ticker);

CREATE INDEX IF NOT EXISTS idx_screener_ta_ticker_fetched
    ON screener_ta (ticker, fetched_at);
