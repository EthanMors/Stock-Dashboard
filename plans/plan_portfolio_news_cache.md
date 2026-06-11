# Plan: Portfolio News Analysis — SQLite Persistence & Staleness Cache

## 1. Task Summary

Currently, `pages/9_portfolio.py` stores Gemini news analysis results only in
`st.session_state.news_results`, which is lost on every page refresh. This plan
adds a two-table SQLite database (`db/portfolio.db`) that persists every analysis
run as an immutable historical record and tracks which article URLs have already
been seen per ticker. On subsequent visits the page checks whether any new
articles have been published since the last run; if not, it renders the stored
result immediately (no Gemini call); if yes, it re-runs analysis and appends a
new row. `st.session_state` is kept as a fast in-session overlay so SQLite is
not queried twice for the same ticker within one session. The historical table
enables future sentiment-over-time charts.

When the plan is fully executed:
- Refreshing the page after analyzing a ticker shows the stored result instantly
  with a "Cached analysis from {timestamp}" caption — no Gemini subprocess is
  launched.
- If new articles exist since the last analysis, Gemini is re-run and the new
  result is appended to the DB.
- Every analysis run is preserved in `portfolio_news_analysis` as its own row;
  old rows are never updated or deleted.
- The "Analyze All Positions" button applies the same DB-cache logic per ticker.

---

## 2. Files Involved

| File | Status | Change |
|------|--------|--------|
| `stock-dashboard/db/portfolio_schema.sql` | **CREATE** | DDL for two new tables |
| `stock-dashboard/data/portfolio_cache.py` | **CREATE** | SQLite helper module |
| `stock-dashboard/pages/9_portfolio.py` | **MODIFY** | Import helper; replace session-only cache with DB-backed logic |

No files are deleted.

---

## 3. Prerequisites & Dependencies

- No new pip packages required. All imports used (`sqlite3`, `json`, `os`,
  `datetime`) are part of the Python standard library and are already available
  in the project's venv.
- No environment variable changes needed.
- `db/portfolio.db` will be auto-created by `init_db()` on first import of
  `data/portfolio_cache.py`. No manual migration step is required.

---

## 4. Step-by-Step Implementation

---

### Step 1: Create `db/portfolio_schema.sql`

**File:** `stock-dashboard/db/portfolio_schema.sql`  
**Action:** Create this file from scratch with the following exact content.

**Why:** The schema file is the authoritative DDL definition, matching the
project's convention where every database has its schema defined in
`db/<name>_schema.sql` (see `db/schema.sql` and `db/wsb_schema.sql`).
`init_db()` in the next step will read and execute this file.

```sql
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
```

---

### Step 2: Create `data/portfolio_cache.py`

**File:** `stock-dashboard/data/portfolio_cache.py`  
**Action:** Create this file from scratch with the following exact content.

**Why:** This module encapsulates all SQLite I/O for the portfolio news cache,
following the exact pattern established by `data/cache.py` — a module-level
`_DB_PATH` constant resolved relative to `__file__`, a private `_get_connection`
function that ensures tables exist, and public helper functions used by the page.
Calling `init_db()` at module import time (the last line of the file) means the
page only needs to `import data.portfolio_cache` and the DB is ready.

