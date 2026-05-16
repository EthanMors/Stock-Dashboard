# Implementor Skills — Stock Dashboard

Reference for the `spec-implementer` agent. Contains exact code patterns, file paths, schemas, and conventions to follow when executing a plan from a plan markdown file.

---

## Project Layout (absolute paths from `stock-dashboard/`)

```
stock-dashboard/
├── dashboard.py              # Home page — market snapshot
├── pages/N_*.py              # Multi-page Streamlit pages (N = 1–9)
├── data/                     # All data fetchers, analyzers, cache modules
├── analytics/                # Options math (chain.py, volatility.py, etc.)
├── components/               # Reusable Streamlit UI (charts, cards, forms)
├── db/                       # SQLite files + .sql schema files
│   ├── cache.db              # Metrics + hedge fund cache
│   ├── thesis.db             # Theses, watchlist, snapshots
│   ├── wsb.db                # Reddit posts + sentiment
│   └── portfolio.db          # News analysis + options analysis
└── .env                      # Environment variables (never commit)
```

**Run from `stock-dashboard/` with venv activated:**
```powershell
.\venv\Scripts\Activate.ps1
streamlit run dashboard.py
```

---

## New Page Template

Every new page follows this skeleton exactly:

```python
# pages/N_pagename.py

import streamlit as st
from components.gemini_usage_bar import render_gemini_usage_bar
# Add other imports here

st.set_page_config(page_title="Page Title", layout="wide")

render_gemini_usage_bar()   # always first after set_page_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _helper_function(...) -> ...:
    ...


# ---------------------------------------------------------------------------
# Cached data fetchers (wrap expensive calls ONLY)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def _cached_something(param: str):
    return expensive_call(param)


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    with st.sidebar:
        ...


def _render_main() -> None:
    ...


# ---------------------------------------------------------------------------
# Page entry
# ---------------------------------------------------------------------------

_render_sidebar()
_render_main()
```

Rules:
- Name file `N_pagename.py` where N is the next available integer in `pages/`.
- `st.set_page_config` must be the first Streamlit call.
- `render_gemini_usage_bar()` always goes immediately after `set_page_config`.
- Never compute metrics or ratios inline in pages — call `data/calculator.py` functions.
- Never open raw SQLite connections in pages — use helpers from `data/cache.py`, `data/portfolio_cache.py`, or `data/thesis_form.py`.
- Use `@st.cache_data(ttl=N)` only to wrap calls to external services or expensive DB reads. Don't double-cache (don't cache inside a module that already caches at the SQLite layer).

---

## Gemini CLI Invocation

### Flash (default — news, Reddit, fast analysis)

```python
import subprocess
import re
import json
from data.gemini_tracker import record_call

def _run_gemini_flash(prompt: str) -> tuple[str, str]:
    """Returns (stdout, stderr). Prompt via stdin."""
    try:
        result = subprocess.run(
            ["gemini.cmd", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=90,
        )
        output = result.stdout.strip()
        if output:
            record_call("flash")
        return output, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "Timed out after 90s"
    except Exception as exc:
        return "", str(exc)
```

### Pro (Gemini 2.5 Pro — options analysis, deep reasoning)

```python
def _run_gemini_pro(prompt: str) -> tuple[str, str]:
    """Returns (stdout, stderr). Prompt via stdin."""
    try:
        result = subprocess.run(
            ["gemini.cmd", "-m", "gemini-2.5-pro", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
        output = result.stdout.strip()
        if output:
            record_call("pro")
        return output, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "Timed out after 120s"
    except Exception as exc:
        return "", str(exc)
```

### Parsing JSON from Gemini output

```python
def _parse_gemini_json(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None
```

**Key rules:**
- NEVER use the Gemini Python SDK or Anthropic SDK — only `subprocess` + `gemini.cmd`.
- `-p ""` is the headless flag for non-interactive mode. Always include it.
- `-m gemini-2.5-pro` selects the Pro model. Omit `-m` for Flash (default).
- Always call `record_call("flash")` or `record_call("pro")` after a successful call (non-empty output).
- Daily limits: Flash = 1000/day, Pro = 50/day. Pro costs more; use Flash for anything that doesn't require deep reasoning.

---

## SQLite Database Patterns

### DB connection helper (use this pattern exactly)

