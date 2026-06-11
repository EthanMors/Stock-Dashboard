# Plan: Twitter/X Social Sentiment Integration

## Overview

This plan adds Twitter/X scraping (via the `twscrape` async library) as a second social sentiment source alongside the existing Reddit WSB feature. The current `pages/8_reddit.py` page is renamed to `pages/8_social.py` with a two-tab layout ("Reddit" | "Twitter") so users can look up sentiment on either platform for any ticker. Two new data modules are created — `data/twitter_fetcher.py` (scraping) and `data/twitter_sentiment.py` (Gemini Flash analysis) — following the exact same patterns as `data/reddit_fetcher.py` and `data/wsb_sentiment.py`. A new SQLite database `db/twitter.db` is introduced with a dedicated schema file `db/twitter_schema.sql`. The `.env.example` and `requirements.txt` are updated with the new dependencies and credentials.

---

## Files to Create

| File | Purpose |
|------|---------|
| `stock-dashboard/db/twitter_schema.sql` | DDL for `twitter_tweets` and `twitter_ticker_summaries` tables |
| `stock-dashboard/data/twitter_fetcher.py` | twscrape async wrapper; `init_twitter_api()`, `search_tweets_for_ticker()` |
| `stock-dashboard/data/twitter_sentiment.py` | Gemini Flash sentiment analysis for tweets; mirrors `wsb_sentiment.py` |

## Files to Modify

| File | Change |
|------|--------|
| `stock-dashboard/pages/8_reddit.py` | Rename to `8_social.py`; update page config; wrap existing Reddit code in a "Reddit" tab; add new "Twitter" tab |
| `stock-dashboard/requirements.txt` | Add `twscrape` and `nest_asyncio` |
| `stock-dashboard/.env.example` | Add `TWITTER_USERNAME`, `TWITTER_PASSWORD`, `TWITTER_EMAIL`, `TWITTER_EMAIL_PASSWORD` |

## Files to Delete

| File | Reason |
|------|--------|
| `stock-dashboard/pages/8_reddit.py` | Superseded by the renamed `8_social.py` |

---

## Prerequisites & Dependencies

### Step 0A: Install new Python packages

Run the following command from inside the `stock-dashboard/` directory with the venv activated:

```powershell
pip install twscrape nest_asyncio
```

