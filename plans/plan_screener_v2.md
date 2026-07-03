# Plan: AI Stock Screener v2 — Grounded Data, Smarter Quant, Bigger Universe, Picks Tracker

## 1. Task Summary

The AI Stock Screener (`pages/12_screener.py` + `data/screener_agent.py`) currently runs a
3-stage funnel (Python quant → Gemini Flash catalyst ranking → Gemini Pro deep-dive → portfolio
fit) over small, mostly-megacap industry pools, using only ~10 raw `yfinance` `.info` fields and
a 600-character business summary as "data." The Gemini stages therefore hallucinate catalysts
from model memory instead of reading real current data, and the quant score ignores rich data
already sitting in the dashboard's own SQLite caches (13F filings, WSB sentiment, macro news).

This upgrade makes five changes, all additive to the existing 3-stage UX:

1. **Ground the AI stages in real data** — Stage 2 (Flash) gets real recent news headlines per
   candidate; Stage 3 (Pro) gets WSB sentiment, 13F ownership, news, and macro context.
2. **New Stage-1 factors from existing dashboard data** — a "smart money" (13F) subscore, a
   redesigned "hype" subscore blending WSB sentiment with volume trend, and an options
   put/call-ratio readout for the top 5 only.
3. **Better yfinance usage in the quant score** — earnings-date catalyst timing, QoQ revenue
   growth acceleration, institutional-ownership signal, and SPY-relative momentum.
4. **Expanded universe** — industry pools grow from 8 to ~20-22 tickers each, biased toward
   mid/small-cap growth, plus a new dynamic "Reddit Trending" pool sourced from `wsb.db`.
5. **Picks tracker / feedback loop** — every Flash/Pro finalist is persisted to a new
   `db/screener.db`, shown in a new "📈 Picks Tracker" tab with live price-since-pick tracking,
   and registered as a new `screener` signal type in the AI Signal Backtester
   (`pages/11_backtest.py`).

**Expected outcome when fully executed:** the screener page still has the same 3-stage
click-through flow plus a Portfolio Fit tab (unchanged UX), but the Stage-1 leaderboard now
scores a larger, more growth-biased universe with 5 weighted factors (a new 5th "Smart Money"
slider appears in the sidebar), the Flash and Pro prompts are grounded in real news/WSB/13F/macro
data with graceful degradation when any data source is empty, there is a new 5th tab
"📈 Picks Tracker" showing historical hit-rate, and screener picks can be selected as a signal
type inside the existing AI Signal Backtester page.

## 2. Files Involved

**Create:**
- `stock-dashboard/db/screener_schema.sql` — DDL for the new picks-tracker DB.
- `stock-dashboard/data/screener_cache.py` — SQLite CRUD for `db/screener.db`.

**Modify:**
- `stock-dashboard/data/fetcher.py` — add `get_next_earnings_date()` and
  `get_quarterly_income_stmt()`.
- `stock-dashboard/data/screener_agent.py` — expand `INDUSTRY_POOLS`, add
  `DEFAULT_WEIGHTS["smart_money"]`, add Reddit-trending pool fetch, concurrency, all new
  subscore logic, news/WSB/13F/macro grounding in prompts, options-positioning enrichment.
- `stock-dashboard/data/backtest_signals.py` — add `get_screener_signals()` and a `"screener"`
  branch in `score_signal()`.
- `stock-dashboard/data/backtest_engine.py` — import and dispatch `get_screener_signals()`.
- `stock-dashboard/pages/11_backtest.py` — register `"screener"` in `_ALL_SIGNAL_TYPES`,
  `_SIGNAL_LABELS`, `_EQUITY_COLORS`.
- `stock-dashboard/pages/12_screener.py` — 5th sidebar slider, Reddit-trending pool option,
  positioning column in the leaderboard, save picks on Flash/Pro completion, new
  "📈 Picks Tracker" tab.

**Do not modify:** `data/agy_client.py`, `data/gemini_tracker.py` (per task constraints).

## 3. Prerequisites & Dependencies

No new pip packages are required — `concurrent.futures` is stdlib, and `analytics.chain` /
`analytics.positioning` / `data.news_fetcher` / `data.hedge_fund_fetcher` /
`data.macro_news_cache` already exist and are already dependencies of the running app.

No new environment variables are required. `MASSIVE_API_KEY` (news) is already optional and
`fetch_news()` already returns `[]` gracefully when unset — the new prompt-grounding code must
treat that empty list as "no news available," never raise.

No manual DB migration is needed — `data/screener_cache.py` calls `init_db()` at import time
(same pattern as `data/macro_news_cache.py`), which creates `db/screener.db` and its tables the
first time the module is imported.

## 4. Database Changes

### New database: `db/screener.db`

### Step 1: Create `stock-dashboard/db/screener_schema.sql`

Create this new file with exactly this content:

```sql
CREATE TABLE IF NOT EXISTS screener_runs (
    run_id       TEXT PRIMARY KEY,
    run_at       TEXT NOT NULL,
    industry     TEXT NOT NULL,
    weights_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screener_runs_run_at ON screener_runs (run_at);

CREATE TABLE IF NOT EXISTS screener_picks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    industry       TEXT NOT NULL,
    boom_score     REAL,
    stage_reached  TEXT NOT NULL,
    boom_potential TEXT,
    conviction     TEXT,
    entry_price    REAL,
    alert_price    REAL,
    picked_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screener_picks_ticker    ON screener_picks (ticker);
CREATE INDEX IF NOT EXISTS idx_screener_picks_picked_at ON screener_picks (picked_at);
CREATE INDEX IF NOT EXISTS idx_screener_picks_run_id    ON screener_picks (run_id);
```

Notes on the schema (do not deviate):
- `stage_reached` is `'flash'` or `'pro'`. A ticker may have two rows across a run's lifetime:
  one written when it becomes a Flash finalist, and a second written when the Pro deep-dive
  verdict comes back for it. This is intentional (append-only history), not an upsert.
