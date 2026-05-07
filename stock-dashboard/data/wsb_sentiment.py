import json
import re
import subprocess

_PROMPT_TEMPLATE = """\
You are a financial sentiment analyzer for Reddit posts about stocks.
Analyze the sentiment of this Reddit post specifically regarding the stock ticker {ticker}.

Post Title: {title}
Post Body: {body}

Respond ONLY with a JSON object in this exact format with no extra text:
{{"sentiment_score": <float between -1.0 and 1.0>, "sentiment_label": "<positive|negative|neutral>"}}

Rules:
- sentiment_score: -1.0 is extremely bearish, 0.0 is neutral, 1.0 is extremely bullish
- sentiment_label: must be exactly one of "positive", "negative", or "neutral"
- Base the analysis only on how the post discusses {ticker}, not other tickers mentioned
"""

_BATCH_PROMPT_TEMPLATE = """\
You are a financial sentiment analyzer. You will be given up to 5 Reddit posts about the stock ticker {ticker}, sourced from multiple subreddits. Read all posts together and form a holistic view.

Posts:
{posts_block}

Respond ONLY with a JSON object in this exact format with no extra text:
{{
  "sentiment_score": <float between -1.0 and 1.0>,
  "sentiment_label": "<positive|negative|neutral>",
  "summary": "<2-4 sentence paragraph summarizing what Reddit is saying about {ticker}, written in plain English for an investor>"
}}

Rules:
- sentiment_score: -1.0 is extremely bearish, 0.0 is neutral, 1.0 is extremely bullish
- sentiment_label: must be exactly one of "positive", "negative", or "neutral"
- summary: must be 2-4 sentences, must mention specific themes from the posts, written as if briefing a retail investor
- Base the entire analysis only on how the posts discuss {ticker}, not other tickers mentioned
"""


def _run_gemini(prompt: str) -> str:
    """Call the Gemini CLI and return stdout. Returns empty string on any failure."""
    try:
        result = subprocess.run(
            ["gemini.cmd", "-p", prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
        return result.stdout.strip()
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

    result = {"sentiment_score": score, "sentiment_label": label}
    if require_summary:
        result["summary"] = str(data.get("summary", "")).strip()
    return result


def analyze_sentiment(title: str, body: str, ticker: str) -> dict:
    """Return {"sentiment_score": float, "sentiment_label": str}."""
    _default = {"sentiment_score": 0.0, "sentiment_label": "neutral"}
    prompt = _PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        title=title,
        body=body[:2000],
    )
    raw = _run_gemini(prompt)
    if not raw:
        return _default
    return _parse_json_response(raw) or _default


def analyze_batch_sentiment(posts: list[dict], ticker: str) -> dict:
    """Analyze a batch of posts together and return aggregate sentiment + summary."""
    _default = {"sentiment_score": 0.0, "sentiment_label": "neutral", "summary": ""}
    if not posts:
        return _default

    lines = []
    for i, post in enumerate(posts, start=1):
        title = post.get("title", "").strip()
        body  = post.get("body", "").strip()[:500]
        sub   = post.get("subreddit", "unknown")
        score = post.get("score", 0)
        lines.append(
            f"[Post {i}] r/{sub} | {score} upvotes\n"
            f"Title: {title}\n"
            f"Body: {body if body else '(link post, no body)'}"
        )

    prompt = _BATCH_PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        posts_block="\n\n".join(lines),
    )
    raw = _run_gemini(prompt)
    if not raw:
        return _default
    return _parse_json_response(raw, require_summary=True) or _default
