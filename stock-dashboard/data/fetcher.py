import streamlit as st
import yfinance as yf
import pandas as pd


@st.cache_data(ttl=3600)
def get_stock_info(ticker: str) -> dict:
    """Fetch full info dict for a ticker from yfinance."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return info if info else {}
    except Exception:
        return {}


@st.cache_data(ttl=3600)
def get_price_history(ticker: str, period: str = "1y") -> pd.DataFrame:
    """Fetch OHLCV price history for a ticker and period."""
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period=period)
        return df if not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _safe_to_dict(obj) -> dict:
    """Convert a DataFrame to a plain dict, returning {} on failure."""
    try:
        if obj is None:
            return {}
        if isinstance(obj, pd.DataFrame):
            return obj.to_dict()
        return {}
    except Exception:
        return {}


@st.cache_data(ttl=3600)
def get_financials(ticker: str) -> dict:
    """Fetch income statement, balance sheet, and cash flow as nested dicts."""
    try:
        stock = yf.Ticker(ticker)
        return {
            "income_stmt": _safe_to_dict(stock.income_stmt),
            "balance_sheet": _safe_to_dict(stock.balance_sheet),
            "cash_flow": _safe_to_dict(stock.cash_flow),
        }
    except Exception:
        return {}


@st.cache_data(ttl=3600)
def get_earnings_history(ticker: str) -> pd.DataFrame:
    """Fetch historical EPS reported vs estimated for a ticker."""
    try:
        stock = yf.Ticker(ticker)
        df = stock.earnings_history
        return df if df is not None and not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()
