import os
import re
from collections import Counter
import requests
from datetime import datetime, timezone

_REDDIT_USERNAME = os.getenv("REDDIT_USERNAME", "stock_dashboard_user")
_HEADERS = {
    "User-Agent": f"python:StockDashboardWSB:v1.0.0 (by /u/{_REDDIT_USERNAME})"
}
_BASE_URL = "https://www.reddit.com"
_TIMEOUT = 10

_GENERAL_SUBREDDITS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "stockmarket",
]

_TICKER_BLACKLIST = {
    # Prepositions & Conjunctions
    "THE", "AND", "FOR", "WITH", "FROM", "BUT", "THAT", "THIS", "THEY", "WHAT",
    "ABOUT", "THERE", "THEN", "THAN", "ONLY", "ALSO", "EVEN", "SOME", "TIME",
    "JUST", "KNOW", "TAKE", "PEOPLE", "COULD", "THINK", "ALSO", "EVEN", "LOOK",
    "WANT", "BECAUSE", "GOOD", "YEAR", "WORK", "BACK", "FIRST", "WELL", "MAKE",
    "MANY", "MOST", "VERY", "SOME", "TIME", "JUST", "KNOW", "TAKE", "PEOPLE",
    "THEN", "THAN", "ONLY", "ALSO", "EVEN", "LOOK", "WANT", "BECAUSE", "GOOD",
    # Short words
    "TO", "IN", "ON", "OF", "AT", "BY", "AS", "IF", "OR", "SO", "IT", "IS", "AM",
    "ARE", "WAS", "BE", "AN", "UP", "DO", "GO", "ME", "MY", "WE", "US", "HE", "HI",
    "YOU", "ALL", "NOT", "CAN", "MAY", "ANY", "OUT", "OFF", "NEW", "NOW", "ONE",
    # 4-Letter & Common Words
    "LIKE", "WEEK", "STILL", "YOUR", "HAVE", "WERE", "THEY", "WITH", "THIS",
    "THAT", "FROM", "WHEN", "WENT", "TIME", "SOME", "MORE", "MOST", "ONLY",
    "ALSO", "EVEN", "LOOK", "WANT", "BEEN", "WILL", "MUCH", "OVER", "SAME",
    "THEY", "THEM", "THEN", "THAN", "EACH", "YOUR", "DONE", "HERE", "MUST",
    "WHICH", "THEIR", "THERE", "WOULD", "COULD", "AMP", "ABOVE", "BELOW",
    "AFTER", "BEFORE", "THESE", "THOSE", "OTHER", "INTO", "UNDER", "ABOUT",
    "RIGHT", "LEFT", "FULL", "PART", "SIDE", "PAST", "NEXT", "LAST", "HERE",
    "TRADE", "TRADING", "GAIN", "GAINS", "LOSS", "LOSSES", "COST", "PRICE",
    "GET", "GOT", "TAKE", "TOOK", "MAKE", "MADE", "KNOW", "THINK", "SAY", "SAID",
    "DAY", "DAYS", "WEEK", "WEEKS", "YEAR", "YEARS", "MONTH", "MONTHS",
    "CALLS", "PUTS", "PLAYS", "PLAY", "STRIKE", "LONG", "SHORT", "BULL", "BEAR",
    "HIGH", "LOW", "OPEN", "CLOSE", "FREE", "OLD", "NEW", "BIG", "SMALL", "VERY",
    "AGAIN", "SEE", "SEEN", "GOING", "GOES", "WENT", "GONE", "OWN", "OWNS", "OWNED",
    "AUTO", "CARS", "CAR", "TECH", "WELL", "BEST", "BAD", "WORSE", "WORST",
    "WEBP", "WIDTH", "PNG", "REDD", "JPG", "JPEG", "GIF", "SVG", "HEIGHT", "ASSET",
    "PREVIEW", "FORMAT", "REDDIT", "SUBREDDIT", "POST", "COMMENT", "USER", "NAME",
    "LINK", "URL", "HTTPS", "HTTP", "WWW", "COM", "NET", "ORG", "INFO", "BLOG",
    "PRE", "POST", "EDIT", "UPDATE", "FIX", "FIXED", "BUG", "TEST", "VERSION",
    "FILE", "DATA", "JSON", "XML", "HTML", "CSS", "JS", "PYTHON", "CODE",
    "EVER", "NEVER", "HAS", "HAD", "THING", "THINGS", "LOL", "LMAO", "ROFL",
    "SINCE", "BEAT", "BEATING", "TOO", "MOVE", "MOVING", "MOVED", "LOOK", "LOOKS",
    "LOOKING", "LOOKED", "WAIT", "WAITING", "WAITED", "STOP", "STOPPED",
    "STOPPING", "START", "STARTED", "STARTING", "KEEP", "KEPT", "KEEPING",
    "FEEL", "FEELS", "FEELING", "FELT", "HOPE", "HOPES", "HOPING", "HOPED",
    "COULD", "SHOULD", "WOULD", "MIGHT", "MAYBE", "PROBABLY", "REALLY", "VERY",
    "ACTUALLY", "SURE", "TRUE", "FALSE", "REAL", "FAKE", "YES", "NO", "MAY",
    # 2-letter contractions/fragments
    "VE", "RE", "LL", "NT", "ST", "RD", "TH", "ND",
    # WSB Slang & Terms
    "YOLO", "APE", "MOON", "PUMP", "DUMP", "CALL", "PUTS", "ITM", "OTM", "DD",
    "BTFD", "FOMO", "GAINS", "LOSS", "MOASS", "ROCKET", "LAMBO", "TENDIES",
    "DIAMOND", "HANDS", "HODL", "ATH", "BULL", "BEAR", "LONG", "SHORT", "STRIKE",
    "EXP", "DATE", "BUY", "SELL", "HOLD", "POST", "EDIT", "GUYS", "GUY", "MEME",
    # Common Abbreviations/Tech
    "USA", "CEO", "FED", "BTC", "ETH", "USD", "IPO", "ETF", "SPY", "QQQ", "DIA",
    "IWM", "HTTPS", "WWW", "COM", "ORG", "NET", "URL", "JSON", "API", "CPU", "GPU",
    "AI", "GPT", "LLM", "TECH", "STOCK", "SHARE", "CASH", "MONEY", "MARKET"
}

