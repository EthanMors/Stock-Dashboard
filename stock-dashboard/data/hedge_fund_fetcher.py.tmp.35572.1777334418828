import datetime
import random
from decimal import Decimal
from typing import Optional, TypedDict

import streamlit as st
from edgar import get_filings, set_identity

from data.cache import load_hedge_fund_cache, save_hedge_fund_cache

set_identity("ethanjosemorris@gmail.com")

_MAX_FILINGS_TO_SCAN = 300
_CONCENTRATED_THRESHOLD = 15
_CACHE_KEY = "concentrated_funds_v1"


class HoldingRow(TypedDict):
    issuer: str
    ticker: str
    value: float
    shares: float
    pct_of_portfolio: float
    put_call: str


class FundProfile(TypedDict):
    name: str
    cik: str
    report_period: str
    filing_date: str
    total_holdings: int
    total_value: float
    holdings: list


class CategorizedFunds(TypedDict):
    top5: list
    bottom5: list
    daily_middle: list
    middle_count: int
    as_of_date: str


def _normalize_value(raw) -> float:
    """Convert edgartools value to float dollars, handling Decimal/int/None."""
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _parse_holdings(report) -> list:
    """Convert ThirteenF holdings DataFrame to list of HoldingRow dicts."""
    try:
        df = report.holdings
        if df is None or df.empty:
            return []
    except Exception:
        return []

    total_val = _normalize_value(report.total_value)
    rows = []

    cols_lower = {c.lower(): c for c in df.columns}
    value_col = cols_lower.get("value") or cols_lower.get("val")
    shares_col = (
        cols_lower.get("sharesprnamount")
        or cols_lower.get("shares")
        or cols_lower.get("amount")
    )
    issuer_col = (
        cols_lower.get("issuer")
        or cols_lower.get("nameofissuer")
        or cols_lower.get("name")
    )
    ticker_col = cols_lower.get("ticker") or cols_lower.get("symbol")
    putcall_col = (
        cols_lower.get("putcall")
        or cols_lower.get("put_call")
        or cols_lower.get("optiontype")
    )

    for _, row in df.iterrows():
        val = _normalize_value(row.get(value_col) if value_col else None)
        shares = _normalize_value(row.get(shares_col) if shares_col else None)
        pct = (val / total_val * 100) if total_val > 0 else 0.0
        put_call = str(row.get(putcall_col, "") or "") if putcall_col else ""

        rows.append(
            HoldingRow(
                issuer=str(row.get(issuer_col, "") or "") if issuer_col else "",
                ticker=str(row.get(ticker_col, "") or "") if ticker_col else "",
                value=val,
                shares=shares,
                pct_of_portfolio=round(pct, 2),
                put_call=put_call,
            )
        )

    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows


def _pick_daily_middle(funds: list) -> list:
    """Select one fund from the middle tier deterministically based on today's date."""
    if not funds:
        return []
    seed = int(datetime.date.today().strftime("%Y%m%d"))
    rng = random.Random(seed)
    return [rng.choice(funds)]


def _apply_unit_correction(funds: list) -> list:
    """
    If total_value for all funds looks implausibly small (< $1M), edgartools is
    returning raw thousands from the SEC filing — multiply everything by 1000.
    """
    if not funds:
        return funds
    max_val = max(f["total_value"] for f in funds)
    if max_val < 1_000_000:
        for f in funds:
            f["total_value"] *= 1000
            for h in f["holdings"]:
                h["value"] *= 1000
    return funds


@st.cache_data(ttl=86400)
def get_concentrated_funds(
    max_scan: int = _MAX_FILINGS_TO_SCAN,
    threshold: int = _CONCENTRATED_THRESHOLD,
) -> list:
    """
    Scan recent 13F-HR filings and return all concentrated funds (< threshold positions).
    Results are cached in SQLite per calendar day and in Streamlit's in-memory cache.
    """
    cached = load_hedge_fund_cache(_CACHE_KEY)
    if cached is not None:
        return cached

    try:
        filings_batch = get_filings(form="13F-HR")
    except Exception:
        return []

    seen_ciks: set = set()
    concentrated = []
    scanned = 0

    for filing in filings_batch:
        if scanned >= max_scan:
            break
        scanned += 1

        cik = str(getattr(filing, "cik", "") or "")
        if not cik or cik in seen_ciks:
            continue
        seen_ciks.add(cik)

        try:
            report = filing.obj()
            if report is None:
                continue

            has_table = getattr(report, "has_infotable", True)
            if not has_table:
                continue

            n_positions = getattr(report, "total_holdings", None)
            if n_positions is None or n_positions >= threshold:
                continue

            total_val = _normalize_value(getattr(report, "total_value", None))
            holdings = _parse_holdings(report)
            name = (
                getattr(report, "management_company_name", None)
                or getattr(filing, "company", None)
                or cik
            )
            report_period = str(getattr(report, "report_period", "") or "")
            filing_date = str(getattr(report, "filing_date", "") or getattr(filing, "filing_date", "") or "")

            concentrated.append(
                FundProfile(
                    name=str(name),
                    cik=cik,
                    report_period=report_period,
                    filing_date=filing_date,
                    total_holdings=int(n_positions),
                    total_value=total_val,
                    holdings=holdings,
                )
            )
        except Exception:
            continue

    concentrated = _apply_unit_correction(concentrated)

    if concentrated:
        save_hedge_fund_cache(_CACHE_KEY, concentrated)

    return concentrated


@st.cache_data(ttl=86400)
def get_categorized_funds() -> CategorizedFunds:
    """Categorize concentrated funds into top5, bottom5, and daily middle pick."""
    funds = get_concentrated_funds()

    if not funds:
        return CategorizedFunds(
            top5=[],
            bottom5=[],
            daily_middle=[],
            middle_count=0,
            as_of_date=str(datetime.date.today()),
        )

    sorted_funds = sorted(funds, key=lambda f: f["total_value"], reverse=True)
    n = len(sorted_funds)

    top5 = sorted_funds[:5]

    if n >= 10:
        bottom5 = sorted_funds[n - 5:]
        middle = sorted_funds[5: n - 5]
    else:
        bottom5 = []
        middle = []

    return CategorizedFunds(
        top5=top5,
        bottom5=bottom5,
        daily_middle=_pick_daily_middle(middle),
        middle_count=len(middle),
        as_of_date=str(datetime.date.today()),
    )
