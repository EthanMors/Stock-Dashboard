# Reddit / WallStreetBets Tab — Implementation Plan

## Overview

Add a new Streamlit page (`6_reddit.py`) that searches r/WallStreetBets for a user-supplied
ticker, returns the 10 most relevant posts, runs Gemini sentiment analysis on each one, and
persists everything in a SQLite database. Posts already in the database are served from cache
instead of being re-fetched and re-analyzed.

---

## Architecture

```
frontend/pages/6_reddit.py        ← Streamlit UI page
backend/data/reddit_fetcher.py    ← Reddit JSON API calls
backend/data/wsb_sentiment.py     ← Gemini sentiment analysis
backend/db/wsb.db                 ← SQLite database (auto-created at runtime)
backend/db/wsb_schema.sql         ← Table definitions
```

---

## Step 1 — Add environment variables

Edit `.env` and `.env.example`. Add these two lines at the bottom:

```
REDDIT_USERNAME=your_reddit_username
GEMINI_API_KEY=your_gemini_api_key
```

- `REDDIT_USERNAME` is any Reddit username; it is only used to build the User-Agent header
  required by Reddit's API policy. It does not need to be authenticated.
- `GEMINI_API_KEY` is your Google AI Studio key.

---

## Step 2 — Add dependencies to requirements.txt

Append these two lines to `requirements.txt`:

```
google-generativeai
python-dotenv
```

`python-dotenv` may already be present; add it only if it is missing.

---

## Step 3 — Create `backend/db/wsb_schema.sql`

Create a new file at `backend/db/wsb_schema.sql` with this exact content:

```sql
CREATE TABLE IF NOT EXISTS wsb_posts (
    post_id         TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    title           TEXT,
    body            TEXT,
    author          TEXT,
    score           INTEGER DEFAULT 0,
    num_comments    INTEGER DEFAULT 0,
    created_utc     REAL,
    url             TEXT,
    permalink       TEXT,
    fetched_at      TEXT,
    sentiment_score REAL,
    sentiment_label TEXT,
    analyzed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_wsb_ticker ON wsb_posts (ticker);
```

Column notes:
- `post_id` — Reddit's unique post ID (e.g. `"1abc23"`), used as primary key to deduplicate
- `ticker` — Uppercased ticker searched when this post was fetched (e.g. `"AAPL"`)
- `body` — Reddit `selftext` field; may be empty string for link posts
- `sentiment_score` — Float from -1.0 (bearish) to 1.0 (bullish); NULL until analyzed
- `sentiment_label` — One of `"positive"`, `"negative"`, `"neutral"`; NULL until analyzed
- `analyzed_at` — ISO-8601 timestamp of when Gemini analysis was run

---

## Step 4 — Create `backend/data/reddit_fetcher.py`

Create a new file at `backend/data/reddit_fetcher.py`.

### 4a. Imports and constants

```python
import os
import requests
from datetime import datetime, timezone

_REDDIT_USERNAME = os.getenv("REDDIT_USERNAME", "stock_dashboard_user")
_HEADERS = {
    "User-Agent": f"python:StockDashboardWSB:v1.0.0 (by /u/{_REDDIT_USERNAME})"
}
_BASE_URL = "https://www.reddit.com"
_SUBREDDIT = "wallstreetbets"
_TIMEOUT = 10
```

### 4b. Function `fetch_wsb_posts(ticker: str, limit: int = 10) -> list[dict]`

This function searches r/wallstreetbets for posts mentioning the ticker and returns up to
`limit` posts as plain dicts.

URL to call:
```
https://www.reddit.com/r/wallstreetbets/search.json
  ?q={ticker}
  &restrict_sr=1
  &sort=top
  &t=month
  &limit={limit}
```

- `restrict_sr=1` restricts results to r/wallstreetbets only
- `sort=top` returns most-upvoted matches
- `t=month` looks within the past month

Request code:

```python
def fetch_wsb_posts(ticker: str, limit: int = 10) -> list[dict]:
    url = (
        f"{_BASE_URL}/r/{_SUBREDDIT}/search.json"
        f"?q={ticker}&restrict_sr=1&sort=top&t=month&limit={limit}"
    )
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        children = resp.json()["data"]["children"]
    except Exception:
        return []

    posts = []
    for child in children:
        d = child.get("data", {})
        post_id = d.get("id")
        if not post_id:
            continue
        posts.append({
            "post_id":      post_id,
            "ticker":       ticker.upper(),
            "title":        d.get("title", ""),
            "body":         d.get("selftext", ""),
            "author":       d.get("author", ""),
            "score":        d.get("score", 0),
            "num_comments": d.get("num_comments", 0),
            "created_utc":  d.get("created_utc"),
            "url":          d.get("url", ""),
            "permalink":    _BASE_URL + d.get("permalink", ""),
            "fetched_at":   datetime.now(timezone.utc).isoformat(),
        })
    return posts
```

