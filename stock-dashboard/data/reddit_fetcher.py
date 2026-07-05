"""Reddit data access layer with multi-backend fallback.

Reddit now returns 403 for unauthenticated calls to its ``.json`` endpoints from
most client IPs, which used to silently empty out every Reddit feature in the
app. Each fetch therefore tries, in order:

1. Reddit JSON API   — richest data (score, comments, selftext); often blocked.
2. Reddit RSS (Atom) — served with a browser User-Agent; no vote/comment counts.
3. pullpush.io       — public Pushshift mirror; full metadata but can lag/rate-limit.

All requests go through a shared throttle (one request per ~1.5 s, single retry
on 429) so bursts from batch pages don't trip rate limits. When every backend
fails, ``get_last_error()`` explains why so pages can distinguish "Reddit is
unreachable" from "nobody is talking about this stock".
"""

import html
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone

import requests

_REDDIT_USERNAME = os.getenv("REDDIT_USERNAME", "stock_dashboard_user")
_SCRIPT_HEADERS = {
    "User-Agent": f"python:StockDashboardWSB:v1.0.0 (by /u/{_REDDIT_USERNAME})"
}
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_BASE_URL = "https://www.reddit.com"
_PULLPUSH_URL = "https://api.pullpush.io/reddit/search/submission/"
_TIMEOUT = 12

_GENERAL_SUBREDDITS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "stockmarket",
]

# ---------------------------------------------------------------------------
# Shared request throttle — Reddit and pullpush both 429 quickly on bursts.
# ---------------------------------------------------------------------------

_MIN_REQUEST_GAP = 3.0  # seconds between any two outbound requests
_throttle_lock = threading.Lock()
_last_request_at = 0.0

# Once Reddit's JSON API returns 403 (unauthenticated block), skip JSON
# attempts for a while instead of burning two doomed requests per fetch.
_JSON_BLOCK_TTL = 900.0
_json_blocked_until = 0.0

_last_error: str = ""


def get_last_error() -> str:
    """Human-readable reason the most recent fetch returned nothing ('' if OK)."""
    return _last_error


def _throttled_get(url: str, headers: dict, params: dict | None = None):
    """GET with global rate spacing and one backoff retry on 429. None on failure."""
    global _last_request_at
    for attempt in (1, 2):
        with _throttle_lock:
            wait = _MIN_REQUEST_GAP - (time.time() - _last_request_at)
            if wait > 0:
                time.sleep(wait)
            _last_request_at = time.time()
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
        except Exception:
            return None
        if resp.status_code == 429 and attempt == 1:
            time.sleep(10)
            continue
        return resp
    return None


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
    "AI", "GPT", "LLM", "TECH", "STOCK", "SHARE", "CASH", "MONEY", "MARKET",
    # Additional common words observed leaking through as fake tickers
    "TODAY", "ITS", "WHY", "LOST", "DOWN", "WAY", "AGO", "END", "RISK", "WSB",
    "HOW", "WHO", "DID", "OUR", "HIS", "HER", "SHE", "HIM", "WAS", "ARE", "GOT",
    "TLDR", "IMO", "IMHO", "EPS", "PE", "YTD", "EOD", "AH", "PM", "AM", "EST",
    "PDT", "IRA", "401K", "FUND", "BANK", "DEBT", "RATE", "RATES", "NEWS",
    "LIVE", "SOON", "HUGE", "MASSIVE", "TOTAL", "FINAL", "DAILY", "HOURLY",
    "WEEKLY", "GREEN", "RED", "FLAT", "PLUS", "LESS", "HALF", "TWICE", "ONCE",
    # Real tradable symbols that are almost always English words on Reddit.
    # ($TV-style cashtags still count them.)
    "TV", "NOTE", "LINE", "PLAY", "SAFE", "NICE", "COOL", "EAT", "RUN", "JOB",
}