```python
import sqlite3, os

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "mydb.db")

def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row   # enables dict-like access
    return conn
```

### Init DB from schema file

```python
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "mydb_schema.sql")

def init_db() -> None:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()

init_db()   # call at module import time
```

### Read row → dict (with JSON deserialization)

```python
def get_row(ticker: str) -> dict | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM my_table WHERE ticker = ? ORDER BY analyzed_at DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data = dict(row)
    # Deserialize JSON columns
    raw = data.get("key_themes") or "[]"
    try:
        data["key_themes"] = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data["key_themes"] = []
    # Normalize INTEGER booleans
    data["is_fallback"] = bool(data.get("is_fallback", 0))
    return data
```

### Write row

```python
def save_row(ticker: str, data: dict) -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO my_table (ticker, value, analyzed_at)
            VALUES (?, ?, ?)
            """,
            (ticker.upper(), data["value"], now_iso),
        )
        conn.commit()
    finally:
        conn.close()
```

---

## Database Schemas (exact columns)

### `portfolio.db` — News + Options Analysis

**`portfolio_news_analysis`**
```
id                          INTEGER PK AUTOINCREMENT
ticker                      TEXT NOT NULL
sentiment_score             REAL        -- [-1.0, 1.0]
sentiment_label             TEXT        -- "positive"|"negative"|"neutral"
summary                     TEXT
impact_level                INTEGER     -- [1, 10]
key_themes                  TEXT        -- JSON array of strings
is_stock_specific           INTEGER     -- 0 or 1 (boolean)
article_count               INTEGER
sector                      TEXT
is_fallback                 INTEGER     -- 0 or 1
latest_article_url          TEXT
latest_article_published_utc REAL       -- Unix timestamp (float)
analyzed_at                 TEXT NOT NULL  -- ISO UTC "YYYY-MM-DDTHH:MM:SS"
```

**`portfolio_news_seen_articles`**
```
ticker          TEXT NOT NULL
article_url     TEXT NOT NULL
published_utc   REAL
title           TEXT
PRIMARY KEY (ticker, article_url)
```

**`portfolio_options_analysis`**
```
id                      INTEGER PK AUTOINCREMENT
ticker                  TEXT NOT NULL
expiry                  TEXT NOT NULL   -- "YYYY-MM-DD"
opt_type                TEXT NOT NULL   -- "calls"|"puts"
spot_price              REAL
directional_bias        TEXT            -- "bullish"|"bearish"|"neutral"
bias_strength           TEXT            -- "strong"|"moderate"|"weak"
confidence              TEXT            -- "high"|"medium"|"low"
iv_analysis             TEXT
pcr_analysis            TEXT
max_pain_analysis       TEXT
gamma_exposure_analysis TEXT
key_levels              TEXT
unusual_activity        TEXT
risk_factors            TEXT
summary                 TEXT
metrics_json            TEXT            -- JSON with computed metrics
analyzed_at             TEXT NOT NULL   -- ISO UTC
```

### `cache.db` — Metrics + Hedge Funds

**`metric_cache`**
```
ticker      TEXT PK
metric_json TEXT    -- serialized metrics dict
timestamp   TEXT    -- ISO UTC
```

**`hedge_fund_cache`**
```
cache_key   TEXT PK    -- date string "YYYY-MM-DD"
data_json   TEXT
cached_date TEXT       -- expires when date changes
```

**`hedge_fund_filings`**
```
cik              TEXT PK
name             TEXT
accession_number TEXT
report_period    TEXT
filing_date      TEXT
total_holdings   INTEGER
total_value      REAL
holdings_json    TEXT    -- JSON list of HoldingRow dicts
fetched_at       TEXT    -- ISO UTC
```

### `wsb.db` — Reddit

**`wsb_posts`**
```
post_id         TEXT PK
ticker          TEXT
title           TEXT
body            TEXT
author          TEXT
score           INTEGER
num_comments    INTEGER
created_utc     REAL
url             TEXT
permalink       TEXT
fetched_at      TEXT
sentiment_score REAL
sentiment_label TEXT
analyzed_at     TEXT
```

**`wsb_ticker_summaries`**
```
ticker          TEXT PK
subreddits      TEXT
sentiment_score REAL
sentiment_label TEXT
summary         TEXT
hype_level      INTEGER
post_ids        TEXT    -- JSON array
analyzed_at     TEXT
```

