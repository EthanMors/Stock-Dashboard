"""Seasonality, macro-regime, and sector-rotation computation.

Pure data + math. No Streamlit, no Gemini, no printing — the page renders what
this returns and `seasonality_agent.py` interprets it.

Everything here is derived from price history (yfinance) plus a small table of
scheduled macro events, so it works with no API keys.

Public API
----------
get_monthly_seasonality(ticker, years)        -> dict
get_seasonality_matrix(tickers, years)        -> pd.DataFrame   (rows=ticker, cols=month)
get_macro_regime()                            -> dict
get_sector_momentum()                         -> list[dict]
get_portfolio_sector_exposure(positions)      -> dict
get_upcoming_catalysts(tickers, days_ahead)   -> list[dict]
build_rotation_scorecard(exposure, momentum, month) -> list[dict]
build_analysis_payload(positions)             -> dict           (everything, for the agent)
"""

import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

from data.seasonality_cache import get_cached_stats, save_cached_stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BENCHMARK = "SPY"
DEFAULT_LOOKBACK_YEARS = 20

# Sector name -> SPDR sector ETF. Mirrors data/news_analyzer.py's map so sector
# strings coming off yfinance line up across the dashboard.
SECTOR_ETFS: dict[str, str] = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Consumer Cyclical": "XLY",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Consumer Defensive": "XLP",
}
ETF_TO_SECTOR = {v: k for k, v in SECTOR_ETFS.items()}

# Approximate S&P 500 sector weights (%), used as the neutral benchmark an
# over/underweight is measured against. Refresh occasionally — these drift.
SPX_SECTOR_WEIGHTS: dict[str, float] = {
    "Technology": 32.0,
    "Financial Services": 13.5,
    "Consumer Cyclical": 10.5,
    "Healthcare": 9.5,
    "Communication Services": 9.5,
    "Industrials": 8.0,
    "Consumer Defensive": 5.5,
    "Energy": 3.5,
    "Utilities": 2.5,
    "Real Estate": 2.0,
    "Basic Materials": 1.8,
}

# Position-dict field candidates — same shapes the Webull payload uses
# elsewhere in the dashboard (see data/mpt_agent.py).
_TICKER_FIELDS = ["symbol", "ticker", "tickerSymbol", "stockSymbol", "sym"]
_MARKET_VALUE_FIELDS = [
    "marketValue", "market_value", "mktValue", "mkt_value", "positionValue",
    "position_value", "currentValue", "current_value",
]

# Macro indicator tickers. Ratios are computed as numerator/denominator.
_MACRO_TICKERS = [
    "^VIX", "^TNX", "^IRX", "^TYX", "SPY", "IWM", "XLY", "XLP",
    "HYG", "LQD", "TIP", "IEF", "GLD", "USO", "CPER", "UUP",
]

# FOMC meeting decision dates (second day of each two-day meeting).
# Update once a year when the Fed publishes the next calendar.
FOMC_DATES = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

# Recurring seasonal windows worth flagging when the date lands inside them.
SEASONAL_WINDOWS = [
    ((1, 1), (1, 5), "January effect", "Small caps and prior-year losers often bounce early in January."),
    ((5, 1), (5, 31), "'Sell in May' begins", "The weak May-October stretch starts; historically the softest six months."),
    ((8, 1), (9, 30), "August-September weak spot", "September is historically the worst month for US equities."),
    ((10, 1), (10, 31), "October volatility", "High realised vol, but often the turning point into the strong season."),
    ((11, 1), (12, 31), "Best six months", "November-April is historically the strongest stretch for equities."),
    ((12, 20), (12, 31), "Santa Claus rally", "The last five sessions of the year plus the first two of January."),
]

_MONTH_NAMES = list(calendar.month_name)[1:]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def extract_ticker(position: dict) -> str:
    """Pull the uppercase ticker out of a Webull position dict."""
    for field in _TICKER_FIELDS:
        value = position.get(field, "")
        if value and isinstance(value, str):
            return value.upper().strip()
    return ""


def extract_market_value(position: dict) -> float:
    """Pull the market value out of a position dict, falling back to qty*price."""
    for field in _MARKET_VALUE_FIELDS:
        value = position.get(field)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    try:
        qty = float(position.get("quantity", position.get("qty", 0)) or 0)
        price = float(position.get("lastPrice", position.get("last_price", 0)) or 0)
        return qty * price
    except (TypeError, ValueError):
        return 0.0


