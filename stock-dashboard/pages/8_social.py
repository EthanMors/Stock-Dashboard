import json
import os
import sqlite3
from datetime import datetime, timezone

import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header, render_sidebar_nav
from data.reddit_fetcher import fetch_top_posts_for_ticker, fetch_daily_top_tickers, TOP_N
from data.wsb_sentiment import analyze_sentiment, analyze_batch_sentiment
from data.twitter_fetcher import search_tweets_for_ticker
from data.twitter_sentiment import analyze_tweet_sentiment, analyze_batch_tweet_sentiment

st.set_page_config(page_title="Social Sentiment", page_icon="🌐", layout="wide")

render_gemini_usage_bar()
inject_global_css()
render_sidebar_nav()

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
    page_header("Social Sentiment", "Reddit WallStreetBets and Twitter/X sentiment analysis.")

    reddit_tab, twitter_tab = st.tabs(["Reddit", "Twitter/X"])

    with reddit_tab:
        _render_reddit_tab()

    with twitter_tab:
        _render_twitter_tab()


main()