### `thesis.db` — Investment Theses

**`thesis`**
```
id                 INTEGER PK AUTOINCREMENT
ticker             TEXT
date_created       TEXT
date_updated       TEXT
conviction_level   TEXT    -- "High"|"Medium"|"Low"
business_summary   TEXT
moat_description   TEXT
why_undervalued    TEXT
catalyst_short     TEXT
catalyst_medium    TEXT
bear_case          TEXT
bear_probability   REAL
bull_price         REAL
base_price         REAL
bear_price         REAL
time_horizon_months INTEGER
entry_price        REAL
status             TEXT    -- "Active"|"Closed"|"Watching"
```

---

## Data Module Function Signatures

### `data/fetcher.py`

```python
@st.cache_data(ttl=3600)
def get_stock_info(ticker: str) -> dict: ...
    # Returns yfinance .info dict

@st.cache_data(ttl=3600)
def get_price_history(ticker: str, period: str = "1y") -> pd.DataFrame: ...
    # Returns OHLCV DataFrame

@st.cache_data(ttl=3600)
def get_financials(ticker: str) -> dict: ...
    # Returns {"income_stmt": df, "balance_sheet": df, "cash_flow": df}

@st.cache_data(ttl=3600)
def get_earnings_history(ticker: str) -> pd.DataFrame: ...
```

### `data/calculator.py`

All functions take raw `info` dict from `get_stock_info()` and return `(float_value, status_string)`:

```python
def calc_pe_ratio(info: dict) -> tuple[float, str]: ...
def calc_ev_ebitda(info: dict) -> tuple[float, str]: ...
def calc_p_fcf(info: dict) -> tuple[float, str]: ...
def calc_peg_ratio(info: dict) -> tuple[float, str]: ...
def calc_gross_margin(info: dict) -> tuple[float, str]: ...
def calc_operating_margin(info: dict) -> tuple[float, str]: ...
def calc_net_margin(info: dict) -> tuple[float, str]: ...
def calc_roic(info: dict) -> tuple[float, str]: ...
def calc_revenue_yoy(info: dict) -> tuple[float, str]: ...
def calc_eps_yoy(info: dict) -> tuple[float, str]: ...
def calc_fcf_yoy(info: dict) -> tuple[float, str]: ...
def calc_net_debt_ebitda(info: dict) -> tuple[float, str]: ...
def calc_interest_coverage(info: dict) -> tuple[float, str]: ...
def calc_current_ratio(info: dict) -> tuple[float, str]: ...
def calc_short_interest(info: dict) -> tuple[float, str]: ...
def calc_insider_ownership(info: dict) -> tuple[float, str]: ...
```

### `data/portfolio_cache.py`

```python
def init_db() -> None: ...
def get_latest_analysis(ticker: str) -> dict | None: ...
def save_analysis(ticker, result_dict, article_count, sector, is_fallback, articles) -> None: ...
def has_new_articles(ticker: str, articles: list[dict]) -> bool: ...
def get_sentiment_history(ticker: str) -> list[dict]: ...
def save_options_analysis(ticker, expiry, opt_type, spot_price, result_dict) -> None: ...
def get_latest_options_analysis(ticker, expiry, opt_type) -> dict | None: ...
def is_options_analysis_fresh(analyzed_at_str: str) -> bool: ...
    # TTL = 4 hours
```

### `data/news_analyzer.py`

```python
def analyze_articles(articles: list[dict], ticker: str, sector: str, previous_analysis: dict | None) -> dict:
    # Returns: {sentiment_score, sentiment_label, summary, impact_level, key_themes, is_stock_specific}

def get_sector_info(ticker: str) -> str:
    # Returns sector string via yfinance info["sector"]
```

### `data/options_agent.py`

```python
def run_options_analysis(
    ticker: str,
    price: float,
    expiry: str,
    opt_type: str,
    calls_df: pd.DataFrame,
    puts_df: pd.DataFrame,
    calls_display_df: pd.DataFrame,
    puts_display_df: pd.DataFrame,
) -> dict | None:
    # Returns dict with all _REQUIRED_FIELDS plus "metrics" key
    # Returns {"_error": str, "metrics": dict} on failure
```

### `data/webull_positions.py`