- `boom_potential` / `conviction` / `alert_price` are `NULL` for `stage_reached='flash'` rows
  (Flash doesn't produce those fields) and populated for `stage_reached='pro'` rows.
- No `FOREIGN KEY` constraint is used (SQLite FKs are off by default in this codebase's other
  schema files — see `db/macro_news_schema.sql` for the same convention).

## 5. Step-by-Step Implementation

---

### Step 2: Create `stock-dashboard/data/screener_cache.py`

Create this new file with exactly this content:

```python
"""SQLite persistence for the AI Stock Screener's picks tracker.

Owns db/screener.db. Every screener run is recorded in screener_runs, and every
Flash/Pro finalist is recorded as a row in screener_picks (append-only — a ticker
gets one row when it becomes a Flash finalist and a second row when its Pro
verdict comes back). See data/screener_agent.py for how these are populated and
pages/12_screener.py "Picks Tracker" tab for how they are displayed.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener_schema.sql")


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create screener.db tables from screener_schema.sql if they don't exist."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


init_db()


def save_run(industry: str, weights: dict) -> str:
    """Insert a new screener_runs row and return the generated run_id (UUID4 string)."""
    run_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO screener_runs (run_id, run_at, industry, weights_json) VALUES (?, ?, ?, ?)",
            (run_id, now_iso, industry, json.dumps(weights)),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


def save_pick(
    run_id: str,
    ticker: str,
    industry: str,
    boom_score: float | None,
    stage_reached: str,
    boom_potential: str | None,
    conviction: str | None,
    entry_price: float | None,
    alert_price: float | None,
) -> None:
    """Insert one finalist pick row tied to run_id. Always inserts (append-only)."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO screener_picks
                (run_id, ticker, industry, boom_score, stage_reached,
                 boom_potential, conviction, entry_price, alert_price, picked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                ticker.upper(),
                industry,
                boom_score,
                stage_reached,
                boom_potential,
                conviction,
                entry_price,
                alert_price,
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_picks(limit: int = 200) -> list[dict]:
    """Return all recorded picks, most recent first, as a list of dicts."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM screener_picks ORDER BY picked_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
```

**Why:** This is a genuinely new data domain (a persistent pick-history log), so per
`planner-skills.md` it gets its own DB and its own `data/` module rather than being bolted onto
`cache.py` or `portfolio_cache.py`. The `_get_connection` / `init_db()` / schema-file pattern is
copied exactly from `data/macro_news_cache.py`.

---

### Step 3: Add two new fetcher functions to `stock-dashboard/data/fetcher.py`

**File**: `data/fetcher.py`
**Location**: Append at the end of the file, after the existing `get_batch_history` function
(currently the last function in the file).

Add these two new functions:

```python
@st.cache_data(ttl=3600)
def get_next_earnings_date(ticker: str) -> str | None:
    """Return the next upcoming earnings date as 'YYYY-MM-DD', or None if unknown/past.

    Uses yfinance's get_earnings_dates(), which returns both past and future dates;
    this filters to the earliest date that is still in the future (or today).
    """
    try:
        stock = yf.Ticker(ticker)
        df = stock.get_earnings_dates(limit=8)
        if df is None or df.empty:
            return None
        now = pd.Timestamp.now(tz=df.index.tz) if df.index.tz is not None else pd.Timestamp.now()
        future = df[df.index >= now]
        if future.empty:
            return None
        return future.index.min().strftime("%Y-%m-%d")
    except Exception:
        return None


@st.cache_data(ttl=3600)
def get_quarterly_income_stmt(ticker: str) -> dict:
    """Fetch the quarterly income statement as a nested dict {period_str: {line_item: value}}.

    Mirrors get_financials() but uses yfinance's quarterly_income_stmt (columns = quarter-end
    dates, most recent first) instead of the annual income_stmt.
    """
    try:
        stock = yf.Ticker(ticker)
        df = stock.quarterly_income_stmt
        if df is None or df.empty:
            return {}
        df = df.copy()
        df.columns = [str(c) for c in df.columns]
        return df.to_dict()
    except Exception:
        return {}
```

**Why:** These are two new yfinance endpoints (earnings calendar, quarterly income statement)
that don't fit any existing `fetcher.py` function. Per `planner-skills.md` decision framework,
"adding another function to an existing external service" belongs in the existing module, not a
new one. Both wrapped in `@st.cache_data(ttl=3600)` consistent with every other function in this
file (in-session memoization of an external yfinance call — no SQLite involved, so no
double-caching violation).

---

### Step 4: Rewrite `stock-dashboard/data/screener_agent.py`

This step replaces large portions of the file. Sub-steps below are given in file order. Read
each sub-step's "Location" carefully — some are full-function replacements, some are insertions.

#### Step 4a — Imports

**Location**: Top of file, replace the existing import block (currently lines 32-39):

```python
import json
import re

import pandas as pd

from data.agy_client import PRO_MODEL, run_agy
from data.gemini_tracker import record_call
from data.fetcher import get_stock_info, get_batch_history
```

Replace with:

```python
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import pandas as pd

from data.agy_client import PRO_MODEL, run_agy
from data.gemini_tracker import record_call
from data.fetcher import (
    get_stock_info,
    get_batch_history,
    get_next_earnings_date,
    get_quarterly_income_stmt,
)
```

**Why:** `ThreadPoolExecutor`/`as_completed` power the new Stage-1 concurrency; `sqlite3`/`os`
power the new direct `wsb.db` lookups; `date`/`datetime`/`timezone` power earnings-date math and
news-date formatting; the two new fetcher functions power the earnings-timing and
revenue-acceleration factors.

#### Step 4b — Industry pools (expanded universe)

**Location**: Replace the entire `INDUSTRY_POOLS` dict (currently lines 45-52).

```python
INDUSTRY_POOLS: dict[str, list[str]] = {
    "Semiconductors & AI Hardware": [
        "NVDA", "AMD", "AVGO", "MU", "TSM", "ASML", "AMAT", "LRCX", "KLAC", "MRVL",
        "ARM", "ON", "SWKS", "QRVO", "MPWR", "ENTG", "TER", "COHR", "LSCC", "AEHR",
        "CRDO", "WOLF",
    ],
    "Cloud & AI Software": [
        "MSFT", "AMZN", "GOOGL", "PLTR", "DDOG", "NET", "ZS", "CRM", "SNOW", "MDB",
        "CRWD", "PATH", "GTLB", "HUBS", "BILL", "ESTC", "CFLT", "DOCN", "AI", "SOUN",
        "IOT", "NOW",
    ],
    "Biotech & Digital Health": [
        "TEM", "RXRX", "UTHR", "BNTX", "LEGN", "VRTX", "AMGN", "MRNA", "EXAS", "NTLA",
        "CRSP", "BEAM", "VKTX", "ALNY", "SRPT", "ARWR", "IONS", "INSM", "NBIX", "DXCM",
        "HIMS", "GH",
    ],
    "Fintech & Digital Finance": [
        "SQ", "PYPL", "SOFI", "HOOD", "NU", "AFRM", "V", "MA", "COIN", "UPST",
        "MARA", "RIOT", "TOST", "FOUR", "GPN", "FIS", "MQ", "DAVE", "FLYW", "WEX",
        "LC", "PSFE",
    ],
    "Nuclear & Energy Transition": [
        "OKLO", "SMR", "CEG", "VST", "GEV", "NXT", "FSLR", "ENPH", "SEDG", "RUN",
        "PLUG", "BE", "BLDP", "LEU", "UEC", "CCJ", "DNN", "NNE", "BWXT", "TLN",
        "SHLS", "STEM",
    ],
    "Space & Frontier Tech": [
        "RKLB", "LUNR", "ASTS", "ACHR", "JOBY", "KTOS", "AVAV", "PL", "RDW", "SPCE",
        "RCAT", "EVLV", "ONDS", "SATS", "IRDM", "VSAT", "BKSY", "SIDU", "MNTS",
    ],
}

# Special sentinel industry label for the dynamic Reddit-trending pool (see
# get_reddit_trending_pool below). Not a key in INDUSTRY_POOLS.
REDDIT_TRENDING_LABEL = "🔥 Reddit Trending (Dynamic)"
```

**Why:** Grows each pool from 8 to ~20-22 names, biased toward mid/small-cap growth per the
task's "next big boom" goal instead of the prior half-megacap lists. `REDDIT_TRENDING_LABEL` is a
sentinel string the page uses to know when to call `get_reddit_trending_pool()` instead of
indexing `INDUSTRY_POOLS`.

#### Step 4c — Default weights (5th factor)

**Location**: Replace the entire `DEFAULT_WEIGHTS` dict (currently lines 54-60).

```python
# Default Stage-1 factor weights (must sum to ~1.0; the page lets the user tune).
DEFAULT_WEIGHTS = {
    "growth":      0.25,   # revenue/earnings growth & acceleration
    "momentum":    0.25,   # SPY-relative price momentum + 52w-high proximity + earnings timing
    "analyst":     0.15,   # analyst target upside + institutional ownership
    "hype":        0.15,   # WSB sentiment + short-squeeze + volume-trend potential
    "smart_money": 0.20,   # 13F fund coverage & aggregate conviction weight
}
```

**Why:** Adds the new `smart_money` factor and renormalizes so all five weights still sum to 1.0.

#### Step 4d — Reddit-trending pool fetch (new function)

**Location**: Insert immediately after the `DEFAULT_WEIGHTS` block, before the
"Small numeric helpers" section comment.

```python
# ---------------------------------------------------------------------------
# Dynamic Reddit-trending pool
# ---------------------------------------------------------------------------

_WSB_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "wsb.db")


def get_reddit_trending_pool(limit: int = 20) -> list[str]:
    """Return the top `limit` tickers by total WSB mentions over the last 7 days.

    Reads daily_ticker_mentions from wsb.db (populated by pages/8_social.py). Returns
    an empty list if the DB/table doesn't exist yet or has no rows in the window —
    callers MUST handle the empty-list case with a UI notice, never assume non-empty.
    """
    if not os.path.exists(_WSB_DB_PATH):
        return []
    conn = sqlite3.connect(_WSB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT ticker, SUM(mentions) AS total_mentions
            FROM daily_ticker_mentions
            WHERE date >= date('now', '-7 day')
            GROUP BY ticker
            ORDER BY total_mentions DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [r["ticker"].upper() for r in rows]
```

**Why:** Implements the "dynamic Reddit Trending pool" requirement by reading the
already-populated `daily_ticker_mentions` table (see `pages/8_social.py`) rather than making live
Reddit API calls. Wrapped in a direct `sqlite3.connect` (not `@st.cache_data`, and not routed
through `cache.py` since this is a different DB) consistent with the `data/backtest_signals.py`
`_wsb_conn()` pattern. Falls back to `[]` on any failure so the page can show a graceful notice.

#### Step 4e — Small numeric helpers (add one new helper)

**Location**: Immediately after the existing `_scale` function (currently lines 75-82), before
the "Stage 1 — Python Boom Score" section comment.

```python
def _catalyst_timing_pts(days_to_earnings: int | None) -> float:
    """Score 0-100: closer upcoming earnings (within ~45 days) scores higher.

    None (unknown earnings date) is treated as neutral (50). A negative value
    (stale/past date data) is also treated as neutral. Beyond 45 days out scores 0.
    """
    if days_to_earnings is None or days_to_earnings < 0:
        return 50.0
    if days_to_earnings > 45:
        return 0.0
    return round(100.0 - (days_to_earnings / 45.0) * 100.0, 1)
```

**Why:** Implements "Closer earnings (within ~45 days) scores higher; unknown = neutral" exactly
as specified, isolated as a small pure function like its neighbor `_scale`.

#### Step 4f — Momentum returns: make SPY-relative

**Location**: Replace the entire `_momentum_returns` function (currently lines 89-114).

```python
def _momentum_returns(tickers: list[str]) -> dict[str, dict]:
    """Return {ticker: {"ret_3m", "ret_6m", "ret_3m_rel", "ret_6m_rel"}} from one batch fetch.

    ret_3m/ret_6m are absolute % returns. ret_3m_rel/ret_6m_rel are the ticker's return
    minus SPY's return over the same window (relative strength). SPY is fetched once via
    the same get_batch_history call by appending it to the requested ticker list — it is
    NOT counted as one of the scored tickers. Any *_rel value is None if SPY data or the
    ticker's own data is unavailable (caller must fall back to absolute returns).
    """
    universe = list(dict.fromkeys(tickers + ["SPY"]))
    out: dict[str, dict] = {
        t: {"ret_3m": None, "ret_6m": None, "ret_3m_rel": None, "ret_6m_rel": None}
        for t in tickers
    }
    try:
        hist = get_batch_history(tuple(universe), period="6mo")
    except Exception:
        hist = pd.DataFrame()
    if hist is None or hist.empty:
        return out

    def _returns(col: str):
        if col not in hist.columns:
            return None, None
        series = hist[col].dropna()
        if len(series) < 2:
            return None, None
        last = series.iloc[-1]
        first = series.iloc[0]
        r6 = (last / first - 1.0) * 100.0 if first else None
        r3 = None
        # ~63 trading days ≈ 3 months
        if len(series) > 63:
            ref = series.iloc[-63]
            r3 = (last / ref - 1.0) * 100.0 if ref else None
        return r3, r6

    spy_r3, spy_r6 = _returns("SPY")

    for t in tickers:
        r3, r6 = _returns(t)
        out[t]["ret_3m"] = r3
        out[t]["ret_6m"] = r6
        out[t]["ret_3m_rel"] = (r3 - spy_r3) if (r3 is not None and spy_r3 is not None) else None
        out[t]["ret_6m_rel"] = (r6 - spy_r6) if (r6 is not None and spy_r6 is not None) else None
    return out
```

**Why:** Implements "Relative strength: compute 3m return minus SPY 3m return... use relative
rather than absolute returns in the momentum subscore," fetching SPY exactly once per screen via
the existing `get_batch_history` batch call as instructed.

#### Step 4g — Smart-money map (new function)

**Location**: Insert immediately after `_momentum_returns` (the function just replaced above).

```python
def _smart_money_map() -> dict[str, dict]:
    """Aggregate 13F fund coverage per ticker from the local hedge-fund cache DB.

    Reads ONLY from the local SQLite cache via get_all_funds_from_db() — this makes
    zero network/EDGAR calls, so it is safe to call once per Stage-1 run over the
    whole universe. Returns {ticker: {"fund_count": int, "total_weight_pct": float,
    "funds": [fund_name, ...]}}. Empty dict if the cache is empty or unavailable.
    """
    try:
        from data.hedge_fund_fetcher import get_all_funds_from_db
        funds = get_all_funds_from_db()
    except Exception:
        funds = []

    out: dict[str, dict] = {}
    for f in funds:
        for h in f.get("holdings", []):
            t = str(h.get("ticker", "")).upper().strip()
            if not t:
                continue
            entry = out.setdefault(t, {"fund_count": 0, "total_weight_pct": 0.0, "funds": []})
            entry["fund_count"] += 1
            entry["total_weight_pct"] += float(h.get("pct_of_portfolio", 0.0) or 0.0)
            entry["funds"].append(f.get("name", ""))
    return out
```

**Why:** Implements the "smart money" subscore data source exactly as specified — "read from
hedge fund cache DB, no network calls in Stage 1" — by calling `get_all_funds_from_db()` (a pure
DB read) rather than `get_concentrated_funds()` (which triggers an EDGAR network poll).

#### Step 4h — WSB map (new function)

**Location**: Insert immediately after `_smart_money_map`.

```python
def _wsb_map(tickers: list[str]) -> dict[str, dict]:
    """Batch-lookup wsb_ticker_summaries rows for the given tickers (one query, one connection).

    Returns {ticker: {"sentiment_score", "sentiment_label", "hype_level", "analyzed_at"}}.
    Missing tickers are simply absent from the returned dict. Empty dict if wsb.db/table
    doesn't exist yet (module is fresh-installed / Reddit page never used).
    """
    out: dict[str, dict] = {}
    if not tickers or not os.path.exists(_WSB_DB_PATH):
        return out
    conn = sqlite3.connect(_WSB_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in tickers)
        rows = conn.execute(
            f"""
            SELECT ticker, sentiment_score, sentiment_label, hype_level, analyzed_at
            FROM wsb_ticker_summaries
            WHERE ticker IN ({placeholders})
            """,
            [t.upper() for t in tickers],
        ).fetchall()
        for r in rows:
            out[r["ticker"].upper()] = dict(r)
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return out
```