Return value: list of dicts with exactly the keys shown above.
Return empty list on any error; never raise.

---

## Step 5 — Create `backend/data/wsb_sentiment.py`

Create a new file at `backend/data/wsb_sentiment.py`.

### 5a. Imports and setup

```python
import os
import json
import re
from datetime import datetime, timezone

import google.generativeai as genai

_API_KEY = os.getenv("GEMINI_API_KEY", "")
if _API_KEY:
    genai.configure(api_key=_API_KEY)

_MODEL_NAME = "gemini-1.5-flash"
_PROMPT_TEMPLATE = """\
You are a financial sentiment analyzer for Reddit posts about stocks.
Analyze the sentiment of this Reddit post specifically regarding the stock ticker {ticker}.

Post Title: {title}
Post Body: {body}

Respond ONLY with a JSON object in this exact format with no extra text:
{{"sentiment_score": <float between -1.0 and 1.0>, "sentiment_label": "<positive|negative|neutral>"}}

Rules:
- sentiment_score: -1.0 is extremely bearish, 0.0 is neutral, 1.0 is extremely bullish
- sentiment_label: must be exactly one of "positive", "negative", or "neutral"
- Base the analysis only on how the post discusses {ticker}, not other tickers mentioned
"""
```

### 5b. Function `analyze_sentiment(title: str, body: str, ticker: str) -> dict`

```python
def analyze_sentiment(title: str, body: str, ticker: str) -> dict:
    """Return {"sentiment_score": float, "sentiment_label": str}.
    Returns neutral defaults on any failure."""
    _default = {"sentiment_score": 0.0, "sentiment_label": "neutral"}

    if not _API_KEY:
        return _default

    prompt = _PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        title=title,
        body=body[:2000],  # cap body length to stay within token limits
    )

    try:
        model = genai.GenerativeModel(_MODEL_NAME)
        response = model.generate_content(prompt)
        raw = response.text.strip()

        # Extract JSON object from the response using regex
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return _default

        data = json.loads(match.group())
        score = float(data.get("sentiment_score", 0.0))
        score = max(-1.0, min(1.0, score))  # clamp to valid range

        label = data.get("sentiment_label", "neutral")
        if label not in ("positive", "negative", "neutral"):
            # Infer label from score if the model returned an unexpected string
            if score > 0.1:
                label = "positive"
            elif score < -0.1:
                label = "negative"
            else:
                label = "neutral"

        return {"sentiment_score": score, "sentiment_label": label}

    except Exception:
        return _default
```

Return value: always returns a dict with `"sentiment_score"` (float) and
`"sentiment_label"` (str). Never raises.

---

## Step 6 — Create `frontend/pages/6_reddit.py`

Create a new file at `frontend/pages/6_reddit.py`.

This file handles the database, the orchestration logic, and the Streamlit UI all in one place
(consistent with the pattern used by the other page files in this project).

### 6a. Imports

```python
import os
import sqlite3
from datetime import datetime, timezone

import streamlit as st

from backend.data.reddit_fetcher import fetch_wsb_posts
from backend.data.wsb_sentiment import analyze_sentiment, _API_KEY

st.set_page_config(page_title="WSB Reddit", page_icon="📡", layout="wide")
```

### 6b. Database helpers

The database file lives at `backend/db/wsb.db` relative to this file.
Compute the absolute path using `__file__`:

```python
_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "backend", "db", "wsb.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "backend", "db", "wsb_schema.sql")
```

Function `_get_conn() -> sqlite3.Connection`:

```python
def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(_SCHEMA_PATH) as f:
        conn.executescript(f.read())
    return conn
```

Function `_get_cached_posts(post_ids: list[str]) -> dict[str, dict]`:

Returns a dict mapping post_id → row dict for every post_id that already exists in the DB.

