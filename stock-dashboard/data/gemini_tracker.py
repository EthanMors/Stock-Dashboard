import json
import os
from datetime import datetime

_USAGE_FILE = os.path.join(os.path.dirname(__file__), "gemini_usage.json")

# AGY / Gemini CLI daily request limits.
# Can be overridden via environment variables.
FLASH_DAILY_LIMIT = int(os.getenv("AGY_FLASH_LIMIT", "1000"))
PRO_DAILY_LIMIT = int(os.getenv("AGY_PRO_LIMIT", "50"))

_EMPTY_DAY = {
    "date": "",
    "total": 0,
    "flash": 0,
    "pro": 0,
    "last_request": None,
    "last_tier": None,
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
            json.dump(data, fh, indent=2)
    except Exception:
        pass


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def record_call(model: str) -> None:
    """Increment the request counter for 'flash' or 'pro'. Resets at midnight."""
    model_lower = str(model).lower().strip()
    tier = "pro" if "pro" in model_lower else "flash"

    data = _load()
    today = _today_str()

    if data.get("date") != today:
        data = dict(_EMPTY_DAY)
        data["date"] = today

    data["total"] = data.get("total", 0) + 1
    data[tier] = data.get(tier, 0) + 1
    data["last_request"] = datetime.now().strftime("%H:%M:%S")
    data["last_tier"] = tier

    _save(data)


def can_make_call(tier: str = "flash") -> bool:
    """Check if daily quota is available for the given tier."""
    stats = get_today_stats()
    tier_lower = tier.lower().strip()
    if tier_lower == "pro":
        return stats["pro"] < PRO_DAILY_LIMIT
    return stats["flash"] < FLASH_DAILY_LIMIT


def get_today_stats() -> dict:
    """Return {"date", "total", "flash", "pro", "last_request", "last_tier"} for today."""
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
        "last_tier": data.get("last_tier"),
    }
