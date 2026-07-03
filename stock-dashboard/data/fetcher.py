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


@st.cache_data(ttl=900)
def get_batch_history(tickers: tuple, period: str = "1y") -> pd.DataFrame:
    """Fetch daily adjusted close prices for multiple tickers in a single yf.download call.

    Parameters
    ----------
    tickers : tuple of uppercase ticker strings (use tuple so it is hashable for @st.cache_data).
              Should include any benchmark ticker (e.g. "SPY") already appended by the caller.
    period  : yfinance period string — "1mo", "3mo", "6mo", or "1y".

    Returns
    -------
    DataFrame with one column per ticker (column name = ticker string), rows = trading dates.
    Returns an empty DataFrame on any error.
    Drops any ticker column that is entirely NaN.
    """
    try:
        tickers_list = list(tickers)
        if not tickers_list:
            return pd.DataFrame()
        raw = yf.download(
            tickers_list,
            period=period,
            auto_adjust=True,
            progress=False,
        )
        if raw is None or raw.empty:
            return pd.DataFrame()
        # yfinance returns MultiIndex columns when >1 ticker is requested
        if isinstance(raw.columns, pd.MultiIndex):
            close_df = raw["Close"]
        else:
            # Single ticker — raw columns are OHLCV; rename Close to the ticker name
            close_df = raw[["Close"]].rename(columns={"Close": tickers_list[0]})
        # Drop entirely-NaN columns (tickers with no data)
        close_df = close_df.dropna(axis=1, how="all")
        return close_df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3600)
def get_next_earnings_date(ticker: str) -> str | None:
    """Return the next upcoming earnings date as 'YYYY-MM-DD', or None if unknown/past.

    Uses yfinance's get_earnings_dates(), which returns both past and future dates;
    this filters to the earliest date that is still in the future (or today).
    """
    try:
        stock = yf.Ticker(ticker)
        df = stock.get_earnings_dates(limit=8)
        if df is None or df.empty:
            return None
        now = pd.Timestamp.now(tz=df.index.tz) if df.index.tz is not None else pd.Timestamp.now()
        future = df[df.index >= now]
        if future.empty:
            return None
        return future.index.min().strftime("%Y-%m-%d")
    except Exception:
        return None


@st.cache_data(ttl=3600)
def get_quarterly_income_stmt(ticker: str) -> dict:
    """Fetch the quarterly income statement as a nested dict {period_str: {line_item: value}}.

    Mirrors get_financials() but uses yfinance's quarterly_income_stmt (columns = quarter-end
    dates, most recent first) instead of the annual income_stmt.
    """
    try:
        stock = yf.Ticker(ticker)
        df = stock.quarterly_income_stmt
        if df is None or df.empty:
            return {}
        df = df.copy()
        df.columns = [str(c) for c in df.columns]
        return df.to_dict()
    except Exception:
        return {}
