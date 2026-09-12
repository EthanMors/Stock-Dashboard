# Plan: Speculative Stock Screener (Page 12)

> **IMPLEMENTOR NOTE:** Before writing any code, read `stock-dashboard/implementor-skills.md` in full. It contains the exact Gemini CLI invocation pattern, SQLite helper patterns, DB path convention, timestamp format, and caching rules you must follow. Every code pattern in this plan is derived from that file and the existing codebase.

---

## Overview

This plan adds a new Streamlit page — **Speculative Stock Screener** (`pages/12_screener.py`) — that surfaces small/micro-cap stocks with large upside potential using `yfinance`'s built-in screener API (`yf.screen`). Users can adjust filters (market cap tier, price range, minimum volume, sector), run the screener, review a scored results table, then invoke Gemini 2.5 Pro to produce a structured risk analysis (risk score, upside thesis, key risks, red flags, verdict) for any candidate. Analysis results are persisted in a new SQLite database (`db/screener.db`) so re-runs reuse cached results without consuming Gemini quota.

The outcome when fully executed: a working page accessible from the sidebar at position 12, with a filter panel, sortable results table with a computed speculation score, per-stock expandable price chart, single-stock and batch Gemini risk analysis, risk score badges, and financial-advice disclaimers.

---

## Files to Create

| File | Purpose |
|------|---------|
| `stock-dashboard/db/screener_schema.sql` | DDL for `screener_results` and `screener_analysis` tables |
| `stock-dashboard/data/screener.py` | yfinance screener calls, speculation score computation, screener result caching |
| `stock-dashboard/data/screener_agent.py` | Gemini 2.5 Pro risk analysis, JSON parsing, SQLite persistence |
| `stock-dashboard/pages/12_screener.py` | Streamlit page — filters, results table, analysis display |

## Files to Modify

| File | What Changes |
|------|-------------|
| `stock-dashboard/components/ui.py` | Add `("Screener", "pages/12_screener.py")` entry to `_NAV_PAGES` list |

---

## Prerequisites & Dependencies

No new pip packages required. All dependencies are already installed in the project venv:
- `yfinance` — already used in `data/fetcher.py`
- `pandas` — already a dependency
- `plotly` — already used in `components/charts.py`
- `streamlit` — already used throughout

No new environment variables required.

---

## Database Changes

### New database: `db/screener.db`

