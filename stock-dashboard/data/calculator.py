from __future__ import annotations
from typing import Optional


def _status(value: float, good_max: float, warn_max: float, lower_is_better: bool = True) -> str:
    """Return benchmark status string based on thresholds.

    For lower_is_better metrics (e.g. PE): good < good_max, warn < warn_max, else warn.
    For higher_is_better metrics: invert the comparison direction.
    """
    if lower_is_better:
        if value <= good_max:
            return "good"
        elif value <= warn_max:
            return "neutral"
        return "warn"
    else:
        if value >= good_max:
            return "good"
        elif value >= warn_max:
            return "neutral"
        return "warn"


def _safe(value) -> Optional[float]:
    """Return float or None if value is missing/zero-ish sentinel."""
    try:
        f = float(value)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------

def calc_pe_ratio(info: dict) -> tuple[Optional[float], str]:
    """Calculate trailing P/E ratio and benchmark status."""
    v = _safe(info.get("trailingPE"))
    if v is None:
        return None, "neutral"
    return v, _status(v, 20, 35)


def calc_ev_ebitda(info: dict) -> tuple[Optional[float], str]:
    """Calculate EV/EBITDA and benchmark status."""
    v = _safe(info.get("enterpriseToEbitda"))
    if v is None:
        return None, "neutral"
    return v, _status(v, 15, 25)


def calc_p_fcf(info: dict) -> tuple[Optional[float], str]:
    """Calculate Price-to-Free-Cash-Flow and benchmark status."""
    price = _safe(info.get("currentPrice"))
    fcf = _safe(info.get("freeCashflow"))
    shares = _safe(info.get("sharesOutstanding"))
    if None in (price, fcf, shares) or shares == 0:
        return None, "neutral"
    fcf_per_share = fcf / shares
    if fcf_per_share <= 0:
        return None, "warn"
    v = price / fcf_per_share
    return v, _status(v, 20, 35)


def calc_peg_ratio(info: dict) -> tuple[Optional[float], str]:
    """Calculate PEG ratio and benchmark status."""
    v = _safe(info.get("pegRatio"))
    if v is None:
        return None, "neutral"
    return v, _status(v, 1.0, 2.0)


# ---------------------------------------------------------------------------
# Profitability
# ---------------------------------------------------------------------------

def calc_gross_margin(info: dict) -> tuple[Optional[float], str]:
    """Calculate gross margin % and benchmark status."""
    v = _safe(info.get("grossMargins"))
    if v is None:
        return None, "neutral"
    pct = v * 100
    return pct, _status(pct, 100, 30, lower_is_better=False)


def calc_operating_margin(info: dict) -> tuple[Optional[float], str]:
    """Calculate operating margin % and benchmark status."""
    v = _safe(info.get("operatingMargins"))
    if v is None:
        return None, "neutral"
    pct = v * 100
    return pct, _status(pct, 100, 10, lower_is_better=False)


def calc_net_margin(info: dict) -> tuple[Optional[float], str]:
    """Calculate net profit margin % and benchmark status."""
    v = _safe(info.get("profitMargins"))
    if v is None:
        return None, "neutral"
    pct = v * 100
    return pct, _status(pct, 100, 5, lower_is_better=False)


def calc_roic(info: dict) -> tuple[Optional[float], str]:
    """Estimate ROIC as returnOnEquity proxy and benchmark status."""
    v = _safe(info.get("returnOnEquity"))
    if v is None:
        return None, "neutral"
    pct = v * 100
    return pct, _status(pct, 100, 10, lower_is_better=False)


# ---------------------------------------------------------------------------
# Growth
# ---------------------------------------------------------------------------

def _yoy_pct(current, prior) -> Optional[float]:
    """Compute year-over-year percentage change, returning None if invalid."""
    c, p = _safe(current), _safe(prior)
    if None in (c, p) or p == 0:
        return None
    return ((c - p) / abs(p)) * 100