```python
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# DB path — db/portfolio.db, resolved relative to this file so it works
# regardless of the cwd when Streamlit launches.
# ---------------------------------------------------------------------------
_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "portfolio.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "portfolio_schema.sql")


def _get_connection() -> sqlite3.Connection:
    """Return a sqlite3.Connection with row_factory set to sqlite3.Row."""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Read db/portfolio_schema.sql and execute it to create tables if absent."""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    conn = _get_connection()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


def get_latest_analysis(ticker: str) -> Optional[dict]:
    """Return the most recently stored analysis row for *ticker*, or None.

    The returned dict has these keys:
        id, ticker, sentiment_score, sentiment_label, summary, impact_level,
        key_themes (list[str] — deserialized from JSON), is_stock_specific (bool),
        article_count, sector, is_fallback (bool), latest_article_url,
        latest_article_published_utc, analyzed_at (str ISO timestamp)
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM   portfolio_news_analysis
            WHERE  ticker = ?
            ORDER  BY analyzed_at DESC
            LIMIT  1
            """,
            (ticker.upper(),),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    data = dict(row)
    # Deserialize key_themes from JSON string back to list
    raw_themes = data.get("key_themes") or "[]"
    try:
        data["key_themes"] = json.loads(raw_themes)
    except (json.JSONDecodeError, TypeError):
        data["key_themes"] = []
    # Normalize booleans
    data["is_stock_specific"] = bool(data.get("is_stock_specific", 1))
    data["is_fallback"] = bool(data.get("is_fallback", 0))
    return data


def save_analysis(
    ticker: str,
    result_dict: dict,
    article_count: int,
    sector: str,
    is_fallback: bool,
    articles: list[dict],
) -> None:
    """Insert a new analysis row and upsert all article URLs for *ticker*.

    Parameters
    ----------
    ticker       : Stock ticker (will be uppercased).
    result_dict  : Dict returned by analyze_articles(); must contain keys:
                   sentiment_score, sentiment_label, summary, impact_level,
                   key_themes, is_stock_specific.
    article_count: Total number of articles returned by fetch_news() (before
                   truncation to _MAX_ARTICLES_TO_SCRAPE).
    sector       : Sector string from get_sector_info(), or "" if not applicable.
    is_fallback  : True when sector-level articles were used as a supplement.
    articles     : The raw list of article dicts from fetch_news().  Each dict
                   must have at minimum an "article_url" key; "published_utc"
                   and "title" are used when present.
    """
    ticker = ticker.upper()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # Determine the most recently published article for the staleness check
    latest_url: str = ""
    latest_pub: Optional[float] = None
    for art in articles:
        pub = art.get("published_utc")
        url = art.get("article_url", "")
        if pub is not None and url:
            try:
                pub_float = float(pub)
            except (TypeError, ValueError):
                continue
            if latest_pub is None or pub_float > latest_pub:
                latest_pub = pub_float
                latest_url = url

    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO portfolio_news_analysis (
                ticker, sentiment_score, sentiment_label, summary,
                impact_level, key_themes, is_stock_specific,
                article_count, sector, is_fallback,
                latest_article_url, latest_article_published_utc, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                result_dict.get("sentiment_score"),
                result_dict.get("sentiment_label"),
                result_dict.get("summary"),
                result_dict.get("impact_level"),
                json.dumps(result_dict.get("key_themes", [])),
                1 if result_dict.get("is_stock_specific", True) else 0,
                article_count,
                sector,
                1 if is_fallback else 0,
                latest_url,
                latest_pub,
                now_iso,
            ),
        )

        # Upsert every article URL so future staleness checks work
        for art in articles:
            url = art.get("article_url", "")
            if not url:
                continue
            pub = art.get("published_utc")
            try:
                pub_float = float(pub) if pub is not None else None
            except (TypeError, ValueError):
                pub_float = None
            title = art.get("title", "")
            conn.execute(
                """
                INSERT INTO portfolio_news_seen_articles (ticker, article_url, published_utc, title)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker, article_url) DO NOTHING
                """,
                (ticker, url, pub_float, title),
            )

        conn.commit()
    finally:
        conn.close()


def has_new_articles(ticker: str, articles: list[dict]) -> bool:
    """Return True if any article_url in *articles* is not yet in the DB for *ticker*.

    Also returns True when the seen-articles table has no rows for *ticker* at all
    (i.e., this ticker has never been analyzed).

    Parameters
    ----------
    ticker   : Stock ticker (will be uppercased).
    articles : The raw article list from fetch_news().
    """
    ticker = ticker.upper()
    conn = _get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM portfolio_news_seen_articles WHERE ticker = ?",
            (ticker,),
        ).fetchone()[0]
        if count == 0:
            return True

        for art in articles:
            url = art.get("article_url", "")
            if not url:
                continue
            exists = conn.execute(
                """
                SELECT 1 FROM portfolio_news_seen_articles
                WHERE  ticker = ? AND article_url = ?
                LIMIT  1
                """,
                (ticker, url),
            ).fetchone()
            if exists is None:
                return True
    finally:
        conn.close()

    return False


def get_sentiment_history(ticker: str) -> list[dict]:
    """Return all analysis rows for *ticker* ordered by analyzed_at ASC.

    Each dict contains at minimum: analyzed_at (str), sentiment_score (float).
    All other columns are also included for completeness.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM   portfolio_news_analysis
            WHERE  ticker = ?
            ORDER  BY analyzed_at ASC
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        data = dict(row)
        raw_themes = data.get("key_themes") or "[]"
        try:
            data["key_themes"] = json.loads(raw_themes)
        except (json.JSONDecodeError, TypeError):
            data["key_themes"] = []
        data["is_stock_specific"] = bool(data.get("is_stock_specific", 1))
        data["is_fallback"] = bool(data.get("is_fallback", 0))
        result.append(data)
    return result


# ---------------------------------------------------------------------------
# Initialize DB tables on import
# ---------------------------------------------------------------------------
init_db()
```

