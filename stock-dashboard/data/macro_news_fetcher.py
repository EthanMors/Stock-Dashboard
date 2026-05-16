import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Optional

from data.news_fetcher import _is_paywalled, fetch_news

_RSS_FEEDS = {
    "monetary_policy": ["https://feeds.reuters.com/reuters/businessNews"],
    "geopolitical": ["https://feeds.reuters.com/Reuters/worldNews"],
    "macro_economy": ["https://www.marketwatch.com/rss/topstories"],
    "energy_commodities": ["https://feeds.reuters.com/reuters/companyNews"],
}

_ETF_PROXIES = {
    "sector_financials": "XLF",
    "sector_technology": "XLK",
    "sector_energy": "XLE",
}


def _parse_pub_date(s: str) -> float:
    """Parse a publication date string to Unix timestamp float.

    Tries RFC 2822 format first, then ISO format. Returns 0.0 on failure.
    """
    if not s or not isinstance(s, str):
        return 0.0

    # Try RFC 2822 format (email.utils.parsedate_to_datetime)
    try:
        dt = parsedate_to_datetime(s)
        return dt.timestamp()
    except (TypeError, ValueError):
        pass

    # Try ISO format
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        pass

    return 0.0


def _fetch_rss(url: str, category: str) -> list[dict]:
    """Fetch and parse an RSS feed.

    Handles both RSS 2.0 <item> and Atom <entry> formats.
    Filters out paywalled articles.
    Returns empty list on any exception.
    """
    try:
        import requests

        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        articles = []

        # RSS 2.0 <item> elements
        for item in root.findall(".//item"):
            title_elem = item.find("title")
            link_elem = item.find("link")
            pubdate_elem = item.find("pubDate")
            description_elem = item.find("description")

            title = title_elem.text if title_elem is not None else ""
            link = link_elem.text if link_elem is not None else ""
            pubdate = pubdate_elem.text if pubdate_elem is not None else ""
            description = description_elem.text if description_elem is not None else ""

            if link and not _is_paywalled(link):
                articles.append({
                    "article_url": link,
                    "title": title,
                    "description": description,
                    "published_utc": _parse_pub_date(pubdate),
                    "source": url.split("/")[-1][:30],
                    "feed_category": category,
                })

        # Atom <entry> elements
        for entry in root.findall(".//entry"):
            title_elem = entry.find("{http://www.w3.org/2005/Atom}title") or entry.find("title")
            link_elem = entry.find("{http://www.w3.org/2005/Atom}link[@rel='alternate']") or entry.find("link")
            pubdate_elem = entry.find("{http://www.w3.org/2005/Atom}published") or entry.find("published")
            summary_elem = entry.find("{http://www.w3.org/2005/Atom}summary") or entry.find("summary")

            title = title_elem.text if title_elem is not None else ""
            link = link_elem.get("href") if link_elem is not None else ""
            pubdate = pubdate_elem.text if pubdate_elem is not None else ""
            description = summary_elem.text if summary_elem is not None else ""

            if link and not _is_paywalled(link):
                articles.append({
                    "article_url": link,
                    "title": title,
                    "description": description,
                    "published_utc": _parse_pub_date(pubdate),
                    "source": url.split("/")[-1][:30],
                    "feed_category": category,
                })

        return articles
    except Exception:
        return []


def _fetch_etf_proxy(ticker: str, category: str) -> list[dict]:
    """Fetch news for an ETF ticker and remap keys.

    Reuses the existing fetch_news() function from news_fetcher.py.
    """
    try:
        articles = fetch_news(ticker, limit=25)
        result = []
        for art in articles:
            pub_raw = art.get("published_utc", 0.0)
            pub_ts = _parse_pub_date(pub_raw) if isinstance(pub_raw, str) else float(pub_raw or 0.0)
            result.append({
                "article_url": art.get("article_url", ""),
                "title": art.get("title", ""),
                "description": art.get("description", ""),
                "published_utc": pub_ts,
                "source": art.get("publisher", {}).get("name", "")[:30],
                "feed_category": category,
            })
        return result
    except Exception:
        return []


def fetch_macro_articles(category: Optional[str] = None) -> list[dict]:
    """Fetch macro and sector-wide news articles.

    Combines RSS feeds and ETF proxy calls. Deduplicates by URL.
    Returns up to 40 articles sorted by published_utc (descending).

    Parameters
    ----------
    category : Specific feed_category to fetch, or None for all categories

    Returns
    -------
    List of article dicts with keys: article_url, title, description, published_utc, source, feed_category
    """
    articles_dict = {}

    # Fetch RSS feeds
    feeds_to_fetch = {category: _RSS_FEEDS[category]} if category and category in _RSS_FEEDS else _RSS_FEEDS

    for cat, urls in feeds_to_fetch.items():
        for url in urls:
            rss_articles = _fetch_rss(url, cat)
            for art in rss_articles:
                key = art.get("article_url", "")
                if key and key not in articles_dict:
                    articles_dict[key] = art

    # Fetch ETF proxies
    proxies_to_fetch = {category: _ETF_PROXIES[category]} if category and category in _ETF_PROXIES else _ETF_PROXIES

    for cat, ticker in proxies_to_fetch.items():
        etf_articles = _fetch_etf_proxy(ticker, cat)
        for art in etf_articles:
            key = art.get("article_url", "")
            if key and key not in articles_dict:
                articles_dict[key] = art

    # Convert to list, sort by published_utc descending, cap at 40
    result = sorted(articles_dict.values(), key=lambda a: a.get("published_utc", 0.0), reverse=True)
    return result[:40]
