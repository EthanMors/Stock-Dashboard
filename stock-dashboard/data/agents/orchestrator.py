from datetime import datetime

import streamlit as st


@st.cache_data(ttl=1800)
def run_pipeline(ticker: str, articles: list[dict]) -> dict:
    """Run the full three-stage news intelligence pipeline.

    Stage 1 — HeadlineProcessor: deduplicate articles by topic (Gemini Flash Lite)
    Stage 2 — Summarizer: scrape and summarize each article (Gemini Flash)
    Stage 3 — StrategicAnalyst: produce investment analysis (Claude Sonnet)

    Each stage fails gracefully; downstream stages receive what was completed.
    Result is cached for 30 minutes.
    """
    from data.agents.headline_processor import process_headlines
    from data.agents.summarizer import summarize_articles
    from data.agents.strategic_analyst import analyze_strategically
    from data.fetcher import get_stock_info

    errors: list[str] = []
    result: dict = {
        "ticker": ticker,
        "articles_original_count": len(articles),
        "articles_after_dedup": len(articles),
        "filtered_articles": list(articles),
        "summaries": {},
        "analysis": {},
        "errors": errors,
        "generated_at": datetime.utcnow().isoformat(),
    }

    # Stage 1: Headline deduplication
    filtered = articles
    try:
        filtered = process_headlines(list(articles))
        result["filtered_articles"] = filtered
        result["articles_after_dedup"] = len(filtered)
    except Exception as exc:
        errors.append(f"Headline processor failed: {exc}")

    # Stage 2: Summarization
    summaries: dict[int, str] = {}
    try:
        summaries = summarize_articles(filtered)
        result["summaries"] = summaries
    except Exception as exc:
        errors.append(f"Summarizer failed: {exc}")

    # Stage 3: Strategic analysis (only if summaries exist)
    if summaries:
        try:
            company_info = get_stock_info(ticker)
            analysis = analyze_strategically(ticker, company_info, filtered, summaries)
            result["analysis"] = analysis
        except Exception as exc:
            errors.append(f"Strategic analyst failed: {exc}")
    else:
        errors.append("Strategic analyst skipped: no summaries available.")

    return result
