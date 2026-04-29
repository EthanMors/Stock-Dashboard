import json
import os
from urllib.parse import urlparse

from google import genai
from google.genai import types

_SOURCE_PRIORITY: dict[str, int] = {
    "reuters.com": 1,
    "apnews.com": 2,
    "marketwatch.com": 3,
    "cnbc.com": 4,
    "morningstar.com": 5,
    "investors.com": 6,
    "thestreet.com": 7,
    "investopedia.com": 8,
    "kiplinger.com": 9,
    "fool.com": 10,
    "zacks.com": 11,
    "nasdaq.com": 12,
    "businessinsider.com": 13,
    "finance.yahoo.com": 14,
    "benzinga.com": 15,
    "prnewswire.com": 16,
    "globenewswire.com": 17,
}


def _get_priority(url: str) -> int:
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        return _SOURCE_PRIORITY.get(host, 99)
    except Exception:
        return 99


def process_headlines(articles: list[dict]) -> list[dict]:
    """Deduplicate articles by topic, keeping the best source per unique story.

    Falls back to returning all articles unchanged if the API call fails.
    """
    if not articles:
        return articles

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key or api_key == "your_gemini_api_key_here":
        return articles

    try:
        client = genai.Client(api_key=api_key)

        article_lines = []
        for i, a in enumerate(articles):
            title = a.get("title", "")
            publisher = a.get("publisher", {}).get("name", "")
            desc = (a.get("description", "") or "")[:200]
            url = a.get("article_url", "")
            priority = _get_priority(url)
            article_lines.append(
                f"[{i}] Title: {title}\n"
                f"    Publisher: {publisher} (source priority rank: {priority}/17 — lower is better)\n"
                f"    Description: {desc}"
            )

        articles_text = "\n\n".join(article_lines)

        prompt = f"""You are a financial news editor curating a briefing for an investor.
Below are {len(articles)} news articles tagged with index numbers [0], [1], etc.

Your task:
1. Group articles that cover the SAME story or event (e.g., multiple outlets reporting the same earnings release, the same analyst upgrade, the same product launch, the same regulatory action).
2. From each group of duplicates, keep only the article with the LOWEST source priority rank number (1 = best source, 17 = worst source). If two articles in a group have the same priority rank, keep the one with the longer description.
3. Keep every article that covers a UNIQUE story not covered by any other article — do not drop unique stories.
4. Return a JSON object with a single key "keep_indices" containing a list of integer indices to retain.

Articles:
{articles_text}

Return ONLY valid JSON:
{{"keep_indices": [list of integer indices to keep]}}"""

        response = client.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        result = json.loads(response.text)
        indices = result.get("keep_indices", [])

        if not isinstance(indices, list) or not indices:
            return articles

        kept = [articles[i] for i in indices if isinstance(i, int) and 0 <= i < len(articles)]
        return kept if kept else articles

    except Exception:
        return articles
