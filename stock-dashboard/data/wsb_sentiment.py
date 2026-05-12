import json
import re
import subprocess

from data.gemini_tracker import record_call

_PROMPT_TEMPLATE = """\
You are a specialized Reddit Sentiment Analyst (Persona: reddit-analyst).
Analyze the sentiment of this Reddit post regarding the ticker {ticker}.

Post Title: {title}
Post Body: {body}

Rules:
1. **Ticker Context**: Focus ONLY on {ticker}. Ignore other tickers mentioned unless they are being compared.
2. **Slang & Sarcasm**: Understand Reddit slang:
   - "To the moon", "Diamond hands", "Rocket", "Tendies" = Bullish
   - "Guh", "Bags", "Rugged", "Puts on my life" = Bearish
   - "YOLO" = High conviction (check direction)
   - Watch for sarcasm: self-deprecating loss porn is often Bearish sentiment but Bullish community "hype".
3. **Sentiment Score**: -1.0 (Extreme Bearish) to 1.0 (Extreme Bullish). 0.0 is Neutral.
4. **Sentiment Label**: Exactly "positive", "negative", or "neutral".

Respond ONLY with a JSON object:
{{"sentiment_score": <float>, "sentiment_label": "<label>"}}
"""

_BATCH_PROMPT_TEMPLATE = """\
You are a senior Financial Sentiment Analyst specializing in Reddit markets.
Analyze these {ticker} related posts to form a holistic "sub-vibe".

Posts:
{posts_block}

Guidelines:
- **Weighting**: Give more weight to "DD" (Due Diligence) posts than memes.
- **Clustering**: Identify if the sentiment is unified or if there's a "Bulls vs Bears" war.
- **Retail Intent**: Is the community actually buying, or just joking/meming?

Respond ONLY with a JSON object:
{{
  "sentiment_score": <float -1.0 to 1.0>,
  "sentiment_label": "<positive|negative|neutral>",
  "summary": "<2-4 sentence expert summary of the community sentiment, mentioning specific themes>",
  "hype_level": <integer 1-10>
}}
"""


def _run_gemini(prompt: str) -> str:
    """Call the Gemini CLI and return stdout. Returns empty string on any failure.

    Prompt is passed via stdin rather than as a -p argument to avoid cmd.exe
    interpreting angle brackets (<positive|negative|neutral>) as I/O redirects,
    which silently produced empty output and rc=255.
    The empty -p "" flag keeps the CLI in headless (non-interactive) mode.
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

    result = {"sentiment_score": score, "sentiment_label": label}
    if require_summary:
        result["summary"] = str(data.get("summary", "")).strip()
        result["hype_level"] = int(data.get("hype_level", 0))
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
    _default = {"sentiment_score": 0.0, "sentiment_label": "neutral", "summary": "", "hype_level": 0}
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
