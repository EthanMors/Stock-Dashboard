import os

from google import genai

from data.news_fetcher import scrape_article

_SCRAPE_ERROR_PREFIXES = (
    "Access denied",
    "HTTP ",
    "Request failed",
    "Could not extract",
)


def _is_scrape_error(text: str) -> bool:
    return any(text.startswith(p) for p in _SCRAPE_ERROR_PREFIXES)


def summarize_articles(articles: list[dict]) -> dict[int, str]:
    """Scrape and summarize each article. Returns {index: summary_text}.

    Uses existing scrape_article() from news_fetcher (cached at 15 min).
    Falls back to article description if scraping fails.
    Returns {} if Gemini API key is not configured.
    """
    if not articles:
        return {}

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key or api_key == "your_gemini_api_key_here":
        return {}

    summaries: dict[int, str] = {}

    try:
        client = genai.Client(api_key=api_key)
    except Exception:
        return {}

    for i, article in enumerate(articles):
        url = article.get("article_url", "")
        if not url:
            continue

        try:
            text = scrape_article(url)

            if _is_scrape_error(text):
                fallback = (article.get("description", "") or "").strip()
                if not fallback:
                    continue
                text = fallback

            title = article.get("title", "")
            prompt = (
                f"Article title: {title}\n\n"
                f"Article text:\n{text}\n\n"
                "Write a 2-3 sentence factual summary of this article. "
                "Focus on the key facts, figures, and implications. "
                "Do not add information not present in the text. "
                "Write in plain English with no markdown formatting."
            )

            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            summary = response.text.strip()
            if summary:
                summaries[i] = summary

        except Exception:
            continue

    return summaries
