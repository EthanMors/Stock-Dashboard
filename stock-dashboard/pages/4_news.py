from datetime import datetime, timezone

import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from data.news_fetcher import PAYWALL_DOMAINS, FREE_DOMAINS, fetch_news, scrape_article

st.set_page_config(page_title="News", layout="wide")

render_gemini_usage_bar()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTIMENT_ICON = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}


def _sentiment_label(insights: list) -> str:
    if not insights:
        return ""
    s = insights[0].get("sentiment", "")
    return f"{_SENTIMENT_ICON.get(s, '')} {s.capitalize()}" if s else ""


def _format_date(utc_str: str) -> str:
    try:
        dt = datetime.strptime(utc_str[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        return dt.strftime("%b %d, %Y  %H:%M UTC")
    except Exception:
        return utc_str or "—"


# ---------------------------------------------------------------------------
# Sidebar — paywall reference
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    with st.sidebar:
        st.header("Source Filter Info")
        with st.expander("Blocked (paywall)", expanded=True):
            st.markdown(
                "These domains are **skipped** because they return no article "
                "text to unauthenticated scrapers:"
            )
            for d in sorted(PAYWALL_DOMAINS):
                st.markdown(f"- `{d}`")
        with st.expander("Confirmed free", expanded=False):
            st.markdown(
                "These domains are known to serve full article text:"
            )
            for d in sorted(FREE_DOMAINS):
                st.markdown(f"- `{d}`")


# ---------------------------------------------------------------------------
# Article card
# ---------------------------------------------------------------------------

def _render_article(article: dict, idx: int) -> None:
    publisher = article.get("publisher", {}).get("name", "Unknown")
    title = article.get("title", "No title")
    url = article.get("article_url", "")
    description = article.get("description", "")
    published = _format_date(article.get("published_utc", ""))
    insights = article.get("insights", [])
    tickers = article.get("tickers", [])

    sentiment = _sentiment_label(insights)

    header = f"**{title}**"
    with st.expander(header):
        meta_col, sentiment_col = st.columns([4, 1])
        with meta_col:
            st.caption(f"{publisher} · {published}")
            if tickers:
                st.caption("Tickers: " + "  ".join(f"`{t}`" for t in tickers))
            if description:
                st.markdown(f"*{description}*")
            st.markdown(f"[Open article ↗]({url})")
        with sentiment_col:
            if sentiment:
                st.markdown(f"**Sentiment**")
                st.markdown(sentiment)

        st.markdown("---")
        if st.button("Scrape full text", key=f"scrape_{idx}"):
            with st.spinner("Fetching article…"):
                content = scrape_article(url)
            st.text_area("Article text", content, height=320, key=f"content_{idx}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _render_sidebar()
    st.header("📰 Stock News")

    from data.news_fetcher import _API_KEY
    if not _API_KEY or _API_KEY == "your_api_key_here":
        st.error(
            "**Massive API key not configured.** "
            "Open `stock-dashboard/.env` and replace `your_api_key_here` "
            "with your key from [massive.com](https://massive.com)."
        )
        return

    ticker_input = st.text_input(
        "Ticker Symbol",
        value=st.session_state.get("active_ticker", ""),
        placeholder="e.g. AAPL",
        key="news_ticker_input",
    ).upper().strip()

    if not ticker_input:
        st.info("Enter a ticker symbol above to load news.")
        return

    with st.spinner(f"Fetching news for {ticker_input}…"):
        articles = fetch_news(ticker_input)

    if not articles:
        st.warning(
            f"No articles found for **{ticker_input}**. "
            "The ticker may be unsupported, or all results were from paywalled sources."
        )
        return

    st.caption(
        f"{len(articles)} articles — paywalled sources ({', '.join(sorted(PAYWALL_DOMAINS)[:4])}…) excluded"
    )
    st.markdown("---")

    for i, article in enumerate(articles):
        _render_article(article, i)


main()