**Why:** Single batched query (not N queries) for the WSB hype blend, following the
`data/backtest_signals.py get_reddit_signals` connection pattern.

#### Step 4i — Revenue growth acceleration (new function)

**Location**: Insert immediately after `_wsb_map`.

```python
def _revenue_growth_acceleration(quarterly: dict) -> float | None:
    """QoQ revenue growth acceleration = latest QoQ growth% minus prior QoQ growth%.

    `quarterly` is the {period_str: {line_item: value}} dict returned by
    fetcher.get_quarterly_income_stmt() (columns are quarter-end date strings, most
    recent first once sorted descending). Returns None if fewer than 3 quarters of
    'Total Revenue' data are available, or if any of the 3 values are zero/None.
    """
    if not quarterly:
        return None
    revenue_by_period: dict[str, float] = {}
    for period, line_items in quarterly.items():
        if not isinstance(line_items, dict):
            continue
        val = line_items.get("Total Revenue")
        if val is not None:
            try:
                revenue_by_period[period] = float(val)
            except (TypeError, ValueError):
                continue
    if len(revenue_by_period) < 3:
        return None
    ordered_periods = sorted(revenue_by_period.keys(), reverse=True)  # most recent first
    latest, prior, prior2 = (revenue_by_period[p] for p in ordered_periods[:3])
    if not prior or not prior2:
        return None
    growth_latest = (latest / prior - 1.0) * 100.0
    growth_prior = (prior / prior2 - 1.0) * 100.0
    return growth_latest - growth_prior
```

**Why:** Implements "Growth acceleration: use get_financials quarterly income statement to
compute QoQ revenue growth acceleration" exactly as specified, degrading to `None` (which the
scorer must treat as "fall back to the original growth-score weighting") when fewer than 3
quarters are available.

#### Step 4j — `_score_ticker`: full rewrite

**Location**: Replace the entire `_score_ticker` function (currently lines 117-190).

```python
def _score_ticker(
    info: dict,
    mom: dict,
    weights: dict,
    smart_money: dict | None = None,
    wsb: dict | None = None,
    earnings_date: str | None = None,
    quarterly: dict | None = None,
) -> dict:
    """Compute the multi-factor boom score and the raw factor readouts for one ticker."""
    price = _safe(info.get("currentPrice")) or _safe(info.get("regularMarketPrice"))
    hi_52 = _safe(info.get("fiftyTwoWeekHigh"))
    rev_growth = _safe(info.get("revenueGrowth"))            # fraction, e.g. 0.42
    earn_growth = (_safe(info.get("earningsGrowth"))
                   or _safe(info.get("earningsQuarterlyGrowth")))
    gross_m = _safe(info.get("grossMargins"))
    target = _safe(info.get("targetMeanPrice"))
    short_float = _safe(info.get("shortPercentOfFloat"))     # fraction
    avg_vol = _safe(info.get("averageVolume"))
    inst_pct = _safe(info.get("heldPercentInstitutions"))
    net_purchase = _safe(info.get("netSharePurchaseActivity"))
    vol_10d = _safe(info.get("averageVolume10days")) or _safe(info.get("averageDailyVolume10Day"))

    ret_3m = mom.get("ret_3m")
    ret_6m = mom.get("ret_6m")
    ret_3m_rel = mom.get("ret_3m_rel")
    ret_6m_rel = mom.get("ret_6m_rel")

    # --- Days to next earnings -------------------------------------------
    days_to_earnings = None
    if earnings_date:
        try:
            days_to_earnings = (datetime.strptime(earnings_date, "%Y-%m-%d").date() - date.today()).days
        except ValueError:
            days_to_earnings = None

    # --- Revenue growth acceleration ---------------------------------------
    revenue_accel = _revenue_growth_acceleration(quarterly or {})

    # --- Growth & quality sub-score (0-100) -------------------------------
    rev_pts = _scale((rev_growth or 0) * 100, 5, 60)          # 5%→0, 60%+→100
    earn_pts = _scale((earn_growth or 0) * 100, 0, 80)
    margin_pts = _scale((gross_m or 0) * 100, 30, 80)
    accel_pts = _scale(revenue_accel, -10, 20) if revenue_accel is not None else None
    if accel_pts is not None:
        growth_score = 0.35 * rev_pts + 0.25 * earn_pts + 0.15 * margin_pts + 0.25 * accel_pts
    else:
        growth_score = 0.45 * rev_pts + 0.35 * earn_pts + 0.20 * margin_pts

    # --- Momentum sub-score (0-100) — SPY-relative, with catalyst timing --
    near_high = (price / hi_52 * 100.0) if (price and hi_52) else None
    near_high_pts = _scale(near_high, 60, 100)                # within 0-40% of high
    r3_pts = _scale(ret_3m_rel, -15, 30) if ret_3m_rel is not None else _scale(ret_3m, -10, 50)
    r6_pts = _scale(ret_6m_rel, -20, 50) if ret_6m_rel is not None else _scale(ret_6m, -10, 80)
    catalyst_pts = _catalyst_timing_pts(days_to_earnings)
    momentum_score = 0.30 * near_high_pts + 0.25 * r3_pts + 0.25 * r6_pts + 0.20 * catalyst_pts

    # --- Analyst / institutional sub-score (0-100) -------------------------
    upside = ((target / price - 1.0) * 100.0) if (target and price) else None
    upside_pts = _scale(upside, 0, 50)
    inst_pts = _scale((inst_pct or 0) * 100, 20, 80)
    if net_purchase is not None:
        if net_purchase > 0:
            inst_pts = min(100.0, inst_pts + 10.0)
        elif net_purchase < 0:
            inst_pts = max(0.0, inst_pts - 10.0)
    if upside is not None:
        analyst_score = 0.65 * upside_pts + 0.35 * inst_pts
    else:
        analyst_score = inst_pts

    # --- Hype sub-score (0-100) — WSB blend + short float + volume trend --
    short_pts = _scale((short_float or 0) * 100, 3, 25)       # 3%→0, 25%+→100
    vol_trend = (vol_10d / avg_vol) if (vol_10d and avg_vol) else None
    vol_trend_pts = _scale(vol_trend, 1.0, 3.0)                # 1x→0, 3x+→100
    wsb_pts = None
    if wsb and wsb.get("hype_level") is not None:
        hype_lvl = _safe(wsb.get("hype_level")) or 0.0
        sentiment = _safe(wsb.get("sentiment_score")) or 0.0
        wsb_pts = _scale(hype_lvl, 0, 10)
        if sentiment < 0:
            wsb_pts *= 0.5
    if wsb_pts is not None:
        hype_score = 0.45 * wsb_pts + 0.30 * short_pts + 0.25 * vol_trend_pts
    else:
        hype_score = 0.55 * short_pts + 0.45 * vol_trend_pts

    # --- Smart-money sub-score (0-100) --------------------------------------
    sm = smart_money or {}
    fund_count = sm.get("fund_count", 0)
    total_weight = sm.get("total_weight_pct", 0.0)
    fund_count_pts = _scale(fund_count, 0, 8)
    weight_pts = _scale(total_weight, 0, 20)
    smart_money_score = 0.6 * fund_count_pts + 0.4 * weight_pts

    boom_score = (
        weights.get("growth", 0)      * growth_score
        + weights.get("momentum", 0)    * momentum_score
        + weights.get("analyst", 0)     * analyst_score
        + weights.get("hype", 0)        * hype_score
        + weights.get("smart_money", 0) * smart_money_score
    )

    return {
        "ticker": str(info.get("symbol", "")).upper(),
        "name": info.get("shortName") or info.get("longName") or "",
        "sector": info.get("sector", ""),
        "industry": info.get("industry", ""),
        "price": price,
        "market_cap": _safe(info.get("marketCap")),
        "boom_score": round(boom_score, 1),
        "subscores": {
            "growth": round(growth_score, 1),
            "momentum": round(momentum_score, 1),
            "analyst": round(analyst_score, 1),
            "hype": round(hype_score, 1),
            "smart_money": round(smart_money_score, 1),
        },
        "factors": {
            "rev_growth_pct": round(rev_growth * 100, 1) if rev_growth is not None else None,
            "earn_growth_pct": round(earn_growth * 100, 1) if earn_growth is not None else None,
            "gross_margin_pct": round(gross_m * 100, 1) if gross_m is not None else None,
            "revenue_accel_pct": round(revenue_accel, 1) if revenue_accel is not None else None,
            "ret_3m_pct": round(ret_3m, 1) if ret_3m is not None else None,
            "ret_6m_pct": round(ret_6m, 1) if ret_6m is not None else None,
            "ret_3m_rel_pct": round(ret_3m_rel, 1) if ret_3m_rel is not None else None,
            "ret_6m_rel_pct": round(ret_6m_rel, 1) if ret_6m_rel is not None else None,
            "pct_of_52w_high": round(near_high, 1) if near_high is not None else None,
            "days_to_earnings": days_to_earnings,
            "analyst_upside_pct": round(upside, 1) if upside is not None else None,
            "institutional_pct": round(inst_pct * 100, 1) if inst_pct is not None else None,
            "short_pct_float": round(short_float * 100, 1) if short_float is not None else None,
            "vol_trend_x": round(vol_trend, 2) if vol_trend is not None else None,
            "smart_money_fund_count": fund_count,
            "smart_money_weight_pct": round(total_weight, 2),
            "recommendation": info.get("recommendationKey", ""),
            "business_summary": (info.get("longBusinessSummary", "") or "")[:600],
        },
    }
```

**Why:** Folds every new Stage-1 factor (catalyst timing, revenue acceleration,
institutional/insider signal, WSB-blended hype, smart money, SPY-relative momentum) into the
existing four subscore buckets plus one new bucket, all degrading gracefully to the prior
(pre-upgrade) formula when the new data is unavailable.

#### Step 4k — `compute_quant_scores`: concurrency + new inputs + positioning enrichment

**Location**: Replace the entire `compute_quant_scores` function (currently lines 193-208).

