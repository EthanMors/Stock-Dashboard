"""FRED (Federal Reserve Economic Data) fetcher.

Pulls a small set of high-signal US macro indicators from the FRED REST API:

    unemployment      UNRATE     Civilian unemployment rate (%)
    fed_funds         FEDFUNDS   Effective federal funds (borrowing) rate (%)
    cpi               CPIAUCSL   Consumer Price Index — used to derive YoY inflation
    treasury_10y      DGS10      10-Year Treasury constant-maturity yield (%)
    real_gdp          GDPC1      Real Gross Domestic Product ($B, chained)

Requires a free FRED API key in the environment as ``FRED_API_KEY``
(https://fred.stlouisfed.org/docs/api/api_key.html).

Public API
----------
is_configured() -> bool
    True when ``FRED_API_KEY`` is present.

get_macro_indicators() -> dict
    Returns ``{series_key: indicator_dict, ..., "_meta": {...}}`` for the five
    tracked series. Each indicator_dict has: id, label, units, value, date,
    previous, change, pct_change, extra (e.g. ``yoy_inflation_pct`` for CPI).
    A failed series carries an ``"_error"`` key instead of values.
"""

import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

_API_KEY = os.getenv("FRED_API_KEY", "")
_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# series_key -> (FRED series id, human label, units, observations to pull)
# Pull a few extra observations so derived figures (YoY, last valid daily
# reading) survive missing/holiday gaps in the raw series.
_SERIES = {
    "unemployment": ("UNRATE",   "Unemployment Rate",      "%",     13),
    "fed_funds":    ("FEDFUNDS", "Federal Funds Rate",     "%",     13),
    "cpi":          ("CPIAUCSL", "CPI Inflation (YoY)",    "%",     14),
    "treasury_10y": ("DGS10",    "10-Year Treasury Yield", "%",     30),
    "real_gdp":     ("GDPC1",    "Real GDP",               "$B",     6),
}


def is_configured() -> bool:
    """True when a FRED API key is available."""
    return bool(_API_KEY)


def _fetch_observations(series_id: str, limit: int) -> list[tuple[str, float]]:
    """Return ``[(date, value), ...]`` newest-first, skipping missing values."""
    params = {
        "series_id": series_id,
        "api_key": _API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    resp = requests.get(_BASE_URL, params=params, timeout=15)
    resp.raise_for_status()
    observations = resp.json().get("observations", [])

    clean: list[tuple[str, float]] = []
    for obs in observations:
        raw = obs.get("value")
        if raw in (".", "", None):  # FRED uses "." for missing readings
            continue
        try:
            clean.append((obs["date"], float(raw)))
        except (ValueError, KeyError):
            continue
    return clean  # newest first


def _build_indicator(key: str, series_id: str, label: str, units: str, limit: int) -> dict:
    """Fetch one series and assemble its indicator dict."""
    base = {"id": series_id, "label": label, "units": units}
    try:
        obs = _fetch_observations(series_id, limit)
    except Exception as exc:  # network / HTTP / JSON failure
        return {**base, "_error": str(exc)}

    if not obs:
        return {**base, "_error": "No observations returned."}

    date, value = obs[0]
    prev = obs[1][1] if len(obs) > 1 else None
    change = (value - prev) if prev is not None else None
    pct_change = (change / prev * 100.0) if (prev not in (None, 0)) else None

    extra: dict = {}
    headline = value
    if key == "cpi" and len(obs) >= 13:
        # Convert the raw index level into a year-over-year inflation rate.
        year_ago = obs[12][1]
        if year_ago:
            yoy = (value / year_ago - 1.0) * 100.0
            extra["yoy_inflation_pct"] = yoy
            extra["index_level"] = value
            headline = yoy  # display inflation %, not the index number

    return {
        **base,
        "value": headline,
        "raw_value": value,
        "date": date,
        "previous": prev,
        "change": change,
        "pct_change": pct_change,
        "extra": extra,
    }


def get_macro_indicators() -> dict:
    """Fetch all five tracked macro indicators.

    Returns a dict keyed by series_key plus a ``"_meta"`` entry. When the API
    key is missing the result is ``{"_meta": {"configured": False, "error": ...}}``.
    """
    if not _API_KEY:
        return {"_meta": {"configured": False,
                          "error": "FRED_API_KEY not set in .env"}}

    result: dict = {
        "_meta": {
            "configured": True,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }
    }
    for key, (series_id, label, units, limit) in _SERIES.items():
        result[key] = _build_indicator(key, series_id, label, units, limit)
    return result
