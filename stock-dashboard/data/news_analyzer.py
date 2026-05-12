import json
import re
import subprocess

import yfinance as yf

from data.gemini_tracker import record_call

_SECTOR_ETF_MAP: dict[str, str] = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Consumer Cyclical": "XLY",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Consumer Defensive": "XLP",
}

_PROMPT_TEMPLATE = """\
You are a specialized Financial News Analyst (Persona: news-analyst).
Analyze the following news articles about {ticker}{sector_context}.
{previous_context}
Articles:
{articles_block}

Rules:
1. Focus on impact to {ticker} specifically. If using sector/industry news as context, note the extrapolation.
2. Sentiment score: -1.0 (extreme bearish) to +1.0 (extreme bullish). 0.0 is neutral.
3. Impact level: 1 (trivial) to 10 (major catalyst). High impact = earnings, M&A, regulatory actions, major contract wins/losses.
4. Extract 2-5 key themes as short actionable phrases (e.g., "AI capex tailwind", "margin compression risk").
5. is_stock_specific: true if articles directly mention {ticker}, false if sector-level fallback analysis.
6. If previous analysis context is provided, note in the summary whether sentiment has shifted, themes have evolved, or new developments contradict the prior view.

Respond ONLY with a JSON object:
{{"sentiment_score": <float>, "sentiment_label": "<positive|negative|neutral>", "summary": "<3-5 sentence expert synthesis>", "impact_level": <integer 1-10>, "key_themes": ["<theme1>", "<theme2>"], "is_stock_specific": <true or false>}}
"""


def _run_gemini(prompt: str) -> str:
    """Call Gemini CLI via subprocess, passing prompt via stdin to avoid shell interpretation issues."""
    try:
        result = subprocess.run(
            ["gemini.cmd", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=90,
        )
        output = result.stdout.strip()
        if output:
            record_call("flash")
        return output
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""


def _parse_response(raw: str) -> dict | None:
    """Extract and validate the first JSON object from a Gemini response string."""
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

    themes = data.get("key_themes", [])
    if not isinstance(themes, list):
        themes = []

    return {
        "sentiment_score": score,
        "sentiment_label": label,
        "summary": str(data.get("summary", "")).strip(),
        "impact_level": max(1, min(10, int(data.get("impact_level", 1)))),
        "key_themes": [str(t) for t in themes[:5]],
        "is_stock_specific": bool(data.get("is_stock_specific", True)),
    }


def get_sector_info(ticker: str) -> tuple[str, str]:
    """Return (sector, sector_etf_ticker) for a given ticker via yfinance.

    Returns ("", "") on failure so callers can skip sector fallback gracefully.
    """
    try:
        info = yf.Ticker(ticker).info
        sector = info.get("sector", "")
        etf = _SECTOR_ETF_MAP.get(sector, "")
        return sector, etf
    except Exception:
        return "", ""


def analyze_articles(
    articles: list[dict],
    ticker: str,
    sector: str = "",
    is_sector_fallback: bool = False,
    previous_analysis: dict | None = None,
) -> dict:
    """Analyze a batch of news articles for a ticker via Gemini CLI.

    Each article dict should have at minimum a "title" key.
    "content" (scraped text) and "source" are used when present.

    Returns a dict with keys: sentiment_score, sentiment_label, summary,
    impact_level, key_themes, is_stock_specific.
    Returns a neutral default dict on failure.
    """
    _default: dict = {
        "sentiment_score": 0.0,
        "sentiment_label": "neutral",
        "summary": "",
        "impact_level": 0,
        "key_themes": [],
        "is_stock_specific": not is_sector_fallback,
    }
    if not articles:
        return _default

    lines: list[str] = []
    for i, art in enumerate(articles, start=1):
        title = art.get("title", "").strip()
        content = art.get("content", "").strip()[:2000]
        source = art.get("source", "")
        header = f"[Article {i}]{f' — {source}' if source else ''}"
        lines.append(
            f"{header}\nTitle: {title}\n"
            f"Content: {content if content else '(no body content extracted)'}"
        )

    sector_context = f" (sector: {sector} — sector-level fallback)" if sector and is_sector_fallback else ""

    previous_context = ""
    if previous_analysis:
        themes_str = ", ".join(previous_analysis.get("key_themes", [])) or "none"
        previous_context = (
            f"\nPrevious Analysis (from {previous_analysis.get('analyzed_at', 'unknown date')}):\n"
            f"- Sentiment: {previous_analysis.get('sentiment_label', 'neutral')} "
            f"({previous_analysis.get('sentiment_score', 0.0):+.2f})\n"
            f"- Impact Level: {previous_analysis.get('impact_level', 0)}/10\n"
            f"- Key Themes: {themes_str}\n"
            f"- Summary: {previous_analysis.get('summary', '')}\n"
        )

    prompt = _PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        sector_context=sector_context,
        previous_context=previous_context,
        articles_block="\n\n".join(lines),
    )
    raw = _run_gemini(prompt)
    if not raw:
        return _default
    return _parse_response(raw) or _default