def calc_revenue_yoy(financials: dict) -> tuple[Optional[float], str]:
    """Calculate revenue year-over-year growth % and benchmark status."""
    stmt = financials.get("income_stmt", {})
    rev = stmt.get("Total Revenue", {})
    if not rev:
        return None, "neutral"
    dates = sorted(rev.keys(), reverse=True)
    if len(dates) < 2:
        return None, "neutral"
    v = _yoy_pct(rev[dates[0]], rev[dates[1]])
    if v is None:
        return None, "neutral"
    return v, _status(v, 100, 5, lower_is_better=False)


def calc_eps_yoy(financials: dict) -> tuple[Optional[float], str]:
    """Calculate EPS year-over-year growth % and benchmark status."""
    stmt = financials.get("income_stmt", {})
    eps = stmt.get("Basic EPS", {})
    if not eps:
        return None, "neutral"
    dates = sorted(eps.keys(), reverse=True)
    if len(dates) < 2:
        return None, "neutral"
    v = _yoy_pct(eps[dates[0]], eps[dates[1]])
    if v is None:
        return None, "neutral"
    return v, _status(v, 100, 5, lower_is_better=False)


def calc_fcf_yoy(financials: dict) -> tuple[Optional[float], str]:
    """Calculate free cash flow year-over-year growth % and benchmark status."""
    cf = financials.get("cash_flow", {})
    fcf = cf.get("Free Cash Flow", {})
    if not fcf:
        return None, "neutral"
    dates = sorted(fcf.keys(), reverse=True)
    if len(dates) < 2:
        return None, "neutral"
    v = _yoy_pct(fcf[dates[0]], fcf[dates[1]])
    if v is None:
        return None, "neutral"
    return v, _status(v, 100, 5, lower_is_better=False)


# ---------------------------------------------------------------------------
# Balance Sheet
# ---------------------------------------------------------------------------

def calc_net_debt_ebitda(info: dict) -> tuple[Optional[float], str]:
    """Calculate Net Debt / EBITDA and benchmark status."""
    total_debt = _safe(info.get("totalDebt"))
    cash = _safe(info.get("totalCash"))
    ebitda = _safe(info.get("ebitda"))
    if None in (total_debt, cash, ebitda) or ebitda == 0:
        return None, "neutral"
    net_debt = total_debt - cash
    v = net_debt / ebitda
    return v, _status(v, 2.0, 4.0)


def calc_interest_coverage(financials: dict) -> tuple[Optional[float], str]:
    """Calculate interest coverage ratio (EBIT / interest expense) and benchmark status."""
    stmt = financials.get("income_stmt", {})
    ebit_row = stmt.get("EBIT", {})
    interest_row = stmt.get("Interest Expense", {})
    if not ebit_row or not interest_row:
        return None, "neutral"
    dates = sorted(ebit_row.keys(), reverse=True)
    if not dates:
        return None, "neutral"
    ebit = _safe(ebit_row[dates[0]])
    interest = _safe(interest_row.get(dates[0]))
    if None in (ebit, interest) or interest == 0:
        return None, "neutral"
    v = ebit / abs(interest)
    return v, _status(v, 100, 3.0, lower_is_better=False)


def calc_current_ratio(info: dict) -> tuple[Optional[float], str]:
    """Calculate current ratio and benchmark status."""
    v = _safe(info.get("currentRatio"))
    if v is None:
        return None, "neutral"
    return v, _status(v, 100, 1.5, lower_is_better=False)


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------

def calc_short_interest(info: dict) -> tuple[Optional[float], str]:
    """Calculate short interest as % of float and benchmark status."""
    v = _safe(info.get("shortPercentOfFloat"))
    if v is None:
        return None, "neutral"
    pct = v * 100
    return pct, _status(pct, 5.0, 15.0)


def calc_insider_ownership(info: dict) -> tuple[Optional[float], str]:
    """Calculate insider ownership % and benchmark status."""
    v = _safe(info.get("heldPercentInsiders"))
    if v is None:
        return None, "neutral"
    pct = v * 100
    return pct, _status(pct, 100, 5.0, lower_is_better=False)