```python
def _fetch_ticker_bundle(ticker: str) -> dict:
    """Fetch all per-ticker Stage-1 yfinance inputs. Safe to run inside a worker thread —
    each call goes through an @st.cache_data-wrapped fetcher function."""
    info = get_stock_info(ticker) or {}
    info.setdefault("symbol", ticker)
    earnings_date = get_next_earnings_date(ticker)
    quarterly = get_quarterly_income_stmt(ticker)
    return {"info": info, "earnings_date": earnings_date, "quarterly": quarterly}


def enrich_top_candidates_with_positioning(top_rows: list[dict]) -> list[dict]:
    """Attach a put/call-ratio positioning readout to the Stage-1 top candidates ONLY.

    Options chains are expensive to fetch, so this must NEVER be called on the full
    universe — only on the already-ranked top 5 (or fewer). Mutates each row in place
    by setting row["positioning"] to a dict (or None on any failure) and returns the
    same list for convenience.
    """
    import yfinance as yf
    from analytics.chain import load_raw_chain, clean_chain
    from analytics.positioning import put_call_ratio

    for row in top_rows:
        row["positioning"] = None
        ticker = row.get("ticker", "")
        if not ticker:
            continue
        try:
            t = yf.Ticker(ticker)
            exps = list(t.options)[:2]  # nearest 2 expirations only, keep it cheap
            if not exps:
                continue
            raw = load_raw_chain(ticker, exps)
            if raw is None or raw.empty:
                continue
            chain = clean_chain(raw)
            if chain.empty:
                continue
            pcr = put_call_ratio(chain, method="volume")
            ratio = pcr.get("pcr")
            row["positioning"] = {
                "put_call_ratio": round(ratio, 2) if isinstance(ratio, (int, float)) and ratio not in (float("inf"),) else None,
                "call_volume": int(pcr.get("call_total", 0) or 0),
                "put_volume": int(pcr.get("put_total", 0) or 0),
            }
        except Exception:
            continue
    return top_rows


def compute_quant_scores(tickers: list[str], weights: dict | None = None) -> list[dict]:
    """Stage 1: score and rank a list of tickers by boom potential (descending).

    Fetches all per-ticker yfinance data concurrently (ThreadPoolExecutor, max_workers=8)
    to keep the expanded ~20-25 ticker universe fast. Smart-money and WSB data are fetched
    once for the whole batch (DB reads, cheap). After ranking, attaches an options
    put/call-ratio readout to the top 5 only (see enrich_top_candidates_with_positioning).
    """
    weights = weights or DEFAULT_WEIGHTS
    tickers = [t.upper().strip() for t in tickers if t.strip()]
    if not tickers:
        return []

    mom = _momentum_returns(tickers)
    smart_money = _smart_money_map()
    wsb_rows = _wsb_map(tickers)

    bundles: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_ticker_bundle, t): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                bundles[t] = fut.result()
            except Exception:
                bundles[t] = {"info": {}, "earnings_date": None, "quarterly": {}}

    scored: list[dict] = []
    for t in tickers:
        bundle = bundles.get(t, {})
        info = bundle.get("info") or {}
        if not info:
            continue
        row = _score_ticker(
            info=info,
            mom=mom.get(t, {}),
            weights=weights,
            smart_money=smart_money.get(t, {}),
            wsb=wsb_rows.get(t),
            earnings_date=bundle.get("earnings_date"),
            quarterly=bundle.get("quarterly") or {},
        )
        scored.append(row)

    scored.sort(key=lambda r: r["boom_score"], reverse=True)
    enrich_top_candidates_with_positioning(scored[:5])
    return scored
```

**Why:** Implements the chosen concurrency strategy exactly ("ThreadPoolExecutor, max_workers=8"
as specified in the task) so the expanded ~20-25 ticker universe scores in roughly
`ceil(N/8)` sequential-call-equivalents instead of `N`. Smart-money and WSB lookups are batched
once outside the per-ticker loop (they're local DB reads, not per-ticker network calls).
Positioning enrichment runs on `scored[:5]` — the list slice shares the same dict objects as the
full `scored` list, so mutating `row["positioning"]` on the slice also updates the full list; this
satisfies "computed only for the top 5 after ranking... NOT part of the Stage-1 rank of the whole
universe" since `row["positioning"]` is attached for display/prompt use only and never enters
`boom_score`.

#### Step 4l — News-block helper (new function)

**Location**: Insert immediately after `_parse_json` (currently ends at line 248), before the
"Stage 2 — Flash catalyst ranking" section comment.

```python
def _fmt_pub_date(raw) -> str:
    """Format a Massive.com published_utc value (float unix ts or ISO string) as YYYY-MM-DD."""
    if not raw:
        return "?"
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).strftime("%Y-%m-%d")
        return str(raw)[:10]
    except Exception:
        return "?"


def _news_block(ticker: str, limit: int = 6) -> str:
    """Return a bounded, prompt-ready block of recent headlines for `ticker`.

    Degrades gracefully to an explicit "no data" line when MASSIVE_API_KEY is unset,
    the API call fails, or no articles are found — never raises.
    """
    try:
        from data.news_fetcher import fetch_news
        articles = fetch_news(ticker, limit=limit) or []
    except Exception:
        articles = []
    if not articles:
        return "  (no recent news available for this ticker)"
    lines = []
    for a in articles[:limit]:
        title = (a.get("title") or "").strip()[:140]
        if not title:
            continue
        pub = _fmt_pub_date(a.get("published_utc"))
        publisher = a.get("publisher")
        pub_name = publisher.get("name", "") if isinstance(publisher, dict) else ""
        lines.append(f"  - [{pub}] {title} ({pub_name})")
    return "\n".join(lines) if lines else "  (no recent news available for this ticker)"
```

**Why:** Implements "Stage 2 (Flash) prompt: include a per-candidate block of recent news
headlines... Degrade gracefully if news API unconfigured/fails (empty block + note in prompt)"
using the exact `title`/`published_utc`/`publisher.name` fields confirmed in
`data/news_fetcher.py fetch_news()` output (verified against usage in `pages/4_news.py`).

#### Step 4m — Ownership + macro helpers (new functions)

**Location**: Insert immediately after `_news_block`.

```python
def _ownership_summary(ticker: str, max_funds: int = 5) -> list[dict]:
    """Return up to `max_funds` tracked 13F funds holding `ticker`, sorted by weight desc.

    Each item: {"fund": str, "pct_of_fund_portfolio": float}. Empty list if no tracked
    fund holds the ticker or the hedge-fund cache is empty/unavailable.
    """
    try:
        from data.hedge_fund_fetcher import get_all_funds_from_db
        funds = get_all_funds_from_db()
    except Exception:
        return []
    ticker_upper = ticker.upper()
    matches = []
    for f in funds:
        for h in f.get("holdings", []):
            if str(h.get("ticker", "")).upper() == ticker_upper:
                matches.append({
                    "fund": f.get("name", ""),
                    "pct_of_fund_portfolio": h.get("pct_of_portfolio", 0.0),
                })
                break
    matches.sort(key=lambda m: m["pct_of_fund_portfolio"], reverse=True)
    return matches[:max_funds]


def _macro_context_block(max_items: int = 3) -> str:
    """Return a bounded block of the most recent macro news analyses (last 72h)."""
    try:
        from data.macro_news_cache import get_recent_analyses
        rows = get_recent_analyses(hours=72, limit=max_items)
    except Exception:
        rows = []
    if not rows:
        return "  (no recent macro data available)"
    lines = []
    for r in rows[:max_items]:
        cat = r.get("macro_category", "neutral")
        summ = (r.get("summary") or "")[:180]
        lines.append(f"  - [{cat}] {summ}")
    return "\n".join(lines) if lines else "  (no recent macro data available)"
```

**Why:** Implements "(b) 13F ownership summary — which tracked funds hold the ticker and
approximate weight" and "(d) a short macro context block from the latest macro_news_analysis
rows (or a graceful 'no macro data' note)" using the existing `get_all_funds_from_db()` (DB-only,
no network) and `data.macro_news_cache.get_recent_analyses()` (already exists, confirmed
signature `get_recent_analyses(category=None, impact_type=None, hours=24, limit=50)`).

#### Step 4n — `_candidate_brief`: add new factors to the display block

**Location**: Replace the entire `_candidate_brief` function (currently lines 255-268).

```python
def _candidate_brief(row: dict) -> str:
    f = row["factors"]
    s = row["subscores"]
    pos = row.get("positioning")
    pos_line = ""
    if pos:
        pos_line = (
            f"\n  Put/Call(vol)={pos.get('put_call_ratio')} "
            f"(call_vol={pos.get('call_volume')}, put_vol={pos.get('put_volume')})"
        )
    return (
        f"[{row['ticker']}] {row['name']} — {row['sector']} / {row['industry']}\n"
        f"  BoomScore {row['boom_score']} (growth {s['growth']}, momentum {s['momentum']}, "
        f"analyst {s['analyst']}, hype {s['hype']}, smart_money {s['smart_money']})\n"
        f"  RevGrowth={f['rev_growth_pct']}% EarnGrowth={f['earn_growth_pct']}% "
        f"GrossMargin={f['gross_margin_pct']}% RevAccel={f['revenue_accel_pct']}pp\n"
        f"  Ret3m(rel SPY)={f['ret_3m_rel_pct']}% Ret6m(rel SPY)={f['ret_6m_rel_pct']}% "
        f"%of52wHigh={f['pct_of_52w_high']}% DaysToEarnings={f['days_to_earnings']} "
        f"AnalystUpside={f['analyst_upside_pct']}% (rating: {f['recommendation']})\n"
        f"  ShortFloat={f['short_pct_float']}% VolTrend={f['vol_trend_x']}x "
        f"SmartMoney={f['smart_money_fund_count']} funds ({f['smart_money_weight_pct']}% agg weight) "
        f"InstOwnership={f['institutional_pct']}%"
        f"{pos_line}\n"
        f"  Business: {f['business_summary']}"
    )
```

**Why:** `_candidate_brief` is shared by Stage 2, Stage 3, and the Portfolio-Fit prompt — updating
it once surfaces all the new quant factors (revenue acceleration, relative returns, earnings
timing, smart money, institutional ownership, positioning) everywhere it's used.

#### Step 4o — Stage 2 prompt: add news grounding

**Location**: Insert immediately after `_candidate_brief` (the function just replaced above), and
before the `_FLASH_PROMPT` string constant.

```python
def _candidate_brief_with_news(row: dict) -> str:
    """Stage-2-only variant of _candidate_brief: appends a bounded recent-news block."""
    base = _candidate_brief(row)
    news = _news_block(row["ticker"], limit=6)
    return f"{base}\n  RECENT NEWS:\n{news}"
```

**Location**: In `rank_catalysts_flash`, replace this line (currently line 305):

```python
    block = "\n\n".join(_candidate_brief(r) for r in top_candidates[:5])
```

with:

```python
    block = "\n\n".join(_candidate_brief_with_news(r) for r in top_candidates[:5])
```

**Why:** Implements "Stage 2 (Flash) prompt: include a per-candidate block of recent news
headlines... for the top-5 candidates," keeping `_candidate_brief` itself news-free so Stage 3
and the Fit agent (which have their own, separately-grounded briefs) don't pay for a duplicate
news fetch.

#### Step 4p — Stage 3 prompt: add WSB + 13F + news + macro grounding

**Location**: Insert immediately after `_candidate_brief_with_news` (added in the previous
sub-step), before the `_PRO_PROMPT` string constant.

