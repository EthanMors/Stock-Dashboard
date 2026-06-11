"""
data/twitter_sentiment.py

Gemini Flash sentiment analysis for tweets.
Mirrors data/wsb_sentiment.py exactly — same subprocess pattern,
same JSON parsing, same return shapes — but prompts are written
for tweet content rather than Reddit posts.
"""

import json
import re
import subprocess

from data.gemini_tracker import record_call

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SINGLE_PROMPT_TEMPLATE = """\
You are a Financial Sentiment Analyst specializing in Twitter/X social media.
Analyze the sentiment of this tweet regarding the stock ticker {ticker}.

Tweet text:
{text}

Rules:
1. Focus ONLY on sentiment toward {ticker}. Ignore other tickers unless directly compared.
2. Understand Twitter finance slang and abbreviations:
   - "To the moon", "LFG", "BTFD", "HODL", "rockets" = Bullish
   - "Dump it", "Rug pull", "Bags", "Down bad", "puts" = Bearish
   - Watch for sarcasm and irony — context matters.
3. sentiment_score: float from -1.0 (extreme bearish) to 1.0 (extreme bullish). 0.0 = neutral.
4. sentiment_label: exactly "positive", "negative", or "neutral".

Respond ONLY with a JSON object:
{{"sentiment_score": <float>, "sentiment_label": "<label>"}}
"""

_BATCH_PROMPT_TEMPLATE = """\
You are a senior Financial Sentiment Analyst specializing in Twitter/X social media.
Analyze these {ticker} related tweets to form a holistic sentiment picture.

Tweets:
{tweets_block}

Guidelines:
- Weight tweets with higher engagement (likes, retweets) more heavily.
- Identify if the community is unified or split between bulls and bears.
- Note if the activity appears to be organic or coordinated/hype-driven.
- hype_level: integer 1 (no buzz) to 10 (extreme viral hype).

Respond ONLY with a JSON object:
{{
  "sentiment_score": <float -1.0 to 1.0>,
  "sentiment_label": "<positive|negative|neutral>",
  "summary": "<2-4 sentence expert summary of the Twitter sentiment, mentioning specific themes>",
  "hype_level": <integer 1-10>
}}
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_gemini(prompt: str) -> str:
    """Call the Gemini Flash CLI and return stdout.

    Prompt is passed via stdin (not as -p argument) to avoid shell
    interpretation of special characters like < > | in tweet text.
    Returns empty string on any failure.
    """
    try:
        result = subprocess.run(
            ["gemini.cmd", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
        output = result.stdout.strip()
        if output:
            record_call("flash")
        return output
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""


def _parse_json_response(raw: str, require_summary: bool = False) -> dict | None:
    """Extract and parse the first JSON object from a Gemini response string."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    score = float(data.get("sentiment_score", 0.0))
    score = max(-1.0, min(1.0, score))
    label = data.get("sentiment_label", "neutral")
    if label not in ("positive", "negative", "neutral"):
        label = "positive" if score > 0.1 else "negative" if score < -0.1 else "neutral"

    result: dict = {"sentiment_score": score, "sentiment_label": label}
    if require_summary:
        result["summary"]    = str(data.get("summary", "")).strip()
        result["hype_level"] = int(data.get("hype_level", 0))
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_tweet_sentiment(text: str, ticker: str) -> dict:
    """Analyze a single tweet's sentiment toward *ticker*.

    Returns:
        {"sentiment_score": float, "sentiment_label": str}
        Falls back to {"sentiment_score": 0.0, "sentiment_label": "neutral"}
        on any Gemini failure.
    """
    _default = {"sentiment_score": 0.0, "sentiment_label": "neutral"}
    prompt = _SINGLE_PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        text=text[:2000],
    )
    raw = _run_gemini(prompt)
    if not raw:
        return _default
    return _parse_json_response(raw) or _default


def analyze_batch_tweet_sentiment(tweets: list[dict], ticker: str) -> dict:
    """Analyze a batch of tweets together and return aggregate sentiment + summary.

    Args:
        tweets: list of tweet dicts (each must have at least "text",
                "like_count", "retweet_count" keys)
        ticker: stock ticker symbol

    Returns:
        {"sentiment_score": float, "sentiment_label": str,
         "summary": str, "hype_level": int}
        Falls back to all-zero/neutral defaults on Gemini failure.
    """
    _default = {
        "sentiment_score": 0.0,
        "sentiment_label": "neutral",
        "summary":         "",
        "hype_level":      0,
    }
    if not tweets:
        return _default

    lines = []
    for i, tweet in enumerate(tweets, start=1):
        text    = tweet.get("text", "").strip()[:500]
        likes   = tweet.get("like_count", 0)
        rts     = tweet.get("retweet_count", 0)
        author  = tweet.get("author_username", "unknown")
        lines.append(
            f"[Tweet {i}] @{author} | {likes} likes | {rts} retweets\n"
            f"Text: {text if text else '(no text)'}"
        )

    prompt = _BATCH_PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        tweets_block="\n\n".join(lines),
    )
    raw = _run_gemini(prompt)
    if not raw:
        return _default
    return _parse_json_response(raw, require_summary=True) or _default