```python
def _get_cached_posts(post_ids: list[str]) -> dict[str, dict]:
    if not post_ids:
        return {}
    conn = _get_conn()
    try:
        placeholders = ",".join("?" * len(post_ids))
        rows = conn.execute(
            f"SELECT * FROM wsb_posts WHERE post_id IN ({placeholders})",
            post_ids,
        ).fetchall()
        return {r["post_id"]: dict(r) for r in rows}
    finally:
        conn.close()
```

Function `_save_post(post: dict) -> None`:

Inserts or replaces a fully-analyzed post dict into the DB. The dict must contain all columns
defined in the schema.

```python
def _save_post(post: dict) -> None:
    conn = _get_conn()
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
```

### 6c. Orchestration function `_load_posts(ticker: str) -> list[dict]`

This is the core logic. It ties together fetching, DB lookup, and analysis.

```python
def _load_posts(ticker: str) -> list[dict]:
    # 1. Fetch the top 10 posts from Reddit for this ticker
    raw_posts = fetch_wsb_posts(ticker, limit=10)
    if not raw_posts:
        return []

    # 2. Check which post_ids are already in the DB
    post_ids = [p["post_id"] for p in raw_posts]
    cached = _get_cached_posts(post_ids)

    results = []
    for post in raw_posts:
        pid = post["post_id"]

        if pid in cached:
            # Post already analyzed — use DB data, skip Gemini call
            results.append(cached[pid])
        else:
            # New post — run Gemini analysis and save to DB
            sentiment = analyze_sentiment(
                title=post["title"],
                body=post["body"],
                ticker=ticker,
            )
            full_post = {
                **post,
                "sentiment_score": sentiment["sentiment_score"],
                "sentiment_label": sentiment["sentiment_label"],
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_post(full_post)
            results.append(full_post)

    return results
```

### 6d. Display helper `_render_post(post: dict, idx: int) -> None`

Renders one post inside a Streamlit expander.

```python
_SENTIMENT_COLOR = {"positive": "#00c853", "negative": "#ff1744", "neutral": "#ffd600"}
_SENTIMENT_ICON  = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}

def _render_post(post: dict, idx: int) -> None:
    score_str = f"{post['score']:,}" if post.get("score") is not None else "—"
    label = post.get("sentiment_label", "neutral")
    icon  = _SENTIMENT_ICON.get(label, "🟡")
    header = f"{icon} {post['title'][:90]}"

    with st.expander(header, expanded=(idx == 0)):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Upvotes",   score_str)
        col2.metric("Comments",  post.get("num_comments", "—"))
        col3.metric("Sentiment", label.capitalize())
        col4.metric("Score",     f"{post.get('sentiment_score', 0.0):.2f}")

        if post.get("body"):
            st.markdown(post["body"][:800] + ("…" if len(post.get("body","")) > 800 else ""))

        st.markdown(f"[Open on Reddit ↗]({post.get('permalink', '')})")
        st.caption(
            f"Posted by u/{post.get('author','?')} · "
            f"Post ID: {post.get('post_id','')} · "
            f"{'From DB cache' if post.get('analyzed_at') else 'Just analyzed'}"
        )
```

### 6e. `main()` function and entry point

```python
def _render_sidebar() -> None:
    with st.sidebar:
        st.header("About WSB Analysis")
        st.markdown(
            "Searches **r/WallStreetBets** for the top posts mentioning your ticker "
            "over the past month. Each post is analyzed by Gemini for bullish/bearish "
            "sentiment. Results are cached in SQLite — previously-seen post IDs are "
            "served from the database without re-calling Gemini."
        )
        st.markdown("---")
        st.caption("Powered by Reddit public JSON API + Google Gemini")


def main() -> None:
    _render_sidebar()
    st.title("📡 WallStreetBets Sentiment")
    st.markdown("##### Reddit crowd sentiment · Powered by Gemini AI")
    st.markdown("---")

    # Gemini key check
    if not _API_KEY or _API_KEY == "your_gemini_api_key":
        st.error(
            "**Gemini API key not configured.** "
            "Open `.env` and set `GEMINI_API_KEY=<your key>`."
        )
        return

    ticker_input = st.text_input(
        "Ticker Symbol",
        value=st.session_state.get("active_ticker", ""),
        placeholder="e.g. AAPL",
        key="wsb_ticker_input",
    ).upper().strip()

    if not ticker_input:
        st.info("Enter a ticker symbol above to search WallStreetBets.")
        return

    st.session_state["active_ticker"] = ticker_input

    with st.spinner(f"Loading WSB posts for {ticker_input}…"):
        posts = _load_posts(ticker_input)

    if not posts:
        st.warning(
            f"No posts found for **{ticker_input}** on r/WallStreetBets. "
            "Try a different ticker or check back later."
        )
        return

    # Summary bar
    n_pos = sum(1 for p in posts if p.get("sentiment_label") == "positive")
    n_neg = sum(1 for p in posts if p.get("sentiment_label") == "negative")
    n_neu = sum(1 for p in posts if p.get("sentiment_label") == "neutral")
    avg_score = sum(p.get("sentiment_score", 0.0) for p in posts) / len(posts)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Posts Found",    len(posts))
    c2.metric("🟢 Bullish",    n_pos)
    c3.metric("🔴 Bearish",    n_neg)
    c4.metric("Avg Score",      f"{avg_score:.2f}")
    st.markdown("---")

    for i, post in enumerate(posts):
        _render_post(post, i)


main()
```