Two tables are needed — one for caching raw screener results (so repeated UI loads don't re-hit Yahoo), and one for persisting Gemini analysis results across sessions.

### New schema file: `db/screener_schema.sql`

```sql
CREATE TABLE IF NOT EXISTS screener_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_key    TEXT NOT NULL,
    results_json  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sr_screen_key
    ON screener_results (screen_key, fetched_at);

CREATE TABLE IF NOT EXISTS screener_analysis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    risk_score      INTEGER,
    upside_thesis   TEXT,
    key_risks       TEXT,
    red_flags       TEXT,
    verdict         TEXT,
    raw_metrics     TEXT,
    analyzed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sa_ticker
    ON screener_analysis (ticker);

CREATE INDEX IF NOT EXISTS idx_sa_ticker_analyzed_at
    ON screener_analysis (ticker, analyzed_at);
```

---

## Step-by-Step Implementation

### Step 1: Create the schema file

**File:** `stock-dashboard/db/screener_schema.sql`
**Action:** Create this file with the exact content shown in the "Database Changes" section above.

Write the file with this exact content (no extra whitespace before the first line):

```sql
CREATE TABLE IF NOT EXISTS screener_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_key    TEXT NOT NULL,
    results_json  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sr_screen_key
    ON screener_results (screen_key, fetched_at);

CREATE TABLE IF NOT EXISTS screener_analysis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    risk_score      INTEGER,
    upside_thesis   TEXT,
    key_risks       TEXT,
    red_flags       TEXT,
    verdict         TEXT,
    raw_metrics     TEXT,
    analyzed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sa_ticker
    ON screener_analysis (ticker);

CREATE INDEX IF NOT EXISTS idx_sa_ticker_analyzed_at
    ON screener_analysis (ticker, analyzed_at);
```

**Why:** The schema file is the single source of truth for the DB structure. The owning data module (`data/screener.py`) will read and execute it via `init_db()` at import time, following the exact pattern used by `data/portfolio_cache.py`.

---

### Step 2: Create `data/screener.py`

**File:** `stock-dashboard/data/screener.py`
**Action:** Create the file with the exact content below.

**Why:** This module owns all yfinance screener calls and the speculation score computation. It also provides the SQLite persistence layer for raw screener results (1-hour TTL). It follows the same module structure as `data/cache.py` and `data/portfolio_cache.py`.

```python
"""
data/screener.py
Speculative stock screener — yfinance screen API wrapper, speculation score
computation, and SQLite persistence for screener results (1h TTL).
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import yfinance as yf
from yfinance import EquityQuery

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener_schema.sql")


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Read and execute the schema DDL. Idempotent — safe to call on every import."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


init_db()  # run at import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Market cap tier boundaries (in USD)
_MICRO_CAP_MAX = 300_000_000      # < $300M
_SMALL_CAP_MAX = 2_000_000_000    # < $2B

# Screener result cache TTL
_CACHE_TTL_HOURS = 1

# Valid sectors for the sector filter dropdown. Empty string = "All Sectors".
SECTOR_OPTIONS = [
    "",
    "Technology",
    "Healthcare",
    "Consumer Cyclical",
    "Financial Services",
    "Basic Materials",
    "Communication Services",
    "Energy",
    "Industrials",
    "Real Estate",
    "Consumer Defensive",
    "Utilities",
]

# ---------------------------------------------------------------------------
# SQLite cache helpers for raw screener results
# ---------------------------------------------------------------------------

def _save_screener_results(screen_key: str, results: list[dict]) -> None:
    """Persist a list of screener result dicts to SQLite, keyed by screen_key."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO screener_results (screen_key, results_json, fetched_at) VALUES (?, ?, ?)",
            (screen_key, json.dumps(results), now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def _load_screener_results(screen_key: str) -> Optional[list[dict]]:
    """Load cached screener results if they are within _CACHE_TTL_HOURS old."""
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT results_json, fetched_at
            FROM screener_results
            WHERE screen_key = ?
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (screen_key,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data = dict(row)
    try:
        fetched_at = datetime.fromisoformat(data["fetched_at"].replace("Z", "+00:00"))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    if datetime.now(timezone.utc) - fetched_at > timedelta(hours=_CACHE_TTL_HOURS):
        return None

    try:
        return json.loads(data["results_json"])
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# yfinance screener
# ---------------------------------------------------------------------------

def _build_equity_query(
    min_market_cap: int,
    max_market_cap: int,
    min_price: float,
    max_price: float,
    min_volume: int,
    sector: str,
) -> EquityQuery:
    """Build a custom EquityQuery from user filter parameters.

    Parameters
    ----------
    min_market_cap : int — minimum market cap in USD (e.g. 10_000_000)
    max_market_cap : int — maximum market cap in USD (e.g. 2_000_000_000)
    min_price      : float — minimum stock price
    max_price      : float — maximum stock price
    min_volume     : int — minimum average daily volume (3-month)
    sector         : str — sector string as returned by Yahoo Finance, or "" for all sectors

    Returns
    -------
    EquityQuery object ready to pass to yf.screen()
    """
    conditions = [
        EquityQuery("IS-IN", ["exchange", "NMS", "NYQ"]),
        EquityQuery("EQ", ["region", "us"]),
        EquityQuery("BTWN", ["intradaymarketcap", min_market_cap, max_market_cap]),
        EquityQuery("GTE", ["intradayprice", min_price]),
        EquityQuery("LTE", ["intradayprice", max_price]),
        EquityQuery("GTE", ["avgdailyvol3m", min_volume]),
    ]
    if sector:
        conditions.append(EquityQuery("EQ", ["sector", sector]))

    return EquityQuery("AND", conditions)


def _run_screen(
    min_market_cap: int,
    max_market_cap: int,
    min_price: float,
    max_price: float,
    min_volume: int,
    sector: str,
    size: int = 100,
) -> list[dict]:
    """
    Execute the yfinance custom equity screen.

    Returns a list of raw quote dicts as returned by Yahoo Finance.
    Returns an empty list on any error.
    """
    query = _build_equity_query(min_market_cap, max_market_cap, min_price, max_price, min_volume, sector)
    try:
        result = yf.screen(
            query,
            sortField="averageDailyVolume3Month",
            sortAsc=False,
            size=size,
        )
        quotes = result.get("quotes", [])
        return [q for q in quotes if isinstance(q, dict)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Speculation score
# ---------------------------------------------------------------------------

def _compute_speculation_score(quote: dict) -> int:
    """
    Compute a 0–100 speculation score for a single Yahoo Finance quote dict.

    Scoring components (each 0–20 points):
    1. Distance from 52-week low (higher = more upside momentum shown):
       fiftyTwoWeekLowChangePercent / 2, capped at 20
    2. Distance below 52-week high (more room to recover = more upside):
       If fiftyTwoWeekHighChangePercent < 0, score = min(abs(pct) * 20, 20); else 0
    3. Volume spike (10-day avg vs 3-month avg):
       If averageDailyVolume10Day and averageDailyVolume3Month both present:
       ratio = 10day / 3month; score = min((ratio - 1) * 10, 20) if ratio > 1 else 0
    4. Small cap premium (smaller = higher speculation potential):
       marketCap < 100M → 20 pts; < 300M → 15 pts; < 500M → 10 pts; < 2B → 5 pts; else 0
    5. Forward PE discount (low or N/A forward PE suggests growth not yet priced in):
       forwardPE present and 0 < forwardPE < 15 → 20 pts
       forwardPE present and 15 <= forwardPE < 25 → 10 pts
       forwardPE None or <= 0 → 5 pts (unprofitable = speculative by nature)
       else 0

    Returns an integer 0–100.
    """
    score = 0

    # Component 1: distance above 52-week low (momentum upward)
    low_chg_pct = quote.get("fiftyTwoWeekLowChangePercent")
    if low_chg_pct is not None:
        try:
            score += min(int(float(low_chg_pct) * 100 / 2), 20)
        except (TypeError, ValueError):
            pass

    # Component 2: distance below 52-week high (upside recovery room)
    high_chg_pct = quote.get("fiftyTwoWeekHighChangePercent")
    if high_chg_pct is not None:
        try:
            v = float(high_chg_pct)
            if v < 0:
                score += min(int(abs(v) * 100 * 20), 20)
        except (TypeError, ValueError):
            pass

    # Component 3: volume spike (10-day vs 3-month average)
    vol_10d = quote.get("averageDailyVolume10Day")
    vol_3m = quote.get("averageDailyVolume3Month")
    if vol_10d and vol_3m and vol_3m > 0:
        try:
            ratio = float(vol_10d) / float(vol_3m)
            if ratio > 1:
                score += min(int((ratio - 1) * 10), 20)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # Component 4: small cap premium
    market_cap = quote.get("marketCap")
    if market_cap is not None:
        try:
            mc = float(market_cap)
            if mc < 100_000_000:
                score += 20
            elif mc < 300_000_000:
                score += 15
            elif mc < 500_000_000:
                score += 10
            elif mc < 2_000_000_000:
                score += 5
        except (TypeError, ValueError):
            pass

    # Component 5: forward PE discount
    fwd_pe = quote.get("forwardPE")
    if fwd_pe is not None:
        try:
            pe = float(fwd_pe)
            if 0 < pe < 15:
                score += 20
            elif 15 <= pe < 25:
                score += 10
        except (TypeError, ValueError):
            pass
    else:
        score += 5  # unprofitable / no forward PE = inherently speculative

    return max(0, min(score, 100))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_screener(
    min_market_cap: int,
    max_market_cap: int,
    min_price: float,
    max_price: float,
    min_volume: int,
    sector: str,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Run the speculative stock screener with the given filters and return
    a DataFrame of candidates with computed speculation scores.

    Uses a 1-hour SQLite cache keyed by the filter combination. Pass
    force_refresh=True to bypass the cache.

    Parameters
    ----------
    min_market_cap : int — minimum market cap in USD
    max_market_cap : int — maximum market cap in USD
    min_price      : float — minimum stock price in USD
    max_price      : float — maximum stock price in USD
    min_volume     : int — minimum 3-month average daily volume
    sector         : str — Yahoo Finance sector string, or "" for all sectors
    force_refresh  : bool — if True, bypass cache and re-fetch from Yahoo

    Returns
    -------
    pd.DataFrame with columns:
        ticker, name, price, change_pct, market_cap, volume_3m, volume_10d,
        week52_high, week52_low, week52_high_chg_pct, week52_low_chg_pct,
        forward_pe, eps_ttm, speculation_score
    Returns an empty DataFrame if the screen returns no results or errors.
    """
    screen_key = (
        f"{min_market_cap}_{max_market_cap}_{min_price}_{max_price}"
        f"_{min_volume}_{sector or 'ALL'}"
    )

    if not force_refresh:
        cached = _load_screener_results(screen_key)
        if cached is not None:
            return _quotes_to_dataframe(cached)

    quotes = _run_screen(min_market_cap, max_market_cap, min_price, max_price, min_volume, sector)

    if not quotes:
        return pd.DataFrame()

    _save_screener_results(screen_key, quotes)
    return _quotes_to_dataframe(quotes)


def _quotes_to_dataframe(quotes: list[dict]) -> pd.DataFrame:
    """
    Convert a list of Yahoo Finance quote dicts to the canonical screener DataFrame.

    Returns a DataFrame with columns:
        ticker, name, price, change_pct, market_cap, volume_3m, volume_10d,
        week52_high, week52_low, week52_high_chg_pct, week52_low_chg_pct,
        forward_pe, eps_ttm, speculation_score
    Returns an empty DataFrame if quotes is empty.
    """
    if not quotes:
        return pd.DataFrame()

    rows = []
    for q in quotes:
        ticker = q.get("symbol", "")
        if not ticker:
            continue
        score = _compute_speculation_score(q)
        rows.append({
            "ticker": ticker.upper(),
            "name": q.get("shortName") or q.get("longName") or ticker,
            "price": q.get("regularMarketPrice"),
            "change_pct": q.get("regularMarketChangePercent"),
            "market_cap": q.get("marketCap"),
            "volume_3m": q.get("averageDailyVolume3Month"),
            "volume_10d": q.get("averageDailyVolume10Day"),
            "week52_high": q.get("fiftyTwoWeekHigh"),
            "week52_low": q.get("fiftyTwoWeekLow"),
            "week52_high_chg_pct": q.get("fiftyTwoWeekHighChangePercent"),
            "week52_low_chg_pct": q.get("fiftyTwoWeekLowChangePercent"),
            "forward_pe": q.get("forwardPE"),
            "eps_ttm": q.get("epsTrailingTwelveMonths"),
            "speculation_score": score,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.sort_values("speculation_score", ascending=False).reset_index(drop=True)
    return df
```

---

### Step 3: Create `data/screener_agent.py`

**File:** `stock-dashboard/data/screener_agent.py`
**Action:** Create the file with the exact content below.

**Why:** This module owns the Gemini 2.5 Pro risk analysis for individual speculative stocks. It follows the exact same pattern as `data/hedge_fund_agent.py` and `data/options_agent.py`: `_run_gemini_pro` → `_build_prompt` → `_parse_response` → public `run_risk_analysis`. Analysis results are persisted to `screener.db` (same database as `screener.py`) so re-runs reuse cached output without consuming Pro quota.

```python
"""
data/screener_agent.py
Gemini 2.5 Pro risk analysis for speculative stock screener candidates.
Persists results to db/screener.db (screener_analysis table).
"""

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

from data.gemini_tracker import record_call

# ---------------------------------------------------------------------------
# DB setup (shares screener.db with data/screener.py)
# ---------------------------------------------------------------------------

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "screener_schema.sql")


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    """Ensure the schema exists. Called at import time. Safe if already initialised."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


_init_db()

# ---------------------------------------------------------------------------
# Analysis cache TTL
# ---------------------------------------------------------------------------

_ANALYSIS_TTL_HOURS = 24  # Re-analyse once per day at most


def is_analysis_fresh(analyzed_at_str: str) -> bool:
    """Return True if the analysis timestamp is within _ANALYSIS_TTL_HOURS."""
    try:
        analyzed_at = datetime.fromisoformat(analyzed_at_str.replace("Z", "+00:00"))
        if analyzed_at.tzinfo is None:
            analyzed_at = analyzed_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - analyzed_at < timedelta(hours=_ANALYSIS_TTL_HOURS)
    except (ValueError, TypeError):
        return False


def get_cached_analysis(ticker: str) -> Optional[dict]:
    """
    Return the most recent analysis for ticker from screener_analysis if still fresh.
    Returns None if no row exists or the row is stale.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT risk_score, upside_thesis, key_risks, red_flags, verdict,
                   raw_metrics, analyzed_at
            FROM screener_analysis
            WHERE ticker = ?
            ORDER BY analyzed_at DESC
            LIMIT 1
            """,
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data = dict(row)
    if not is_analysis_fresh(data.get("analyzed_at", "")):
        return None

    # Deserialize JSON columns
    for col in ("key_risks", "red_flags", "raw_metrics"):
        raw = data.get(col) or "[]"
        try:
            data[col] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data[col] = []

    return data


def save_analysis(ticker: str, result: dict) -> None:
    """
    Persist a Gemini risk analysis result to the screener_analysis table.

    Parameters
    ----------
    ticker : str — uppercase ticker symbol
    result : dict — must contain keys: risk_score (int), upside_thesis (str),
                    key_risks (list[str]), red_flags (list[str]),
                    verdict (str), raw_metrics (dict)
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO screener_analysis
                (ticker, risk_score, upside_thesis, key_risks, red_flags,
                 verdict, raw_metrics, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker.upper(),
                int(result.get("risk_score", 5)),
                str(result.get("upside_thesis", "")),
                json.dumps(result.get("key_risks", [])),
                json.dumps(result.get("red_flags", [])),
                str(result.get("verdict", "")),
                json.dumps(result.get("raw_metrics", {})),
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Gemini 2.5 Pro runner (identical to hedge_fund_agent.py / options_agent.py)
# ---------------------------------------------------------------------------

def _run_gemini_pro(prompt: str) -> tuple[str, str]:
    """Call Gemini 2.5 Pro via CLI. Returns (stdout, stderr). Prompt passed via stdin."""
    try:
        result = subprocess.run(
            ["gemini.cmd", "-m", "gemini-2.5-pro", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )
        output = result.stdout.strip()
        if output:
            record_call("pro")
        return output, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "Timed out after 180s"
    except Exception as exc:
        return "", str(exc)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are a speculative equity risk analyst. Your task is to assess the risk/reward
profile of a small or micro-cap stock based on the quantitative metrics below.
These are HIGH-RISK speculative stocks. Your analysis must be rigorous, honest about
risks, and use the exact numbers provided.

=== STOCK METRICS ===
Ticker:                    {ticker}
Company Name:              {name}
Current Price:             ${price}
Market Cap:                {market_cap}
52-Week High:              ${week52_high}  (current is {week52_high_chg_pct}% from high)
52-Week Low:               ${week52_low}   (current is {week52_low_chg_pct}% above low)
Avg Daily Volume (3M):     {volume_3m}
Avg Daily Volume (10D):    {volume_10d}
Volume Spike Ratio (10D/3M): {volume_spike}
Forward PE:                {forward_pe}
EPS (TTM):                 {eps_ttm}
Speculation Score:         {speculation_score}/100

=== YOUR ANALYSIS TASK ===
Analyze this speculative stock and produce a structured risk assessment. Be specific
— reference the actual numbers above. Do not invent data not present in the metrics.

For risk_score: rate overall risk from 1 (extremely low risk) to 10 (extremely high risk).
For upside_thesis: what specific factors in the numbers above suggest potential upside? 2-3 sentences.
For key_risks: list 3-5 specific risk factors based on the data (e.g. thin volume, near 52w high, no earnings, high dilution risk for micro-caps).
For red_flags: list any concrete warning signs visible in these metrics. If none, return an empty list.
For verdict: assign exactly one of these labels:
  "lottery ticket" — extremely speculative, could go 10x or zero
  "speculative buy" — elevated risk but metrics suggest real upside potential
  "hold/watch" — not actionable yet, monitor for catalyst
  "avoid" — risk/reward unfavourable based on these metrics

Rules:
- Be specific: reference actual numbers from the metrics block
- verdict must be exactly one of the four labels listed above
- key_risks and red_flags must be lists of plain strings (no sub-objects)
- risk_score must be an integer 1-10
- Respond ONLY with a single JSON object (no markdown fences, no preamble, no trailing text)

{{
  "risk_score": <integer 1-10>,
  "upside_thesis": "<2-3 sentence specific upside case>",
  "key_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "red_flags": ["<flag 1>"],
  "verdict": "<lottery ticket|speculative buy|hold/watch|avoid>"
}}
"""


def _fmt_market_cap(mc) -> str:
    """Format a market cap number as a human-readable string."""
    if mc is None:
        return "N/A"
    try:
        mc = float(mc)
        if mc >= 1e9:
            return f"${mc / 1e9:.2f}B"
        if mc >= 1e6:
            return f"${mc / 1e6:.0f}M"
        if mc >= 1e3:
            return f"${mc / 1e3:.0f}K"
        return f"${mc:,.0f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_optional(val, fmt: str = ".2f", suffix: str = "") -> str:
    """Format an optional numeric value; return 'N/A' if None."""
    if val is None:
        return "N/A"
    try:
        return format(float(val), fmt) + suffix
    except (TypeError, ValueError):
        return "N/A"


def _build_prompt(metrics: dict) -> str:
    """Build the Gemini prompt string from a metrics dict (row from screener DataFrame)."""
    vol_3m = metrics.get("volume_3m")
    vol_10d = metrics.get("volume_10d")

    if vol_3m and vol_10d and float(vol_3m) > 0:
        try:
            spike = f"{float(vol_10d) / float(vol_3m):.2f}x"
        except (TypeError, ValueError, ZeroDivisionError):
            spike = "N/A"
    else:
        spike = "N/A"

    high_chg = metrics.get("week52_high_chg_pct")
    low_chg = metrics.get("week52_low_chg_pct")

    return _PROMPT_TEMPLATE.format(
        ticker=str(metrics.get("ticker", "")).upper(),
        name=str(metrics.get("name", "N/A")),
        price=_fmt_optional(metrics.get("price")),
        market_cap=_fmt_market_cap(metrics.get("market_cap")),
        week52_high=_fmt_optional(metrics.get("week52_high")),
        week52_high_chg_pct=_fmt_optional(high_chg, ".1f"),
        week52_low=_fmt_optional(metrics.get("week52_low")),
        week52_low_chg_pct=_fmt_optional(low_chg, ".1f"),
        volume_3m=_fmt_optional(vol_3m, ",.0f") if vol_3m is not None else "N/A",
        volume_10d=_fmt_optional(vol_10d, ",.0f") if vol_10d is not None else "N/A",
        volume_spike=spike,
        forward_pe=_fmt_optional(metrics.get("forward_pe")),
        eps_ttm=_fmt_optional(metrics.get("eps_ttm")),
        speculation_score=metrics.get("speculation_score", 0),
    )


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------

_VALID_VERDICTS = {"lottery ticket", "speculative buy", "hold/watch", "avoid"}


def _parse_response(raw: str) -> Optional[dict]:
    """Extract and parse the JSON object from Gemini's raw stdout."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    # Validate and normalize risk_score
    try:
        rs = int(data.get("risk_score", 5))
        rs = max(1, min(10, rs))
    except (TypeError, ValueError):
        rs = 5
    data["risk_score"] = rs

    # Normalize verdict
    verdict = str(data.get("verdict", "")).lower().strip()
    if verdict not in _VALID_VERDICTS:
        verdict = "hold/watch"
    data["verdict"] = verdict

    # Ensure list fields are lists of strings
    for field in ("key_risks", "red_flags"):
        val = data.get(field, [])
        if not isinstance(val, list):
            val = []
        data[field] = [str(item) for item in val]

    # Ensure text fields are strings
    data["upside_thesis"] = str(data.get("upside_thesis", "")).strip()

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_risk_analysis(metrics: dict) -> dict:
    """
    Run Gemini 2.5 Pro risk analysis for a single speculative stock.

    Checks the SQLite cache first. If a fresh analysis (< 24h old) exists,
    returns it without calling Gemini. Otherwise calls Gemini, parses the
    result, saves it to SQLite, and returns it.

    Parameters
    ----------
    metrics : dict — a single row from the screener DataFrame as a dict.
              Must contain at minimum: ticker, name, price, market_cap,
              week52_high, week52_low, week52_high_chg_pct,
              week52_low_chg_pct, volume_3m, volume_10d, forward_pe,
              eps_ttm, speculation_score

    Returns
    -------
    dict with keys: risk_score (int), upside_thesis (str),
                    key_risks (list[str]), red_flags (list[str]),
                    verdict (str), raw_metrics (dict)
    On failure returns {"_error": str, "risk_score": 5, "verdict": "hold/watch",
                        "upside_thesis": "", "key_risks": [], "red_flags": []}
    """
    ticker = str(metrics.get("ticker", "")).upper()

    # Check cache first
    cached = get_cached_analysis(ticker)
    if cached is not None:
        return cached

    prompt = _build_prompt(metrics)
    raw, stderr = _run_gemini_pro(prompt)

    if not raw:
        return {
            "_error": stderr or "Gemini returned empty output.",
            "risk_score": 5,
            "verdict": "hold/watch",
            "upside_thesis": "",
            "key_risks": [],
            "red_flags": [],
            "raw_metrics": metrics,
        }

    result = _parse_response(raw)
    if result is None:
        return {
            "_error": f"Could not parse Gemini response.\n\nRaw output:\n{raw[:500]}",
            "risk_score": 5,
            "verdict": "hold/watch",
            "upside_thesis": "",
            "key_risks": [],
            "red_flags": [],
            "raw_metrics": metrics,
        }

    result["raw_metrics"] = metrics
    save_analysis(ticker, result)
    return result


def run_batch_risk_analysis(rows: list[dict]) -> dict[str, dict]:
    """
    Run risk analysis for a list of screener row dicts. Returns a dict mapping
    ticker → analysis result dict. Uses cached results where available.
    Each row must be a dict with the same keys as a screener DataFrame row.
    Calls Gemini serially to avoid rate limit issues.

    Parameters
    ----------
    rows : list[dict] — list of screener DataFrame rows converted to dicts

    Returns
    -------
    dict mapping ticker (str) → result dict (same shape as run_risk_analysis return)
    """
    results = {}
    for row in rows:
        ticker = str(row.get("ticker", "")).upper()
        if not ticker:
            continue
        results[ticker] = run_risk_analysis(row)
    return results
```

---

### Step 4: Create `pages/12_screener.py`

**File:** `stock-dashboard/pages/12_screener.py`
**Action:** Create the file with the exact content below.

**Why:** This is the Streamlit page. It follows the exact page template from `implementor-skills.md`: `set_page_config` → `render_gemini_usage_bar()` → `inject_global_css()` → `render_sidebar_nav()` → constants → helpers → cached functions → render functions → page entry calls. All data logic is delegated to `data/screener.py` and `data/screener_agent.py`. Raw SQLite is never opened here.

```python
# pages/12_screener.py

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header, render_sidebar_nav, section_header, plotly_dark_layout
from data.screener import run_screener, SECTOR_OPTIONS, _MICRO_CAP_MAX, _SMALL_CAP_MAX
from data.screener_agent import run_risk_analysis, run_batch_risk_analysis, get_cached_analysis
from data.gemini_tracker import get_today_stats, PRO_DAILY_LIMIT

st.set_page_config(page_title="Speculative Screener", layout="wide")

render_gemini_usage_bar()
inject_global_css()
render_sidebar_nav()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MIN_PRICE = 0.50
_DEFAULT_MAX_PRICE = 20.0
_DEFAULT_MIN_VOLUME = 100_000
_DEFAULT_MAX_BATCH = 5  # max stocks for batch analysis (Pro quota protection)

_VERDICT_COLORS = {
    "lottery ticket": "#ffd600",   # amber
    "speculative buy": "#00c853",  # green
    "hold/watch": "#2979ff",       # blue
    "avoid": "#ff1744",            # red
}

_VERDICT_ICONS = {
    "lottery ticket": "?",
    "speculative buy": "++",
    "hold/watch": "~",
    "avoid": "X",
}

_CAP_TIER_OPTIONS = {
    "Micro-cap (< $300M)": (1_000_000, _MICRO_CAP_MAX),
    "Small-cap (< $2B)":   (1_000_000, _SMALL_CAP_MAX),
    "Micro + Small (< $2B, sorted by score)": (1_000_000, _SMALL_CAP_MAX),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_market_cap(mc) -> str:
    """Format a market cap value for display in the table."""
    if mc is None or (isinstance(mc, float) and pd.isna(mc)):
        return "N/A"
    try:
        mc = float(mc)
        if mc >= 1e9:
            return f"${mc / 1e9:.2f}B"
        if mc >= 1e6:
            return f"${mc / 1e6:.0f}M"
        return f"${mc:,.0f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_pct(val) -> str:
    """Format a decimal fraction as a percentage string (e.g. 0.15 → '+15.0%')."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    try:
        v = float(val)
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_price(val) -> str:
    """Format a price value as a USD string."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    try:
        return f"${float(val):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_volume(val) -> str:
    """Format a volume number with M/K suffix."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    try:
        v = float(val)
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v / 1_000:.0f}K"
        return f"{v:.0f}"
    except (TypeError, ValueError):
        return "N/A"


def _score_color(score: int) -> str:
    """Return a hex color for a speculation score 0-100."""
    if score >= 70:
        return "#ff1744"  # high speculation = red warning
    if score >= 45:
        return "#ffd600"  # medium = amber
    return "#00c853"      # lower = green (less risky)


def _risk_score_color(rs: int) -> str:
    """Return a hex color for a Gemini risk score 1-10."""
    if rs >= 8:
        return "#ff1744"
    if rs >= 5:
        return "#ffd600"
    return "#00c853"


def _build_price_chart(ticker: str, period: str = "6mo") -> go.Figure:
    """
    Fetch price history via yfinance and return a Plotly line chart.
    Returns an empty figure with a message if no data is available.
    This is called inside an @st.cache_data wrapper in the page.
    """
    import yfinance as yf
    try:
        hist = yf.Ticker(ticker).history(period=period)
    except Exception:
        hist = None

    if hist is None or hist.empty:
        fig = go.Figure()
        fig.update_layout(
            **plotly_dark_layout(
                annotations=[{
                    "text": f"No price data for {ticker}",
                    "xref": "paper", "yref": "paper",
                    "x": 0.5, "y": 0.5, "showarrow": False,
                    "font": {"size": 14, "color": "#556080"},
                }],
                xaxis_visible=False,
                yaxis_visible=False,
            )
        )
        return fig

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=hist.index,
        y=hist["Close"],
        mode="lines",
        name=ticker,
        line={"color": "#4f8ef7", "width": 2},
        fill="tozeroy",
        fillcolor="rgba(79,142,247,0.08)",
    ))
    fig.update_layout(
        **plotly_dark_layout(
            title=f"{ticker} — 6-Month Price",
            xaxis_title="Date",
            yaxis_title="Price (USD)",
            height=260,
            xaxis_rangeslider_visible=False,
        )
    )
    return fig


# ---------------------------------------------------------------------------
# Cached data fetchers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def _cached_screener(
    min_market_cap: int,
    max_market_cap: int,
    min_price: float,
    max_price: float,
    min_volume: int,
    sector: str,
) -> pd.DataFrame:
    """
    Wrapper that applies @st.cache_data around run_screener().
    The underlying run_screener() also checks a SQLite cache (1h TTL), but
    @st.cache_data here prevents re-hitting SQLite on every Streamlit rerun.
    """
    return run_screener(
        min_market_cap=min_market_cap,
        max_market_cap=max_market_cap,
        min_price=min_price,
        max_price=max_price,
        min_volume=min_volume,
        sector=sector,
        force_refresh=False,
    )


@st.cache_data(ttl=600)
def _cached_price_chart(ticker: str) -> go.Figure:
    """Cache the price chart for 10 minutes per ticker."""
    return _build_price_chart(ticker)


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown("---")
        st.markdown("### About This Page")
        with st.expander("What is Speculative Screening?", expanded=True):
            st.markdown(
                "This screener surfaces **small and micro-cap stocks** that exhibit "
                "characteristics associated with high upside potential — unusual volume, "
                "proximity to 52-week lows, small market cap, and forward PE discounts. "
                "A **Speculation Score (0–100)** is computed from these factors."
            )
        with st.expander("How is the Speculation Score calculated?"):
            st.markdown(
                "The score combines five equally weighted factors (0–20 pts each):\n"
                "1. **52-week momentum** — how far above the 52-week low\n"
                "2. **52-week recovery room** — how far below the 52-week high\n"
                "3. **Volume spike** — 10-day vs 3-month average volume ratio\n"
                "4. **Small cap premium** — smaller cap = higher potential\n"
                "5. **Forward PE discount** — low or no PE = unloved / early stage\n\n"
                "Higher score = more speculative (more risk AND more potential upside)."
            )
        with st.expander("What does the Gemini Risk Analysis do?"):
            st.markdown(
                "Gemini 2.5 Pro reviews the stock's quantitative metrics and returns:\n"
                "- **Risk Score (1–10):** 1 = very low risk, 10 = extremely high risk\n"
                "- **Upside Thesis:** what the numbers suggest about upside potential\n"
                "- **Key Risks:** 3–5 specific risk factors from the metrics\n"
                "- **Red Flags:** any concrete warning signs\n"
                "- **Verdict:** lottery ticket / speculative buy / hold-watch / avoid\n\n"
                "Results are cached for 24 hours to conserve Gemini Pro quota (50/day)."
            )
        st.markdown("---")
        st.caption(
            "Data sourced from Yahoo Finance via yfinance. "
            "Gemini analysis via Google Gemini 2.5 Pro CLI."
        )


def _render_filters() -> tuple[int, int, float, float, int, str]:
    """
    Render the filter controls and return the current filter values as a tuple:
    (min_market_cap, max_market_cap, min_price, max_price, min_volume, sector)
    """
    section_header("Screener Filters")

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        cap_tier = st.selectbox(
            "Market Cap Tier",
            options=list(_CAP_TIER_OPTIONS.keys()),
            index=0,
            key="screener_cap_tier",
        )
        min_cap, max_cap = _CAP_TIER_OPTIONS[cap_tier]

    with col2:
        price_range = st.slider(
            "Price Range (USD)",
            min_value=0.10,
            max_value=50.0,
            value=(_DEFAULT_MIN_PRICE, _DEFAULT_MAX_PRICE),
            step=0.10,
            key="screener_price_range",
        )
        min_price, max_price = price_range

    with col3:
        volume_options = {
            "50K+": 50_000,
            "100K+": 100_000,
            "250K+": 250_000,
            "500K+": 500_000,
            "1M+": 1_000_000,
        }
        vol_label = st.selectbox(
            "Min Avg Daily Volume (3M)",
            options=list(volume_options.keys()),
            index=1,
            key="screener_volume",
        )
        min_volume = volume_options[vol_label]

    with col4:
        sector_display = ["All Sectors"] + [s for s in SECTOR_OPTIONS if s]
        sector_sel = st.selectbox(
            "Sector",
            options=sector_display,
            index=0,
            key="screener_sector",
        )
        sector = "" if sector_sel == "All Sectors" else sector_sel

    return min_cap, max_cap, min_price, max_price, min_volume, sector


def _render_results_table(df: pd.DataFrame) -> None:
    """
    Render the screener results as a styled dataframe.
    df must have the columns produced by data/screener.py run_screener().
    """
    if df.empty:
        st.info(
            "No candidates found with the current filters. "
            "Try relaxing the market cap tier, price range, or minimum volume."
        )
        return

    section_header(f"Candidates ({len(df)} found, sorted by Speculation Score)")

    display_df = pd.DataFrame({
        "Ticker":       df["ticker"],
        "Name":         df["name"],
        "Price":        df["price"].apply(_fmt_price),
        "Day Chg%":     df["change_pct"].apply(_fmt_pct),
        "Market Cap":   df["market_cap"].apply(_fmt_market_cap),
        "Vol (3M Avg)": df["volume_3m"].apply(_fmt_volume),
        "Vol Spike":    df.apply(
            lambda r: f"{float(r['volume_10d']) / float(r['volume_3m']):.1f}x"
            if r.get("volume_10d") and r.get("volume_3m") and float(r.get("volume_3m", 0)) > 0
            else "N/A",
            axis=1,
        ),
        "52W High Chg": df["week52_high_chg_pct"].apply(_fmt_pct),
        "52W Low Chg":  df["week52_low_chg_pct"].apply(_fmt_pct),
        "Fwd PE":       df["forward_pe"].apply(
            lambda v: f"{float(v):.1f}" if v is not None and not pd.isna(v) else "N/A"
        ),
        "EPS (TTM)":    df["eps_ttm"].apply(
            lambda v: f"{float(v):.2f}" if v is not None and not pd.isna(v) else "N/A"
        ),
        "Spec Score":   df["speculation_score"],
    })

    st.dataframe(display_df, use_container_width=True, hide_index=True)


def _render_stock_detail(row: pd.Series, analysis: dict | None) -> None:
    """
    Render an expander with price chart, key metrics, and Gemini analysis for one stock.

    Parameters
    ----------
    row      : pd.Series — one row from the screener DataFrame
    analysis : dict | None — pre-loaded Gemini analysis, or None if not yet run
    """
    score = int(row.get("speculation_score", 0))
    score_color = _score_color(score)
    ticker = str(row.get("ticker", ""))
    name = str(row.get("name", ticker))

    label = (
        f"{ticker} — {name}  |  "
        f"Score: {score}/100  |  "
        f"Price: {_fmt_price(row.get('price'))}"
    )

    with st.expander(label, expanded=False):
        # Price chart
        chart_fig = _cached_price_chart(ticker)
        st.plotly_chart(chart_fig, use_container_width=True)

        # Key metrics row
        mc1, mc2, mc3, mc4, mc5 = st.columns(5)
        mc1.metric("Price", _fmt_price(row.get("price")))
        mc2.metric("Market Cap", _fmt_market_cap(row.get("market_cap")))
        mc3.metric("52W High Chg", _fmt_pct(row.get("week52_high_chg_pct")))
        mc4.metric("52W Low Chg", _fmt_pct(row.get("week52_low_chg_pct")))
        mc5.metric(
            "Spec Score",
            f"{score}/100",
            help="Speculation score 0-100: higher = more speculative (higher risk AND higher potential upside)",
        )

        st.markdown("---")

        # Gemini analysis section
        if analysis is not None and "_error" not in analysis:
            _render_analysis_block(ticker, analysis)
        else:
            # Show analyze button
            stats = get_today_stats()
            pro_remaining = PRO_DAILY_LIMIT - stats.get("pro", 0)

            if pro_remaining <= 0:
                st.warning("Gemini Pro daily limit (50) reached. Analysis unavailable until tomorrow.")
            else:
                btn_key = f"analyze_{ticker}"
                if st.button(
                    f"Analyze {ticker} with Gemini Pro",
                    key=btn_key,
                    help=f"Uses 1 Gemini Pro call. {pro_remaining} remaining today.",
                ):
                    with st.spinner(f"Running Gemini 2.5 Pro analysis for {ticker}..."):
                        result = run_risk_analysis(row.to_dict())
                    st.session_state[f"analysis_{ticker}"] = result
                    st.rerun()

                if analysis is not None and "_error" in analysis:
                    st.error(f"Analysis failed: {analysis['_error']}")


def _render_analysis_block(ticker: str, analysis: dict) -> None:
    """
    Render the Gemini risk analysis results for a single stock.
    This is called from within an expander in _render_stock_detail.
    """
    risk_score = analysis.get("risk_score", 5)
    verdict = analysis.get("verdict", "hold/watch")
    verdict_color = _VERDICT_COLORS.get(verdict, "#aab4cf")
    verdict_icon = _VERDICT_ICONS.get(verdict, "~")
    risk_color = _risk_score_color(risk_score)

    # Header row
    col_rs, col_vd, col_spacer = st.columns([1, 2, 5])
    col_rs.markdown(
        f'<div style="background:{risk_color};border-radius:8px;padding:8px 12px;'
        f'text-align:center;font-weight:700;font-size:1.1rem;color:#0d1020;">'
        f'Risk {risk_score}/10</div>',
        unsafe_allow_html=True,
    )
    col_vd.markdown(
        f'<div style="background:{verdict_color};border-radius:8px;padding:8px 12px;'
        f'text-align:center;font-weight:700;font-size:0.9rem;color:#0d1020;">'
        f'[{verdict_icon}] {verdict.upper()}</div>',
        unsafe_allow_html=True,
    )

    st.markdown("&nbsp;", unsafe_allow_html=True)

    # Upside thesis
    upside = analysis.get("upside_thesis", "")
    if upside:
        st.markdown(f"**Upside Thesis:** {upside}")

    # Key risks
    key_risks = analysis.get("key_risks", [])
    if key_risks:
        st.markdown("**Key Risks:**")
        for r in key_risks:
            st.markdown(f"- {r}")

    # Red flags
    red_flags = analysis.get("red_flags", [])
    if red_flags:
        st.markdown("**Red Flags:**")
        for f in red_flags:
            st.markdown(f"- {f}")

    analyzed_at = analysis.get("analyzed_at", "")
    if analyzed_at:
        st.caption(f"Analysis cached at: {analyzed_at} UTC")


def _render_batch_analysis_section(df: pd.DataFrame) -> None:
    """
    Render the 'Analyze Top N with Gemini' batch section below the results table.
    """
    if df.empty:
        return

    section_header("Batch Gemini Analysis")

    stats = get_today_stats()
    pro_remaining = PRO_DAILY_LIMIT - stats.get("pro", 0)

    max_n = min(_DEFAULT_MAX_BATCH, len(df), pro_remaining)

    if pro_remaining <= 0:
        st.warning("Gemini Pro daily limit (50) reached. Batch analysis unavailable until tomorrow.")
        return

    col_n, col_btn = st.columns([2, 2])
    with col_n:
        n = st.number_input(
            f"Number of top candidates to analyze (max {max_n})",
            min_value=1,
            max_value=max_n,
            value=min(3, max_n),
            step=1,
            key="screener_batch_n",
            help=f"Each stock uses 1 Gemini Pro call. {pro_remaining} remaining today.",
        )
    with col_btn:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        if st.button(
            f"Analyze Top {n} Candidates",
            key="screener_batch_analyze",
            help=f"Will use up to {n} Gemini Pro calls.",
        ):
            top_rows = df.head(int(n)).to_dict(orient="records")
            with st.spinner(f"Running Gemini 2.5 Pro analysis for {n} stocks..."):
                batch_results = run_batch_risk_analysis(top_rows)
            for ticker, result in batch_results.items():
                st.session_state[f"analysis_{ticker}"] = result
            st.rerun()


def _render_disclaimer() -> None:
    """Render the mandatory financial disclaimer."""
    st.markdown("---")
    st.warning(
        "**DISCLAIMER:** This page is for informational and educational purposes only. "
        "It does NOT constitute financial advice, investment recommendations, or an offer "
        "to buy or sell securities. Speculative and micro-cap stocks carry extreme risk "
        "including total loss of principal. Past screening results do not predict future "
        "performance. Always conduct your own due diligence and consult a licensed financial "
        "advisor before making any investment decisions."
    )


# ---------------------------------------------------------------------------
# Page entry
# ---------------------------------------------------------------------------

def main() -> None:
    _render_sidebar()

    page_header(
        "Speculative Stock Screener",
        "Surface small and micro-cap candidates with high upside potential, "
        "then assess risk with Gemini 2.5 Pro.",
    )

    _render_disclaimer()

    # --- Filters ---
    min_cap, max_cap, min_price, max_price, min_volume, sector = _render_filters()

    col_run, col_refresh = st.columns([1, 1])
    with col_run:
        run_btn = st.button("Run Screener", key="screener_run", type="primary")
    with col_refresh:
        refresh_btn = st.button(
            "Force Refresh (bypass cache)",
            key="screener_refresh",
            help="Re-fetch from Yahoo Finance, ignoring the 1-hour cached results.",
        )

    # On first load OR when run button pressed, fetch results
    if "screener_results_df" not in st.session_state or run_btn:
        with st.spinner("Running screener via Yahoo Finance..."):
            df = _cached_screener(min_cap, max_cap, min_price, max_price, min_volume, sector)
        st.session_state["screener_results_df"] = df
        st.session_state["screener_filter_key"] = (
            min_cap, max_cap, min_price, max_price, min_volume, sector
        )

    if refresh_btn:
        with st.spinner("Refreshing from Yahoo Finance (bypassing cache)..."):
            df = run_screener(
                min_market_cap=min_cap,
                max_market_cap=max_cap,
                min_price=min_price,
                max_price=max_price,
                min_volume=min_volume,
                sector=sector,
                force_refresh=True,
            )
        st.session_state["screener_results_df"] = df
        _cached_screener.clear()

    df: pd.DataFrame = st.session_state.get("screener_results_df", pd.DataFrame())

    if df.empty and not run_btn and not refresh_btn:
        st.info("Click **Run Screener** to fetch candidates with the current filters.")
        return

    # --- Results table ---
    _render_results_table(df)

    if df.empty:
        return

    # --- Batch analysis ---
    _render_batch_analysis_section(df)

    # --- Per-stock detail expanders ---
    section_header("Stock Details & Gemini Analysis")

    for _, row in df.iterrows():
        ticker = str(row.get("ticker", ""))
        # Load from session state (set by button click or batch), or try SQLite cache
        analysis = st.session_state.get(f"analysis_{ticker}")
        if analysis is None:
            analysis = get_cached_analysis(ticker)
        _render_stock_detail(row, analysis)


main()
```

---

### Step 5: Update `components/ui.py` to add the Screener page to the sidebar navigation

**File:** `stock-dashboard/components/ui.py`
**Location:** Inside the `_NAV_PAGES` list, which starts at the line containing `_NAV_PAGES = [` and ends at the closing `]`. Currently the last entry is `("Backtest", "pages/11_backtest.py")`.
**Action:** Append one new tuple entry at the end of `_NAV_PAGES`, immediately after the `("Backtest", "pages/11_backtest.py"),` line, before the closing `]`.

Find this exact block in `components/ui.py`:

```python
    ("Technical Analysis", "pages/10_technical_analysis.py"),
    ("Backtest",           "pages/11_backtest.py"),
]
```

Replace it with:

```python
    ("Technical Analysis", "pages/10_technical_analysis.py"),
    ("Backtest",           "pages/11_backtest.py"),
    ("Screener",           "pages/12_screener.py"),
]
```

**Why:** The global sidebar navigation is defined in `_NAV_PAGES` in `components/ui.py`. The built-in Streamlit sidebar nav is hidden by CSS. Every page must be listed here for users to navigate to it. See how every existing page is registered in this list.

---

## Testing Checklist

The implementer must run the app (`streamlit run dashboard.py` from `stock-dashboard/` with venv activated) and verify each of the following:

1. **Sidebar navigation:** Open the app in a browser. Confirm "Screener" appears in the left sidebar navigation list between "Backtest" and the end of the list. Click it — the page should load without errors.

2. **Page layout:** Confirm the page title "Speculative Stock Screener" renders with the subtitle. Confirm the Gemini usage bar appears at the top. Confirm the disclaimer warning box appears.

3. **Initial state:** Before clicking "Run Screener", the page shows "Click Run Screener to fetch candidates with the current filters." No error messages.

4. **Filter controls:** Verify all four filter controls render: Market Cap Tier selectbox, Price Range slider, Min Avg Daily Volume selectbox, Sector selectbox.

5. **Run screener:** Click "Run Screener" with default filters (Micro-cap, $0.50–$20.00, 100K+ volume, All Sectors). A spinner should appear, then the results table renders with multiple rows. The table has columns: Ticker, Name, Price, Day Chg%, Market Cap, Vol (3M Avg), Vol Spike, 52W High Chg, 52W Low Chg, Fwd PE, EPS (TTM), Spec Score. The "Candidates (N found)" section header counts correctly.

6. **Results sorting:** Spec Score column should be sorted descending (highest score first row).

7. **Force refresh:** Click "Force Refresh (bypass cache)" — a spinner appears, results reload. No Python error in the terminal.

8. **Per-stock expanders:** Expand any row in the "Stock Details & Gemini Analysis" section. Confirm the 6-month price chart renders (dark theme, blue line). Confirm the five metric columns render (Price, Market Cap, 52W High Chg, 52W Low Chg, Spec Score).

9. **Analyze with Gemini button:** In an expanded stock detail, if no cached analysis exists, a button "Analyze TICKER with Gemini Pro" should appear. Confirm the button is visible. (Do not click unless you have Pro quota; if you do click, verify a spinner appears, then the analysis renders with Risk Score badge, verdict badge, upside thesis, key risks, and red flags.)

10. **Batch analysis section:** Scroll below the results table to find the "Batch Gemini Analysis" section header. Confirm the number input and "Analyze Top N Candidates" button render. Confirm the help text shows the Pro quota remaining.

11. **Database creation:** Navigate to `stock-dashboard/db/` and confirm `screener.db` has been created. Run in the venv: `python -c "import sqlite3; conn = sqlite3.connect('db/screener.db'); print([r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type=\"table\"').fetchall()])"` — output should include `screener_results` and `screener_analysis`.

12. **Cache TTL hit:** Click "Run Screener" a second time with the same filters within 1 hour. Confirm it returns instantly (cache hit) rather than spinning for several seconds.

13. **Sector filter:** Change the Sector selectbox to "Technology" and click "Run Screener". Results should return only technology stocks. Verify by checking a few tickers.

14. **Empty results:** Set Price Range to $0.10–$0.20 and Min Volume to 1M+ and click "Run Screener". If no results match, the info box "No candidates found with the current filters…" should appear (not an error).

15. **Pro quota gate:** Temporarily set `PRO_DAILY_LIMIT` in `data/gemini_tracker.py` to 0, reload the page, and verify the warning "Gemini Pro daily limit (50) reached" appears instead of the analyze buttons. Restore to 50 after testing.

---

## Rollback Plan

If anything goes wrong, undo all changes in this order:

1. **Delete the new page:** `del stock-dashboard/pages/12_screener.py`
2. **Delete the new data modules:** `del stock-dashboard/data/screener.py` and `del stock-dashboard/data/screener_agent.py`
3. **Delete the schema file:** `del stock-dashboard/db/screener_schema.sql`
4. **Delete the new database (if created):** `del stock-dashboard/db/screener.db`
5. **Revert `components/ui.py`:** Remove the `("Screener", "pages/12_screener.py"),` line from `_NAV_PAGES`.

The app will return to its prior state (pages 1–11) after these five steps. No existing database or module is modified by this plan.