```python
def _finalist_brief_deep(row: dict) -> str:
    """Stage-3-only variant of _candidate_brief: appends WSB sentiment, 13F ownership,
    and recent news for one finalist. Macro context is appended once at the prompt
    level (it's market-wide, not per-ticker) — see deep_dive_pro below."""
    base = _candidate_brief(row)
    ticker = row["ticker"]

    wsb = _wsb_map([ticker]).get(ticker.upper())
    if wsb:
        wsb_line = (
            f"  WSB Sentiment: {wsb.get('sentiment_label', 'neutral')} "
            f"(score {wsb.get('sentiment_score')}), hype {wsb.get('hype_level')}/10 "
            f"(as of {str(wsb.get('analyzed_at', '?'))[:10]})"
        )
    else:
        wsb_line = "  WSB Sentiment: no data on file"

    owners = _ownership_summary(ticker)
    if owners:
        own_line = "  13F Ownership: " + "; ".join(
            f"{o['fund']} ({o['pct_of_fund_portfolio']}% of fund)" for o in owners
        )
    else:
        own_line = "  13F Ownership: no tracked fund currently holds this ticker"

    news = _news_block(ticker, limit=6)

    return f"{base}\n{wsb_line}\n{own_line}\n  RECENT NEWS:\n{news}"
```

**Location**: Replace the `_PRO_PROMPT` string constant (currently lines 318-345):

```python
_PRO_PROMPT = """You are a senior analyst delivering a final boom/risk verdict on 2 finalist stocks \
in the {industry} sector. Use the quantitative profiles below.

FINALISTS:
{finalists_block}

Respond ONLY with valid JSON:
{{
  "verdicts": [
    {{
      "ticker": "<symbol>",
      "boom_potential": "<explosive|high|moderate|limited>",
      "conviction": "<high|medium|low>",
      "thesis": "<2-3 sentence boom thesis grounded in the data>",
      "key_catalysts": ["<catalyst>", "<...>"],
      "key_risks": ["<risk>", "<...>"],
      "suggested_entry": "<price or condition to start a position>",
      "alert_price": <number or null>
    }}
  ],
  "head_to_head": "<1-2 sentences: which finalist is the better boom bet right now and why>"
}}

Rules:
- verdicts: one per finalist.
- Be specific — cite tickers and the numbers provided.
- Do NOT wrap JSON in markdown code fences.
"""
```

with:

```python
_PRO_PROMPT = """You are a senior analyst delivering a final boom/risk verdict on 2 finalist stocks \
in the {industry} sector. Use the quantitative profiles, WSB sentiment, 13F ownership, recent \
news, and macro context below.

FINALISTS:
{finalists_block}

=== MACRO CONTEXT (last 72h) ===
{macro_block}
=== END MACRO CONTEXT ===

Respond ONLY with valid JSON:
{{
  "verdicts": [
    {{
      "ticker": "<symbol>",
      "boom_potential": "<explosive|high|moderate|limited>",
      "conviction": "<high|medium|low>",
      "thesis": "<2-3 sentence boom thesis grounded in the data>",
      "key_catalysts": ["<catalyst>", "<...>"],
      "key_risks": ["<risk>", "<...>"],
      "suggested_entry": "<price or condition to start a position>",
      "alert_price": <number or null>
    }}
  ],
  "head_to_head": "<1-2 sentences: which finalist is the better boom bet right now and why>"
}}

Rules:
- verdicts: one per finalist.
- Be specific — cite tickers and the numbers provided. If WSB/13F/macro data is marked
  unavailable, do not invent it — say the data wasn't available and reason from what is.
- Do NOT wrap JSON in markdown code fences.
"""
```

**Location**: Replace the entire `deep_dive_pro` function (currently lines 348-358):

```python
def deep_dive_pro(industry: str, finalists: list[dict]) -> dict:
    """Stage 3: Pro produces the final boom/risk verdict for the 2 finalists."""
    if not finalists:
        return {"_error": "No finalists to analyze."}
    block = "\n\n".join(_candidate_brief(r) for r in finalists[:2])
    prompt = _PRO_PROMPT.format(industry=industry, finalists_block=block)
    raw, stderr = _run_pro(prompt)
    parsed = _parse_json(raw)
    if not parsed:
        return {"_error": f"Pro deep-dive failed. {stderr[:200]}\n{raw[:400]}"}
    return parsed
```

with:

```python
def deep_dive_pro(industry: str, finalists: list[dict]) -> dict:
    """Stage 3: Pro produces the final boom/risk verdict for the 2 finalists.

    Grounds the prompt in real data: WSB sentiment + 13F ownership + recent news per
    finalist (via _finalist_brief_deep), plus one shared macro-context block. Every
    data source degrades gracefully to an explicit "no data" note — never raises.
    """
    if not finalists:
        return {"_error": "No finalists to analyze."}
    block = "\n\n".join(_finalist_brief_deep(r) for r in finalists[:2])
    macro_block = _macro_context_block()
    prompt = _PRO_PROMPT.format(industry=industry, finalists_block=block, macro_block=macro_block)
    raw, stderr = _run_pro(prompt)
    parsed = _parse_json(raw)
    if not parsed:
        return {"_error": f"Pro deep-dive failed. {stderr[:200]}\n{raw[:400]}"}
    return parsed
```

**Why:** Implements "Stage 3 (Pro) prompt: include for each finalist (a) WSB sentiment row...
(b) 13F ownership summary... (c) recent headlines, (d) a short macro context block... graceful
'no macro data' note" exactly as specified, with all four data sources individually
exception-safe.

**Do not modify** `analyze_portfolio_fit`, `_portfolio_correlation`, `_holdings_profile`, or
`_FIT_PROMPT` — the task does not request changes to the Portfolio Fit agent.

---

### Step 5: Add `get_screener_signals()` to `stock-dashboard/data/backtest_signals.py`

#### Step 5a — DB path + connection helper

**Location**: Immediately after the existing `_WSB_DB` constant (currently line 43):

```python
_PORTFOLIO_DB = os.path.join(os.path.dirname(__file__), "..", "db", "portfolio.db")
_WSB_DB       = os.path.join(os.path.dirname(__file__), "..", "db", "wsb.db")
```

add:

```python
_SCREENER_DB  = os.path.join(os.path.dirname(__file__), "..", "db", "screener.db")
```

**Location**: Immediately after the existing `_wsb_conn` function (currently lines 56-59):

```python
def _wsb_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_WSB_DB)
    conn.row_factory = sqlite3.Row
    return conn
```

add:

```python
def _screener_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_SCREENER_DB)
    conn.row_factory = sqlite3.Row
    return conn
```

#### Step 5b — `score_signal`: add the `"screener"` branch

**Location**: Inside `score_signal`, immediately after the `elif signal_type == "technical":` block
and its `return round(d * confidence, 4)` line (currently lines 176-182), and BEFORE the final
`return 0.0` (currently line 184):

```python
    elif signal_type == "screener":
        stage = raw_output.get("stage_reached", "flash")
        potential = raw_output.get("boom_potential")
        conviction = raw_output.get("conviction")

        if stage == "pro" and potential:
            potential_map = {"explosive": 1.0, "high": 0.8, "moderate": 0.5, "limited": 0.2}
            conviction_map = {"high": 1.0, "medium": 0.7, "low": 0.4}
            p = potential_map.get(potential, 0.5)
            c = conviction_map.get(conviction, 0.7)
            return round(p * c, 4)
        # Flash-only finalist (no Pro verdict yet): weak bullish signal — it made the
        # top-2 catalyst cut, but hasn't been risk-audited.
        return 0.3
```

Also update the function's docstring `signal_type` parameter description (currently: "One of
options_ai|news|reddit|hedge_fund|macro|mpt|technical") to read
"One of options_ai|news|reddit|hedge_fund|macro|mpt|technical|screener".

#### Step 5c — `get_screener_signals` (new function)

**Location**: Append at the end of the file, after `get_technical_signals` (the last function in
the file).

```python
def get_screener_signals(
    tickers: list[str],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """Extract AI screener pick signals from screener_picks in screener.db.

    Every row in screener_picks (Flash finalist or Pro-verdict finalist) becomes one
    signal. Flash-only rows (stage_reached='flash') get a fixed weak-bullish score via
    score_signal(); Pro rows (stage_reached='pro') are scored from boom_potential +
    conviction. See score_signal()'s "screener" branch for the exact mapping.

    Parameters
    ----------
    tickers   : List of uppercase ticker strings.
    date_from : Start date (inclusive).
    date_to   : End date (inclusive).

    Returns
    -------
    List of signal dicts with keys: ticker, signal_date, signal_type, direction,
    score, source_db, raw_json.
    """
    tickers_upper = [t.upper() for t in tickers]
    if not tickers_upper:
        return []
    placeholders = ",".join("?" for _ in tickers_upper)

    conn = _screener_conn()
    try:
        rows = conn.execute(
            f"""
            SELECT ticker, boom_score, stage_reached, boom_potential, conviction, picked_at
            FROM screener_picks
            WHERE ticker IN ({placeholders})
            ORDER BY picked_at ASC
            """,
            tickers_upper,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    signals = []
    for row in rows:
        row_dict = dict(row)
        d = _parse_date(row_dict.get("picked_at", ""))
        if not _date_in_range(d, date_from, date_to):
            continue

        raw = {
            "stage_reached": row_dict.get("stage_reached", "flash"),
            "boom_potential": row_dict.get("boom_potential"),
            "conviction": row_dict.get("conviction"),
        }
        sc = score_signal("screener", raw)
        signals.append({
            "ticker":      row_dict["ticker"].upper(),
            "signal_date": d,
            "signal_type": "screener",
            "direction":   _direction_from_score(sc),
            "score":       sc,
            "source_db":   "screener.db/screener_picks",
            "raw_json":    json.dumps(raw),
        })

    return signals
```

Also update the module docstring's function list (currently lines 16-24) to add
`get_screener_signals(tickers, date_from, date_to) -> list[dict]` after
`get_technical_signals(...)`, and update the `signal_type` description in the module docstring
(line 12) to include `|screener`.

**Why:** Mirrors the exact structure of every other `get_*_signals` function in this file
(`_parse_date`, `_date_in_range`, `score_signal`, `_direction_from_score`), so the new "screener"
signal type is indistinguishable in shape from the pre-existing ones as far as
`backtest_engine.py` and `pages/11_backtest.py` are concerned.

---

### Step 6: Register `screener` in `stock-dashboard/data/backtest_engine.py`

**Location**: In the import block from `data.backtest_signals` (currently lines 39-47):

```python
from data.backtest_signals import (
    get_hedge_fund_signals,
    get_macro_signals,
    get_mpt_signals,
    get_news_signals,
    get_options_ai_signals,
    get_reddit_signals,
    get_technical_signals,
)
```

replace with:

```python
from data.backtest_signals import (
    get_hedge_fund_signals,
    get_macro_signals,
    get_mpt_signals,
    get_news_signals,
    get_options_ai_signals,
    get_reddit_signals,
    get_screener_signals,
    get_technical_signals,
)
```

**Location**: Inside `run_backtest`, immediately after the `if "technical" in signal_types:`
block (currently lines 746-747):

```python
    if "technical" in signal_types:
        all_signals.extend(get_technical_signals(tickers, date_from, date_to))
```

