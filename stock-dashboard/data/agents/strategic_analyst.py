import json
import os

import anthropic


def analyze_strategically(
    ticker: str,
    company_info: dict,
    articles: list[dict],
    summaries: dict[int, str],
) -> dict:
    """Produce a structured strategic analysis of news for a given stock.

    Returns a dict with keys: overall_narrative, stock_impact, industry_impact,
    peer_companies_affected, key_risks, key_catalysts.
    Returns {} if the API key is missing or any unrecoverable error occurs.
    """
    if not summaries:
        return {}

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "your_anthropic_api_key_here":
        return {}

    company_name = (
        company_info.get("longName")
        or company_info.get("shortName")
        or ticker
    )
    sector = company_info.get("sector") or "Unknown sector"
    industry = company_info.get("industry") or "Unknown industry"
    business_summary = (company_info.get("longBusinessSummary", "") or "")[:500]

    summary_lines = []
    for idx, summary_text in summaries.items():
        if 0 <= idx < len(articles):
            title = articles[idx].get("title", f"Article {idx + 1}")
            summary_lines.append(f"{idx + 1}. [{title}]\n   {summary_text}")
    numbered_summaries = "\n\n".join(summary_lines)

    if not numbered_summaries:
        return {}

    prompt = f"""You are a senior equity analyst at a long/short hedge fund preparing a morning briefing memo. A portfolio manager needs to decide whether to adjust a position in {ticker} ({company_name}) before the open.

Company context:
- Ticker: {ticker}
- Company: {company_name}
- Sector: {sector}
- Industry: {industry}
- Business: {business_summary}

Recent news summaries:
{numbered_summaries}

Analyze what these news stories collectively mean for:
1. This specific stock ({ticker})
2. The broader {industry} industry
3. Peer and competitor companies

Respond ONLY with a valid JSON object using exactly these keys:

{{
  "overall_narrative": "2-3 sentence arc connecting all stories into one coherent theme",
  "stock_impact": {{
    "verdict": "bullish",
    "reasoning": "2-3 sentences explaining why"
  }},
  "industry_impact": "1-2 sentences on what this means for the industry",
  "peer_companies_affected": ["CompanyA (TICKER)", "CompanyB (TICKER)"],
  "key_risks": ["risk 1", "risk 2", "risk 3"],
  "key_catalysts": ["catalyst 1", "catalyst 2", "catalyst 3"]
}}

The "verdict" field must be exactly one of: "bullish", "bearish", or "neutral"."""

    _DEFAULTS: dict = {
        "overall_narrative": "Analysis unavailable.",
        "stock_impact": {"verdict": "neutral", "reasoning": ""},
        "industry_impact": "",
        "peer_companies_affected": [],
        "key_risks": [],
        "key_catalysts": [],
    }

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()

        # Strip markdown fencing if present
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)

        for key, default in _DEFAULTS.items():
            if key not in result:
                result[key] = default

        # Normalize verdict
        if isinstance(result.get("stock_impact"), dict):
            verdict = str(result["stock_impact"].get("verdict", "neutral")).lower()
            if verdict not in ("bullish", "bearish", "neutral"):
                verdict = "neutral"
            result["stock_impact"]["verdict"] = verdict

        return result

    except Exception:
        return {}
