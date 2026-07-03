"""
data/twitter_fetcher.py

Fetches tweets for a stock ticker using the twscrape library.
twscrape is async-based; all public functions in this module wrap
async calls with asyncio.run() (with nest_asyncio applied first to
handle Streamlit's internal event loop).
"""

import asyncio
import os
from datetime import datetime, timezone

import nest_asyncio

# Apply nest_asyncio once at import time so asyncio.run() works
# inside Streamlit's already-running event loop.
nest_asyncio.apply()

import twscrape  # noqa: E402 — must come after nest_asyncio.apply()
from dotenv import load_dotenv
from pathlib import Path

# Resolve absolute path to .env (located in parent stock-dashboard directory)
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACCOUNTS_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "db", "twscrape_accounts.db"
)

_TWITTER_USERNAME       = os.getenv("TWITTER_USERNAME", "")
_TWITTER_PASSWORD       = os.getenv("TWITTER_PASSWORD", "")
_TWITTER_EMAIL          = os.getenv("TWITTER_EMAIL", "")
_TWITTER_EMAIL_PASSWORD = os.getenv("TWITTER_EMAIL_PASSWORD", "")


# ---------------------------------------------------------------------------
# API initialisation
# ---------------------------------------------------------------------------

def init_twitter_api() -> twscrape.API:
    """Return a configured twscrape.API instance.

    On the first call (zero accounts in the DB), adds the account from
    the four TWITTER_* env vars.  Raises ValueError if any required env
    var is missing when no accounts exist yet.
    """
    os.makedirs(os.path.dirname(_ACCOUNTS_DB_PATH), exist_ok=True)
    api = twscrape.API(pool=_ACCOUNTS_DB_PATH)

    async def _ensure_account() -> None:
        accounts = await api.pool.accounts_info()
        if len(accounts) == 0:
            # Validate env vars before attempting to add
            missing = [
                name
                for name, val in [
                    ("TWITTER_USERNAME",       _TWITTER_USERNAME),
                    ("TWITTER_PASSWORD",       _TWITTER_PASSWORD),
                    ("TWITTER_EMAIL",          _TWITTER_EMAIL),
                    ("TWITTER_EMAIL_PASSWORD", _TWITTER_EMAIL_PASSWORD),
                ]
                if not val
            ]
            if missing:
                raise ValueError(
                    f"Twitter account not configured. Missing env vars: {', '.join(missing)}. "
                    "Add them to your .env file."
                )
            await api.pool.add_account(
                username=_TWITTER_USERNAME,
                password=_TWITTER_PASSWORD,
                email=_TWITTER_EMAIL,
                email_password=_TWITTER_EMAIL_PASSWORD,
            )
            await api.pool.login_all()

    asyncio.run(_ensure_account())
    return api


# ---------------------------------------------------------------------------
# Tweet search
# ---------------------------------------------------------------------------

def search_tweets_for_ticker(ticker: str, limit: int = 20) -> list[dict]:
    """Search Twitter for tweets about *ticker* and return a list of dicts.

    Each dict has the keys:
        tweet_id, ticker, text, author_username, author_name,
        reply_count, retweet_count, like_count, quote_count,
        created_at (ISO string), url, fetched_at (ISO string)

    Returns an empty list if the API call fails or no tweets are found.
    """
    ticker_upper = ticker.upper()
    query = f"${ticker_upper} OR {ticker_upper} stock"

    async def _search(q: str, n: int) -> list[dict]:
        api = init_twitter_api()
        results: list[dict] = []
        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            async for tweet in api.search(q, limit=n):
                results.append({
                    "tweet_id":        str(tweet.id),
                    "ticker":          ticker_upper,
                    "text":            tweet.rawContent or "",
                    "author_username": tweet.user.username if tweet.user else "",
                    "author_name":     tweet.user.displayname if tweet.user else "",
                    "reply_count":     tweet.replyCount or 0,
                    "retweet_count":   tweet.retweetCount or 0,
                    "like_count":      tweet.likeCount or 0,
                    "quote_count":     tweet.quoteCount or 0,
                    "created_at":      tweet.date.strftime("%Y-%m-%dT%H:%M:%S") if tweet.date else "",
                    "url":             tweet.url or "",
                    "fetched_at":      fetched_at,
                })
        except Exception:
            pass
        return results

    try:
        return asyncio.run(_search(query, limit))
    except Exception:
        return []