_TICKER_PATTERN = re.compile(r"\b[A-Z]{2,5}\b")
# $tsla / $TSLA — the dollar sign is an explicit ticker signal, so the
# blacklist does not apply and 1-letter tickers (e.g. $F) are allowed.
_DOLLAR_TICKER_PATTERN = re.compile(r"\$([A-Za-z]{1,5})\b")

# Company-name → ticker map so posts that never write the symbol still count
# (e.g. "Nvidia is unstoppable" → NVDA). Names are matched case-insensitively
# as whole words.
_NAME_TO_TICKER: dict[str, str] = {
    "apple": "AAPL", "microsoft": "MSFT", "google": "GOOGL", "alphabet": "GOOGL",
    "amazon": "AMZN", "meta": "META", "facebook": "META", "tesla": "TSLA",
    "nvidia": "NVDA", "netflix": "NFLX", "disney": "DIS", "paypal": "PYPL",
    "coinbase": "COIN", "gamestop": "GME", "palantir": "PLTR", "roblox": "RBLX",
    "uber": "UBER", "lyft": "LYFT", "spotify": "SPOT", "snapchat": "SNAP",
    "pinterest": "PINS", "shopify": "SHOP", "alibaba": "BABA", "pfizer": "PFE",
    "moderna": "MRNA", "boeing": "BA", "goldman": "GS", "jpmorgan": "JPM",
    "walmart": "WMT", "costco": "COST", "intel": "INTC", "micron": "MU",
    "broadcom": "AVGO", "qualcomm": "QCOM", "salesforce": "CRM", "oracle": "ORCL",
    "adobe": "ADBE", "starbucks": "SBUX", "mcdonald's": "MCD", "mcdonalds": "MCD",
    "nike": "NKE", "berkshire": "BRK.B", "exxon": "XOM", "chevron": "CVX",
    "rivian": "RIVN", "lucid": "LCID", "opendoor": "OPEN", "sofi": "SOFI",
    "robinhood": "HOOD", "airbnb": "ABNB", "doordash": "DASH", "snowflake": "SNOW",
    "datadog": "DDOG", "crowdstrike": "CRWD", "cloudflare": "NET",
    "supermicro": "SMCI", "super micro": "SMCI", "arm holdings": "ARM",
    "vertex": "VRTX", "nextera": "NEE", "nebius": "NBIS", "carvana": "CVNA",
    "unitedhealth": "UNH", "eli lilly": "LLY", "novo nordisk": "NVO",
    "taiwan semi": "TSM", "tsmc": "TSM", "amd": "AMD",
}
# Reverse map for building search queries: ticker → canonical company name.
_TICKER_TO_NAME: dict[str, str] = {}
for _name, _tick in _NAME_TO_TICKER.items():
    _TICKER_TO_NAME.setdefault(_tick, _name)

_name_cache: dict[str, str] = {}
_NAME_SUFFIX_RE = re.compile(
    r",?\s+(inc\.?|corp\.?|corporation|company|co\.?|ltd\.?|plc|holdings?|"
    r"group|technologies|technology|pharmaceuticals?|therapeutics)\.?$",
    re.IGNORECASE,
)


def _company_name(ticker: str) -> str:
    """Best-effort company name for *ticker* ('' if unknown).

    Checks the static map first, then yfinance (cached per process). Corporate
    suffixes are stripped so search queries match casual Reddit usage.
    """
    ticker = ticker.upper()
    if ticker in _TICKER_TO_NAME:
        return _TICKER_TO_NAME[ticker]
    if ticker in _name_cache:
        return _name_cache[ticker]
    name = ""
    try:
        import yfinance as yf
        raw = yf.Ticker(ticker).info.get("shortName", "") or ""
        name = _NAME_SUFFIX_RE.sub("", raw).strip()
        # A "name" identical to the ticker adds nothing to the search query.
        if name.upper() == ticker or len(name) < 3:
            name = ""
    except Exception:
        name = ""
    _name_cache[ticker] = name.lower()
    return _name_cache[ticker]


