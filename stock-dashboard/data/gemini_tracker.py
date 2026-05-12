import json
import os
from datetime import datetime

_USAGE_FILE = os.path.join(os.path.dirname(__file__), "gemini_usage.json")

# Gemini CLI daily request limits (Pro tier / Google One AI Premium).
# Adjust these if your account has different limits.
FLASH_DAILY_LIMIT = 1000
PRO_DAILY_LIMIT = 50

_EMPTY_DAY = {
    "date": "",
    "total": 0,
    "flash": 0,
    "pro": 0,
    "last_request": None,
}


def _load() -> dict:
    if not os.path.exists(_USAGE_FILE):
        return dict(_EMPTY_DAY)
    try:
        with open(_USAGE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return dict(_EMPTY_DAY)


def _save(data: dict) -> None:
    try:
        with open(_USAGE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def record_call(model: str) -> None:
    """Increment the request counter for 'flash' or 'pro'. Resets at midnight."""
    if model not in ("flash", "pro"):
        return

    data = _load()
    today = _today_str()

    if data.get("date") != today:
        data = dict(_EMPTY_DAY)
        data["date"] = today

    data["total"] = data.get("total", 0) + 1
    data[model] = data.get(model, 0) + 1
    data["last_request"] = datetime.now().strftime("%H:%M:%S")

    _save(data)


def get_today_stats() -> dict:
    """Return {"date", "total", "flash", "pro", "last_request"} for today."""
    data = _load()
    today = _today_str()

    if data.get("date") != today:
        return {**_EMPTY_DAY, "date": today}

    return {
        "date": data.get("date", today),
        "total": int(data.get("total", 0)),
        "flash": int(data.get("flash", 0)),
        "pro": int(data.get("pro", 0)),
        "last_request": data.get("last_request"),
    }