---

### Step 3: Add the import of `portfolio_cache` to `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`  
**Location:** Lines 1–16 (the existing import block). Insert one new import line
immediately after the existing `from data.news_analyzer import analyze_articles, get_sector_info` line (currently line 15).

**Action:** Replace the block:

```python
from data.news_fetcher import fetch_news, scrape_article
from data.news_analyzer import analyze_articles, get_sector_info
```

with:

```python
from data.news_fetcher import fetch_news, scrape_article
from data.news_analyzer import analyze_articles, get_sector_info
from data.portfolio_cache import (
    get_latest_analysis,
    save_analysis,
    has_new_articles,
    get_sentiment_history,
)
```

**Why:** The page needs to call all four helper functions from `portfolio_cache`.
Importing at the top of the file also triggers `init_db()` (called at module
import time in `portfolio_cache.py`), ensuring the DB tables exist before any
user interaction.

---

### Step 4: Add the `_run_analysis_for_ticker` helper function to `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`  
**Location:** After the `_render_analysis` function (which ends at line 106,
closing `)`), and before the `# Account loading` section comment at line 109.
Insert the new function between lines 107 and 109 (i.e., after the blank line
following `_render_analysis`'s closing paren).

**Action:** Insert the following new function block:

```python

def _run_analysis_for_ticker(ticker: str) -> dict:
    """Fetch articles, scrape, and call Gemini for *ticker*.

    Returns the session-state cache dict:
    {
        "result": <analyze_articles() return dict>,
        "article_count": int,
        "sector": str,
        "is_fallback": bool,
        "analyzed_at": str ISO timestamp,
        "from_db": bool,
    }
    Also persists the result to the SQLite DB via save_analysis().
    """
    articles = fetch_news(ticker, limit=20)
    scraped: list[dict] = []
    for art in articles[:_MAX_ARTICLES_TO_SCRAPE]:
        url = art.get("article_url", "")
        content = scrape_article(url) if url else ""
        if content.startswith(("HTTP", "Access denied", "Request failed", "Could not")):
            content = art.get("description", "")
        scraped.append({
            "title": art.get("title", ""),
            "content": content,
            "source": (
                art.get("publisher", {}).get("name", "")
                if isinstance(art.get("publisher"), dict)
                else str(art.get("publisher", ""))
            ),
        })

    is_fallback = False
    sector = ""
    if len(scraped) < _MIN_TICKER_ARTICLES:
        sector, etf = get_sector_info(ticker)
        if etf:
            sector_articles = fetch_news(etf, limit=10)
            for art in sector_articles[:_MAX_ARTICLES_TO_SCRAPE]:
                url = art.get("article_url", "")
                content = scrape_article(url) if url else ""
                if content.startswith(("HTTP", "Access denied", "Request failed", "Could not")):
                    content = art.get("description", "")
                scraped.append({
                    "title": art.get("title", ""),
                    "content": content,
                    "source": (
                        art.get("publisher", {}).get("name", "")
                        if isinstance(art.get("publisher"), dict)
                        else str(art.get("publisher", ""))
                    ),
                })
            is_fallback = True

    result = analyze_articles(scraped, ticker, sector=sector, is_sector_fallback=is_fallback)
    save_analysis(
        ticker=ticker,
        result_dict=result,
        article_count=len(articles),
        sector=sector,
        is_fallback=is_fallback,
        articles=articles,
    )
    from datetime import datetime, timezone
    analyzed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "result": result,
        "article_count": len(articles),
        "sector": sector,
        "is_fallback": is_fallback,
        "analyzed_at": analyzed_at,
        "from_db": False,
    }
```

**Why:** Extracting the scrape+analyze+save logic into a single helper eliminates
the duplicated code that currently exists in the single-ticker block (lines
238–276) and the "Analyze All" loop (lines 298–335). Both callers will now invoke
`_run_analysis_for_ticker(ticker)` instead of repeating the same 30-line
pattern.

---

### Step 5: Add the `_load_or_analyze` helper function to `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`  
**Location:** Immediately after the closing line of `_run_analysis_for_ticker`
added in Step 4 (insert after the last `}` of that function's return statement,
before the `# Account loading` section comment).

**Action:** Insert this function:

```python

def _load_or_analyze(ticker: str) -> dict:
    """Return a session-cache dict for *ticker*, using DB cache when valid.

    Decision tree:
    1. Check st.session_state.news_results — return immediately if present
       (in-session memoization; no DB hit needed).
    2. Query DB for latest analysis via get_latest_analysis().
    3. Fetch fresh article list (fast — already @st.cache_data(ttl=900)).
    4. Call has_new_articles() to detect staleness.
    5a. If NOT stale: populate session_state from DB row and return.
    5b. If stale (or no DB row): call _run_analysis_for_ticker() which
        runs Gemini and saves the new result to DB.
    """
    ticker = ticker.upper()

    # Layer 1: in-session memoization
    if ticker in st.session_state.news_results:
        return st.session_state.news_results[ticker]

    # Layer 2: DB lookup
    cached_db = get_latest_analysis(ticker)
    articles = fetch_news(ticker, limit=20)

    if cached_db is not None and not has_new_articles(ticker, articles):
        # No new articles — serve DB result
        session_entry = {
            "result": {
                "sentiment_score": cached_db["sentiment_score"],
                "sentiment_label": cached_db["sentiment_label"],
                "summary": cached_db["summary"],
                "impact_level": cached_db["impact_level"],
                "key_themes": cached_db["key_themes"],
                "is_stock_specific": cached_db["is_stock_specific"],
            },
            "article_count": cached_db["article_count"],
            "sector": cached_db["sector"] or "",
            "is_fallback": cached_db["is_fallback"],
            "analyzed_at": cached_db["analyzed_at"],
            "from_db": True,
        }
        st.session_state.news_results[ticker] = session_entry
        return session_entry

    # Layer 3: new articles exist (or first-ever analysis) — run Gemini
    session_entry = _run_analysis_for_ticker(ticker)
    st.session_state.news_results[ticker] = session_entry
    return session_entry
```

**Why:** Centralises the three-tier lookup (session state → DB → Gemini) in one
place so neither the single-ticker section nor the "Analyze All" loop need to
duplicate this logic. The function returns a dict whose keys are identical to
those already expected by `_render_analysis` and the display block.

---

### Step 6: Replace the single-ticker analysis block in `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`  
**Location:** Lines 234–287 — the entire block from
`# Single ticker analysis` through `_render_analysis(selected_ticker, cached["result"])`.

**Action:** Replace the entire block (lines 234–287) with the following:

```python
# Single ticker analysis
if selected_ticker and selected_ticker != "— Select a stock —":
    if st.button(f"Run News Analysis for {selected_ticker}") or selected_ticker in st.session_state.news_results:
        with st.spinner(f"Fetching and analyzing news for {selected_ticker}…"):
            cached = _load_or_analyze(selected_ticker)

        article_count = cached["article_count"]
        sector = cached["sector"]
        is_fallback = cached["is_fallback"]
        from_db = cached.get("from_db", False)
        analyzed_at = cached.get("analyzed_at", "")

        note = f"{article_count} articles found"
        if is_fallback and sector:
            note += f" — supplemented with {sector} sector news"
        if from_db and analyzed_at:
            note += f" · Cached analysis from {analyzed_at} UTC"
        st.caption(note)
        _render_analysis(selected_ticker, cached["result"])
```

**Why:** The new block delegates entirely to `_load_or_analyze`, which handles
all three cache layers. The spinner is shown regardless of whether analysis runs
or a DB result is loaded (the DB lookup is fast, so the spinner will disappear
immediately for cached results). The `from_db` flag drives the "Cached analysis
from…" caption so the user can see whether a fresh Gemini call was made.

---

### Step 7: Replace the "Analyze All Positions" loop in `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`  
**Location:** Lines 289–337 — the entire block from `# Analyze all positions`
through `progress.empty()`.

**Action:** Replace the entire block (lines 289–337) with the following:

```python
# Analyze all positions
if analyze_all:
    progress = st.progress(0, text="Starting analysis…")
    for idx, ticker in enumerate(tickers):
        if ticker in st.session_state.news_results and not st.session_state.news_results[ticker].get("from_db", False):
            # Already analyzed fresh this session — skip without DB overhead
            progress.progress((idx + 1) / len(tickers), text=f"Skipping {ticker} (session cache)")
            continue

        progress.progress(idx / len(tickers), text=f"Checking cache for {ticker}…")
        with st.spinner(f"Loading {ticker}…"):
            entry = _load_or_analyze(ticker)
        if entry.get("from_db", False):
            progress.progress((idx + 1) / len(tickers), text=f"Using DB cache: {ticker}")
        else:
            progress.progress((idx + 1) / len(tickers), text=f"Done (Gemini): {ticker}")
            time.sleep(2)

    progress.empty()
```

**Why:** The loop now delegates to `_load_or_analyze` per ticker, which
transparently serves DB-cached results for tickers with no new articles and only
calls Gemini when genuinely needed. The `time.sleep(2)` is preserved but moved
inside the `not from_db` branch so it only fires after an actual Gemini call,
not after a DB hit.

---

### Step 8: Update the "Display all cached results" block in `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`  
**Location:** Lines 341–354 — the `# Display all cached results` block through
`st.rerun()`.

**Action:** Replace the entire block (lines 341–354) with the following:

```python
# Display all cached results
if st.session_state.news_results:
    st.markdown("### Analysis Results")
    for ticker, cached in st.session_state.news_results.items():
        sentiment_label = cached["result"]["sentiment_label"].upper()
        sentiment_score = cached["result"]["sentiment_score"]
        from_db = cached.get("from_db", False)
        analyzed_at = cached.get("analyzed_at", "")
        cache_tag = " [cached]" if from_db else ""
        with st.expander(
            f"**{ticker}** — {sentiment_label} ({sentiment_score:+.2f}){cache_tag}",
            expanded=False,
        ):
            note = f"{cached['article_count']} articles"
            if cached["is_fallback"] and cached["sector"]:
                note += f" · {cached['sector']} sector fallback"
            if from_db and analyzed_at:
                note += f" · Cached from {analyzed_at} UTC"
            st.caption(note)
            _render_analysis(ticker, cached["result"])

    if st.button("Clear All Results"):
        st.session_state.news_results = {}
        st.rerun()
```

**Why:** Adds a `[cached]` tag in the expander header and a "Cached from…"
caption line so users can immediately see which results came from the DB without
having to re-analyze.

---

## 5. Database Changes

### Schema (already specified in Step 1)

The full DDL is written to `db/portfolio_schema.sql` in Step 1. Tables are
created with `CREATE TABLE IF NOT EXISTS` so running `init_db()` multiple times
is always safe.

### Migration

No existing DB needs to be altered. `db/portfolio.db` does not exist yet; it
will be created automatically on first app load after Step 2 is implemented.

### Rollback

To fully undo the database changes:
1. Delete `stock-dashboard/db/portfolio.db` (if it was created).
2. Delete `stock-dashboard/db/portfolio_schema.sql`.
3. Delete `stock-dashboard/data/portfolio_cache.py`.
4. Revert `stock-dashboard/pages/9_portfolio.py` to its original state via git:
   ```
   git checkout stock-dashboard/pages/9_portfolio.py
   ```

---

## 6. UI/UX Specification

### Caption line (single-ticker view, after Step 6)

- When result comes from DB (`from_db == True`) and `analyzed_at` is set:
  `"{article_count} articles found · Cached analysis from {analyzed_at} UTC"`
- When sector fallback was used:
  `"{article_count} articles found — supplemented with {sector} sector news · Cached analysis from {analyzed_at} UTC"`
- When freshly analyzed (Gemini was called, `from_db == False`):
  `"{article_count} articles found"` (or with sector note; no "Cached" suffix)

### Expander label (display-all block, after Step 8)

- Fresh result: `**{TICKER}** — POSITIVE (+0.42)`
- DB-cached result: `**{TICKER}** — POSITIVE (+0.42) [cached]`

### Progress bar text (Analyze All, after Step 7)

- Session-cache hit: `Skipping {TICKER} (session cache)`
- DB-cache hit: `Using DB cache: {TICKER}`
- Gemini run completed: `Done (Gemini): {TICKER}`

No new Streamlit widgets are added. No layout changes beyond the caption/label
modifications described above.

---

## 7. Testing Checklist

Execute all tests from the `stock-dashboard/` directory with the venv activated.

1. **DB auto-creation on first load**
   - Delete `db/portfolio.db` if it exists.
   - Run `streamlit run dashboard.py` and navigate to the Portfolio page.
   - Expected: No error occurs; `db/portfolio.db` is created in the `db/`
     folder.
   - Failure indicator: FileNotFoundError or SQLite OperationalError in the
     terminal.

2. **Schema correctness**
   - After the app loads (test 1 passed), run in a Python shell:
     ```python
     import sqlite3
     conn = sqlite3.connect("db/portfolio.db")
     print(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
     ```
   - Expected: `[('portfolio_news_analysis',), ('portfolio_news_seen_articles',)]`
     (indexes are also present but `.fetchall()` returns only tables by default).
   - Failure indicator: Missing table names.

3. **First analysis — Gemini is called, result is stored**
   - On the Portfolio page, select a ticker (e.g., AAPL) from the dropdown and
     click "Run News Analysis for AAPL".
   - Expected: Spinner appears, Gemini runs (takes ~10–30 s), analysis card
     renders. Caption does NOT contain "Cached". The `portfolio_news_analysis`
     table has one new row for AAPL. `portfolio_news_seen_articles` has rows for
     AAPL.
   - Failure indicator: Error in terminal; no DB rows inserted; caption shows
     "Cached".

4. **Same session — second click uses session_state (no DB hit)**
   - Without refreshing, click "Run News Analysis for AAPL" again (or reselect
     AAPL from the dropdown so the `selected_ticker in st.session_state` branch
     fires).
   - Expected: Result renders instantly (no spinner delay). Caption is unchanged
     from test 3 result.
   - Failure indicator: Another Gemini call fires (visible in terminal); long
     spinner.

5. **Page refresh — DB cache used when no new articles**
   - Refresh the browser tab (Ctrl+R) to clear `st.session_state`.
   - Select AAPL again from the dropdown and click the analyze button.
   - Expected: Result loads quickly (< 2 s); caption contains "Cached analysis
     from {timestamp} UTC"; no Gemini subprocess is launched (no new line in
     `webull_trade_sdk.log` or Gemini usage tracker).
   - Failure indicator: Spinner takes > 5 s; caption has no "Cached"; usage
     counter increments.