_TICKER_PATTERN = re.compile(r"\b[A-Z]{2,5}\b")


def extract_tickers(text: str) -> list[str]:
    """Extract potential stock tickers from text using regex and a blacklist."""
    if not text:
        return []
    candidates = _TICKER_PATTERN.findall(text)
    return [c for c in candidates if c not in _TICKER_BLACKLIST]


def fetch_daily_top_tickers(limit: int = 100) -> list[tuple[str, int]]:
    """Fetch top posts from r/wallstreetbets and return top 10 mentioned tickers."""
    url = f"{_BASE_URL}/r/wallstreetbets/top.json?t=day&limit={limit}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        children = resp.json()["data"]["children"]
    except Exception:
        return []

    ticker_counts = Counter()
    for child in children:
        d = child.get("data", {})
        text = (d.get("title", "") + " " + d.get("selftext", "")).upper()
        tickers = extract_tickers(text)
        # Use set to count each ticker once per post to avoid spam skewing results
        for t in set(tickers):
            ticker_counts[t] += 1

    return ticker_counts.most_common(10)

_TICKER_SUBREDDITS: dict[str, list[str]] = {
    "AAPL":  ["apple"],
    "MSFT":  ["microsoft", "windowsphone"],
    "GOOGL": ["google", "alphabet"],
    "GOOG":  ["google", "alphabet"],
    "AMZN":  ["amazon"],
    "META":  ["facebook", "instagram"],
    "TSLA":  ["teslamotors", "electricvehicles"],
    "NVDA":  ["nvidia", "hardware"],
    "AMD":   ["amd", "hardware"],
    "INTC":  ["intel", "hardware"],
    "NFLX":  ["netflix"],
    "DIS":   ["disney"],
    "PYPL":  ["paypal", "fintech"],
    "SQ":    ["square", "fintech"],
    "COIN":  ["coinbase", "cryptocurrency"],
    "GME":   ["gme", "superstonk"],
    "AMC":   ["amcstock"],
    "PLTR":  ["palantir"],
    "RBLX":  ["roblox"],
    "UBER":  ["uber"],
    "LYFT":  ["lyft"],
    "SPOT":  ["spotify"],
    "TWTR":  ["twitter"],
    "SNAP":  ["snapchat"],
    "PINS":  ["pinterest"],
    "ZM":    ["zoom"],
    "SHOP":  ["shopify"],
    "SE":    ["seagrouphq"],
    "NIO":   ["nio"],
    "BABA":  ["alibaba"],
    "JNJ":   ["johnson_johnson"],
    "PFE":   ["pfizer"],
    "MRNA":  ["modernatx"],
    "BA":    ["boeing"],
    "GS":    ["goldmansachs"],
    "JPM":   ["jpmorgan"],
    "BAC":   ["bankofamerica"],
    "WFC":   ["wellsfargo"],
    "XOM":   ["exxon"],
    "CVX":   ["chevron"],
    "WMT":   ["walmart"],
    "TGT":   ["target"],
    "COST":  ["costco"],
    "HD":    ["homedepot"],
}

_POSTS_PER_SUBREDDIT = 25
TOP_N = 5


def _fetch_subreddit_posts(subreddit: str, ticker: str) -> list[dict]:
    url = (
        f"{_BASE_URL}/r/{subreddit}/search.json"
        f"?q={ticker}&restrict_sr=1&sort=top&t=month&limit={_POSTS_PER_SUBREDDIT}"
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
            "subreddit":    subreddit.lower(),
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


def fetch_top_posts_for_ticker(ticker: str) -> tuple[list[dict], list[str]]:
    """Fetch the top TOP_N posts for ticker across all relevant subreddits.

    Returns:
        posts: list of up to TOP_N post dicts, sorted descending by score
        subreddits_searched: list of subreddit names that were queried
    """
    ticker_upper = ticker.upper()

    ticker_specific = _TICKER_SUBREDDITS.get(ticker_upper, [])
    all_subreddits: list[str] = list(dict.fromkeys(
        _GENERAL_SUBREDDITS + ticker_specific
    ))

    seen_ids: set[str] = set()
    all_posts: list[dict] = []
    for subreddit in all_subreddits:
        for post in _fetch_subreddit_posts(subreddit, ticker_upper):
            if post["post_id"] not in seen_ids:
                seen_ids.add(post["post_id"])
                all_posts.append(post)

    all_posts.sort(key=lambda p: p.get("score", 0), reverse=True)
    top_posts = all_posts[:TOP_N]

    return top_posts, all_subreddits