def _close_frame(tickers: list, period: str) -> pd.DataFrame:
    """Download adjusted closes for `tickers`, always as a DataFrame of columns."""
    unique = sorted({t for t in tickers if t})
    if not unique:
        return pd.DataFrame()
    try:
        raw = yf.download(
            unique,
            period=period,
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="column",
            threads=True,
        )
    except Exception:
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" not in raw.columns.get_level_values(0):
            return pd.DataFrame()
        close = raw["Close"]
    else:
        close = raw[["Close"]] if "Close" in raw.columns else raw
        close.columns = unique[: len(close.columns)]

    return close.dropna(how="all")


def _pct(series: pd.Series, days: int) -> float | None:
    """Percent change over the last `days` trading rows of a price series."""
    clean = series.dropna()
    if len(clean) <= days:
        return None
    start, end = float(clean.iloc[-days - 1]), float(clean.iloc[-1])
    if start == 0:
        return None
    return (end / start - 1.0) * 100.0


def _clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return float(max(low, min(high, value)))


# ---------------------------------------------------------------------------
# Seasonality
# ---------------------------------------------------------------------------

def _empty_stats(ticker: str) -> dict:
    return {"ticker": ticker, "years_of_data": 0, "months": {}, "quarters": {},
            "best_month": "", "worst_month": ""}


def _stats_from_series(ticker: str, series: pd.Series, years: int) -> dict:
    """Build (and cache) the month-of-year stat block from a price series."""
    empty = _empty_stats(ticker)
    series = series.dropna()
    if len(series) < 60:
        return empty

    # Month-end closes -> monthly returns in percent.
    monthly = series.resample("ME").last().dropna()
    returns = monthly.pct_change().dropna() * 100.0
    if returns.empty:
        return empty

    frame = pd.DataFrame({"ret": returns.values}, index=returns.index)
    frame["month"] = frame.index.month
    frame["quarter"] = frame.index.quarter

    months: dict[str, dict] = {}
    for month_num in range(1, 13):
        bucket = frame.loc[frame["month"] == month_num, "ret"]
        if bucket.empty:
            continue
        months[_MONTH_NAMES[month_num - 1]] = {
            "avg": round(float(bucket.mean()), 2),
            "median": round(float(bucket.median()), 2),
            "win_rate": round(float((bucket > 0).mean() * 100.0), 1),
            "best": round(float(bucket.max()), 2),
            "worst": round(float(bucket.min()), 2),
            "stdev": round(float(bucket.std(ddof=0)), 2),
            "count": int(bucket.count()),
        }

    quarters: dict[str, dict] = {}
    for quarter_num in range(1, 5):
        bucket = frame.loc[frame["quarter"] == quarter_num, "ret"]
        if bucket.empty:
            continue
        quarters[f"Q{quarter_num}"] = {
            "avg": round(float(bucket.mean()), 2),
            "win_rate": round(float((bucket > 0).mean() * 100.0), 1),
            "count": int(bucket.count()),
        }

    ranked = sorted(months.items(), key=lambda kv: kv[1]["avg"])
    stats = {
        "ticker": ticker,
        "years_of_data": int(round(len(returns) / 12.0)),
        "months": months,
        "quarters": quarters,
        "best_month": ranked[-1][0] if ranked else "",
        "worst_month": ranked[0][0] if ranked else "",
    }
    save_cached_stats(ticker, years, stats)
    return stats


def get_monthly_seasonality(ticker: str, years: int = DEFAULT_LOOKBACK_YEARS) -> dict:
    """Month-of-year return statistics for one ticker.

    Returns a dict:
        {
          "ticker": str,
          "years_of_data": int,
          "months": {
             "January": {"avg": float, "median": float, "win_rate": float,
                         "best": float, "worst": float, "count": int,
                         "stdev": float},
             ...
          },
          "quarters": {"Q1": {...}, ...},
          "best_month": str, "worst_month": str,
        }

    Returns an empty-ish dict (months == {}) when history is unavailable.
    """
    return get_seasonality_batch([ticker], years).get(
        ticker.upper().strip(), _empty_stats(ticker.upper().strip())
    )


