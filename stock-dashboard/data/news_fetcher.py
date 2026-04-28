import os
from urllib.parse import urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

_API_KEY = os.getenv("MASSIVE_API_KEY", "")
_BASE_URL = "https://api.massive.com"

# -----------------------------------------------------------------------
# Paywall domain list
#
# Hard paywall  — server returns no article text at all; scraping is useless.
# Soft/metered  — a few free articles, then a wall; results are unreliable.
# Both are excluded so BeautifulSoup never wastes a request on them.
# -----------------------------------------------------------------------
PAYWALL_DOMAINS: set[str] = {
    # Hard paywalls (server-side; zero content without a subscription)
    "wsj.com",          # Wall Street Journal
    "barrons.com",      # Barron's (Dow Jones, same gate as WSJ)
    "ft.com",           # Financial Times
    "bloomberg.com",    # Bloomberg
    "theinformation.com",
    "economist.com",
    "hbr.org",          # Harvard Business Review
    "thetimes.co.uk",
    "telegraph.co.uk",
    # Soft / metered (cookie-tracked; scraping often returns a paywall stub)
    "nytimes.com",
    "washingtonpost.com",
    "seekingalpha.com",  # Premium articles are server-gated
}

# Sources confirmed to serve full article text without a subscription
FREE_DOMAINS: set[str] = {
    "reuters.com",
    "cnbc.com",
    "finance.yahoo.com",
    "investopedia.com",
    "benzinga.com",
    "fool.com",
    "zacks.com",
    "prnewswire.com",
    "globenewswire.com",
    "apnews.com",
    "marketwatch.com",
    "thestreet.com",
    "nasdaq.com",
    "kiplinger.com",
    "investors.com",
    "businessinsider.com",
    "morningstar.com",
}

_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# CSS selectors tried in order when extracting article body
_ARTICLE_SELECTORS = [
    "article",
    '[class*="article-body"]',
    '[class*="story-body"]',
    '[class*="article-content"]',
    '[class*="post-content"]',
    '[class*="entry-content"]',
    '[class*="body-copy"]',
    "main",
]

_MAX_CONTENT_CHARS = 4000


def _host(url: str) -> str:
    """Return bare hostname without www prefix."""
    try:
        h = urlparse(url).netloc.lower()
        return h.removeprefix("www.")
    except Exception:
        return ""


def _is_paywalled(url: str) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in PAYWALL_DOMAINS)


@st.cache_data(ttl=900)
def fetch_news(ticker: str, limit: int = 25) -> list[dict]:
    """
    Fetch recent news for *ticker* from Massive.com and drop paywalled URLs.
    Returns a list of article dicts (empty list on error or missing key).
    """
    if not _API_KEY or _API_KEY == "your_api_key_here":
        return []

    try:
        resp = requests.get(
            f"{_BASE_URL}/v2/reference/news",
            params={
                "ticker": ticker.upper(),
                "limit": limit,
                "sort": "published_utc",
                "order": "desc",
            },
            headers={"Authorization": f"Bearer {_API_KEY}"},
            timeout=12,
        )
        resp.raise_for_status()
        articles = resp.json().get("results", [])
        return [a for a in articles if not _is_paywalled(a.get("article_url", ""))]
    except requests.HTTPError as exc:
        st.warning(f"Massive API error {exc.response.status_code}: check your API key.")
        return []
    except Exception:
        return []


@st.cache_data(ttl=900)
def scrape_article(url: str) -> str:
    """
    Fetch *url* and extract the main article text with BeautifulSoup.
    Returns the text (up to _MAX_CONTENT_CHARS) or an error message.
    """
    try:
        resp = requests.get(url, headers=_SCRAPE_HEADERS, timeout=12, allow_redirects=True)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return "Access denied — this article may require a login or subscription."
        return f"HTTP {code} when fetching article."
    except Exception as exc:
        return f"Request failed: {exc}"

    soup = BeautifulSoup(resp.text, "lxml")

    # Drop clutter before extracting text
    for noise in soup(["script", "style", "nav", "footer", "aside",
                        "header", "figure", "figcaption", "noscript"]):
        noise.decompose()

    # Try known article containers
    for selector in _ARTICLE_SELECTORS:
        container = soup.select_one(selector)
        if container:
            text = container.get_text(separator="\n", strip=True)
            if len(text) > 300:
                return text[:_MAX_CONTENT_CHARS]

    # Fallback: collect substantial paragraphs
    paragraphs = [
        p.get_text(strip=True)
        for p in soup.find_all("p")
        if len(p.get_text(strip=True)) > 60
    ]
    if paragraphs:
        return "\n\n".join(paragraphs)[:_MAX_CONTENT_CHARS]

    return "Could not extract article content — the page structure may be unusual."
