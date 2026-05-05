import os
import json
import re
from datetime import datetime, timezone

import google.generativeai as genai

_API_KEY = os.getenv("GEMINI_API_KEY", "")
if _API_KEY:
    genai.configure(api_key=_API_KEY)

_MODEL_NAME = "gemini-1.5-flash"
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


def analyze_sentiment(title: str, body: str, ticker: str) -> dict:
    """Return {"sentiment_score": float, "sentiment_label": str}.
    Returns neutral defaults on any failure."""
    _default = {"sentiment_score": 0.0, "sentiment_label": "neutral"}

    if not _API_KEY:
        return _default

    prompt = _PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        title=title,
        body=body[:2000],  # cap body length to stay within token limits
    )

    try:
        model = genai.GenerativeModel(_MODEL_NAME)
        response = model.generate_content(prompt)
        raw = response.text.strip()

        # Extract JSON object from the response using regex
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return _default

        data = json.loads(match.group())
        score = float(data.get("sentiment_score", 0.0))
        score = max(-1.0, min(1.0, score))  # clamp to valid range

        label = data.get("sentiment_label", "neutral")
        if label not in ("positive", "negative", "neutral"):
            # Infer label from score if the model returned an unexpected string
            if score > 0.1:
                label = "positive"
            elif score < -0.1:
                label = "negative"
            else:
                label = "neutral"

        return {"sentiment_score": score, "sentiment_label": label}

    except Exception:
        return _default
