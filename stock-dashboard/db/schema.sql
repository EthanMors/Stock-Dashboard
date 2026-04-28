CREATE TABLE IF NOT EXISTS thesis (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    date_created        TEXT NOT NULL,
    date_updated        TEXT NOT NULL,
    conviction_level    TEXT NOT NULL CHECK(conviction_level IN ('High', 'Medium', 'Low')),
    business_summary    TEXT,
    moat_description    TEXT,
    why_undervalued     TEXT,
    catalyst_short      TEXT,
    catalyst_medium     TEXT,
    bear_case           TEXT,
    bear_probability    REAL,
    bull_price          REAL,
    base_price          REAL,
    bear_price          REAL,
    time_horizon_months INTEGER,
    entry_price         REAL,
    status              TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active', 'Closed', 'Watching'))
);

CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL UNIQUE,
    date_added  TEXT NOT NULL,
    notes       TEXT,
    alert_price REAL
);

CREATE TABLE IF NOT EXISTS thesis_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    thesis_id     INTEGER NOT NULL REFERENCES thesis(id) ON DELETE CASCADE,
    snapshot_date TEXT NOT NULL,
    metric_json   TEXT NOT NULL
);