```python
def is_configured() -> bool: ...
def get_env_account_ids() -> list[str]: ...
def get_account_list() -> list[dict] | dict: ...   # dict with "error" key on failure
def get_balance(account_id: str) -> dict: ...
def get_positions(account_id: str) -> list[dict]: ...
```

### `data/cache.py`

```python
def save_to_cache(ticker: str, data: dict) -> None: ...
def load_from_cache(ticker: str, max_age_hours: int = 1) -> dict | None: ...
def save_hedge_fund_cache(key: str, data: list) -> None: ...
def load_hedge_fund_cache(key: str) -> list | None: ...
def upsert_hedge_fund_filing(fund: dict, accession_number: str) -> None: ...
def get_all_hedge_fund_filings() -> list[dict]: ...
def get_stored_accession_numbers() -> dict: ...
def get_last_check_time() -> str | None: ...
def save_last_check_time(ts: str) -> None: ...
```

### `data/gemini_tracker.py`

```python
def record_call(model: str) -> None: ...   # model = "flash" or "pro"
def get_today_stats() -> dict: ...
    # Returns: {date, total, flash, pro, last_request}
```

---

## Components

### `components/gemini_usage_bar.py`

```python
from components.gemini_usage_bar import render_gemini_usage_bar
render_gemini_usage_bar()   # call at top of every page, after set_page_config
```

### `components/charts.py`

```python
from components.charts import plot_price_chart, plot_revenue_chart, plot_margin_chart, plot_fcf_chart
# All return Plotly figures; use st.plotly_chart(fig, use_container_width=True)
```

### `components/metric_cards.py`

```python
from components.metric_cards import render_metric_card
render_metric_card(label, value, status, help_text="")
```

---

## Caching Rules (Two-Layer Strategy)

| Scenario | Use |
|----------|-----|
| Expensive yfinance call inside a page | `@st.cache_data(ttl=3600)` |
| Cross-session persistence (news analysis) | SQLite via `portfolio_cache.py` |
| Cross-session persistence (hedge funds) | SQLite via `cache.py` |
| NEVER | Both layers for the same data path |
| NEVER | Raw `sqlite3.connect()` in a page file |
| NEVER | `@st.cache_data` inside a `data/` module that already writes to SQLite |

---

## Environment Variables

```env
MASSIVE_API_KEY=        # Massive.com news API
REDDIT_USERNAME=        # Used in Reddit User-Agent header
WEBULL_APP_KEY=         # Webull Official OpenAPI key
WEBULL_APP_SECRET=      # Webull Official OpenAPI secret
WEBULL_ACCOUNT_ID=      # Primary Webull account number
WEBULL_ACCOUNT_ID1=     # Optional second account
WEBULL_ACCOUNT_ID2=     # Optional third account
WEBULL_REGION_ID=us     # "us" or "hk"
```

Load with:
```python
from dotenv import load_dotenv
import os
load_dotenv()
value = os.getenv("MY_VAR", "default")
```

---

## Adding a New Schema File

When adding new tables to a new or existing database:

1. Create `db/myfeature_schema.sql` with `CREATE TABLE IF NOT EXISTS` statements and indexes.
2. Create `data/myfeature_cache.py` with `_get_connection()`, `init_db()` (reads the .sql file), and CRUD functions.
3. Call `init_db()` at module import time (bottom of the file).
4. Never alter existing schemas in-place; add new tables to an existing db if the data is logically related.

---

## Common Gotchas

- **Ticker casing:** Always call `.upper()` on tickers before saving to the DB.
- **Timestamps:** Always use `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")` for ISO UTC strings.
- **JSON booleans in SQLite:** Store as `INTEGER` (0/1), cast to `bool` on read.
- **JSON lists:** Store as `json.dumps(list)`, deserialize with `json.loads()` wrapped in try/except.
- **DB path:** Always use `os.path.join(os.path.dirname(__file__), "..", "db", "file.db")` — never hardcode paths relative to cwd, because Streamlit runs from `stock-dashboard/` but imports resolve relative to the module.
- **Gemini empty output:** Always check `if output:` before recording a call and before parsing JSON. Return a sensible error dict rather than raising.
- **scipy / numpy in pages:** Already imported in `6_option_chains.py` and `9_portfolio.py`; add to imports if needed in new pages.