6. **Staleness detection — new article triggers re-analysis**
   - Manually insert a fake article URL into `portfolio_news_seen_articles` to
     simulate a prior state, then delete it (or use a ticker that genuinely has a
     new article not yet in the DB). Alternatively: delete all rows for a ticker
     from `portfolio_news_seen_articles` while leaving its `portfolio_news_analysis`
     rows intact, then click Analyze.
   - Expected: `has_new_articles()` returns True; Gemini is re-called; a new row
     is appended to `portfolio_news_analysis` (row count increases by 1); old
     rows are still present.
   - Failure indicator: Old result served; row count unchanged.

7. **Historical records — no overwrite**
   - After tests 3 and 6, query the DB:
     ```python
     import sqlite3
     conn = sqlite3.connect("db/portfolio.db")
     rows = conn.execute("SELECT id, analyzed_at FROM portfolio_news_analysis WHERE ticker='AAPL'").fetchall()
     print(rows)
     ```
   - Expected: At least 2 rows with different `analyzed_at` timestamps.
   - Failure indicator: Only 1 row (old row was overwritten).

8. **Analyze All Positions — DB cache skips Gemini for stale-free tickers**
   - Analyze at least 2 tickers individually first (so they are in the DB).
   - Refresh the browser tab.
   - Click "Analyze All Positions".
   - Expected: Progress bar shows "Using DB cache: {TICKER}" for the pre-analyzed
     tickers and only calls Gemini for tickers not yet in the DB. The `time.sleep(2)`
     pause only occurs after actual Gemini calls.
   - Failure indicator: All tickers trigger Gemini calls; progress bar always
     shows "Done (Gemini)".

