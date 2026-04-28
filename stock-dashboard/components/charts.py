from typing import Optional
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_THEME = "plotly_dark"
_GREEN = "#00c853"
_RED   = "#ff1744"
_BLUE  = "#2979ff"
_AMBER = "#ffd600"
_TEAL  = "#00bcd4"


def _empty_fig(message: str) -> go.Figure:
    """Return a dark-themed empty figure with a centred message."""
    fig = go.Figure()
    fig.update_layout(
        template=_THEME,
        annotations=[{"text": message, "xref": "paper", "yref": "paper",
                       "x": 0.5, "y": 0.5, "showarrow": False,
                       "font": {"size": 16, "color": "gray"}}],
        xaxis_visible=False, yaxis_visible=False,
    )
    return fig


def _add_moving_average(fig: go.Figure, series: pd.Series, window: int, color: str, name: str) -> None:
    """Add a moving-average line trace to an existing figure."""
    ma = series.rolling(window=window).mean()
    fig.add_trace(go.Scatter(
        x=series.index, y=ma, mode="lines",
        name=name, line={"color": color, "width": 1.5, "dash": "dot"},
    ))


def price_chart(hist_df: pd.DataFrame, ticker: str) -> go.Figure:
    """Candlestick chart with 50- and 200-day moving averages."""
    if hist_df is None or hist_df.empty:
        return _empty_fig(f"No price data for {ticker}")

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=hist_df.index,
        open=hist_df["Open"], high=hist_df["High"],
        low=hist_df["Low"],  close=hist_df["Close"],
        name=ticker,
        increasing_line_color=_GREEN,
        decreasing_line_color=_RED,
    ))
    _add_moving_average(fig, hist_df["Close"], 50,  _AMBER, "MA 50")
    _add_moving_average(fig, hist_df["Close"], 200, _TEAL,  "MA 200")
    fig.update_layout(
        template=_THEME,
        title=f"{ticker} — Price",
        xaxis_rangeslider_visible=False,
        xaxis_title="Date",
        yaxis_title="Price (USD)",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
    )
    return fig


def _extract_annual_series(financials: dict, statement_key: str, row_key: str) -> Optional[pd.Series]:
    """Pull a time-series row from a financials sub-dict, sorted oldest-first."""
    stmt = financials.get(statement_key, {})
    row  = stmt.get(row_key, {})
    if not row:
        return None
    s = pd.Series(row)
    s.index = pd.to_datetime(s.index, errors="coerce")
    s = s.sort_index()
    s = s.apply(lambda x: float(x) if x is not None else None)
    return s


def revenue_chart(financials: dict, ticker: str) -> go.Figure:
    """Annual total revenue bar chart."""
    s = _extract_annual_series(financials, "income_stmt", "Total Revenue")
    if s is None or s.empty:
        return _empty_fig(f"No revenue data for {ticker}")

    fig = go.Figure(go.Bar(
        x=[d.year for d in s.index], y=s.values / 1e9,
        marker_color=_BLUE, name="Revenue",
    ))
    fig.update_layout(
        template=_THEME,
        title=f"{ticker} — Annual Revenue",
        xaxis_title="Year", yaxis_title="Revenue (USD B)",
    )
    return fig


def _pct_series(financials: dict, numerator_key: str) -> Optional[pd.Series]:
    """Return a margin series (numerator / total revenue * 100), annual."""
    rev = _extract_annual_series(financials, "income_stmt", "Total Revenue")
    num = _extract_annual_series(financials, "income_stmt", numerator_key)
    if rev is None or num is None:
        return None
    combined = pd.DataFrame({"rev": rev, "num": num}).dropna()
    if combined.empty or (combined["rev"] == 0).all():
        return None
    return (combined["num"] / combined["rev"]) * 100


def margin_chart(financials: dict, ticker: str) -> go.Figure:
    """Gross, operating, and net margin % over time."""
    gross = _pct_series(financials, "Gross Profit")
    op    = _pct_series(financials, "Operating Income")
    net   = _pct_series(financials, "Net Income")

    if all(s is None for s in [gross, op, net]):
        return _empty_fig(f"No margin data for {ticker}")

    fig = go.Figure()
    for series, name, color in [
        (gross, "Gross Margin",     _GREEN),
        (op,    "Operating Margin", _BLUE),
        (net,   "Net Margin",       _AMBER),
    ]:
        if series is not None and not series.empty:
            fig.add_trace(go.Scatter(
                x=[d.year for d in series.index], y=series.values,
                mode="lines+markers", name=name,
                line={"color": color, "width": 2},
            ))
    fig.update_layout(
        template=_THEME,
        title=f"{ticker} — Margins %",
        xaxis_title="Year", yaxis_title="Margin (%)",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
    )
    return fig


def fcf_chart(financials: dict, ticker: str) -> go.Figure:
    """Annual free cash flow bar chart, coloured green/red by sign."""
    s = _extract_annual_series(financials, "cash_flow", "Free Cash Flow")
    if s is None or s.empty:
        return _empty_fig(f"No FCF data for {ticker}")

    colors = [_GREEN if v >= 0 else _RED for v in s.values]
    fig = go.Figure(go.Bar(
        x=[d.year for d in s.index], y=s.values / 1e9,
        marker_color=colors, name="FCF",
    ))
    fig.update_layout(
        template=_THEME,
        title=f"{ticker} — Free Cash Flow",
        xaxis_title="Year", yaxis_title="FCF (USD B)",
    )
    return fig


def earnings_chart(earnings_df: pd.DataFrame, ticker: str) -> go.Figure:
    """EPS actual vs. estimate grouped bar chart."""
    if earnings_df is None or earnings_df.empty:
        return _empty_fig(f"No earnings data for {ticker}")

    df = earnings_df.copy()
    actual_col   = next((c for c in df.columns if "Reported" in c or "Actual" in c), None)
    estimate_col = next((c for c in df.columns if "Estimate" in c), None)

    if actual_col is None and estimate_col is None:
        return _empty_fig(f"Unrecognised earnings columns for {ticker}")

    x = df.index.astype(str) if not isinstance(df.index[0], str) else df.index

    fig = go.Figure()
    if estimate_col:
        fig.add_trace(go.Bar(
            x=x, y=df[estimate_col], name="EPS Estimate",
            marker_color=_AMBER, opacity=0.7,
        ))
    if actual_col:
        fig.add_trace(go.Bar(
            x=x, y=df[actual_col], name="EPS Actual",
            marker_color=_GREEN,
        ))
    fig.update_layout(
        template=_THEME,
        title=f"{ticker} — EPS Actual vs. Estimate",
        barmode="group",
        xaxis_title="Quarter", yaxis_title="EPS (USD)",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
    )
    return fig
