from datetime import datetime, timezone
from itertools import groupby

import streamlit as st

from components.gemini_usage_bar import render_gemini_usage_bar
from data.news_fetcher import PAYWALL_DOMAINS, FREE_DOMAINS, fetch_news, scrape_article
from data import macro_news_fetcher, macro_news_analyzer, macro_news_cache

st.set_page_config(page_title="News", layout="wide")

render_gemini_usage_bar()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SENTIMENT_ICON = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}

_CATEGORY_DISPLAY = {
    "monetary_policy": "Monetary Policy",
    "geopolitical": "Geopolitical",
    "macro_economy": "Macro Economy",
    "energy_commodities": "Energy & Commodities",
    "sector_financials": "Sector: Financials",
    "sector_technology": "Sector: Technology",
    "sector_energy": "Sector: Energy",
}

_MACRO_CAT_DISPLAY = {
    "bullish_for_equities": "Bullish for Equities",
    "bearish_for_equities": "Bearish for Equities",
    "mixed": "Mixed Impact",
    "sector_specific": "Sector Specific",
    "neutral": "Neutral",
}


def _impact_icon(level: int) -> str:
    """Return an emoji icon based on impact level."""
    if 1 <= level <= 3:
        return "📰"
    elif 4 <= level <= 6:
        return "⚡"
    else:  # 7-10
        return "🚨"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sentiment_label(insights: list) -> str:
    if not insights:
        return ""
    s = insights[0].get("sentiment", "")
    return f"{_SENTIMENT_ICON.get(s, '')} {s.capitalize()}" if s else ""


def _format_date(utc_input) -> str:
    try:
        # Handle float Unix timestamps
        if isinstance(utc_input, (int, float)):
            dt = datetime.fromtimestamp(utc_input, tz=timezone.utc)
            return dt.strftime("%b %d, %Y  %H:%M UTC")
        # Handle ISO string format
        utc_str = str(utc_input)
        dt = datetime.strptime(utc_str[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        return dt.strftime("%b %d, %Y  %H:%M UTC")
    except Exception:
        return str(utc_input) if utc_input else "—"


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


def _render_macro_card(analysis: dict, idx: int) -> None:
    """Render a macro news analysis card."""
    title = analysis.get("title", "No title")
    url = analysis.get("article_url", "")
    source = analysis.get("source", "Unknown")
    published = _format_date(analysis.get("published_utc", ""))
    sentiment_score = analysis.get("sentiment_score", 0.0)
    sentiment_label = analysis.get("sentiment_label", "neutral")
    summary = analysis.get("summary", "")
    impact_level = analysis.get("impact_level", 0)
    key_themes = analysis.get("key_themes", [])
    affected_sectors = analysis.get("affected_sectors", [])
    macro_category = analysis.get("macro_category", "neutral")

    icon = _impact_icon(impact_level)
    header = f"{icon} {title}"

    with st.expander(header):
        col1, col2 = st.columns([3, 1])

        with col1:
            st.caption(f"{source} · {published}")
            st.markdown(f"*{summary}*" if summary else "*(no summary)*")
            st.markdown(f"[Open article ↗]({url})")

            if affected_sectors:
                st.markdown("**Affected Sectors:**")
                sector_pills = " ".join(f"`{s}`" for s in affected_sectors)
                st.markdown(sector_pills)

            if key_themes:
                st.markdown("**Key Themes:**")
                theme_pills = " ".join(f"`{t}`" for t in key_themes)
                st.markdown(theme_pills)

        with col2:
            sentiment_icon = _SENTIMENT_ICON.get(sentiment_label, "")
            st.markdown("**Sentiment**")
            st.markdown(f"{sentiment_icon} {sentiment_label.capitalize()}\n({sentiment_score:+.1f})")

            st.markdown("**Impact**")
            st.markdown(f"{impact_level}/10")

            st.markdown("**Category**")
            cat_label = _MACRO_CAT_DISPLAY.get(macro_category, macro_category)
            st.markdown(f"{cat_label}")


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------

def _render_stock_news_tab() -> None:
    """Render the Stock News tab (existing ticker-specific flow)."""
    _render_sidebar()
    st.header("📈 Stock News")

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


def _render_market_pulse_tab() -> None:
    """Render the Market Pulse tab (macro news)."""
    st.header("🌍 Market Pulse")

    # Build category options from _CATEGORY_DISPLAY and _ETF_PROXIES
    all_categories = sorted(set(list(_CATEGORY_DISPLAY.keys())))
    category_options = ["All"] + [_CATEGORY_DISPLAY.get(c, c) for c in all_categories]

    # Build macro category display options
    macro_cat_options = ["All"] + [_MACRO_CAT_DISPLAY.get(c, c) for c in sorted(_MACRO_CAT_DISPLAY.keys())]

    # Filter UI
    col1, col2, col3 = st.columns(3)
    with col1:
        selected_display = st.selectbox("Category", category_options, key="macro_cat_select")
        selected_cat = None
        if selected_display != "All":
            # Reverse lookup
            for k, v in _CATEGORY_DISPLAY.items():
                if v == selected_display:
                    selected_cat = k
                    break

    with col2:
        selected_macro_display = st.selectbox("Market Impact", macro_cat_options, key="macro_impact_select")
        selected_macro = None
        if selected_macro_display != "All":
            for k, v in _MACRO_CAT_DISPLAY.items():
                if v == selected_macro_display:
                    selected_macro = k
                    break

    with col3:
        min_impact_slider = st.slider("Min Impact Level", 1, 10, value=4, key="min_impact_slider")

    col_refresh = st.columns([1, 4])
    with col_refresh[0]:
        refresh_clicked = st.button("🔄 Refresh Macro News", key="refresh_macro_news")

    st.markdown("---")

    # Fetch and analyze if not fresh
    if not macro_news_cache.is_category_fresh(selected_cat, 120) or refresh_clicked:
        with st.spinner("Fetching and analyzing macro news…"):
            raw = macro_news_fetcher.fetch_macro_articles(selected_cat)
            seen = macro_news_cache.get_article_urls_seen(selected_cat or "all")
            new_articles = [a for a in raw if a["article_url"] not in seen]

            if new_articles:
                # Batch by feed_category, 5 articles per call
                for feed_cat, batch in groupby(sorted(new_articles, key=lambda a: a["feed_category"]),
                                               key=lambda a: a["feed_category"]):
                    batch_list = list(batch)[:5]
                    result = macro_news_analyzer.analyze_macro_articles(batch_list, feed_cat)
                    for article in batch_list:
                        macro_news_cache.save_macro_analysis(article, result)

    # Fetch analyses
    if selected_cat:
        analyses = macro_news_cache.get_recent_analyses(category=selected_cat, impact_type=selected_macro,
                                                        hours=48, limit=50)
    else:
        analyses = macro_news_cache.get_recent_analyses(category=None, impact_type=selected_macro,
                                                        hours=48, limit=50)

    # Filter by impact level (client-side)
    analyses = [a for a in analyses if a.get("impact_level", 0) >= min_impact_slider]

    if not analyses:
        st.info("No macro news articles match your filters.")
        return

    st.caption(f"{len(analyses)} articles")
    for i, analysis in enumerate(analyses):
        _render_macro_card(analysis, i)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _render_sidebar()

    tab1, tab2 = st.tabs(["📈 Stock News", "🌍 Market Pulse"])

    with tab1:
        _render_stock_news_tab()

    with tab2:
        _render_market_pulse_tab()


main()