def get_seasonality_batch(tickers: list, years: int = DEFAULT_LOOKBACK_YEARS) -> dict:
    """Month-of-year stats for many tickers, keyed by ticker.

    Cached tickers are served from SQLite; every remaining ticker is fetched in
    a single batched yfinance download rather than one request each, which is
    the difference between seconds and minutes for a full portfolio.
    """
    wanted = []
    for raw in tickers:
        ticker = str(raw).upper().strip()
        if ticker and ticker not in wanted:
            wanted.append(ticker)

    results: dict[str, dict] = {}
    missing = []
    for ticker in wanted:
        cached = get_cached_stats(ticker, years)
        if cached is not None:
            results[ticker] = cached
        else:
            missing.append(ticker)

    if missing:
        closes = _close_frame(missing, period=f"{years}y")
        for ticker in missing:
            if closes.empty or ticker not in closes.columns:
                results[ticker] = _empty_stats(ticker)
                continue
            results[ticker] = _stats_from_series(ticker, closes[ticker], years)

    return results


def _matrix_from_batch(batch: dict, tickers: list) -> pd.DataFrame:
    """Ticker x month average-return frame for the subset of `tickers` in `batch`."""
    rows: dict[str, dict] = {}
    for raw in tickers:
        ticker = str(raw).upper().strip()
        stats = batch.get(ticker)
        if not stats or not stats.get("months"):
            continue
        rows[ticker] = {
            month: stats["months"].get(month, {}).get("avg", np.nan)
            for month in _MONTH_NAMES
        }
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame.from_dict(rows, orient="index")[_MONTH_NAMES]


def get_seasonality_matrix(tickers: list, years: int = DEFAULT_LOOKBACK_YEARS) -> pd.DataFrame:
    """Average monthly return (%) per ticker — rows are tickers, columns months.

    Skips tickers with no usable history. Returns an empty frame if none work.
    """
    batch = get_seasonality_batch(tickers, years)
    rows: dict[str, dict] = {}
    for ticker, stats in batch.items():
        if not stats.get("months"):
            continue
        rows[ticker] = {
            month: stats["months"].get(month, {}).get("avg", np.nan)
            for month in _MONTH_NAMES
        }
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame.from_dict(rows, orient="index")[_MONTH_NAMES]


def get_month_ahead(reference: date | None = None) -> tuple[int, str]:
    """(month_number, month_name) of the month that starts next."""
    today = reference or datetime.now(timezone.utc).date()
    month = 1 if today.month == 12 else today.month + 1
    return month, _MONTH_NAMES[month - 1]


def get_seasonal_windows(reference: date | None = None) -> list:
    """Seasonal windows the reference date currently sits inside."""
    today = reference or datetime.now(timezone.utc).date()
    active = []
    for (start_m, start_d), (end_m, end_d), name, note in SEASONAL_WINDOWS:
        start = date(today.year, start_m, start_d)
        end = date(today.year, end_m, end_d)
        if start <= today <= end:
            active.append({"window": name, "note": note,
                           "ends": end.isoformat()})
    return active


# ---------------------------------------------------------------------------
# Macro regime
# ---------------------------------------------------------------------------