def extract_tickers(text: str) -> list[str]:
    """Extract stock tickers from text.

    Matches, in decreasing confidence:
    - $TSLA-style cashtags (any case, blacklist not applied)
    - bare uppercase symbols (blacklist applied)
    - well-known company names ("nvidia" → NVDA)
    """
    if not text:
        return []
    found: list[str] = []
    for m in _DOLLAR_TICKER_PATTERN.findall(text):
        found.append(m.upper())
    upper = text.upper()
    found.extend(c for c in _TICKER_PATTERN.findall(upper) if c not in _TICKER_BLACKLIST)
    lower = text.lower()
    for name, tick in _NAME_TO_TICKER.items():
        if re.search(rf"\b{re.escape(name)}\b", lower):
            found.append(tick)
    return found


# ---------------------------------------------------------------------------
# Backend response normalization
# ---------------------------------------------------------------------------

def _post_from_reddit_child(d: dict, ticker: str = "") -> dict | None:
    post_id = d.get("id")
    if not post_id:
        return None
    return {
        "post_id":      post_id,
        "ticker":       ticker.upper(),
        "subreddit":    str(d.get("subreddit", "")).lower(),
        "title":        d.get("title", ""),
        "body":         d.get("selftext", ""),
        "author":       d.get("author", ""),
        "score":        d.get("score", 0),
        "num_comments": d.get("num_comments", 0),
        "created_utc":  d.get("created_utc"),
        "url":          d.get("url", ""),
        "permalink":    _BASE_URL + d.get("permalink", ""),
        "fetched_at":   datetime.now(timezone.utc).isoformat(),
    }


_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_POST_ID_RE = re.compile(r"/comments/([a-z0-9]+)/")


