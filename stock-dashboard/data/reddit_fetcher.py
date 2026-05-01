import os
import requests
from datetime import datetime, timezone

_REDDIT_USERNAME = os.getenv("REDDIT_USERNAME", "stock_dashboard_user")
_HEADERS = {
    "User-Agent": f"python:StockDashboardWSB:v1.0.0 (by /u/{_REDDIT_USERNAME})"
}
_BASE_URL = "https://www.reddit.com"
_SUBREDDIT = "wallstreetbets"
_TIMEOUT = 10


def fetch_wsb_posts(ticker: str, limit: int = 10) -> list[dict]:
    """Fetch the top posts from r/wallstreetbets for a given ticker."""
    url = (
        f"{_BASE_URL}/r/{_SUBREDDIT}/search.json"
        f"?q={ticker}&restrict_sr=1&sort=top&t=month&limit={limit}"
    )
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        children = resp.json()["data"]["children"]
    except Exception:
        return []

    posts = []
    for child in children:
        d = child.get("data", {})
        post_id = d.get("id")
        if not post_id:
            continue
        posts.append({
            "post_id":      post_id,
            "ticker":       ticker.upper(),
            "title":        d.get("title", ""),
            "body":         d.get("selftext", ""),
            "author":       d.get("author", ""),
            "score":        d.get("score", 0),
            "num_comments": d.get("num_comments", 0),
            "created_utc":  d.get("created_utc"),
            "url":          d.get("url", ""),
            "permalink":    _BASE_URL + d.get("permalink", ""),
            "fetched_at":   datetime.now(timezone.utc).isoformat(),
        })
    return posts