add immediately after it:

```python

    if "screener" in signal_types:
        all_signals.extend(get_screener_signals(tickers, date_from, date_to))
```

Also update the `run_backtest` docstring's `signal_types` description (currently: "list[str] —
subset of ['options_ai','news','reddit', 'hedge_fund','macro','mpt','technical']") to add
`,'screener'` to the list.

**Why:** Wires the new signal source into the existing dispatch pattern with zero changes to any
other part of the orchestrator.

---

### Step 7: Register `screener` in `stock-dashboard/pages/11_backtest.py`

**Location**: Replace the `_ALL_SIGNAL_TYPES` list (currently lines 33-41):

```python
_ALL_SIGNAL_TYPES = [
    "options_ai",
    "news",
    "reddit",
    "hedge_fund",
    "macro",
    "mpt",
    "technical",
]
```

with:

```python
_ALL_SIGNAL_TYPES = [
    "options_ai",
    "news",
    "reddit",
    "hedge_fund",
    "macro",
    "mpt",
    "technical",
    "screener",
]
```

**Location**: In `_SIGNAL_LABELS` (currently lines 43-52), add one new entry immediately before
the closing `}`:

```python
_SIGNAL_LABELS = {
    "options_ai":  "Options AI",
    "news":        "News Sentiment",
    "reddit":      "Reddit WSB",
    "hedge_fund":  "Hedge Fund",
    "macro":       "Macro News",
    "mpt":         "MPT Analysis",
    "technical":   "Technical Patterns",
    "screener":    "AI Screener Picks",
    "SPY":         "SPY Benchmark",
}
```

**Location**: In `_EQUITY_COLORS` (currently lines 61-70), add one new entry immediately before
the closing `}`:

```python
_EQUITY_COLORS = {
    "options_ai":  "#2196F3",
    "news":        "#4CAF50",
    "reddit":      "#FF9800",
    "hedge_fund":  "#9C27B0",
    "macro":       "#F44336",
    "mpt":         "#00BCD4",
    "technical":   "#8BC34A",
    "screener":    "#E91E63",
    "SPY":         "#607D8B",
}
```

**Why:** These three constants are the entire registration surface for a new signal type in this
page — `run_backtest` (Step 6) already dispatches on the checkbox-selected values from
`_ALL_SIGNAL_TYPES`, and the Overview/Equity/Heatmap/Breakdown/Export tabs all read labels/colors
from these two dicts generically.

---

### Step 8: Update `stock-dashboard/pages/12_screener.py`

#### Step 8a — Imports

**Location**: Replace the entire import block (currently lines 9-22):

```python
import pandas as pd
import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header, render_sidebar_nav, section_header
from data import screener_agent
from data.screener_agent import (
    INDUSTRY_POOLS,
    DEFAULT_WEIGHTS,
    compute_quant_scores,
    rank_catalysts_flash,
    deep_dive_pro,
    analyze_portfolio_fit,
)
```

with:

```python
import pandas as pd
import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header, render_sidebar_nav, section_header
from data import screener_agent, screener_cache
from data.fetcher import get_batch_history
from data.screener_agent import (
    INDUSTRY_POOLS,
    REDDIT_TRENDING_LABEL,
    DEFAULT_WEIGHTS,
    compute_quant_scores,
    get_reddit_trending_pool,
    rank_catalysts_flash,
    deep_dive_pro,
    analyze_portfolio_fit,
)
```