9. **get_sentiment_history returns data in order**
   - After test 7, in a Python shell:
     ```python
     from data.portfolio_cache import get_sentiment_history
     rows = get_sentiment_history("AAPL")
     print([(r["analyzed_at"], r["sentiment_score"]) for r in rows])
     ```
   - Expected: List of tuples, timestamps ascending, no exception.
   - Failure indicator: Empty list; exception; wrong order.

10. **Clear All Results button**
    - After some tickers are displayed in the "Analysis Results" section, click
      "Clear All Results".
    - Expected: The results section disappears; `st.session_state.news_results`
      is empty. DB rows are unaffected (verify by querying SQLite — row count
      unchanged).
    - Failure indicator: Page errors; DB rows deleted.

11. **Missing API key (MASSIVE_API_KEY not set)**
    - Temporarily remove `MASSIVE_API_KEY` from `.env` and restart the app.
    - Expected: `fetch_news()` returns `[]`; `has_new_articles()` returns True
      (no articles → no seen URLs → defaults to True); if a DB record exists from
      a prior run, Gemini will be called again. No crash.
    - Failure indicator: Exception in `portfolio_cache.py` or page crash.

---

## 8. Rollback Plan

If the implementation causes a critical failure:

1. **Revert `pages/9_portfolio.py`** to its original state:
   ```powershell
   git checkout stock-dashboard/pages/9_portfolio.py
   ```

2. **Delete the new files** (they are not tracked by git yet):
   ```powershell
   Remove-Item "stock-dashboard\data\portfolio_cache.py"
   Remove-Item "stock-dashboard\db\portfolio_schema.sql"
   ```

3. **Delete the DB** (optional — it has no user data from before this feature):
   ```powershell
   Remove-Item "stock-dashboard\db\portfolio.db"
   ```

4. Restart Streamlit. The app will return to exactly the pre-implementation
   state, with `st.session_state`-only news caching.