---

## Step 7 — Update `frontend/dashboard.py`

In `frontend/dashboard.py`, add the new page to the `_PAGES` list so it appears in the sidebar navigation.

Find this block:

```python
_PAGES = [
    {"icon": "📊", "label": "Metrics",     "path": "pages/1_metrics.py"},
    {"icon": "📝", "label": "Thesis",      "path": "pages/2_thesis.py"},
    {"icon": "👁️",  "label": "Watchlist",   "path": "pages/3_watchlist.py"},
    {"icon": "📰", "label": "News",        "path": "pages/4_news.py"},
    {"icon": "🏦", "label": "Hedge Funds", "path": "pages/5_hedge_funds.py"},
]
```

Add one entry at the end:

```python
    {"icon": "📡", "label": "WSB Reddit",  "path": "pages/6_reddit.py"},
```

Also add it to the landing page columns in `_render_landing()`.
Find `col1, col2, col3, col4, col5 = st.columns(5)` and change it to:

```python
col1, col2, col3, col4, col5, col6 = st.columns(6)
```

Then add a `with col6:` block after the existing `with col5:` block:

```python
    with col6:
        st.markdown("### 📡 WSB Reddit")
        st.markdown(
            "Top WallStreetBets posts for any ticker with Gemini AI sentiment "
            "analysis. Results cached in SQLite — posts already seen are served "
            "instantly without re-calling the API."
        )
        st.page_link("pages/6_reddit.py", label="Open WSB Reddit →")
```

---

## Complete file checklist

| Action | File |
|--------|------|
| Create | `backend/db/wsb_schema.sql` |
| Create | `backend/data/reddit_fetcher.py` |
| Create | `backend/data/wsb_sentiment.py` |
| Create | `frontend/pages/6_reddit.py` |
| Modify | `requirements.txt` — add `google-generativeai` |
| Modify | `.env` — add `REDDIT_USERNAME` and `GEMINI_API_KEY` |
| Modify | `.env.example` — add same two keys with placeholder values |
| Modify | `frontend/dashboard.py` — add page to `_PAGES`, update landing page |

---

## Key behaviors and edge cases

### Deduplication
- The `_load_posts` function always fetches 10 post IDs from Reddit first.
- It then does a single batch DB query for all 10 IDs.
- Only IDs missing from the DB get sent to Gemini.
- A post is identified by its Reddit `post_id` field — not title or URL.
- `INSERT OR REPLACE` means if the same post is looked up under a different ticker,
  it will be overwritten with the latest ticker's data. This is acceptable behavior.

### Gemini not configured
- If `GEMINI_API_KEY` is empty, `analyze_sentiment` returns `{"sentiment_score": 0.0, "sentiment_label": "neutral"}`.
- The page itself shows an error banner and returns early before any processing if the key is missing.

### Reddit API rate limits
- The Reddit public JSON API has a soft rate limit (~60 requests/minute for unauthenticated clients).
- The User-Agent header is required by Reddit's API rules; requests without it are rejected.
- No authentication (OAuth) is needed — the public search endpoint is open.

### Body length cap
- Reddit post bodies are capped at 2000 characters when sent to Gemini to stay within token limits.
- The full body is stored in the DB regardless of the cap.

### `wsb.db` is auto-created
- The `_get_conn()` function calls `os.makedirs` and runs the schema SQL on every connection.
- `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` make this idempotent.
- No manual database setup step is needed.

---

## Running the app after implementation

```bash
# From the stock-dashboard/ root directory
streamlit run frontend/dashboard.py
```

Navigate to the new "📡 WSB Reddit" page in the sidebar.
