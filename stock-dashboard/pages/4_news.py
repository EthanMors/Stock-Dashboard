import os
from datetime import datetime, timezone

import streamlit as st

from data.news_fetcher import PAYWALL_DOMAINS, FREE_DOMAINS, fetch_news, scrape_article
from data.agents.orchestrator import run_pipeline

st.set_page_config(page_title="News", layout="wide")


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
# AI Intelligence section
# ---------------------------------------------------------------------------

_VERDICT_CONFIG = {
    "bullish": {"fn": st.success, "label": "Bullish"},
    "bearish": {"fn": st.error,   "label": "Bearish"},
    "neutral": {"fn": st.warning, "label": "Neutral"},
}


def _render_summaries_only(result: dict) -> None:
    summaries = result.get("summaries", {})
    filtered_articles = result.get("filtered_articles", [])
    with st.expander(f"Article Summaries ({len(summaries)})", expanded=True):
        for idx, summary_text in summaries.items():
            if 0 <= idx < len(filtered_articles):
                title = filtered_articles[idx].get("title", f"Article {idx + 1}")
                st.markdown(f"**{title}**")
                st.markdown(summary_text)
                st.markdown("---")


def _render_pipeline_result(result: dict, ticker: str) -> None:
    errors = result.get("errors", [])
    original = result.get("articles_original_count", 0)
    deduped = result.get("articles_after_dedup", 0)
    analysis = result.get("analysis", {})
    summaries = result.get("summaries", {})
    filtered_articles = result.get("filtered_articles", [])

    st.caption(
        f"Pipeline: {original} articles → {deduped} unique stories → "
        f"{len(summaries)} summaries → strategic analysis"
    )

    if errors:
        with st.expander("Pipeline warnings", expanded=False):
            for err in errors:
                st.warning(err)

    if not analysis:
        st.warning(
            "Strategic analysis could not be generated. "
            "Check that `ANTHROPIC_API_KEY` is set and valid in your `.env` file."
        )
        if summaries:
            _render_summaries_only(result)
        return

    # Overall narrative
    st.info(analysis.get("overall_narrative", ""))

    # Stock impact verdict
    stock_impact = analysis.get("stock_impact", {})
    verdict = str(stock_impact.get("verdict", "neutral")).lower()
    reasoning = stock_impact.get("reasoning", "")
    cfg = _VERDICT_CONFIG.get(verdict, _VERDICT_CONFIG["neutral"])
    cfg["fn"](f"**{ticker} — {cfg['label']}:** {reasoning}")

    # Risks and catalysts
    col_risk, col_cat = st.columns(2)
    with col_risk:
        st.markdown("**Key Risks**")
        for risk in analysis.get("key_risks", []):
            st.markdown(f"- {risk}")
    with col_cat:
        st.markdown("**Key Catalysts**")
        for catalyst in analysis.get("key_catalysts", []):
            st.markdown(f"- {catalyst}")

    # Industry and peers
    with st.expander("Industry & Peer Analysis", expanded=False):
        industry_impact = analysis.get("industry_impact", "")
        if industry_impact:
            st.markdown("**Industry Impact**")
            st.markdown(industry_impact)
        peers = analysis.get("peer_companies_affected", [])
        if peers:
            st.markdown("**Peers Affected**")
            st.markdown("  ".join(f"`{p}`" for p in peers))

    # Article summaries
    if summaries:
        with st.expander(f"Article Summaries ({len(summaries)} articles)", expanded=False):
            for idx, summary_text in summaries.items():
                if 0 <= idx < len(filtered_articles):
                    title = filtered_articles[idx].get("title", f"Article {idx + 1}")
                    publisher = filtered_articles[idx].get("publisher", {}).get("name", "")
                    st.markdown(f"**{title}**")
                    if publisher:
                        st.caption(publisher)
                    st.markdown(summary_text)
                    st.markdown("---")


def _render_ai_intelligence(ticker: str, articles: list[dict]) -> None:
    st.markdown("---")
    st.subheader("AI Intelligence")
    st.caption(
        "Powered by Gemini 2.0 Flash (deduplication + summarization) "
        "and Claude Sonnet (strategic analysis). Results cached 30 min."
    )

    gemini_key = os.getenv("GEMINI_API_KEY", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    gemini_ok = bool(gemini_key and gemini_key != "your_gemini_api_key_here")
    anthropic_ok = bool(anthropic_key and anthropic_key != "your_anthropic_api_key_here")

    if not gemini_ok and not anthropic_ok:
        st.info(
            "Set `GEMINI_API_KEY` and `ANTHROPIC_API_KEY` in your `.env` file "
            "to enable AI analysis."
        )
        return

    session_key = f"ai_result_{ticker}"

    if st.button("Run AI Analysis", key="run_ai_btn"):
        with st.spinner(
            "Running multi-agent pipeline: deduplicating headlines, "
            "scraping articles, summarizing, and analyzing… (20-40 seconds)"
        ):
            pipeline_result = run_pipeline(ticker, articles)
        st.session_state[session_key] = pipeline_result

    stored = st.session_state.get(session_key)
    if stored:
        _render_pipeline_result(stored, ticker)


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

    _render_ai_intelligence(ticker_input, articles)


main()