This installs:
- `twscrape` — async Twitter scraping library that uses real Twitter accounts (no API key). Manages account login state in a local SQLite DB.
- `nest_asyncio` — allows `asyncio.run()` to be called from within an already-running event loop (needed because Streamlit's internal event loop can conflict with twscrape).

### Step 0B: Verify twscrape package name

The package is installed as `twscrape` and imported as `import twscrape`. Confirm by running `python -c "import twscrape; print(twscrape.__version__)"` after install. If this fails, stop and report the error — do not proceed.

---

## Step-by-Step Implementation

---

### Step 1: Create `db/twitter_schema.sql`

**File to create:** `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\db\twitter_schema.sql`

**Action:** Create this file with the following exact content (no changes, no omissions):

```sql
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
```

**Why:** The schema file defines the two persistent tables for Twitter data. Using `CREATE TABLE IF NOT EXISTS` makes the DDL idempotent — safe to run on every module import. Indexes on `ticker` are required because all queries filter by ticker. This follows the identical pattern used in `db/wsb_schema.sql`.

---

### Step 2: Create `data/twitter_fetcher.py`

**File to create:** `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\data\twitter_fetcher.py`

**Action:** Create this file with the following exact content:

```python
"""
data/twitter_fetcher.py

Fetches tweets for a stock ticker using the twscrape library.
twscrape is async-based; all public functions in this module wrap
async calls with asyncio.run() (with nest_asyncio applied first to
handle Streamlit's internal event loop).
"""

import asyncio
import os
from datetime import datetime, timezone

import nest_asyncio

# Apply nest_asyncio once at import time so asyncio.run() works
# inside Streamlit's already-running event loop.
nest_asyncio.apply()

import twscrape  # noqa: E402 — must come after nest_asyncio.apply()
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACCOUNTS_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "db", "twscrape_accounts.db"
)

_TWITTER_USERNAME       = os.getenv("TWITTER_USERNAME", "")
_TWITTER_PASSWORD       = os.getenv("TWITTER_PASSWORD", "")
_TWITTER_EMAIL          = os.getenv("TWITTER_EMAIL", "")
_TWITTER_EMAIL_PASSWORD = os.getenv("TWITTER_EMAIL_PASSWORD", "")


# ---------------------------------------------------------------------------
# API initialisation
# ---------------------------------------------------------------------------

def init_twitter_api() -> twscrape.API:
    """Return a configured twscrape.API instance.

    On the first call (zero accounts in the DB), adds the account from
    the four TWITTER_* env vars.  Raises ValueError if any required env
    var is missing when no accounts exist yet.
    """
    os.makedirs(os.path.dirname(_ACCOUNTS_DB_PATH), exist_ok=True)
    api = twscrape.API(pool=_ACCOUNTS_DB_PATH)

    async def _ensure_account() -> None:
        accounts = await api.pool.accounts_info()
        if len(accounts) == 0:
            # Validate env vars before attempting to add
            missing = [
                name
                for name, val in [
                    ("TWITTER_USERNAME",       _TWITTER_USERNAME),
                    ("TWITTER_PASSWORD",       _TWITTER_PASSWORD),
                    ("TWITTER_EMAIL",          _TWITTER_EMAIL),
                    ("TWITTER_EMAIL_PASSWORD", _TWITTER_EMAIL_PASSWORD),
                ]
                if not val
            ]
            if missing:
                raise ValueError(
                    f"Twitter account not configured. Missing env vars: {', '.join(missing)}. "
                    "Add them to your .env file."
                )
            await api.pool.add_account(
                username=_TWITTER_USERNAME,
                password=_TWITTER_PASSWORD,
                email=_TWITTER_EMAIL,
                email_password=_TWITTER_EMAIL_PASSWORD,
            )
            await api.pool.login_all()

    asyncio.run(_ensure_account())
    return api


# ---------------------------------------------------------------------------
# Tweet search
# ---------------------------------------------------------------------------

def search_tweets_for_ticker(ticker: str, limit: int = 20) -> list[dict]:
    """Search Twitter for tweets about *ticker* and return a list of dicts.

    Each dict has the keys:
        tweet_id, ticker, text, author_username, author_name,
        reply_count, retweet_count, like_count, quote_count,
        created_at (ISO string), url, fetched_at (ISO string)

    Returns an empty list if the API call fails or no tweets are found.
    """
    ticker_upper = ticker.upper()
    query = f"${ticker_upper} OR {ticker_upper} stock"

    async def _search(q: str, n: int) -> list[dict]:
        api = init_twitter_api()
        results: list[dict] = []
        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            async for tweet in api.search(q, limit=n):
                results.append({
                    "tweet_id":        str(tweet.id),
                    "ticker":          ticker_upper,
                    "text":            tweet.rawContent or "",
                    "author_username": tweet.user.username if tweet.user else "",
                    "author_name":     tweet.user.displayname if tweet.user else "",
                    "reply_count":     tweet.replyCount or 0,
                    "retweet_count":   tweet.retweetCount or 0,
                    "like_count":      tweet.likeCount or 0,
                    "quote_count":     tweet.quoteCount or 0,
                    "created_at":      tweet.date.strftime("%Y-%m-%dT%H:%M:%S") if tweet.date else "",
                    "url":             tweet.url or "",
                    "fetched_at":      fetched_at,
                })
        except Exception:
            pass
        return results

    try:
        return asyncio.run(_search(query, limit))
    except Exception:
        return []
```

**Why:** This module owns the entire twscrape data contract. `nest_asyncio.apply()` is called once at module import time so that `asyncio.run()` works regardless of Streamlit's internal event loop state. `init_twitter_api()` is idempotent — if accounts already exist, it is a no-op. The DB path uses `os.path.dirname(__file__)` relative resolution, matching the pattern from `implementor-skills.md`. The returned list of dicts is shaped to match the `twitter_tweets` schema columns exactly so the page can insert rows directly.

---

### Step 3: Create `data/twitter_sentiment.py`

**File to create:** `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\data\twitter_sentiment.py`

**Action:** Create this file with the following exact content:

```python
"""
data/twitter_sentiment.py

Gemini Flash sentiment analysis for tweets.
Mirrors data/wsb_sentiment.py exactly — same subprocess pattern,
same JSON parsing, same return shapes — but prompts are written
for tweet content rather than Reddit posts.
"""

import json
import re
import subprocess

from data.gemini_tracker import record_call

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SINGLE_PROMPT_TEMPLATE = """\
You are a Financial Sentiment Analyst specializing in Twitter/X social media.
Analyze the sentiment of this tweet regarding the stock ticker {ticker}.

Tweet text:
{text}

Rules:
1. Focus ONLY on sentiment toward {ticker}. Ignore other tickers unless directly compared.
2. Understand Twitter finance slang and abbreviations:
   - "To the moon", "LFG", "BTFD", "HODL", "rockets" = Bullish
   - "Dump it", "Rug pull", "Bags", "Down bad", "puts" = Bearish
   - Watch for sarcasm and irony — context matters.
3. sentiment_score: float from -1.0 (extreme bearish) to 1.0 (extreme bullish). 0.0 = neutral.
4. sentiment_label: exactly "positive", "negative", or "neutral".

Respond ONLY with a JSON object:
{{"sentiment_score": <float>, "sentiment_label": "<label>"}}
"""

_BATCH_PROMPT_TEMPLATE = """\
You are a senior Financial Sentiment Analyst specializing in Twitter/X social media.
Analyze these {ticker} related tweets to form a holistic sentiment picture.

Tweets:
{tweets_block}

Guidelines:
- Weight tweets with higher engagement (likes, retweets) more heavily.
- Identify if the community is unified or split between bulls and bears.
- Note if the activity appears to be organic or coordinated/hype-driven.
- hype_level: integer 1 (no buzz) to 10 (extreme viral hype).

Respond ONLY with a JSON object:
{{
  "sentiment_score": <float -1.0 to 1.0>,
  "sentiment_label": "<positive|negative|neutral>",
  "summary": "<2-4 sentence expert summary of the Twitter sentiment, mentioning specific themes>",
  "hype_level": <integer 1-10>
}}
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_gemini(prompt: str) -> str:
    """Call the Gemini Flash CLI and return stdout.

    Prompt is passed via stdin (not as -p argument) to avoid shell
    interpretation of special characters like < > | in tweet text.
    Returns empty string on any failure.
    """
    try:
        result = subprocess.run(
            ["gemini.cmd", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
        output = result.stdout.strip()
        if output:
            record_call("flash")
        return output
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""


def _parse_json_response(raw: str, require_summary: bool = False) -> dict | None:
    """Extract and parse the first JSON object from a Gemini response string."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    score = float(data.get("sentiment_score", 0.0))
    score = max(-1.0, min(1.0, score))
    label = data.get("sentiment_label", "neutral")
    if label not in ("positive", "negative", "neutral"):
        label = "positive" if score > 0.1 else "negative" if score < -0.1 else "neutral"

    result: dict = {"sentiment_score": score, "sentiment_label": label}
    if require_summary:
        result["summary"]    = str(data.get("summary", "")).strip()
        result["hype_level"] = int(data.get("hype_level", 0))
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_tweet_sentiment(text: str, ticker: str) -> dict:
    """Analyze a single tweet's sentiment toward *ticker*.

    Returns:
        {"sentiment_score": float, "sentiment_label": str}
        Falls back to {"sentiment_score": 0.0, "sentiment_label": "neutral"}
        on any Gemini failure.
    """
    _default = {"sentiment_score": 0.0, "sentiment_label": "neutral"}
    prompt = _SINGLE_PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        text=text[:2000],
    )
    raw = _run_gemini(prompt)
    if not raw:
        return _default
    return _parse_json_response(raw) or _default


def analyze_batch_tweet_sentiment(tweets: list[dict], ticker: str) -> dict:
    """Analyze a batch of tweets together and return aggregate sentiment + summary.

    Args:
        tweets: list of tweet dicts (each must have at least "text",
                "like_count", "retweet_count" keys)
        ticker: stock ticker symbol

    Returns:
        {"sentiment_score": float, "sentiment_label": str,
         "summary": str, "hype_level": int}
        Falls back to all-zero/neutral defaults on Gemini failure.
    """
    _default = {
        "sentiment_score": 0.0,
        "sentiment_label": "neutral",
        "summary":         "",
        "hype_level":      0,
    }
    if not tweets:
        return _default

    lines = []
    for i, tweet in enumerate(tweets, start=1):
        text    = tweet.get("text", "").strip()[:500]
        likes   = tweet.get("like_count", 0)
        rts     = tweet.get("retweet_count", 0)
        author  = tweet.get("author_username", "unknown")
        lines.append(
            f"[Tweet {i}] @{author} | {likes} likes | {rts} retweets\n"
            f"Text: {text if text else '(no text)'}"
        )

    prompt = _BATCH_PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        tweets_block="\n\n".join(lines),
    )
    raw = _run_gemini(prompt)
    if not raw:
        return _default
    return _parse_json_response(raw, require_summary=True) or _default
```

**Why:** This module is a direct mirror of `data/wsb_sentiment.py`. The `_run_gemini()` and `_parse_json_response()` functions are identical in signature and behavior. The two public functions (`analyze_tweet_sentiment` and `analyze_batch_tweet_sentiment`) match the naming pattern of `analyze_sentiment` / `analyze_batch_sentiment` in `wsb_sentiment.py`. Using Flash model is correct here — tweet sentiment is a simple classification task, not a deep reasoning task (per the Flash vs Pro table in `planner-skills.md`).

---

### Step 4: Update `requirements.txt`

**File:** `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\requirements.txt`

**Action:** Open the file. After the last line (`httpx>=0.27.0`), add the following two lines:

```
twscrape>=0.13.0
nest_asyncio>=1.6.0
```

The full file should now end with:

```
# MCP server + integration testing
mcp[cli]>=1.0.0
playwright>=1.45.0
httpx>=0.27.0
twscrape>=0.13.0
nest_asyncio>=1.6.0
```

**Why:** Declares the two new runtime dependencies so they are included in any future `pip install -r requirements.txt` execution.

---

### Step 5: Update `.env.example`

**File:** `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\.env.example`

**Action:** Open the file. After the last existing line (`WEBULL_REGION_ID=us`), append the following block exactly (including the blank line before the comment):

```

# Twitter/X scraping credentials (requires a real free Twitter/X account)
# TWITTER_USERNAME   — your Twitter/X username WITHOUT the @ symbol
# TWITTER_PASSWORD   — your Twitter/X account password
# TWITTER_EMAIL      — the email address linked to your Twitter/X account
#                      (used by twscrape for login verification)
# TWITTER_EMAIL_PASSWORD — password for that email account; used for IMAP
#                          auto-verification if X sends a confirmation code
#                          during headless login (optional but recommended)
TWITTER_USERNAME=your_twitter_username_here
TWITTER_PASSWORD=your_twitter_password_here
TWITTER_EMAIL=your_email@example.com
TWITTER_EMAIL_PASSWORD=your_email_password_here
```

**Why:** Documents the four new required env vars for the Twitter feature. New users who copy `.env.example` → `.env` will see exactly what values to fill in.

---

### Step 6: Delete `pages/8_reddit.py` and create `pages/8_social.py`

**Step 6A — Delete the old file.**

Delete the file at:
`C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\pages\8_reddit.py`

**Step 6B — Create the new file.**

**File to create:** `C:\Users\ethan\Downloads\Stock Dashboard\stock-dashboard\pages\8_social.py`

**Action:** Create this file with the following exact content:

```python
import json
import os
import sqlite3
from datetime import datetime, timezone

import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from data.reddit_fetcher import fetch_top_posts_for_ticker, fetch_daily_top_tickers, TOP_N
from data.wsb_sentiment import analyze_sentiment, analyze_batch_sentiment
from data.twitter_fetcher import search_tweets_for_ticker
from data.twitter_sentiment import analyze_tweet_sentiment, analyze_batch_tweet_sentiment

st.set_page_config(page_title="Social Sentiment", page_icon="🌐", layout="wide")

render_gemini_usage_bar()

# ---------------------------------------------------------------------------
# Reddit DB paths (unchanged from 8_reddit.py)
# ---------------------------------------------------------------------------

_WSB_DB_PATH     = os.path.join(os.path.dirname(__file__), "..", "db", "wsb.db")
_WSB_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "wsb_schema.sql")

# ---------------------------------------------------------------------------
# Twitter DB paths
# ---------------------------------------------------------------------------

_TWITTER_DB_PATH     = os.path.join(os.path.dirname(__file__), "..", "db", "twitter.db")
_TWITTER_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "twitter_schema.sql")

# ---------------------------------------------------------------------------
# Shared UI constants
# ---------------------------------------------------------------------------

_SENTIMENT_COLOR = {"positive": "#00c853", "negative": "#ff1744", "neutral": "#ffd600"}
_SENTIMENT_ICON  = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}


# ===========================================================================
# Reddit DB helpers  (identical to 8_reddit.py — do not modify logic)
# ===========================================================================

def _get_wsb_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_WSB_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_WSB_DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(_WSB_SCHEMA_PATH) as f:
        conn.executescript(f.read())
    return conn


def _get_daily_mentions(date_str: str) -> list[dict]:
    conn = _get_wsb_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM daily_ticker_mentions WHERE date = ? ORDER BY mentions DESC",
            (date_str,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _save_daily_mentions(date_str: str, top_tickers: list[tuple[str, int]]) -> None:
    conn = _get_wsb_conn()
    try:
        for ticker, count in top_tickers:
            conn.execute(
                """INSERT OR REPLACE INTO daily_ticker_mentions (date, ticker, mentions)
                   VALUES (?, ?, ?)""",
                (date_str, ticker, count),
            )
        conn.commit()
    finally:
        conn.close()


def _get_cached_posts(post_ids: list[str]) -> dict[str, dict]:
    if not post_ids:
        return {}
    conn = _get_wsb_conn()
    try:
        placeholders = ",".join("?" * len(post_ids))
        rows = conn.execute(
            f"SELECT * FROM wsb_posts WHERE post_id IN ({placeholders})",
            post_ids,
        ).fetchall()
        return {r["post_id"]: dict(r) for r in rows}
    finally:
        conn.close()


def _save_post(post: dict) -> None:
    conn = _get_wsb_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO wsb_posts
               (post_id, ticker, title, body, author, score, num_comments,
                created_utc, url, permalink, fetched_at,
                sentiment_score, sentiment_label, analyzed_at)
               VALUES
               (:post_id, :ticker, :title, :body, :author, :score, :num_comments,
                :created_utc, :url, :permalink, :fetched_at,
                :sentiment_score, :sentiment_label, :analyzed_at)""",
            post,
        )
        conn.commit()
    finally:
        conn.close()


def _get_cached_wsb_summary(ticker: str) -> dict | None:
    conn = _get_wsb_conn()
    try:
        row = conn.execute(
            "SELECT * FROM wsb_ticker_summaries WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _save_wsb_summary(
    ticker: str,
    subreddits: list[str],
    sentiment_score: float,
    sentiment_label: str,
    summary: str,
    hype_level: int,
    post_ids: list[str],
) -> None:
    conn = _get_wsb_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO wsb_ticker_summaries
               (ticker, subreddits, sentiment_score, sentiment_label,
                summary, hype_level, post_ids, analyzed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker.upper(),
                json.dumps(subreddits),
                sentiment_score,
                sentiment_label,
                summary,
                hype_level,
                json.dumps(post_ids),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# Twitter DB helpers
# ===========================================================================

def _get_twitter_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_TWITTER_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_TWITTER_DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(_TWITTER_SCHEMA_PATH) as f:
        conn.executescript(f.read())
    return conn


def _get_cached_tweets(tweet_ids: list[str]) -> dict[str, dict]:
    if not tweet_ids:
        return {}
    conn = _get_twitter_conn()
    try:
        placeholders = ",".join("?" * len(tweet_ids))
        rows = conn.execute(
            f"SELECT * FROM twitter_tweets WHERE tweet_id IN ({placeholders})",
            tweet_ids,
        ).fetchall()
        return {r["tweet_id"]: dict(r) for r in rows}
    finally:
        conn.close()


def _save_tweet(tweet: dict) -> None:
    conn = _get_twitter_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO twitter_tweets
               (tweet_id, ticker, text, author_username, author_name,
                reply_count, retweet_count, like_count, quote_count,
                created_at, url, fetched_at,
                sentiment_score, sentiment_label, analyzed_at)
               VALUES
               (:tweet_id, :ticker, :text, :author_username, :author_name,
                :reply_count, :retweet_count, :like_count, :quote_count,
                :created_at, :url, :fetched_at,
                :sentiment_score, :sentiment_label, :analyzed_at)""",
            tweet,
        )
        conn.commit()
    finally:
        conn.close()


def _get_cached_twitter_summary(ticker: str) -> dict | None:
    conn = _get_twitter_conn()
    try:
        row = conn.execute(
            "SELECT * FROM twitter_ticker_summaries WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _save_twitter_summary(
    ticker: str,
    sentiment_score: float,
    sentiment_label: str,
    summary: str,
    hype_level: int,
    tweet_ids: list[str],
) -> None:
    conn = _get_twitter_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO twitter_ticker_summaries
               (ticker, sentiment_score, sentiment_label,
                summary, hype_level, tweet_ids, analyzed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker.upper(),
                sentiment_score,
                sentiment_label,
                summary,
                hype_level,
                json.dumps(tweet_ids),
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# Reddit orchestration (identical logic to 8_reddit.py's _load_data)
# ===========================================================================

def _load_reddit_data(ticker: str) -> tuple[list[dict], dict | None]:
    top_posts, subreddits_searched = fetch_top_posts_for_ticker(ticker)
    if not top_posts:
        return [], None

    post_ids = [p["post_id"] for p in top_posts]
    cached_posts = _get_cached_posts(post_ids)

    analyzed_posts: list[dict] = []
    for post in top_posts:
        pid = post["post_id"]
        if pid in cached_posts:
            cached = cached_posts[pid]
            if cached.get("sentiment_score") is None or cached.get("analyzed_at") is None:
                sentiment = analyze_sentiment(
                    title=cached.get("title", ""),
                    body=cached.get("body", ""),
                    ticker=ticker,
                )
                cached["sentiment_score"] = sentiment["sentiment_score"]
                cached["sentiment_label"] = sentiment["sentiment_label"]
                cached["analyzed_at"] = datetime.now(timezone.utc).isoformat()
                _save_post(cached)
            analyzed_posts.append(cached)
        else:
            sentiment = analyze_sentiment(
                title=post["title"],
                body=post["body"],
                ticker=ticker,
            )
            full_post = {
                **post,
                "sentiment_score": sentiment["sentiment_score"],
                "sentiment_label": sentiment["sentiment_label"],
                "analyzed_at":     datetime.now(timezone.utc).isoformat(),
            }
            _save_post(full_post)
            analyzed_posts.append(full_post)

    summary_row = _get_cached_wsb_summary(ticker)

    if summary_row is None:
        batch_result = analyze_batch_sentiment(top_posts, ticker)
        _save_wsb_summary(
            ticker=ticker,
            subreddits=subreddits_searched,
            sentiment_score=batch_result["sentiment_score"],
            sentiment_label=batch_result["sentiment_label"],
            summary=batch_result["summary"],
            hype_level=batch_result["hype_level"],
            post_ids=post_ids,
        )
        summary_row = _get_cached_wsb_summary(ticker)

    return analyzed_posts, summary_row


# ===========================================================================
# Twitter orchestration
# ===========================================================================

def _load_twitter_data(ticker: str) -> tuple[list[dict], dict | None]:
    """Fetch tweets, analyze sentiment per-tweet and in batch, cache in twitter.db.

    Returns:
        (analyzed_tweets, summary_row)
        analyzed_tweets: list of tweet dicts with sentiment_score and sentiment_label filled in
        summary_row: dict from twitter_ticker_summaries, or None if no tweets found
    """
    raw_tweets = search_tweets_for_ticker(ticker, limit=20)
    if not raw_tweets:
        return [], None

    tweet_ids = [t["tweet_id"] for t in raw_tweets]
    cached_tweets = _get_cached_tweets(tweet_ids)

    analyzed_tweets: list[dict] = []
    for tweet in raw_tweets:
        tid = tweet["tweet_id"]
        if tid in cached_tweets:
            cached = cached_tweets[tid]
            if cached.get("sentiment_score") is None or cached.get("analyzed_at") is None:
                sentiment = analyze_tweet_sentiment(
                    text=cached.get("text", ""),
                    ticker=ticker,
                )
                cached["sentiment_score"] = sentiment["sentiment_score"]
                cached["sentiment_label"] = sentiment["sentiment_label"]
                cached["analyzed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                _save_tweet(cached)
            analyzed_tweets.append(cached)
        else:
            sentiment = analyze_tweet_sentiment(
                text=tweet["text"],
                ticker=ticker,
            )
            full_tweet = {
                **tweet,
                "sentiment_score": sentiment["sentiment_score"],
                "sentiment_label": sentiment["sentiment_label"],
                "analyzed_at":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            }
            _save_tweet(full_tweet)
            analyzed_tweets.append(full_tweet)

    summary_row = _get_cached_twitter_summary(ticker)

    if summary_row is None:
        batch_result = analyze_batch_tweet_sentiment(raw_tweets, ticker)
        _save_twitter_summary(
            ticker=ticker,
            sentiment_score=batch_result["sentiment_score"],
            sentiment_label=batch_result["sentiment_label"],
            summary=batch_result["summary"],
            hype_level=batch_result["hype_level"],
            tweet_ids=tweet_ids,
        )
        summary_row = _get_cached_twitter_summary(ticker)

    return analyzed_tweets, summary_row


# ===========================================================================
# Rendering — Reddit (identical to 8_reddit.py)
# ===========================================================================

def _render_reddit_summary_section(ticker: str, summary_row: dict) -> None:
    label   = summary_row.get("sentiment_label", "neutral")
    score   = summary_row.get("sentiment_score", 0.0)
    summary = summary_row.get("summary", "")
    hype    = summary_row.get("hype_level", 0)
    icon    = _SENTIMENT_ICON.get(label, "🟡")
    color   = _SENTIMENT_COLOR.get(label, "#ffd600")

    try:
        subreddits_searched = json.loads(summary_row.get("subreddits", "[]"))
    except (json.JSONDecodeError, TypeError):
        subreddits_searched = []

    subreddit_tags = " · ".join(f"r/{s}" for s in subreddits_searched)

    st.markdown(
        f"""
        <div style="
            background: linear-gradient(135deg, #1e1e2e 0%, #2a2a3e 100%);
            border-left: 5px solid {color};
            border-radius: 8px;
            padding: 20px 24px;
            margin-bottom: 16px;
        ">
            <div style="font-size: 1.4rem; font-weight: 700; color: {color}; margin-bottom: 6px;">
                {icon} {label.capitalize()} Sentiment &mdash; Score: {score:+.2f} &mdash; 🔥 Hype: {hype}/10
            </div>
            <div style="font-size: 0.95rem; color: #ccc; margin-bottom: 12px;">
                {summary if summary else "No summary available."}
            </div>
            <div style="font-size: 0.75rem; color: #888;">
                Subreddits searched: {subreddit_tags}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_post(post: dict, idx: int) -> None:
    score_str = f"{post['score']:,}" if post.get("score") is not None else "—"
    label = post.get("sentiment_label", "neutral")
    icon  = _SENTIMENT_ICON.get(label, "🟡")
    sub   = post.get("subreddit", "")
    sub_tag = f"[r/{sub}] " if sub else ""
    header = f"{icon} {sub_tag}{post.get('title', '')[:80]}"

    with st.expander(header, expanded=(idx == 0)):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Upvotes",   score_str)
        col2.metric("Comments",  post.get("num_comments", "—"))
        col3.metric("Sentiment", label.capitalize())
        col4.metric("Score",     f"{post.get('sentiment_score', 0.0):.2f}")

        if post.get("body"):
            st.markdown(
                post["body"][:800] + ("…" if len(post.get("body", "")) > 800 else "")
            )

        st.markdown(f"[Open on Reddit ↗]({post.get('permalink', '')})")
        st.caption(
            f"Posted by u/{post.get('author', '?')} · "
            f"r/{post.get('subreddit', '?')} · "
            f"Post ID: {post.get('post_id', '')} · "
            f"{'From DB cache' if post.get('analyzed_at') else 'Just analyzed'}"
        )


# ===========================================================================
# Rendering — Twitter
# ===========================================================================

def _render_twitter_summary_section(ticker: str, summary_row: dict) -> None:
    label   = summary_row.get("sentiment_label", "neutral")
    score   = summary_row.get("sentiment_score", 0.0)
    summary = summary_row.get("summary", "")
    hype    = summary_row.get("hype_level", 0)
    icon    = _SENTIMENT_ICON.get(label, "🟡")
    color   = _SENTIMENT_COLOR.get(label, "#ffd600")

    st.markdown(
        f"""
        <div style="
            background: linear-gradient(135deg, #1e1e2e 0%, #2a2a3e 100%);
            border-left: 5px solid {color};
            border-radius: 8px;
            padding: 20px 24px;
            margin-bottom: 16px;
        ">
            <div style="font-size: 1.4rem; font-weight: 700; color: {color}; margin-bottom: 6px;">
                {icon} {label.capitalize()} Sentiment &mdash; Score: {score:+.2f} &mdash; 🔥 Hype: {hype}/10
            </div>
            <div style="font-size: 0.95rem; color: #ccc; margin-bottom: 12px;">
                {summary if summary else "No summary available."}
            </div>
            <div style="font-size: 0.75rem; color: #888;">
                Source: Twitter/X · Powered by Gemini AI
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_tweet(tweet: dict, idx: int) -> None:
    label        = tweet.get("sentiment_label", "neutral")
    icon         = _SENTIMENT_ICON.get(label, "🟡")
    author       = tweet.get("author_username", "unknown")
    text_preview = tweet.get("text", "")[:80]
    header       = f"{icon} @{author}: {text_preview}"

    with st.expander(header, expanded=(idx == 0)):
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Likes",     tweet.get("like_count", 0))
        col2.metric("Retweets",  tweet.get("retweet_count", 0))
        col3.metric("Replies",   tweet.get("reply_count", 0))
        col4.metric("Sentiment", label.capitalize())
        col5.metric("Score",     f"{tweet.get('sentiment_score', 0.0):.2f}")

        full_text = tweet.get("text", "")
        if full_text:
            st.markdown(
                full_text[:800] + ("…" if len(full_text) > 800 else "")
            )

        tweet_url = tweet.get("url", "")
        if tweet_url:
            st.markdown(f"[Open on Twitter/X ↗]({tweet_url})")

        st.caption(
            f"@{tweet.get('author_name', tweet.get('author_username', '?'))} · "
            f"Posted: {tweet.get('created_at', '?')} · "
            f"Tweet ID: {tweet.get('tweet_id', '')} · "
            f"{'From DB cache' if tweet.get('analyzed_at') else 'Just analyzed'}"
        )


# ===========================================================================
# Sidebar
# ===========================================================================

def _render_sidebar() -> None:
    with st.sidebar:
        st.header("About Social Sentiment")
        st.markdown(
            "Analyze crowd sentiment for any stock ticker across two social platforms:\n\n"
            "**Reddit** — Searches multiple subreddits relevant to your ticker for "
            "the top posts by upvote count. Analyzed by Gemini AI for sentiment.\n\n"
            "**Twitter/X** — Searches Twitter for recent tweets mentioning the ticker. "
            "Analyzed by Gemini AI for sentiment and hype level.\n\n"
            "All results are cached in SQLite to avoid redundant AI calls."
        )
        st.markdown("---")
        st.caption("Powered by Reddit public JSON API + twscrape + Google Gemini CLI")


# ===========================================================================
# Tab rendering — Reddit
# ===========================================================================

def _render_reddit_tab() -> None:
    st.subheader("📡 Reddit Sentiment")
    st.markdown("##### Multi-subreddit crowd sentiment · Powered by Gemini AI")
    st.markdown("---")

    # Daily Mentions Section
    st.subheader("🔥 Daily Ticker Mentions (r/WallStreetBets)")
    selected_date = st.date_input("Select Date", datetime.now().date(), key="reddit_date_input")
    date_str = selected_date.isoformat()

    mentions = _get_daily_mentions(date_str)

    if not mentions and selected_date == datetime.now().date():
        with st.spinner("Scraping today's top mentions from r/WallStreetBets..."):
            top_tickers = fetch_daily_top_tickers(limit=100)
            if top_tickers:
                _save_daily_mentions(date_str, top_tickers)
                mentions = _get_daily_mentions(date_str)

    if mentions:
        cols = st.columns(min(len(mentions), 10))
        for i, m in enumerate(mentions[:10]):
            cols[i].metric(m["ticker"], m["mentions"])
    else:
        st.info(f"No mention data available for {date_str}.")

    st.markdown("---")

    ticker_input = st.text_input(
        "Ticker Symbol",
        value=st.session_state.get("active_ticker", ""),
        placeholder="e.g. AAPL",
        key="wsb_ticker_input",
    ).upper().strip()

    if not ticker_input:
        st.info("Enter a ticker symbol above to search Reddit.")
        return

    st.session_state["active_ticker"] = ticker_input

    with st.spinner(f"Loading Reddit posts for {ticker_input} across multiple subreddits…"):
        posts, summary_row = _load_reddit_data(ticker_input)

    if not posts:
        st.warning(
            f"No posts found for **{ticker_input}** on Reddit. "
            "Try a different ticker or check back later."
        )
        return

    if summary_row:
        st.subheader("AI Sentiment Summary")
        _render_reddit_summary_section(ticker_input, summary_row)

    n_pos    = sum(1 for p in posts if p.get("sentiment_label") == "positive")
    n_neg    = sum(1 for p in posts if p.get("sentiment_label") == "negative")
    avg_score = sum(p.get("sentiment_score", 0.0) for p in posts) / len(posts)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Top Posts",  len(posts))
    c2.metric("🟢 Bullish", n_pos)
    c3.metric("🔴 Bearish", n_neg)
    c4.metric("Avg Score",  f"{avg_score:.2f}")
    st.markdown("---")

    st.subheader(f"Top {TOP_N} Posts by Upvotes")
    for i, post in enumerate(posts):
        _render_post(post, i)

    st.markdown("---")
    st.caption(
        "The AI summary is cached per ticker. To force a fresh analysis, "
        "use the button below (this will re-call Gemini)."
    )
    if st.button("Refresh AI Summary", key="wsb_refresh_summary"):
        conn = _get_wsb_conn()
        try:
            conn.execute(
                "DELETE FROM wsb_ticker_summaries WHERE ticker = ?",
                (ticker_input,),
            )
            conn.commit()
        finally:
            conn.close()
        st.rerun()


# ===========================================================================
# Tab rendering — Twitter
# ===========================================================================

def _render_twitter_tab() -> None:
    st.subheader("Twitter/X Sentiment")
    st.markdown("##### Real-time tweet sentiment · Powered by twscrape + Gemini AI")
    st.markdown("---")

    ticker_input = st.text_input(
        "Ticker Symbol",
        value=st.session_state.get("active_ticker", ""),
        placeholder="e.g. AAPL",
        key="twitter_ticker_input",
    ).upper().strip()

    if not ticker_input:
        st.info("Enter a ticker symbol above to search Twitter/X.")
        return

    st.session_state["active_ticker"] = ticker_input

    with st.spinner(f"Loading tweets for {ticker_input}…"):
        try:
            tweets, summary_row = _load_twitter_data(ticker_input)
        except ValueError as exc:
            st.error(
                f"Twitter account not configured: {exc}\n\n"
                "Add your Twitter credentials to the `.env` file and restart the app."
            )
            return
        except Exception as exc:
            st.error(f"Failed to load Twitter data: {exc}")
            return

    if not tweets:
        st.warning(
            f"No tweets found for **{ticker_input}**. "
            "The Twitter account may need to log in again, or try a different ticker."
        )
        return

    if summary_row:
        st.subheader("AI Sentiment Summary")
        _render_twitter_summary_section(ticker_input, summary_row)

    n_pos     = sum(1 for t in tweets if t.get("sentiment_label") == "positive")
    n_neg     = sum(1 for t in tweets if t.get("sentiment_label") == "negative")
    avg_score = sum(t.get("sentiment_score", 0.0) for t in tweets) / len(tweets)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tweets Analyzed", len(tweets))
    c2.metric("🟢 Bullish",      n_pos)
    c3.metric("🔴 Bearish",      n_neg)
    c4.metric("Avg Score",       f"{avg_score:.2f}")
    st.markdown("---")

    st.subheader("Individual Tweets")
    for i, tweet in enumerate(tweets):
        _render_tweet(tweet, i)

    st.markdown("---")
    st.caption(
        "The AI summary is cached per ticker. To force a fresh analysis, "
        "use the button below (this will re-call Gemini and re-fetch tweets)."
    )
    if st.button("Refresh Twitter Analysis", key="twitter_refresh_summary"):
        conn = _get_twitter_conn()
        try:
            conn.execute(
                "DELETE FROM twitter_ticker_summaries WHERE ticker = ?",
                (ticker_input,),
            )
            conn.commit()
        finally:
            conn.close()
        st.rerun()


# ===========================================================================
# Page entry point
# ===========================================================================

def main() -> None:
    _render_sidebar()
    st.title("🌐 Social Sentiment")
    st.markdown("##### Reddit + Twitter/X crowd sentiment · Powered by Gemini AI")
    st.markdown("---")

    reddit_tab, twitter_tab = st.tabs(["Reddit", "Twitter/X"])

    with reddit_tab:
        _render_reddit_tab()

    with twitter_tab:
        _render_twitter_tab()


main()
```

**Why:** This single file replaces `8_reddit.py` and adds the Twitter tab. All Reddit DB helper functions and the `_load_reddit_data` orchestration function are copied verbatim from `8_reddit.py` (with `_get_conn()` renamed to `_get_wsb_conn()` and `_get_cached_summary()` renamed to `_get_cached_wsb_summary()` to avoid a naming conflict with the Twitter equivalents). The Twitter helpers mirror the Reddit ones exactly. The `st.tabs()` layout keeps both features on one page as specified in the task. Widget keys are unique across the two tabs (e.g. `"wsb_ticker_input"` vs `"twitter_ticker_input"`) to prevent Streamlit key collisions. The `ValueError` raised by `init_twitter_api()` when credentials are missing is caught and shown as a user-friendly error message.

---

## Database Changes

### New database: `db/twitter.db`

This file does not exist yet and will be created automatically by `_get_twitter_conn()` when the Twitter tab is first used.

**Tables:**

**`twitter_tweets`**
```
tweet_id        TEXT PRIMARY KEY
ticker          TEXT NOT NULL
text            TEXT
author_username TEXT
author_name     TEXT
reply_count     INTEGER DEFAULT 0
retweet_count   INTEGER DEFAULT 0
like_count      INTEGER DEFAULT 0
quote_count     INTEGER DEFAULT 0
created_at      TEXT                     -- ISO UTC "YYYY-MM-DDTHH:MM:SS"
url             TEXT
fetched_at      TEXT                     -- ISO UTC "YYYY-MM-DDTHH:MM:SS"
sentiment_score REAL
sentiment_label TEXT                     -- "positive"|"negative"|"neutral"
analyzed_at     TEXT
```

**`twitter_ticker_summaries`**
```
ticker          TEXT PRIMARY KEY
sentiment_score REAL
sentiment_label TEXT
summary         TEXT
hype_level      INTEGER DEFAULT 0
tweet_ids       TEXT                     -- JSON array of tweet_id strings
analyzed_at     TEXT NOT NULL            -- ISO UTC "YYYY-MM-DDTHH:MM:SS"
```

### New file managed by twscrape: `db/twscrape_accounts.db`

This file is created and managed entirely by the `twscrape` library. It is referenced only by `init_twitter_api()` via the `_ACCOUNTS_DB_PATH` constant. Do not open or manipulate this file directly.

---

## UI/UX Specification

### Page config
```python
st.set_page_config(page_title="Social Sentiment", page_icon="🌐", layout="wide")
```

### Tab layout
Two tabs created with:
```python
reddit_tab, twitter_tab = st.tabs(["Reddit", "Twitter/X"])
```

### Reddit tab (unchanged from `8_reddit.py`)
- Date input: `st.date_input("Select Date", datetime.now().date(), key="reddit_date_input")`
- Ticker input: `st.text_input(..., key="wsb_ticker_input")`
- Daily mentions: up to 10 `st.metric` widgets in columns
- Summary card: custom HTML via `st.markdown(..., unsafe_allow_html=True)`
- 4 KPI metrics: Top Posts, Bullish count, Bearish count, Avg Score
- Per-post expanders with 4-column metrics (Upvotes, Comments, Sentiment, Score)
- Refresh button: `st.button("Refresh AI Summary", key="wsb_refresh_summary")`

### Twitter tab
- Ticker input: `st.text_input(..., key="twitter_ticker_input")`
- Summary card: identical HTML structure to Reddit summary card
- 4 KPI metrics: Tweets Analyzed, Bullish count, Bearish count, Avg Score
- Per-tweet expanders with 5-column metrics (Likes, Retweets, Replies, Sentiment, Score)
- Tweet text truncated to 800 chars with `"…"` appended if longer
- Clickable link: `[Open on Twitter/X ↗](url)`
- Refresh button: `st.button("Refresh Twitter Analysis", key="twitter_refresh_summary")`

### Sidebar
- Title: "About Social Sentiment"
- Describes both Reddit and Twitter features
- Footer: "Powered by Reddit public JSON API + twscrape + Google Gemini CLI"

---

## Testing Checklist

### 1. Dependency verification
- Run `python -c "import twscrape; print(twscrape.__version__)"` from the venv. Expected: prints a version string. Failure: ImportError means pip install didn't work.
- Run `python -c "import nest_asyncio; print(nest_asyncio.__version__)"`. Expected: version string.

### 2. Schema file verification
- Run `python -c "import sqlite3, os; conn = sqlite3.connect('db/twitter.db'); conn.executescript(open('db/twitter_schema.sql').read()); print('Schema OK')"` from `stock-dashboard/`. Expected: "Schema OK". Failure: any SQL error means the DDL is malformed.

### 3. Twitter fetcher module import
- Run `python -c "from data.twitter_fetcher import search_tweets_for_ticker; print('OK')"` from `stock-dashboard/`. Expected: "OK". Failure: ImportError means a syntax error or missing dependency.

### 4. Twitter sentiment module import
- Run `python -c "from data.twitter_sentiment import analyze_tweet_sentiment, analyze_batch_tweet_sentiment; print('OK')"` from `stock-dashboard/`. Expected: "OK".

### 5. .env credentials check
- Open `stock-dashboard/.env` (not `.env.example`). Verify the four `TWITTER_*` variables are present and filled in with real values. If they are missing, add them before testing the Twitter tab.

### 6. Page navigation
- Start the app: `streamlit run dashboard.py` from `stock-dashboard/`.
- In the sidebar, click "Social Sentiment" (page 8). Expected: the page loads with title "🌐 Social Sentiment" and two tabs labeled "Reddit" and "Twitter/X". Failure: page crashes or shows the old "WSB Reddit" title.

### 7. Reddit tab — existing functionality preserved
- Click the "Reddit" tab. Expected: Daily mentions section with date picker renders. Enter ticker "AAPL" in the ticker input. Expected: spinner appears, posts load, sentiment summary card appears, per-post expanders appear. Failure: any error message or missing content means the Reddit code was not copied correctly.

### 8. Reddit tab — refresh button
- With AAPL loaded, click "Refresh AI Summary". Expected: page reruns and the summary is re-generated (Gemini call fires). Failure: KeyError or sqlite3 error.

### 9. Twitter tab — no credentials error
- With `.env` TWITTER_* variables temporarily commented out, click the "Twitter/X" tab, enter "AAPL", click outside the input. Expected: a red `st.error()` message saying "Twitter account not configured: ..." appears. No crash or unhandled exception. Re-add the credentials before continuing.

### 10. Twitter tab — successful tweet fetch
- With credentials set, click the "Twitter/X" tab, enter "NVDA", click outside the input. Expected: spinner appears ("Loading tweets for NVDA…"), then KPI metrics appear (Tweets Analyzed > 0), summary card appears, per-tweet expanders appear each showing Like/Retweet/Reply counts and a sentiment score. Failure: "No tweets found" warning when credentials are valid indicates a twscrape login issue — check the twscrape_accounts.db login state.

### 11. Twitter tab — caching
- With NVDA results displayed, switch to the Reddit tab and back to Twitter. Enter "NVDA" again. Expected: results load noticeably faster than the first time (served from SQLite cache, no Gemini calls). Failure: full spinner + Gemini calls every time indicates the DB cache is not working.

### 12. Twitter tab — refresh button
- With NVDA results displayed, click "Refresh Twitter Analysis". Expected: the ticker summary row is deleted from `twitter_ticker_summaries`, the page reruns, and a new batch analysis is triggered (Gemini call fires again). Failure: no rerun or sqlite3 error.

### 13. Shared session state
- Enter "TSLA" in the Reddit tab ticker input. Switch to the Twitter/X tab. Expected: the Twitter ticker input also shows "TSLA" (because both read from `st.session_state["active_ticker"]`). Failure: Twitter input is empty.

### 14. Edge case — ticker with no tweets
- Enter an obscure ticker (e.g. "XYZQ") in the Twitter tab. Expected: "No tweets found for XYZQ" warning rendered with `st.warning()`. No crash.

### 15. Edge case — ticker with no Reddit posts
- Enter the same obscure ticker in the Reddit tab. Expected: "No posts found for XYZQ on Reddit" warning. No crash.

---

## Rollback Plan

If something goes wrong and the changes need to be reverted completely:

1. Delete `stock-dashboard/pages/8_social.py`.
2. Restore `stock-dashboard/pages/8_reddit.py` from git: `git checkout HEAD -- stock-dashboard/pages/8_reddit.py`.
3. Delete `stock-dashboard/data/twitter_fetcher.py`.
4. Delete `stock-dashboard/data/twitter_sentiment.py`.
5. Delete `stock-dashboard/db/twitter_schema.sql`.
6. Delete `stock-dashboard/db/twitter.db` (if it was created).
7. Delete `stock-dashboard/db/twscrape_accounts.db` (if it was created).
8. Revert `requirements.txt` changes: `git checkout HEAD -- stock-dashboard/requirements.txt`.
9. Revert `.env.example` changes: `git checkout HEAD -- stock-dashboard/.env.example`.
10. Uninstall new packages (optional): `pip uninstall twscrape nest_asyncio -y`.