def _posts_from_rss(xml_text: str, ticker: str = "") -> list[dict]:
    """Parse a Reddit Atom feed into post dicts (score/comments unavailable → 0)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    posts = []
    for entry in root.findall("a:entry", _ATOM_NS):
        link_el = entry.find("a:link", _ATOM_NS)
        link = link_el.get("href", "") if link_el is not None else ""
        id_match = _POST_ID_RE.search(link)
        if not id_match:
            continue
        content = entry.findtext("a:content", "", _ATOM_NS) or ""
        body = html.unescape(_HTML_TAG_RE.sub(" ", content))
        body = re.sub(r"\s+", " ", body).replace("[link] [comments]", "").strip()
        author_el = entry.find("a:author/a:name", _ATOM_NS)
        category = entry.find("a:category", _ATOM_NS)
        sub = category.get("term", "") if category is not None else ""
        published = entry.findtext("a:published", "", _ATOM_NS)
        created = None
        if published:
            try:
                created = datetime.fromisoformat(published).timestamp()
            except ValueError:
                pass
        posts.append({
            "post_id":      id_match.group(1),
            "ticker":       ticker.upper(),
            "subreddit":    sub.lower(),
            "title":        entry.findtext("a:title", "", _ATOM_NS) or "",
            "body":         body[:4000],
            "author":       (author_el.text or "").lstrip("/u/") if author_el is not None else "",
            "score":        0,
            "num_comments": 0,
            "created_utc":  created,
            "url":          link,
            "permalink":    link,
            "fetched_at":   datetime.now(timezone.utc).isoformat(),
        })
    return posts


def _posts_from_pullpush(items: list[dict], ticker: str = "") -> list[dict]:
    posts = []
    for d in items:
        p = _post_from_reddit_child(d, ticker)
        if p:
            # pullpush returns full permalinks already prefixed or relative
            perma = d.get("permalink", "")
            if perma and not perma.startswith("http"):
                p["permalink"] = _BASE_URL + perma
            posts.append(p)
    return posts


# ---------------------------------------------------------------------------
# Multi-backend fetches
# ---------------------------------------------------------------------------

def _fetch_listing(path: str, params: dict, ticker: str = "") -> list[dict] | None:
    """Fetch a Reddit listing (e.g. '/r/x/top' or '/r/a+b/search') via JSON→RSS.

    Returns None when both Reddit backends are unreachable/blocked; a list
    (possibly empty) when a backend answered authoritatively.
    """
    global _json_blocked_until
    if time.time() >= _json_blocked_until:
        for headers in (_BROWSER_HEADERS, _SCRIPT_HEADERS):
            resp = _throttled_get(f"{_BASE_URL}{path}.json", headers, params)
            if resp is not None and resp.ok:
                try:
                    children = resp.json()["data"]["children"]
                except Exception:
                    continue
                return [p for c in children
                        if (p := _post_from_reddit_child(c.get("data", {}), ticker))]
            if resp is not None and resp.status_code == 403:
                _json_blocked_until = time.time() + _JSON_BLOCK_TTL
                break

    resp = _throttled_get(f"{_BASE_URL}{path}.rss", _BROWSER_HEADERS, params)
    if resp is not None and resp.ok:
        posts = _posts_from_rss(resp.text, ticker)
        if posts:
            return posts
    return None


def _fetch_pullpush(params: dict, ticker: str = "") -> list[dict] | None:
    resp = _throttled_get(_PULLPUSH_URL, _BROWSER_HEADERS, params)
    if resp is None or not resp.ok:
        return None
    try:
        items = resp.json().get("data", [])
    except Exception:
        return None
    return _posts_from_pullpush(items, ticker)


def fetch_daily_top_tickers(limit: int = 100) -> list[tuple[str, int]]:
    """Fetch top r/wallstreetbets posts of the day and return top 10 mentioned tickers."""
    global _last_error
    _last_error = ""

    posts = _fetch_listing("/r/wallstreetbets/top", {"t": "day", "limit": limit})
    if not posts:
        day_ago = int(time.time()) - 86400
        posts = _fetch_pullpush({
            "subreddit": "wallstreetbets",
            "after": day_ago,
            "sort_type": "score",
            "sort": "desc",
            "size": min(limit, 100),
        })
    if not posts:
        _last_error = (
            "Reddit and pullpush.io are both unreachable or rate-limiting "
            "(Reddit returns 403 for unauthenticated API access). Try again in a few minutes."
        )
        return []

    ticker_counts = Counter()
    for post in posts:
        text = post.get("title", "") + " " + post.get("body", "")
        # Use set to count each ticker once per post to avoid spam skewing results
        for t in set(extract_tickers(text)):
            ticker_counts[t] += 1

    validated = _validate_tickers([t for t, _ in ticker_counts.most_common(25)])
    return [(t, c) for t, c in ticker_counts.most_common(25) if t in validated][:10]


def _validate_tickers(candidates: list[str]) -> set[str]:
    """Filter candidates to symbols with real market data (one batched yfinance call).

    The blacklist can't cover every English word, so anything that survives it
    is checked against Yahoo Finance in a single batch download. Falls back to
    accepting all candidates if the batch call itself fails.
    """
    if not candidates:
        return set()
    known = {t for t in candidates if t in _TICKER_TO_NAME or t in _TICKER_SUBREDDITS}
    unknown = [t for t in candidates if t not in known and "." not in t]
    if not unknown:
        return known
    try:
        import yfinance as yf
        df = yf.download(unknown, period="5d", progress=False, group_by="ticker",
                         threads=True)
        if df is None or df.empty:
            return known
        for t in unknown:
            try:
                closes = df[t]["Close"] if len(unknown) > 1 else df["Close"]
                if not closes.dropna().empty:
                    known.add(t)
            except Exception:
                continue
        return known
    except Exception:
        return set(candidates)


_TICKER_SUBREDDITS: dict[str, list[str]] = {
    "AAPL":  ["apple"],
    "MSFT":  ["microsoft"],
    "GOOGL": ["google"],
    "GOOG":  ["google"],
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
    "SNAP":  ["snapchat"],
    "PINS":  ["pinterest"],
    "ZM":    ["zoom"],
    "SHOP":  ["shopify"],
    "NIO":   ["nio"],
    "BABA":  ["alibaba"],
    "PFE":   ["pfizer"],
    "MRNA":  ["modernatx"],
    "BA":    ["boeing"],
    "JPM":   ["jpmorgan"],
    "XOM":   ["exxon"],
    "CVX":   ["chevron"],
    "WMT":   ["walmart"],
    "TGT":   ["target"],
    "COST":  ["costco"],
    "HD":    ["homedepot"],
}

_POSTS_PER_SEARCH = 25
TOP_N = 5


def _classify_match(post: dict, ticker: str, company: str) -> str:
    """Tag how a post references the ticker: explicit symbol, company name, or related.

    Cashtags ($open) match any case; the bare symbol must be uppercase, so a
    ticker that is also an English word ("OPEN", "PLAY") isn't credited for
    casual prose like "open robinhood".
    """
    text = (post.get("title", "") + " " + post.get("body", ""))
    if re.search(rf"\${ticker}\b", text, re.IGNORECASE) or re.search(rf"\b{ticker}\b", text):
        return "explicit"
    if company and re.search(rf"\b{re.escape(company)}\b", text, re.IGNORECASE):
        return "company"
    return "related"


def fetch_top_posts_for_ticker(ticker: str) -> tuple[list[dict], list[str], str]:
    """Fetch the top TOP_N posts for ticker across all relevant subreddits.

    Searches for both the symbol and the company name, so posts that say
    "Opendoor" still count for OPEN. Each post gets a "match_type" key:
    "explicit" ($OPEN / OPEN), "company" (name only), or "related"
    (surfaced by Reddit search relevance without a direct mention).

    Returns:
        posts: list of up to TOP_N post dicts, sorted descending by score
        subreddits_searched: list of subreddit names that were queried
        error: '' on success; a user-facing message when all backends failed
    """
    global _last_error
    _last_error = ""
    ticker_upper = ticker.upper()

    ticker_specific = _TICKER_SUBREDDITS.get(ticker_upper, [])
    all_subreddits: list[str] = list(dict.fromkeys(
        _GENERAL_SUBREDDITS + ticker_specific
    ))

    company = _company_name(ticker_upper)
    query = f'{ticker_upper} OR "{company}"' if company else ticker_upper

    # One multireddit search hits every subreddit in a single request,
    # which matters now that each request is throttled.
    multi = "+".join(all_subreddits)
    posts = _fetch_listing(
        f"/r/{multi}/search",
        {"q": query, "restrict_sr": 1, "sort": "top", "t": "month",
         "limit": _POSTS_PER_SEARCH},
        ticker_upper,
    )

    if posts is None:
        month_ago = int(time.time()) - 30 * 86400
        merged: dict[str, dict] = {}
        queries = [ticker_upper] + ([company] if company else [])
        for q in queries:
            pp = _fetch_pullpush({
                "q": q,
                "subreddit": ",".join(_GENERAL_SUBREDDITS),
                "after": month_ago,
                "sort_type": "score",
                "sort": "desc",
                "size": _POSTS_PER_SEARCH,
            }, ticker_upper)
            for p in pp or []:
                merged.setdefault(p["post_id"], p)
        posts = list(merged.values()) if merged else None

    if posts is None:
        _last_error = (
            "Could not reach Reddit (403/blocked) or pullpush.io. "
            "This is a connectivity/rate-limit issue, not a lack of posts."
        )
        return [], all_subreddits, _last_error

    seen_ids: set[str] = set()
    unique_posts: list[dict] = []
    for post in posts:
        if post["post_id"] not in seen_ids:
            seen_ids.add(post["post_id"])
            post["match_type"] = _classify_match(post, ticker_upper, company)
            unique_posts.append(post)

    # Explicit/company mentions outrank search-relevance-only hits; within a
    # tier, higher upvotes first.
    rank = {"explicit": 0, "company": 0, "related": 1}
    unique_posts.sort(key=lambda p: (rank.get(p.get("match_type"), 1),
                                     -(p.get("score") or 0)))
    return unique_posts[:TOP_N], all_subreddits, ""
