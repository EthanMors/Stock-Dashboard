import json
import re
import subprocess

from data.gemini_tracker import record_call

_VALID_IMPACT_TYPES = frozenset({
    "monetary_policy", "geopolitical", "trade_policy",
    "energy_commodities", "sector_tech", "sector_financials",
    "sector_energy", "macro_economy", "other"
})

_VALID_MACRO_CATEGORIES = frozenset({
    "bullish_for_equities", "bearish_for_equities",
    "mixed", "sector_specific", "neutral"
})

_MACRO_PROMPT_TEMPLATE = """\
You are a specialized Macro News Analyst. Analyze the following macro and sector-wide news articles.
Determine their market impact, affected sectors, and overall macro market implications.

Articles:
{articles_block}

Rules:
1. Sentiment score: -1.0 (bearish) to +1.0 (bullish). 0.0 is neutral.
2. Impact level: 1 (trivial) to 10 (major market event).
3. key_themes: 2-5 short actionable phrases (e.g., "Fed rate hike expectations", "Energy supply risk").
4. market_impact_type: must be one of: monetary_policy, geopolitical, trade_policy, energy_commodities, sector_tech, sector_financials, sector_energy, macro_economy, other.
5. affected_sectors: list of 1-4 sector names (e.g., ["Technology", "Energy"]).
6. macro_category: must be one of: bullish_for_equities, bearish_for_equities, mixed, sector_specific, neutral.

Respond ONLY with a JSON object:
{{"sentiment_score": <float>, "sentiment_label": "<positive|negative|neutral>", "summary": "<2-3 sentence analysis>", "impact_level": <integer 1-10>, "key_themes": ["<theme1>", "<theme2>"], "market_impact_type": "<enum>", "affected_sectors": ["<sector1>"], "macro_category": "<enum>"}}
"""

_DEFAULT_RESULT = {
    "sentiment_score": 0.0,
    "sentiment_label": "neutral",
    "summary": "Unable to analyze at this time.",
    "impact_level": 0,
    "key_themes": [],
    "market_impact_type": "other",
    "affected_sectors": [],
    "macro_category": "neutral",
}


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

    # Clamp sentiment score
    score = float(data.get("sentiment_score", 0.0))
    score = max(-1.0, min(1.0, score))

    # Validate sentiment label
    label = data.get("sentiment_label", "neutral")
    if label not in ("positive", "negative", "neutral"):
        label = "positive" if score > 0.1 else "negative" if score < -0.1 else "neutral"

    # Extract and validate key_themes
    themes = data.get("key_themes", [])
    if not isinstance(themes, list):
        themes = []
    themes = [str(t) for t in themes[:5]]

    # Validate market_impact_type
    impact_type = data.get("market_impact_type", "other")
    if impact_type not in _VALID_IMPACT_TYPES:
        impact_type = "other"

    # Extract and cap affected_sectors
    sectors = data.get("affected_sectors", [])
    if not isinstance(sectors, list):
        sectors = []
    sectors = [str(s) for s in sectors[:4]]

    # Validate macro_category
    macro_cat = data.get("macro_category", "neutral")
    if macro_cat not in _VALID_MACRO_CATEGORIES:
        macro_cat = "neutral"

    return {
        "sentiment_score": score,
        "sentiment_label": label,
        "summary": str(data.get("summary", "")).strip(),
        "impact_level": max(1, min(10, int(data.get("impact_level", 1)))),
        "key_themes": themes,
        "market_impact_type": impact_type,
        "affected_sectors": sectors,
        "macro_category": macro_cat,
    }


def analyze_macro_articles(articles: list[dict], feed_category: str) -> dict:
    """Analyze a batch of macro news articles via Gemini CLI.

    Each article dict should have at minimum: "title", "description", "source".

    Returns a dict with keys: sentiment_score, sentiment_label, summary, impact_level,
    key_themes, market_impact_type, affected_sectors, macro_category.
    Returns a neutral default dict on failure.
    """
    if not articles:
        return _DEFAULT_RESULT

    lines: list[str] = []
    for i, art in enumerate(articles, start=1):
        title = art.get("title", "").strip()
        description = art.get("description", "").strip()[:1000]
        source = art.get("source", "")
        header = f"[Article {i}]{f' — {source}' if source else ''}"
        lines.append(
            f"{header}\nTitle: {title}\n"
            f"Description: {description if description else '(no description)'}"
        )

    prompt = _MACRO_PROMPT_TEMPLATE.format(articles_block="\n\n".join(lines))
    raw = _run_gemini(prompt)
    if not raw:
        return _DEFAULT_RESULT
    return _parse_response(raw) or _DEFAULT_RESULT
