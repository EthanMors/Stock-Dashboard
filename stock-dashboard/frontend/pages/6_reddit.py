import os
import sqlite3
from datetime import datetime, timezone

import streamlit as st

from backend.data.reddit_fetcher import fetch_wsb_posts
from backend.data.wsb_sentiment import analyze_sentiment, _API_KEY

st.set_page_config(page_title="WSB Reddit", page_icon="📡", layout="wide")

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "backend", "db", "wsb.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "backend", "db", "wsb_schema.sql")


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(_SCHEMA_PATH) as f:
        conn.executescript(f.read())
    return conn


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