def get_macro_regime() -> dict:
    """Classify the current macro regime from cross-asset price behaviour.

    Returns:
        {
          "label": str,                 # e.g. "Risk-On / Easing"
          "risk_score": float,          # [-100, 100] risk appetite
          "rate_score": float,          # [-100, 100] positive = yields falling
          "growth_score": float,        # [-100, 100] positive = pro-growth
          "inflation_score": float,     # [-100, 100] positive = inflationary
          "indicators": {name: {"value":…, "change_1m":…, "note": str}},
          "favored_sectors": [sector…],
          "pressured_sectors": [sector…],
          "as_of": "YYYY-MM-DD",
        }
    Scores are 0.0 and indicators empty when price data is unavailable.
    """
    closes = _close_frame(_MACRO_TICKERS, period="1y")
    empty = {
        "label": "Unknown", "risk_score": 0.0, "rate_score": 0.0,
        "growth_score": 0.0, "inflation_score": 0.0, "indicators": {},
        "favored_sectors": [], "pressured_sectors": [],
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    if closes.empty:
        return empty

    def level(ticker: str) -> float | None:
        if ticker not in closes.columns:
            return None
        series = closes[ticker].dropna()
        return float(series.iloc[-1]) if not series.empty else None

    def change(ticker: str, days: int = 21) -> float | None:
        if ticker not in closes.columns:
            return None
        return _pct(closes[ticker], days)

    def ratio_change(numerator: str, denominator: str, days: int = 21) -> float | None:
        if numerator not in closes.columns or denominator not in closes.columns:
            return None
        pair = closes[[numerator, denominator]].dropna()
        if len(pair) <= days:
            return None
        series = pair[numerator] / pair[denominator]
        return _pct(series, days)

    indicators: dict[str, dict] = {}

    def record(name: str, value, change_1m, note: str) -> None:
        indicators[name] = {
            "value": None if value is None else round(float(value), 2),
            "change_1m": None if change_1m is None else round(float(change_1m), 2),
            "note": note,
        }

    vix = level("^VIX")
    vix_series = closes["^VIX"].dropna() if "^VIX" in closes.columns else pd.Series(dtype=float)
    vix_percentile = (
        float((vix_series < vix_series.iloc[-1]).mean() * 100.0)
        if len(vix_series) > 60 else None
    )
    record("VIX", vix, change("^VIX"), "Equity volatility — high readings mark risk-off")
    if vix_percentile is not None:
        indicators["VIX"]["percentile_1y"] = round(vix_percentile, 1)

    tnx = level("^TNX")
    tnx_change = change("^TNX", 63)  # ~3 months of trading days
    record("10Y Yield", tnx, change("^TNX"), "Direction of long rates — the discount-rate driver")
    irx = level("^IRX")
    curve = None if (tnx is None or irx is None) else tnx - irx
    record("Yield Curve (10Y-3M)", curve, None, "Negative = inverted; historically a late-cycle signal")

    risk_appetite = ratio_change("XLY", "XLP")
    record("Cyclicals vs Staples (XLY/XLP)", None, risk_appetite, "Rising = investors paying up for cyclicality")
    credit = ratio_change("HYG", "LQD")
    record("Credit Appetite (HYG/LQD)", None, credit, "Rising = junk outperforming IG; credit is relaxed")
    breadth = ratio_change("IWM", "SPY")
    record("Small vs Large (IWM/SPY)", None, breadth, "Rising = broad participation, pro-growth")
    inflation_bd = ratio_change("TIP", "IEF")
    record("Breakevens (TIP/IEF)", None, inflation_bd, "Rising = market pricing more inflation")
    copper = change("CPER", 63)
    record("Copper (CPER)", level("CPER"), copper, "Global industrial demand proxy")
    oil = change("USO", 63)
    record("Oil (USO)", level("USO"), oil, "Feeds headline inflation and energy earnings")
    gold = change("GLD", 63)
    record("Gold (GLD)", level("GLD"), gold, "Rises on real-rate declines and tail risk")
    dollar = change("UUP", 63)
    record("Dollar (UUP)", level("UUP"), dollar, "Strong dollar pressures multinationals and EM")

    # SPY trend vs its 200-day average.
    trend = None
    if "SPY" in closes.columns:
        spy = closes["SPY"].dropna()
        if len(spy) >= 200:
            ma200 = float(spy.rolling(200).mean().iloc[-1])
            if ma200:
                trend = (float(spy.iloc[-1]) / ma200 - 1.0) * 100.0
    record("SPY vs 200DMA", trend, None, "Positive = primary uptrend intact")

    def contribute(value, weight: float, scale: float = 1.0) -> float:
        """Scale a percent reading into score points, ignoring missing data."""
        if value is None:
            return 0.0
        return _clamp(float(value) * scale, -1.0, 1.0) * weight

    # Risk appetite: cyclical leadership, credit, breadth, trend, and vol.
    risk = 0.0
    risk += contribute(risk_appetite, 30.0, 0.20)
    risk += contribute(credit, 25.0, 0.50)
    risk += contribute(breadth, 15.0, 0.20)
    risk += contribute(trend, 20.0, 0.10)
    if vix_percentile is not None:
        risk += (50.0 - vix_percentile) / 50.0 * 10.0
    risk = _clamp(risk)

    # Rates: positive means yields falling (easing tailwind for long duration).
    # Scale 0.04 saturates at a 25% move in the 10Y over three months, which is
    # about as violent as that series gets.
    rate = _clamp(contribute(None if tnx_change is None else -tnx_change, 100.0, 0.04))

    # Growth: copper, breadth, trend, cyclical leadership.
    growth = _clamp(
        contribute(copper, 30.0, 0.05)
        + contribute(breadth, 25.0, 0.20)
        + contribute(trend, 25.0, 0.10)
        + contribute(risk_appetite, 20.0, 0.20)
    )

    # Inflation: breakevens, oil, copper, minus dollar strength.
    inflation = _clamp(
        contribute(inflation_bd, 35.0, 0.50)
        + contribute(oil, 30.0, 0.05)
        + contribute(copper, 20.0, 0.05)
        + contribute(None if dollar is None else -dollar, 15.0, 0.20)
    )

    label = _regime_label(risk, rate, inflation)
    favored, pressured = _regime_sector_tilts(risk, rate, growth, inflation)

    return {
        "label": label,
        "risk_score": round(risk, 1),
        "rate_score": round(rate, 1),
        "growth_score": round(growth, 1),
        "inflation_score": round(inflation, 1),
        "indicators": indicators,
        "favored_sectors": favored,
        "pressured_sectors": pressured,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def _regime_label(risk: float, rate: float, inflation: float) -> str:
    """Name the quadrant the scores land in."""
    risk_part = "Risk-On" if risk > 15 else "Risk-Off" if risk < -15 else "Neutral Risk"
    rate_part = "Easing" if rate > 15 else "Tightening" if rate < -15 else "Steady Rates"
    label = f"{risk_part} / {rate_part}"
    if inflation > 35:
        label += " / Reflationary"
    elif inflation < -35:
        label += " / Disinflationary"
    return label


def _regime_sector_tilts(risk: float, rate: float, growth: float,
                         inflation: float) -> tuple[list, list]:
    """Sectors the regime scores historically favour and pressure.

    Each sector accumulates a score from the regime dimensions it is sensitive
    to; the extremes become the favoured/pressured lists.
    """
    scores: dict[str, float] = {sector: 0.0 for sector in SECTOR_ETFS}

    # Risk appetite: cyclicals benefit, defensives are the hedge.
    for sector in ("Consumer Cyclical", "Technology", "Industrials", "Financial Services"):
        scores[sector] += risk * 0.6
    for sector in ("Consumer Defensive", "Utilities", "Healthcare"):
        scores[sector] -= risk * 0.5

    # Falling rates help long-duration and yield-sensitive sectors; steep/rising
    # rates help banks' net interest margins.
    for sector in ("Technology", "Real Estate", "Utilities"):
        scores[sector] += rate * 0.5
    scores["Financial Services"] -= rate * 0.4

    # Growth: cyclicals and materials lever to it.
    for sector in ("Industrials", "Basic Materials", "Consumer Cyclical", "Technology"):
        scores[sector] += growth * 0.4
    for sector in ("Consumer Defensive", "Utilities"):
        scores[sector] -= growth * 0.3

    # Inflation: real-asset sectors win, long-duration and margin-squeezed lose.
    for sector in ("Energy", "Basic Materials", "Real Estate"):
        scores[sector] += inflation * 0.5
    for sector in ("Technology", "Consumer Cyclical", "Utilities"):
        scores[sector] -= inflation * 0.3

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    favored = [name for name, value in ranked[:3] if value > 5]
    pressured = [name for name, value in ranked[::-1][:3] if value < -5]
    return favored, pressured


# ---------------------------------------------------------------------------
# Sector momentum + exposure
# ---------------------------------------------------------------------------

def get_sector_momentum() -> list:
    """Relative strength of every sector ETF against SPY.

    Returns a list of dicts sorted by `rs_score` descending:
        {"sector", "etf", "ret_1m", "ret_3m", "ret_6m", "ret_12m",
         "rs_1m", "rs_3m", "rs_6m", "rs_score", "above_200dma"}
    Percent values are None when history is missing.
    """
    tickers = list(SECTOR_ETFS.values()) + [BENCHMARK]
    closes = _close_frame(tickers, period="2y")
    if closes.empty or BENCHMARK not in closes.columns:
        return []

    windows = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
    bench = {name: _pct(closes[BENCHMARK], days) for name, days in windows.items()}

    results = []
    for sector, etf in SECTOR_ETFS.items():
        if etf not in closes.columns:
            continue
        series = closes[etf].dropna()
        if series.empty:
            continue

        rets = {name: _pct(series, days) for name, days in windows.items()}
        rel = {
            name: (None if rets[name] is None or bench[name] is None
                   else round(rets[name] - bench[name], 2))
            for name in windows
        }

        above_200 = None
        if len(series) >= 200:
            ma200 = float(series.rolling(200).mean().iloc[-1])
            above_200 = bool(ma200 and float(series.iloc[-1]) > ma200)

        # Weighted blend favouring the medium term over the noisiest window.
        parts = [(rel["1m"], 0.2), (rel["3m"], 0.45), (rel["6m"], 0.35)]
        weight_used = sum(weight for value, weight in parts if value is not None)
        rs_score = (
            round(sum(value * weight for value, weight in parts if value is not None) / weight_used, 2)
            if weight_used else 0.0
        )

        results.append({
            "sector": sector,
            "etf": etf,
            "ret_1m": None if rets["1m"] is None else round(rets["1m"], 2),
            "ret_3m": None if rets["3m"] is None else round(rets["3m"], 2),
            "ret_6m": None if rets["6m"] is None else round(rets["6m"], 2),
            "ret_12m": None if rets["12m"] is None else round(rets["12m"], 2),
            "rs_1m": rel["1m"],
            "rs_3m": rel["3m"],
            "rs_6m": rel["6m"],
            "rs_score": rs_score,
            "above_200dma": above_200,
        })

    return sorted(results, key=lambda row: row["rs_score"], reverse=True)


def _ticker_sector(ticker: str) -> str:
    """yfinance sector for a ticker, or "" when unavailable."""
    try:
        info = yf.Ticker(ticker).info
        return info.get("sector", "") or ""
    except Exception:
        return ""


def get_portfolio_sector_exposure(positions: list) -> dict:
    """Portfolio weight per sector against the S&P 500 benchmark weight.

    Returns:
        {
          "total_value": float,
          "sectors": [{"sector", "etf", "value", "weight", "benchmark_weight",
                       "gap", "tickers": [...]}],   # sorted by weight desc
          "unclassified": [ticker…],
        }
    """
    holdings: dict[str, float] = {}
    for position in positions or []:
        ticker = extract_ticker(position)
        value = extract_market_value(position)
        if ticker and value > 0:
            holdings[ticker] = holdings.get(ticker, 0.0) + value

    total = sum(holdings.values())
    if total <= 0:
        return {"total_value": 0.0, "sectors": [], "unclassified": []}

    # yfinance .info is a slow per-ticker HTTP round trip — fan them out.
    with ThreadPoolExecutor(max_workers=8) as pool:
        sectors_by_ticker = dict(
            zip(holdings.keys(), pool.map(_ticker_sector, holdings.keys()))
        )

    by_sector: dict[str, dict] = {}
    unclassified: list = []
    for ticker, value in holdings.items():
        sector = sectors_by_ticker.get(ticker, "")
        if not sector or sector not in SECTOR_ETFS:
            unclassified.append(ticker)
            continue
        bucket = by_sector.setdefault(sector, {"value": 0.0, "tickers": []})
        bucket["value"] += value
        bucket["tickers"].append(ticker)

    sectors = []
    for sector, etf in SECTOR_ETFS.items():
        bucket = by_sector.get(sector, {"value": 0.0, "tickers": []})
        weight = bucket["value"] / total * 100.0
        benchmark = SPX_SECTOR_WEIGHTS.get(sector, 0.0)
        sectors.append({
            "sector": sector,
            "etf": etf,
            "value": round(bucket["value"], 2),
            "weight": round(weight, 2),
            "benchmark_weight": benchmark,
            "gap": round(weight - benchmark, 2),
            "tickers": sorted(bucket["tickers"]),
        })

    sectors.sort(key=lambda row: row["weight"], reverse=True)
    return {
        "total_value": round(total, 2),
        "sectors": sectors,
        "unclassified": sorted(unclassified),
    }


# ---------------------------------------------------------------------------
# Catalyst calendar
# ---------------------------------------------------------------------------

def _next_earnings_date(ticker: str) -> str | None:
    """Next scheduled earnings date for a ticker as "YYYY-MM-DD", or None."""
    try:
        dates = yf.Ticker(ticker).get_earnings_dates(limit=8)
    except Exception:
        return None
    if dates is None or len(dates) == 0:
        return None

    today = pd.Timestamp.now(tz="UTC")
    try:
        index = dates.index
        if index.tz is None:
            index = index.tz_localize("UTC")
        future = [stamp for stamp in index if stamp >= today]
    except Exception:
        return None
    if not future:
        return None
    return min(future).strftime("%Y-%m-%d")


def get_upcoming_catalysts(tickers: list, days_ahead: int = 60) -> list:
    """Scheduled events inside the next `days_ahead` days, soonest first.

    Covers FOMC decisions, the monthly CPI/jobs cadence, quarterly opex,
    month/quarter ends, and per-holding earnings dates.

    Returns a list of {"date", "days_out", "event", "kind", "detail"}.
    """
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=days_ahead)
    events: list = []

    def add(event_date: date, event: str, kind: str, detail: str) -> None:
        if today <= event_date <= horizon:
            events.append({
                "date": event_date.isoformat(),
                "days_out": (event_date - today).days,
                "event": event,
                "kind": kind,
                "detail": detail,
            })

    for iso in FOMC_DATES:
        try:
            add(date.fromisoformat(iso), "FOMC rate decision", "fed",
                "Policy statement plus the press conference — the single biggest "
                "scheduled driver of rate expectations and multiples.")
        except ValueError:
            continue

    # CPI and payrolls follow a fixed monthly cadence rather than fixed dates:
    # payrolls on the first Friday, CPI in the second full week.
    for offset in range(0, days_ahead // 28 + 2):
        month_cursor = date(today.year + (today.month - 1 + offset) // 12,
                            (today.month - 1 + offset) % 12 + 1, 1)
        first_friday = month_cursor + timedelta(days=(4 - month_cursor.weekday()) % 7)
        add(first_friday, "Nonfarm payrolls (approx.)", "macro",
            "Jobs report — drives the growth side of the Fed's mandate.")
        add(first_friday + timedelta(days=5), "CPI release (approx.)", "macro",
            "Inflation print — the main input to rate-cut odds.")

        # Quarterly triple witching: third Friday of Mar/Jun/Sep/Dec.
        if month_cursor.month in (3, 6, 9, 12):
            third_friday = month_cursor + timedelta(days=(4 - month_cursor.weekday()) % 7 + 14)
            add(third_friday, "Quarterly options expiration", "market",
                "Triple witching — elevated volume and pinning around big strikes.")

        # Month end: rebalancing flows.
        last_day = date(month_cursor.year, month_cursor.month,
                        calendar.monthrange(month_cursor.year, month_cursor.month)[1])
        add(last_day, "Month end", "market",
            "Index and pension rebalancing flows cluster into the close.")

    holdings = list(tickers or [])
    if holdings:
        with ThreadPoolExecutor(max_workers=8) as pool:
            earnings_dates = dict(zip(holdings, pool.map(_next_earnings_date, holdings)))
        for ticker, earnings in earnings_dates.items():
            if not earnings:
                continue
            try:
                add(date.fromisoformat(earnings), f"{ticker} earnings", "earnings",
                    "Position-specific event risk — implied vol usually peaks into it.")
            except ValueError:
                continue

    events.sort(key=lambda row: (row["date"], row["event"]))
    return events


# ---------------------------------------------------------------------------
# Rotation scorecard
# ---------------------------------------------------------------------------

def build_rotation_scorecard(exposure: dict, momentum: list, regime: dict,
                             month_name: str,
                             seasonality: pd.DataFrame | None = None) -> list:
    """Rank sectors by how much they deserve capital right now.

    Blends four signals per sector:
      * seasonal edge for the month ahead (avg monthly return of the sector ETF)
      * relative-strength momentum vs SPY
      * regime fit (does the current macro regime favour this sector)
      * exposure gap (how underweight the portfolio already is vs the S&P 500)

    Args:
        exposure:    get_portfolio_sector_exposure() output.
        momentum:    get_sector_momentum() output.
        regime:      get_macro_regime() output.
        month_name:  the month the seasonal leg should score, e.g. "October".
        seasonality: optional precomputed matrix from get_seasonality_matrix()
                     over the sector ETFs; fetched per-ETF when omitted.

    Returns a list of dicts sorted by `total_score` descending:
        {"sector", "etf", "weight", "benchmark_weight", "gap", "seasonal_avg",
         "seasonal_win_rate", "rs_score", "regime_fit", "total_score",
         "action", "rationale"}
    """
    gaps = {row["sector"]: row for row in exposure.get("sectors", [])}
    momentum_by_sector = {row["sector"]: row for row in momentum}
    favored = set(regime.get("favored_sectors", []))
    pressured = set(regime.get("pressured_sectors", []))

    rows = []
    for sector, etf in SECTOR_ETFS.items():
        seasonal_avg, seasonal_win = None, None
        if seasonality is not None and etf in seasonality.index and month_name in seasonality.columns:
            value = seasonality.at[etf, month_name]
            seasonal_avg = None if pd.isna(value) else round(float(value), 2)
        else:
            stats = get_monthly_seasonality(etf).get("months", {}).get(month_name)
            if stats:
                seasonal_avg = stats["avg"]
                seasonal_win = stats["win_rate"]

        mom = momentum_by_sector.get(sector, {})
        rs_score = mom.get("rs_score", 0.0) or 0.0
        gap_row = gaps.get(sector, {})
        gap = gap_row.get("gap", -SPX_SECTOR_WEIGHTS.get(sector, 0.0))
        weight = gap_row.get("weight", 0.0)

        regime_fit = 1.0 if sector in favored else -1.0 if sector in pressured else 0.0

        # Each leg is normalised to roughly +/-1 before weighting.
        seasonal_leg = float(np.clip((seasonal_avg or 0.0) / 3.0, -1.0, 1.0))
        momentum_leg = float(np.clip(rs_score / 6.0, -1.0, 1.0))
        # An underweight (negative gap) is an opportunity, so the sign flips.
        gap_leg = float(np.clip(-gap / 10.0, -1.0, 1.0))

        total = (
            seasonal_leg * 25.0
            + momentum_leg * 30.0
            + regime_fit * 25.0
            + gap_leg * 20.0
        )

        rows.append({
            "sector": sector,
            "etf": etf,
            "weight": weight,
            "benchmark_weight": SPX_SECTOR_WEIGHTS.get(sector, 0.0),
            "gap": gap,
            "seasonal_avg": seasonal_avg,
            "seasonal_win_rate": seasonal_win,
            "rs_score": rs_score,
            "regime_fit": regime_fit,
            "total_score": round(total, 1),
            "action": _score_to_action(total, weight),
            "rationale": _build_rationale(sector, month_name, seasonal_avg,
                                          rs_score, regime_fit, gap, weight),
        })

    return sorted(rows, key=lambda row: row["total_score"], reverse=True)


def _score_to_action(score: float, weight: float) -> str:
    if score >= 30:
        return "Add" if weight < 25 else "Hold — already heavy"
    if score >= 10:
        return "Accumulate on weakness"
    if score <= -30:
        return "Trim" if weight > 0 else "Avoid"
    if score <= -10:
        return "Underweight"
    return "Neutral"


def _build_rationale(sector: str, month_name: str, seasonal_avg, rs_score,
                     regime_fit, gap, weight) -> str:
    """Plain-language reason string explaining the score's drivers."""
    parts = []
    if seasonal_avg is not None:
        direction = "averages" if seasonal_avg >= 0 else "averages a loss of"
        parts.append(f"{month_name} {direction} {abs(seasonal_avg):.1f}% historically")
    if rs_score:
        verb = "outperforming" if rs_score > 0 else "lagging"
        parts.append(f"{verb} SPY by {abs(rs_score):.1f}pts on blended momentum")
    if regime_fit > 0:
        parts.append("favoured by the current macro regime")
    elif regime_fit < 0:
        parts.append("pressured by the current macro regime")
    if gap is not None:
        if gap < -3:
            parts.append(f"portfolio is {abs(gap):.1f}pts underweight vs the S&P")
        elif gap > 5:
            parts.append(f"portfolio is {gap:.1f}pts overweight vs the S&P")
        elif weight == 0:
            parts.append("no current exposure")
    if not parts:
        return "No differentiating signal."
    text = "; ".join(parts)
    return text[0].upper() + text[1:]


# ---------------------------------------------------------------------------
# Aggregate payload
# ---------------------------------------------------------------------------

def build_analysis_payload(positions: list,
                           lookback_years: int = DEFAULT_LOOKBACK_YEARS) -> dict:
    """Compute everything the page and the Gemini agent need, in one pass.

    This is the slow call (many yfinance requests) — cache it at the page layer.
    """
    tickers = []
    for position in positions or []:
        ticker = extract_ticker(position)
        if ticker and ticker not in tickers:
            tickers.append(ticker)

    month_num, month_name = get_month_ahead()
    regime = get_macro_regime()
    momentum = get_sector_momentum()
    exposure = get_portfolio_sector_exposure(positions)
    # One batched download covers the sector ETFs, the benchmark, and every
    # holding; the matrices below are just views onto it.
    sector_tickers = list(SECTOR_ETFS.values()) + [BENCHMARK]
    batch = get_seasonality_batch(sector_tickers + tickers, lookback_years)

    sector_matrix = _matrix_from_batch(batch, sector_tickers)
    holdings_matrix = _matrix_from_batch(batch, tickers) if tickers else pd.DataFrame()
    scorecard = build_rotation_scorecard(exposure, momentum, regime, month_name, sector_matrix)
    catalysts = get_upcoming_catalysts(tickers)

    benchmark_stats = batch.get(BENCHMARK, _empty_stats(BENCHMARK))

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "tickers": tickers,
        "month_ahead": month_name,
        "month_ahead_num": month_num,
        "active_seasonal_windows": get_seasonal_windows(),
        "benchmark_seasonality": benchmark_stats,
        "regime": regime,
        "sector_momentum": momentum,
        "exposure": exposure,
        "scorecard": scorecard,
        "catalysts": catalysts,
        "sector_seasonality": sector_matrix,
        "holdings_seasonality": holdings_matrix,
    }
