CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id       TEXT PRIMARY KEY,
    tickers      TEXT NOT NULL,
    date_from    TEXT NOT NULL,
    date_to      TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    signal_types TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    signal_date  TEXT NOT NULL,
    signal_type  TEXT NOT NULL,
    direction    TEXT NOT NULL,
    score        REAL NOT NULL,
    source_db    TEXT,
    raw_json     TEXT,
    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_bs_run_id
    ON backtest_signals (run_id);

CREATE INDEX IF NOT EXISTS idx_bs_ticker_date
    ON backtest_signals (ticker, signal_date);

CREATE INDEX IF NOT EXISTS idx_bs_signal_type
    ON backtest_signals (signal_type);

CREATE TABLE IF NOT EXISTS backtest_outcomes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id        INTEGER NOT NULL,
    forward_return   REAL,
    benchmark_return REAL,
    alpha            REAL,
    price_at_signal  REAL,
    price_at_horizon REAL,
    correct          INTEGER,
    FOREIGN KEY (signal_id) REFERENCES backtest_signals(id)
);

CREATE INDEX IF NOT EXISTS idx_bo_signal_id
    ON backtest_outcomes (signal_id);

CREATE TABLE IF NOT EXISTS backtest_metrics (
    run_id             TEXT NOT NULL,
    signal_type        TEXT NOT NULL,
    horizon_days       INTEGER NOT NULL,
    signal_count       INTEGER,
    hit_rate           REAL,
    avg_return_bull    REAL,
    avg_return_bear    REAL,
    avg_return_neutral REAL,
    ic                 REAL,
    ic_std             REAL,
    icir               REAL,
    t_stat             REAL,
    profit_factor      REAL,
    avg_alpha          REAL,
    sharpe             REAL,
    max_drawdown       REAL,
    PRIMARY KEY (run_id, signal_type, horizon_days)
);