**Why:** Adds the new `screener_cache` module (picks persistence), `get_batch_history` (for the
Picks Tracker's live price lookup), and the two new `screener_agent` exports.

#### Step 8b — Session state: add run_id tracking

**Location**: Replace the `_DEFAULTS` dict (currently lines 33-39):

```python
_DEFAULTS = {
    "scr_scores": None,       # Stage 1 ranked list
    "scr_industry": None,     # industry label that produced scr_scores
    "scr_flash": None,        # Stage 2 result
    "scr_pro": None,          # Stage 3 result
    "scr_fit": {},            # {ticker: portfolio-fit result}
}
```

with:

```python
_DEFAULTS = {
    "scr_scores": None,       # Stage 1 ranked list
    "scr_industry": None,     # industry label that produced scr_scores
    "scr_flash": None,        # Stage 2 result
    "scr_pro": None,          # Stage 3 result
    "scr_fit": {},            # {ticker: portfolio-fit result}
    "scr_run_id": None,       # screener_cache.save_run() id for the current scr_scores run
    "scr_flash_saved": False, # whether Stage-2 finalists were already saved to screener.db
}
```

#### Step 8c — Weight normalization: 5 factors

**Location**: Replace the `_normalize_weights` function (currently lines 74-78):

```python
def _normalize_weights(g: int, m: int, a: int, h: int) -> dict:
    total = g + m + a + h
    if total == 0:
        return DEFAULT_WEIGHTS
    return {"growth": g / total, "momentum": m / total, "analyst": a / total, "hype": h / total}
```

with:

```python
def _normalize_weights(g: int, m: int, a: int, h: int, sm: int) -> dict:
    total = g + m + a + h + sm
    if total == 0:
        return DEFAULT_WEIGHTS
    return {
        "growth": g / total,
        "momentum": m / total,
        "analyst": a / total,
        "hype": h / total,
        "smart_money": sm / total,
    }
```

#### Step 8d — Sidebar: industry list + 5th slider

**Location**: Replace the entire `_render_sidebar` function (currently lines 94-121):

```python
def _render_sidebar() -> dict:
    with st.sidebar:
        st.markdown("### 🔍 Screener Controls")
        industry_options = list(INDUSTRY_POOLS.keys()) + [REDDIT_TRENDING_LABEL]
        industry = st.radio("Industry pool", industry_options, key="scr_pool")

        custom_raw = st.text_input(
            "Add custom tickers (comma-separated)", key="scr_custom",
            help="Appended to the selected industry pool.",
        )

        st.markdown("#### Boom-score weights")
        st.caption("How much each factor drives the Stage-1 rank.")
        w_growth = st.slider("Growth & quality", 0, 100, 25, key="w_growth")
        w_mom = st.slider("Momentum", 0, 100, 25, key="w_mom")
        w_analyst = st.slider("Analyst upside", 0, 100, 15, key="w_analyst")
        w_hype = st.slider("Squeeze / hype", 0, 100, 15, key="w_hype")
        w_smart = st.slider("Smart money (13F)", 0, 100, 20, key="w_smart")

        run = st.button("🚀 Run Screen", use_container_width=True, type="primary")

        st.markdown("---")
        st.caption(
            "Stage 1 = free Python quant. Stage 2 uses 1 Gemini **Flash** call, "
            "Stage 3 + each Portfolio-Fit use 1 Gemini **Pro** call (50/day)."
        )

    custom = [t.strip().upper() for t in custom_raw.split(",") if t.strip()]
    weights = _normalize_weights(w_growth, w_mom, w_analyst, w_hype, w_smart)
    return {"industry": industry, "custom": custom, "weights": weights, "run": run}
```

**Why:** Adds the Reddit-trending sentinel to the radio options and the required 5th "Smart
money" slider, both additive to the existing sidebar layout.

#### Step 8e — Leaderboard: add Put/Call column

**Location**: Replace the entire `_render_leaderboard` function (currently lines 128-157):

```python
def _render_leaderboard(scores: list[dict]) -> None:
    section_header("Stage 1 · Quantitative Boom Leaderboard")
    st.caption(
        "Pure-Python multi-factor rank. The top 5 (highlighted) advance to the Gemini stages "
        "and get an options put/call-ratio readout."
    )

    rows = []
    for i, r in enumerate(scores):
        f = r["factors"]
        pos = r.get("positioning") or {}
        rows.append({
            "Rank": i + 1,
            "Ticker": r["ticker"],
            "Boom": r["boom_score"],
            "Growth": r["subscores"]["growth"],
            "Momentum": r["subscores"]["momentum"],
            "Analyst": r["subscores"]["analyst"],
            "Hype": r["subscores"]["hype"],
            "SmartMoney": r["subscores"]["smart_money"],
            "RevGr%": f["rev_growth_pct"],
            "Ret6m(rel)%": f["ret_6m_rel_pct"],
            "%52wHi": f["pct_of_52w_high"],
            "Upside%": f["analyst_upside_pct"],
            "Short%": f["short_pct_float"],
            "Put/Call": pos.get("put_call_ratio"),
        })
    df = pd.DataFrame(rows)

    def _hl_top5(row):
        return ["background-color: #1d3b2a" if row["Rank"] <= 5 else "" for _ in row]

    styled = df.style.apply(_hl_top5, axis=1).format(precision=1, na_rep="—")
    st.dataframe(styled, use_container_width=True, hide_index=True)

    st.markdown("**Top 5 advancing:** " + ", ".join(r["ticker"] for r in scores[:5]))
```

**Why:** Surfaces the new Smart Money subscore and the top-5-only options positioning readout in
the existing leaderboard table (non-top-5 rows show `—` for `Put/Call` since positioning is only
computed for the top 5, which the `na_rep="—"` formatting already handles for `None` values).

#### Step 8f — Catalyst tab: save Flash finalists as picks

**Location**: Replace the entire `_render_catalyst` function (currently lines 164-199). Only the
button-click block changes; the rendering loop at the bottom is unchanged — reproduce the whole
function exactly as follows:

```python
def _render_catalyst(industry: str, scores: list[dict]) -> None:
    section_header("Stage 2 · Catalyst & Sentiment Ranking (Gemini Flash)")
    if st.button("🤖 Rank catalysts with Flash", key="scr_flash_btn"):
        with st.spinner("Gemini Flash ranking top catalyst plays…"):
            st.session_state.scr_flash = rank_catalysts_flash(industry, scores[:5])
        st.session_state.scr_pro = None  # finalists may have changed
        st.session_state.scr_flash_saved = False

    result = st.session_state.scr_flash
    if result is None:
        st.info("Run the screen, then rank catalysts to surface the 2 strongest near-term plays.")
        return
    if "_error" in result:
        st.error(result["_error"])
        return

    # Persist Flash finalists to the picks tracker (once per Flash result).
    if not st.session_state.scr_flash_saved and st.session_state.scr_run_id:
        by_ticker = {r["ticker"]: r for r in scores}
        for item in result.get("ranked", []):
            ticker = item.get("ticker", "")
            row = by_ticker.get(ticker)
            if row is None:
                continue
            screener_cache.save_pick(
                run_id=st.session_state.scr_run_id,
                ticker=ticker,
                industry=industry,
                boom_score=row.get("boom_score"),
                stage_reached="flash",
                boom_potential=None,
                conviction=None,
                entry_price=row.get("price"),
                alert_price=None,
            )
        st.session_state.scr_flash_saved = True

    for item in result.get("ranked", []):
        ticker = item.get("ticker", "?")
        rank = item.get("rank", "?")
        hype = item.get("hype_score", "?")
        thesis = item.get("boom_thesis", "")
        risk = item.get("risk_flag", "")
        cats = item.get("catalysts", [])
        cat_html = "".join(f"<li style='margin-bottom:2px'>{c}</li>" for c in cats)
        st.markdown(
            f'<div style="background:#161b27;border:1px solid #1e2740;border-left:4px solid #3aa6ff;'
            f'border-radius:8px;padding:14px 18px;margin-bottom:12px">'
            f'<span style="color:#e8eaf0;font-size:1.15rem;font-weight:700">#{rank} · {ticker}</span>'
            f'&nbsp;<span style="background:#3aa6ff22;color:#3aa6ff;padding:2px 9px;border-radius:10px;'
            f'font-size:0.78rem">Hype {hype}/10</span>'
            f'<p style="color:#cdd3e0;font-size:0.92rem;margin:8px 0 6px">{thesis}</p>'
            f'<span style="color:#7a85a0;font-size:0.78rem">CATALYSTS</span>'
            f'<ul style="color:#cdd3e0;font-size:0.85rem;margin:4px 0 6px 18px">{cat_html}</ul>'
            + (f'<span style="color:#e0a02f;font-size:0.82rem">⚠ {risk}</span>' if risk else "")
            + '</div>',
            unsafe_allow_html=True,
        )
```

**Why:** Records both Flash finalists exactly once per Flash result (guarded by
`scr_flash_saved`) using the current Stage-1 price as `entry_price`, satisfying "New SQLite table
storing each screener run... and one row per finalist" for the Flash stage.

#### Step 8g — Deep-dive tab: save Pro verdicts as picks

**Location**: Replace the entire `_render_deep_dive` function (currently lines 215-266). Only the
button-click block changes; reproduce the whole function exactly as follows:

```python
def _render_deep_dive(industry: str, scores: list[dict]) -> None:
    section_header("Stage 3 · Institutional Deep-Dive (Gemini Pro)")
    finalists = _finalists_from_flash(scores)
    st.caption("Finalists: " + ", ".join(r["ticker"] for r in finalists))

    if st.button("🏦 Run Pro deep-dive", key="scr_pro_btn"):
        with st.spinner("Gemini 3.1 Pro delivering boom/risk verdicts…"):
            st.session_state.scr_pro = deep_dive_pro(industry, finalists)

    result = st.session_state.scr_pro
    if result is None:
        st.info("Rank catalysts first, then run the Pro deep-dive on the 2 finalists.")
        return
    if "_error" in result:
        st.error(result["_error"])
        return

    # Persist Pro verdicts to the picks tracker.
    if st.session_state.scr_run_id:
        by_ticker = {r["ticker"]: r for r in scores}
        for v in result.get("verdicts", []):
            ticker = v.get("ticker", "")
            row = by_ticker.get(ticker)
            if row is None:
                continue
            screener_cache.save_pick(
                run_id=st.session_state.scr_run_id,
                ticker=ticker,
                industry=industry,
                boom_score=row.get("boom_score"),
                stage_reached="pro",
                boom_potential=v.get("boom_potential"),
                conviction=v.get("conviction"),
                entry_price=row.get("price"),
                alert_price=v.get("alert_price"),
            )

    for v in result.get("verdicts", []):
        ticker = v.get("ticker", "?")
        boom = v.get("boom_potential", "moderate")
        conv = v.get("conviction", "medium")
        thesis = v.get("thesis", "")
        cats = v.get("key_catalysts", [])
        risks = v.get("key_risks", [])
        entry = v.get("suggested_entry", "")
        alert = v.get("alert_price")
        bc = _IMPACT_COLOR.get(boom, "#95a5a6")
        cat_html = "".join(f"<li>{c}</li>" for c in cats)
        risk_html = "".join(f"<li>{r}</li>" for r in risks)
        st.markdown(
            f'<div style="background:#161b27;border:1px solid #1e2740;border-left:4px solid {bc};'
            f'border-radius:8px;padding:16px 20px;margin-bottom:14px">'
            f'<span style="color:#e8eaf0;font-size:1.25rem;font-weight:700">{ticker}</span>'
            f'&nbsp;<span style="background:{bc}22;color:{bc};padding:2px 10px;border-radius:10px;'
            f'font-size:0.8rem;font-weight:700">{boom.upper()} BOOM POTENTIAL</span>'
            f'&nbsp;<span style="color:#7a85a0;font-size:0.78rem">conviction: {conv}</span>'
            f'<p style="color:#cdd3e0;font-size:0.93rem;margin:10px 0">{thesis}</p>'
            f'<div style="display:flex;gap:24px;flex-wrap:wrap">'
            f'<div><span style="color:#2ecc71;font-size:0.78rem">CATALYSTS</span>'
            f'<ul style="color:#cdd3e0;font-size:0.84rem;margin:4px 0 0 18px">{cat_html}</ul></div>'
            f'<div><span style="color:#e74c3c;font-size:0.78rem">RISKS</span>'
            f'<ul style="color:#cdd3e0;font-size:0.84rem;margin:4px 0 0 18px">{risk_html}</ul></div>'
            f'</div>'
            + (f'<p style="color:#9aa3b8;font-size:0.84rem;margin-top:10px">📍 Entry: {entry}'
               + (f' · Alert ${alert}' if alert else "") + '</p>' if entry else "")
            + '</div>',
            unsafe_allow_html=True,
        )

    h2h = result.get("head_to_head", "")
    if h2h:
        st.success(f"**Head-to-head:** {h2h}")
```

**Why:** Records Pro verdicts as `stage_reached="pro"` rows every time a Pro deep-dive completes
(no dedup guard needed here — re-running Pro on the same finalists intentionally creates a fresh
history row, which is useful for the Picks Tracker's "latest verdict" view).

#### Step 8h — New "📈 Picks Tracker" render function

**Location**: Insert a brand-new function immediately before the
`# Page entry` section comment (currently the line right before `page_header(...)` at the bottom
of the file).

```python
# ---------------------------------------------------------------------------
# Tab 5 — Picks Tracker
# ---------------------------------------------------------------------------

def _render_picks_tracker() -> None:
    section_header("Picks Tracker · Historical Screener Hit-Rate")
    st.caption(
        "Every Flash/Pro finalist from every run is recorded here with its price at pick "
        "time. Use this to see whether the screener's picks actually work over time."
    )

    picks = screener_cache.get_all_picks(limit=200)
    if not picks:
        st.info(
            "No picks recorded yet. Run a screen and advance a candidate to the "
            "Catalyst (Flash) or Deep-Dive (Pro) stage to start tracking."
        )
        return

    unique_tickers = sorted({p["ticker"] for p in picks})
    try:
        hist = get_batch_history(tuple(unique_tickers), period="5d")
    except Exception:
        hist = pd.DataFrame()

    last_price: dict[str, float] = {}
    if hist is not None and not hist.empty:
        for t in unique_tickers:
            if t in hist.columns:
                series = hist[t].dropna()
                if not series.empty:
                    last_price[t] = float(series.iloc[-1])

    rows = []
    returns = []
    for p in picks:
        ticker = p["ticker"]
        entry = p.get("entry_price")
        now_price = last_price.get(ticker)
        pct_return = None
        if entry and now_price:
            pct_return = (now_price / entry - 1.0) * 100.0
            returns.append(pct_return)
        rows.append({
            "Ticker": ticker,
            "Industry": p.get("industry", ""),
            "Picked At": str(p.get("picked_at", ""))[:10],
            "Stage": p.get("stage_reached", ""),
            "Boom Score": p.get("boom_score"),
            "Boom Potential": p.get("boom_potential") or "—",
            "Conviction": p.get("conviction") or "—",
            "Entry Price": entry,
            "Price Now": now_price,
            "% Return": round(pct_return, 2) if pct_return is not None else None,
            "Alert Price": p.get("alert_price"),
        })

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Total Picks Recorded", len(picks))
    with c2:
        hit_rate = (sum(1 for r in returns if r > 0) / len(returns) * 100.0) if returns else None
        st.metric("Positive-Return Hit Rate", f"{hit_rate:.1f}%" if hit_rate is not None else "N/A")
    with c3:
        avg_return = (sum(returns) / len(returns)) if returns else None
        st.metric("Avg Return Since Pick", f"{avg_return:.2f}%" if avg_return is not None else "N/A")

    df = pd.DataFrame(rows)
    st.dataframe(
        df.style.format(precision=2, na_rep="—"),
        use_container_width=True,
        hide_index=True,
    )
```

**Why:** Implements "table of past picks with pick date, price then, price now (batch fetch), %
return since pick, verdict — so the user can see hit-rate over time" using one batched
`get_batch_history` call (not N per-ticker calls) and graceful `"N/A"`/`"—"` fallbacks when there
are zero picks or zero resolvable prices yet.

#### Step 8i — Page entry: run_id capture, Reddit-trending universe, new tab

**Location**: Replace the entire "Page entry" section at the bottom of the file (currently lines
389-423):

```python
page_header(
    "AI Stock Screener",
    "Hunt the next big boom across curated sectors — quant rank, AI catalyst ranking, "
    "Pro deep-dive, and portfolio-fit analysis.",
)

_ctrl = _render_sidebar()

if _ctrl["run"]:
    universe = list(dict.fromkeys(INDUSTRY_POOLS[_ctrl["industry"]] + _ctrl["custom"]))
    with st.spinner(f"Scoring {len(universe)} {_ctrl['industry']} names…"):
        st.session_state.scr_scores = compute_quant_scores(universe, _ctrl["weights"])
    st.session_state.scr_industry = _ctrl["industry"]
    st.session_state.scr_flash = None
    st.session_state.scr_pro = None

_scores = st.session_state.scr_scores
if not _scores:
    st.info("👈 Pick an industry pool, tune the boom-score weights, and click **🚀 Run Screen**.")
    st.stop()

st.caption(f"Showing screen for **{st.session_state.scr_industry}** · {len(_scores)} names scored.")

_tab_lead, _tab_cat, _tab_deep, _tab_fit = st.tabs([
    "📊 Leaderboard", "🤖 Catalyst (Flash)", "🏦 Deep-Dive (Pro)", "🧩 Portfolio Fit",
])
with _tab_lead:
    _render_leaderboard(_scores)
with _tab_cat:
    _render_catalyst(st.session_state.scr_industry, _scores)
with _tab_deep:
    _render_deep_dive(st.session_state.scr_industry, _scores)
with _tab_fit:
    _render_fit(_scores)
```

with:

```python
page_header(
    "AI Stock Screener",
    "Hunt the next big boom across curated sectors — quant rank, AI catalyst ranking, "
    "Pro deep-dive, and portfolio-fit analysis.",
)

_ctrl = _render_sidebar()

if _ctrl["run"]:
    if _ctrl["industry"] == REDDIT_TRENDING_LABEL:
        universe = list(dict.fromkeys(get_reddit_trending_pool(20) + _ctrl["custom"]))
        if not universe:
            st.warning(
                "No Reddit-trending tickers found in the last 7 days. Visit the "
                "Social Sentiment page and refresh daily mentions first, or pick a "
                "different industry pool."
            )
    else:
        universe = list(dict.fromkeys(INDUSTRY_POOLS[_ctrl["industry"]] + _ctrl["custom"]))

    if universe:
        with st.spinner(f"Scoring {len(universe)} {_ctrl['industry']} names…"):
            st.session_state.scr_scores = compute_quant_scores(universe, _ctrl["weights"])
        st.session_state.scr_industry = _ctrl["industry"]
        st.session_state.scr_flash = None
        st.session_state.scr_pro = None
        st.session_state.scr_run_id = screener_cache.save_run(_ctrl["industry"], _ctrl["weights"])
        st.session_state.scr_flash_saved = False

_scores = st.session_state.scr_scores
if not _scores:
    st.info("👈 Pick an industry pool, tune the boom-score weights, and click **🚀 Run Screen**.")
    st.stop()

st.caption(f"Showing screen for **{st.session_state.scr_industry}** · {len(_scores)} names scored.")

_tab_lead, _tab_cat, _tab_deep, _tab_fit, _tab_tracker = st.tabs([
    "📊 Leaderboard", "🤖 Catalyst (Flash)", "🏦 Deep-Dive (Pro)", "🧩 Portfolio Fit",
    "📈 Picks Tracker",
])
with _tab_lead:
    _render_leaderboard(_scores)
with _tab_cat:
    _render_catalyst(st.session_state.scr_industry, _scores)
with _tab_deep:
    _render_deep_dive(st.session_state.scr_industry, _scores)
with _tab_fit:
    _render_fit(_scores)
with _tab_tracker:
    _render_picks_tracker()
```

**Why:** Handles the Reddit-trending sentinel with a graceful empty-universe UI notice (per the
"degrade gracefully... UI notice" requirement), creates one `screener_runs` row per actual Stage-1
run (`save_run`) whose `run_id` the Flash/Pro save-pick calls (Steps 8f/8g) attach to, and adds
the 5th "📈 Picks Tracker" tab. Note the Picks Tracker tab is intentionally NOT gated behind
`if not _scores: st.stop()` being false — since it renders from `screener_cache.get_all_picks()`
directly (all-time history across all runs), a user could reasonably want to view it even without
having run a screen in the current session; however, because `st.stop()` above returns before the
tabs are built when `_scores` is empty, the Picks Tracker tab is only reachable after at least one
run in the current session, consistent with the rest of the page's existing gating pattern. This
is acceptable per the task's "additive tabs" requirement — do not restructure the `st.stop()` gate.

---

## 6. UI/UX Specification

- **Sidebar**: unchanged widget types/order except:
  - `st.radio("Industry pool", ...)` options list grows from 6 to 7 (adds
    `"🔥 Reddit Trending (Dynamic)"` at the end).
  - A 5th `st.slider("Smart money (13F)", 0, 100, 20, key="w_smart")` is added directly after the
    existing "Squeeze / hype" slider, before the "🚀 Run Screen" button.
- **Leaderboard tab**: same `st.dataframe` with `st.style.apply` top-5 highlight; the column set
  grows from 11 to 13 columns (`SmartMoney` inserted after `Hype`, `Put/Call` appended at the
  end; `Ret6m%` is renamed `Ret6m(rel)%` since it now shows the SPY-relative figure).
- **Catalyst (Flash) tab**: visually identical HTML cards; a picks-tracker save happens silently
  in the background on first render after the Flash click (no new visible UI).
- **Deep-Dive (Pro) tab**: visually identical HTML cards; a picks-tracker save happens silently
  in the background on every Pro click.
- **Portfolio Fit tab**: no changes.
- **New "📈 Picks Tracker" tab** (5th tab, rightmost): a `section_header`, one caption line, a
  3-column `st.metric` row (Total Picks Recorded / Positive-Return Hit Rate / Avg Return Since
  Pick), then one `st.dataframe` with columns: Ticker, Industry, Picked At, Stage, Boom Score,
  Boom Potential, Conviction, Entry Price, Price Now, % Return, Alert Price. Empty state:
  `st.info(...)` with no metrics/table shown.
- Colors: reuse the existing `_IMPACT_COLOR` / `_FIT_COLOR` dicts unchanged; no new color
  constants are introduced in the page except the new backtest `_EQUITY_COLORS["screener"] =
  "#E91E63"` entry (pink, visually distinct from all 8 existing signal colors).

## 7. Testing Checklist

1. **App boots cleanly.** Run `streamlit run dashboard.py` from `stock-dashboard/` with the venv
   activated. Navigate to page 12 "AI Stock Screener." Expected: page loads with no traceback,
   sidebar shows 7 industry-pool radio options and 5 weight sliders. Failure: any Python
   exception in the terminal or a Streamlit error banner.
2. **`db/screener.db` auto-creates.** After the app has started once, check that
   `stock-dashboard/db/screener.db` now exists on disk with `screener_runs` and `screener_picks`
   tables (`sqlite3 db/screener.db ".tables"`). Failure: file missing or tables missing.
3. **Stage 1 runs on the expanded universe.** Select "Semiconductors & AI Hardware", click
   "🚀 Run Screen". Expected: leaderboard shows ~22 scored rows (fewer if some tickers'
   `get_stock_info` returns empty — acceptable), completes in well under a minute, and the table
   has a `SmartMoney` column and a `Put/Call` column (mostly `—` except rows 1-5). Failure: page
   hangs for minutes, or a traceback about `ThreadPoolExecutor`.
4. **Reddit Trending pool — empty case.** If `wsb.db`'s `daily_ticker_mentions` table has no rows
   from the last 7 days (fresh install), select "🔥 Reddit Trending (Dynamic)" and click
   "🚀 Run Screen". Expected: an `st.warning` about no trending tickers found, no traceback, and
   `scr_scores` remains whatever it was before (no crash).
5. **Reddit Trending pool — populated case.** Visit page 8 "Social Sentiment," trigger a daily
   mentions refresh (per that page's existing UI) so `daily_ticker_mentions` has rows for today,
   then repeat step 4. Expected: the screen runs on the trending tickers instead of an
   `INDUSTRY_POOLS` list.
6. **Flash grounding with no MASSIVE_API_KEY.** Ensure `.env` has no `MASSIVE_API_KEY` (or an
   invalid one). Click "🤖 Rank catalysts with Flash". Expected: Flash still returns a ranked
   result (news block degrades to the "no recent news available" line inside the prompt, doesn't
   crash `rank_catalysts_flash`).
7. **Flash grounding with a configured MASSIVE_API_KEY.** With a valid key, repeat step 6.
   Expected: same UX; to verify grounding worked, temporarily add a `print(prompt)` inside
   `rank_catalysts_flash` (revert after) and confirm the printed prompt contains a "RECENT NEWS"
   block with real headline lines for at least one of the top-5 tickers.
8. **Pro deep-dive grounding.** After step 6/7, click "🏦 Run Pro deep-dive". Expected: verdict
   cards render normally; no traceback even if `wsb.db` has no row for either finalist ticker and
   no tracked 13F fund holds them (the "no data on file" / "no tracked fund" lines should appear
   if you temporarily print the prompt).
9. **Picks Tracker records Flash finalists.** After step 6 completes successfully, open the new
   "📈 Picks Tracker" tab. Expected: 2 rows appear (one per Flash finalist) with `Stage = flash`,
   `Entry Price` populated, `Boom Potential`/`Conviction` = `—`.
10. **Picks Tracker records Pro verdicts.** After step 8 completes, refresh/re-open the "📈 Picks
    Tracker" tab. Expected: 2 additional rows appear with `Stage = pro`, `Boom Potential` and
    `Conviction` populated, `Alert Price` populated if the model returned one. Total row count is
    now 4 (2 flash + 2 pro) for this run.
11. **Picks Tracker price-now + % return.** In the same tab, confirm `Price Now` and `% Return`
    columns are populated for at least the tickers with valid current market data, and the 3
    summary `st.metric` values are non-`N/A`. Failure: `Price Now` stays `—` for all rows (check
    `get_batch_history` isn't erroring) or a traceback.
12. **Picks Tracker empty state.** Delete `stock-dashboard/db/screener.db` (or test on a fresh
    clone before any run), reload the page, run a screen but do NOT click Flash or Pro, open the
    "📈 Picks Tracker" tab. Expected: the `st.info("No picks recorded yet...")` message, no table,
    no traceback.
13. **Portfolio Fit unaffected.** Run the existing Portfolio Fit flow (select a candidate, add
    holdings, click "🧩 Analyze portfolio fit"). Expected: behaves exactly as before this upgrade
    (this workstream intentionally left `analyze_portfolio_fit` untouched).
14. **Backtest page shows the new signal type.** Navigate to page 11 "AI Signal Backtester."
    Expected: the "Signal Types" checkbox list in the sidebar now includes "AI Screener Picks"
    (checked by default like the others).
15. **Backtest run including screener signals.** With at least one pick recorded from step 9/10
    (note its ticker), enter that ticker in the backtest's ticker list, set the date range to
    include today, leave all signal types checked (or just "AI Screener Picks"), click
    "Run Backtest". Expected: the run completes without error; if fewer than 5 screener signals
    exist across all selected tickers, the Overview tab should show "Insufficient data" for the
    "AI Screener Picks" row rather than crashing — this is the existing, unmodified
    `_MIN_SIGNALS_DISPLAY` behavior in `backtest_engine.py`, not something this plan needs to
    change.
16. **`analytics/` tests still pass.** Run `pytest analytics/tests/` from `stock-dashboard/`.
    Expected: all tests pass unchanged (this plan does not modify `analytics/`, only calls into
    its existing public functions `load_raw_chain`, `clean_chain`, `put_call_ratio`).
17. **No regressions to existing signal types.** Repeat step 14/15 selecting only
    `options_ai`/`news`/`reddit`/`hedge_fund`/`macro`/`mpt`/`technical` (i.e., uncheck
    "AI Screener Picks"). Expected: identical behavior to before this upgrade.

## 8. Rollback Plan

If something goes catastrophically wrong, revert per-file using git (each file listed in
Section 2 was either newly created by this plan or modified by it):

```powershell
# From stock-dashboard/ (or the repo root — adjust path as needed)
git checkout -- data/screener_agent.py
git checkout -- data/fetcher.py
git checkout -- data/backtest_signals.py
git checkout -- data/backtest_engine.py
git checkout -- pages/11_backtest.py
git checkout -- pages/12_screener.py
git clean -f -- data/screener_cache.py
git clean -f -- ../db/screener_schema.sql
Remove-Item -Force ../db/screener.db -ErrorAction SilentlyContinue
```

(Adjust the `db/` path prefix depending on whether the checkout is run from the repo root or from
`stock-dashboard/` — the DB and schema files live at `stock-dashboard/db/`.)

Since `db/screener.db` is created fresh by `data/screener_cache.py init_db()` on next import, and
no other file in the codebase reads from `screener.db` except the new/reverted files themselves,
deleting it is safe and loses only picks-tracker history (no other feature depends on it). No
other database (`cache.db`, `wsb.db`, `portfolio.db`, `thesis.db`, `backtest.db`) is touched by
this plan, so no other rollback is needed.
