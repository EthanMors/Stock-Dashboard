import json
import os
import re
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import groupby

import numpy as np
import streamlit as st
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta, timezone
from scipy.stats import norm
from plotly.subplots import make_subplots
import plotly.graph_objects as go
from analytics.patterns import DetectedPattern, PatternDetectionEngine
from data.gemini_tracker import record_call

from components.gemini_usage_bar import render_gemini_usage_bar
from components.ui import inject_global_css, page_header, render_sidebar_nav, section_header
from data.options_agent import run_options_analysis
from data.webull_positions import (
    is_configured,
    get_account_list,
    get_balance,
    get_env_account_ids,
    get_positions,
)
from data.news_fetcher import fetch_news, scrape_article
from data.news_analyzer import analyze_articles, get_sector_info
from data import macro_news_fetcher, macro_news_analyzer, macro_news_cache
from data.portfolio_cache import (
    get_latest_analysis,
    save_analysis,
    has_new_articles,
    get_sentiment_history,
    save_options_analysis,
    get_latest_options_analysis,
    is_options_analysis_fresh,
    save_hedge_fund_analysis,
    get_latest_hedge_fund_analysis,
    save_mpt_analysis,
    get_latest_mpt_analysis,
)
from data.hedge_fund_agent import run_hedge_fund_analysis
from data.mpt_agent import run_mpt_analysis
from data.hedge_fund_fetcher import get_all_funds_from_db, refresh_hedge_fund_db
from data.reddit_fetcher import fetch_top_posts_for_ticker
from data.wsb_sentiment import analyze_sentiment, analyze_batch_sentiment
from data.portfolio_insights_agent import run_portfolio_insights
from data.fetcher import get_batch_history

st.set_page_config(page_title="Portfolio", layout="wide")


@st.cache_data(ttl=300)
def _cached_account_list():
    return get_account_list()


@st.cache_data(ttl=300)
def _cached_balance(account_id: str):
    return get_balance(account_id)


@st.cache_data(ttl=300)
def _cached_positions(account_id: str):
    return get_positions(account_id)

render_gemini_usage_bar()
inject_global_css()
render_sidebar_nav()

page_header("Portfolio", "AI-powered portfolio analysis — news, options, technical patterns, and smart money.")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MIN_TICKER_ARTICLES = 3   # if fewer direct articles, also pull sector news
_MAX_ARTICLES_TO_SCRAPE = 5
_TICKER_FIELD_CANDIDATES = ["symbol", "ticker", "tickerSymbol", "stockSymbol", "sym"]

_WSB_DB_PATH         = os.path.join(os.path.dirname(__file__), "..", "db", "wsb.db")
_WSB_SCHEMA_PATH     = os.path.join(os.path.dirname(__file__), "..", "db", "wsb_schema.sql")
_WSB_SUMMARY_TTL_HOURS = 4

# ---------------------------------------------------------------------------
# TA Tab — constants (copied from 10_technical_analysis.py, prefixed _ta_port)
# ---------------------------------------------------------------------------

_TA_PORT_PERIOD_CONFIG: dict[str, tuple[str, str]] = {
    "1M":  ("1mo", "1d"),
    "3M":  ("3mo", "1d"),
    "6M":  ("6mo", "1d"),
    "1Y":  ("1y",  "1d"),
}

_TA_PORT_EMA_PALETTE = [
    "#FF6B6B",
    "#4ECDC4",
    "#45B7D1",
    "#FFEAA7",
    "#A29BFE",
    "#FD79A8",
    "#55EFC4",
    "#FDCB6E",
]

_TA_PORT_UP    = "#26a69a"
_TA_PORT_DOWN  = "#ef5350"

_TA_PORT_PATTERN_LABELS = {
    "bos_bullish": "BOS ↑", "bos_bearish": "BOS ↓",
    "choch_bullish": "CHoCH ↑", "choch_bearish": "CHoCH ↓",
    "breakout_resistance": "Resistance Break ↑", "breakout_support": "Support Break ↓",
    "bull_flag": "Bull Flag", "bear_flag": "Bear Flag",
    "pennant_bull": "Bull Pennant", "pennant_bear": "Bear Pennant",
    "asc_triangle": "Asc. Triangle", "desc_triangle": "Desc. Triangle",
    "sym_triangle": "Sym. Triangle",
    "rising_wedge": "Rising Wedge", "falling_wedge": "Falling Wedge",
    "cup_handle": "Cup & Handle",
    "head_shoulders": "H&S", "inv_head_shoulders": "Inv. H&S",
    "double_top": "Double Top", "double_bottom": "Double Bottom",
    "range_breakout_bull": "Range Break ↑", "range_breakout_bear": "Range Break ↓",
}

_TA_PORT_CONFIDENCE_COLORS = {
    "Very High": "#26a69a",
    "High":      "#66bb6a",
    "Moderate":  "#ffa726",
    "Low":       "#ef5350",
    "Very Low":  "#b0bec5",
}

_TA_PORT_BB_LINE  = "rgba(100, 149, 237, 0.85)"
_TA_PORT_BB_FILL  = "rgba(100, 149, 237, 0.07)"
_TA_PORT_BB_MID   = "rgba(100, 149, 237, 0.5)"
_TA_PORT_GRID     = "#1f2937"
_TA_PORT_BG       = "#0e1117"
_TA_PORT_PLOT_BG  = "#161b27"


def _ta_port_confidence_label(score: float) -> str:
    if score >= 0.85: return "Very High"
    if score >= 0.70: return "High"
    if score >= 0.55: return "Moderate"
    if score >= 0.40: return "Low"
    return "Very Low"


@st.cache_data(ttl=86400)
def _fetch_sector(ticker: str) -> str:
    """Fetch the sector string for *ticker* from yfinance Ticker.info.

    Cached 24 hours (86400 s) — sector data rarely changes intraday.
    Returns "Unknown" on any error or if the field is absent.
    """
    try:
        info = yf.Ticker(ticker.upper()).info
        sector = info.get("sector") or info.get("sectorDisp") or ""
        return sector if sector else "Unknown"
    except Exception:
        return "Unknown"


@st.cache_data(ttl=60)
def _ta_port_fetch_ohlcv(ticker: str, period: str, interval: str) -> pd.DataFrame:
    try:
        return yf.Ticker(ticker.upper()).history(
            period=period, interval=interval, auto_adjust=True
        )
    except Exception:
        return pd.DataFrame()


def _ta_port_calc_ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def _ta_port_calc_bollinger(
    close: pd.Series, period: int, std_mult: float
) -> tuple[pd.Series, pd.Series, pd.Series]:
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    return sma + std_mult * std, sma, sma - std_mult * std


@st.cache_data(ttl=300)
def _ta_port_detect_patterns(ticker: str, period: str, interval: str) -> list:
    """Run the full pattern detection pipeline. Cached 5 minutes."""
    try:
        raw = yf.Ticker(ticker.upper()).history(
            period=period, interval=interval, auto_adjust=True
        )
        if raw is None or raw.empty:
            return []
        engine = PatternDetectionEngine(raw, ticker=ticker)
        return engine.detect_all()
    except Exception:
        return []


def _ta_port_bar_at_offset(df_index: pd.Index, start_idx, offset: float):
    """Return the index label at start_idx + offset bars (clamped to df range)."""
    try:
        pos    = df_index.get_loc(start_idx)
        target = max(0, min(int(round(pos + offset)), len(df_index) - 1))
        return df_index[target]
    except Exception:
        return df_index[-1]


def _ta_port_draw_pattern_geometry(
    fig: go.Figure,
    p,
    x_start,
    x_end,
    base_color: str,
    df: pd.DataFrame,
) -> None:
    """Draw pattern-specific geometric overlays on the price sub-chart (row=1)."""
    kl      = p.key_levels or {}
    pt      = p.pattern_type
    col     = base_color
    _TL     = "rgba(100,149,237,0.9)"
    y_lo    = float(df["Low"].min())
    y_hi    = float(df["High"].max())

    def hline(y: float, label: str, *, dash="dot", width=1.5, lcolor=None, alpha=0.85):
        c = lcolor or col
        fig.add_shape(type="line", x0=x_start, y0=y, x1=x_end, y1=y,
                      line=dict(color=c, width=width, dash=dash), opacity=alpha,
                      row=1, col=1)
        fig.add_annotation(x=x_end, y=y, text=f"  {label} ${y:.2f}",
                           showarrow=False, xanchor="left",
                           font=dict(color=c, size=8),
                           bgcolor="rgba(14,17,23,0.72)", borderpad=2, row=1, col=1)

    def diag(xa, ya: float, xb, yb: float, *, dash="dot", width=1.5, lcolor=None, alpha=0.85):
        c = lcolor or col
        fig.add_shape(type="line", x0=xa, y0=ya, x1=xb, y1=yb,
                      line=dict(color=c, width=width, dash=dash), opacity=alpha,
                      row=1, col=1)

    def vline(x, *, dash="dash", width=1.0, lcolor=None, alpha=0.5):
        c = lcolor or col
        fig.add_shape(type="line", x0=x, x1=x, y0=y_lo, y1=y_hi,
                      line=dict(color=c, width=width, dash=dash), opacity=alpha,
                      row=1, col=1)

    def markers(xs, ys, symbol: str, size: int = 10, *, mcolor=None):
        c = mcolor or col
        fig.add_trace(go.Scatter(
            x=list(xs), y=list(ys), mode="markers",
            marker=dict(symbol=symbol, size=size, color=c,
                        line=dict(color="white", width=1)),
            showlegend=False, hoverinfo="skip",
        ), row=1, col=1)

    if pt in ("bos_bullish", "choch_bullish", "bos_bearish", "choch_bearish"):
        lvl = kl.get("trigger_level") or kl.get("broken_swing")
        if lvl:
            tag = "CHoCH" if "choch" in pt else "BOS"
            hline(lvl, tag, dash="dashdot", width=2.0)
            vline(x_end, alpha=0.4)
            sym = "triangle-up" if p.direction == "bullish" else "triangle-down"
            markers([x_start], [lvl], sym, size=11)

    elif pt == "breakout_resistance":
        lvl = kl.get("resistance")
        if lvl: hline(lvl, "Resistance", dash="dot", width=1.5)
    elif pt == "breakout_support":
        lvl = kl.get("support")
        if lvl: hline(lvl, "Support", dash="dot", width=1.5)

    elif pt in ("bull_flag", "bear_flag", "pennant_bull", "pennant_bear"):
        ph        = kl.get("pole_high")
        pl        = kl.get("pole_low")
        fh_end    = kl.get("flag_high")
        fl_end    = kl.get("flag_low")
        fh_start  = kl.get("flag_high_start")
        fl_start  = kl.get("flag_low_start")
        bar_off   = kl.get("flag_bar_offset", 1.0)
        flag_x0   = _ta_port_bar_at_offset(df.index, x_start, bar_off)

        if ph is not None and pl is not None:
            py0, py1 = (pl, ph) if p.direction == "bullish" else (ph, pl)
            diag(x_start, py0, flag_x0, py1, dash="solid", width=1.5, lcolor=col)

        if fh_start is not None and fh_end is not None:
            diag(flag_x0, fh_start, x_end, fh_end, dash="dot", width=1.5, lcolor=_TL)
        elif fh_end is not None:
            hline(fh_end, "Chan. High", dash="dot", width=1.2, lcolor=_TL)

        if fl_start is not None and fl_end is not None:
            diag(flag_x0, fl_start, x_end, fl_end, dash="dot", width=1.5, lcolor=_TL)
        elif fl_end is not None:
            hline(fl_end, "Chan. Low", dash="dot", width=1.2, lcolor=_TL)

    elif pt in ("asc_triangle", "desc_triangle", "sym_triangle",
                "rising_wedge", "falling_wedge"):
        ut_end   = kl.get("upper_trendline")
        lt_end   = kl.get("lower_trendline")
        ut_start = kl.get("upper_start")
        lt_start = kl.get("lower_start")

        if ut_start is not None and ut_end is not None:
            diag(x_start, ut_start, x_end, ut_end, dash="dot", width=1.5, lcolor=_TL)
        elif ut_end is not None:
            hline(ut_end, "Upper TL", dash="dot", width=1.2, lcolor=_TL)

        if lt_start is not None and lt_end is not None:
            diag(x_start, lt_start, x_end, lt_end, dash="dot", width=1.5, lcolor=_TL)
        elif lt_end is not None:
            hline(lt_end, "Lower TL", dash="dot", width=1.2, lcolor=_TL)

    elif pt in ("double_top", "double_bottom"):
        nk      = kl.get("neckline")
        lp      = kl.get("left_peak")
        rp      = kl.get("right_peak")
        p2_off  = kl.get("peak2_bar_offset", 10.0)
        peak2_x = _ta_port_bar_at_offset(df.index, x_start, p2_off)

        if nk:
            hline(nk, "Neckline", dash="dashdot", width=2.0)
        if lp is not None:
            sym = "triangle-down" if pt == "double_top" else "triangle-up"
            markers([x_start, peak2_x], [lp, rp if rp is not None else lp], sym, size=12)

    elif pt in ("head_shoulders", "inv_head_shoulders"):
        t1       = kl.get("trough_1")
        nk       = kl.get("neckline")
        lp       = kl.get("left_peak")
        hd       = kl.get("head")
        rp       = kl.get("right_peak")
        hd_off   = kl.get("head_bar_offset", 0.0)
        rs_off   = kl.get("right_shoulder_bar_offset", 0.0)

        if t1 is not None and nk is not None:
            diag(x_start, t1, x_end, nk, dash="dashdot", width=2.0)

        if lp is not None and hd is not None and rp is not None:
            hd_x = _ta_port_bar_at_offset(df.index, x_start, hd_off)
            rs_x = _ta_port_bar_at_offset(df.index, x_start, rs_off)
            sym  = "triangle-down" if pt == "head_shoulders" else "triangle-up"
            markers([x_start, hd_x, rs_x], [lp, hd, rp], sym, size=10)

    elif pt == "cup_handle":
        lr        = kl.get("cup_left_rim")
        rr        = kl.get("cup_right_rim")
        cb        = kl.get("cup_bottom")
        bot_off   = kl.get("bottom_bar_offset", 0.0)
        rim_off   = kl.get("right_rim_bar_offset", 0.0)
        if lr is not None and rr is not None:
            hline((lr + rr) / 2.0, "Cup Rim", dash="dashdot", width=2.0)
        if cb is not None:
            bot_x = _ta_port_bar_at_offset(df.index, x_start, bot_off)
            markers([bot_x], [cb], "circle", size=9)

    elif pt in ("range_breakout_bull", "range_breakout_bear"):
        rh = kl.get("range_high")
        rl = kl.get("range_low")
        if rh: hline(rh, "Range High", dash="dot", width=1.5)
        if rl: hline(rl, "Range Low",  dash="dot", width=1.5)


def _ta_port_add_pattern_overlays(
    fig: go.Figure,
    patterns: list,
    df: pd.DataFrame,
) -> go.Figure:
    """For each detected pattern >= 0.55 confidence draw shaded region + geometric lines."""
    _MAX_GEOM  = 4
    geom_drawn = 0
    shown_sl   = shown_tp = False

    for p in patterns:
        if p.confidence_score < 0.55:
            continue

        label  = _TA_PORT_PATTERN_LABELS.get(p.pattern_type, p.pattern_type)
        color  = _TA_PORT_UP if p.direction == "bullish" else _TA_PORT_DOWN
        clabel = _ta_port_confidence_label(p.confidence_score)

        try:
            x_start = p.start_index
            if x_start not in df.index:
                x_start = df.index[0]
        except Exception:
            x_start = df.index[0]

        try:
            x_end = p.detected_at_bar
            if x_end not in df.index:
                x_end = df.index[-1]
        except Exception:
            x_end = df.index[-1]

        if geom_drawn < _MAX_GEOM:
            fig.add_vrect(
                x0=x_start, x1=x_end, fillcolor=color,
                opacity=0.06 if geom_drawn == 0 else 0.03,
                layer="below", line_width=0, row=1, col=1,
            )
            _ta_port_draw_pattern_geometry(fig, p, x_start, x_end, color, df)
            geom_drawn += 1

        y_val = p.entry_price or float(df["Close"].iloc[-1])
        fig.add_annotation(
            x=x_end, y=y_val,
            text=f"<b>{label}</b><br>{p.confidence_score:.2f} {clabel}",
            showarrow=True, arrowhead=2, arrowcolor=color, arrowsize=1,
            ax=0, ay=-40, bgcolor=color, opacity=0.85,
            font=dict(color="white", size=9), row=1, col=1,
        )

        if not shown_sl and p.stop_loss:
            fig.add_hline(
                y=p.stop_loss, line_dash="dash",
                line_color="rgba(239,83,80,0.6)", line_width=1,
                annotation_text="SL", annotation_font_size=9,
                annotation_position="right", row=1, col=1,
            )
            shown_sl = True

        if not shown_tp and p.target:
            fig.add_hline(
                y=p.target, line_dash="dash",
                line_color="rgba(38,166,154,0.6)", line_width=1,
                annotation_text="TP", annotation_font_size=9,
                annotation_position="right",
                row=1, col=1,
            )
            shown_tp = True

    return fig


def _ta_port_build_chart(
    df: pd.DataFrame,
    ticker: str,
    show_emas: bool,
    ema_periods: list[int],
    show_bb: bool,
    bb_period: int,
    bb_std: float,
    patterns: list | None = None,
    show_patterns: bool = False,
) -> go.Figure:
    vol_colors = [
        _TA_PORT_UP if c >= o else _TA_PORT_DOWN
        for o, c in zip(df["Open"], df["Close"])
    ]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        row_heights=[0.78, 0.22],
    )

    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            name=ticker.upper(),
            increasing_line_color=_TA_PORT_UP,
            decreasing_line_color=_TA_PORT_DOWN,
            increasing_fillcolor=_TA_PORT_UP,
            decreasing_fillcolor=_TA_PORT_DOWN,
            line_width=1,
        ),
        row=1, col=1,
    )

    if show_bb and len(df) >= bb_period:
        upper, mid, lower = _ta_port_calc_bollinger(df["Close"], bb_period, bb_std)
        bb_label = f"BB({bb_period},{bb_std:.1f})"

        fig.add_trace(go.Scatter(
            x=df.index, y=upper,
            name=f"{bb_label} Upper",
            line=dict(color=_TA_PORT_BB_LINE, width=1, dash="dot"),
        ), row=1, col=1)

        fig.add_trace(go.Scatter(
            x=df.index, y=lower,
            name=f"{bb_label} Lower",
            fill="tonexty",
            fillcolor=_TA_PORT_BB_FILL,
            line=dict(color=_TA_PORT_BB_LINE, width=1, dash="dot"),
        ), row=1, col=1)

        fig.add_trace(go.Scatter(
            x=df.index, y=mid,
            name=f"{bb_label} Mid",
            line=dict(color=_TA_PORT_BB_MID, width=1),
        ), row=1, col=1)

    if show_emas:
        for i, period in enumerate(sorted(ema_periods)):
            if len(df) >= period:
                fig.add_trace(go.Scatter(
                    x=df.index,
                    y=_ta_port_calc_ema(df["Close"], period),
                    name=f"EMA {period}",
                    line=dict(color=_TA_PORT_EMA_PALETTE[i % len(_TA_PORT_EMA_PALETTE)], width=1.5),
                ), row=1, col=1)

    fig.add_trace(
        go.Bar(
            x=df.index,
            y=df["Volume"],
            name="Volume",
            marker_color=vol_colors,
            showlegend=False,
            opacity=0.75,
        ),
        row=2, col=1,
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=_TA_PORT_BG,
        plot_bgcolor=_TA_PORT_PLOT_BG,
        height=700,
        margin=dict(l=0, r=60, t=40, b=0),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.01,
            xanchor="left",
            x=0,
            font=dict(size=11),
            bgcolor="rgba(0,0,0,0)",
        ),
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
    )

    fig.update_xaxes(gridcolor=_TA_PORT_GRID, showgrid=True, zeroline=False)
    fig.update_yaxes(gridcolor=_TA_PORT_GRID, showgrid=True, zeroline=False)
    fig.update_yaxes(side="right", row=1, col=1)
    fig.update_yaxes(side="right", row=2, col=1, title_text="Vol", title_font_size=10)

    if show_patterns and patterns:
        fig = _ta_port_add_pattern_overlays(fig, patterns, df)

    return fig


def _ta_port_run_gemini(prompt: str) -> str:
    """Call Gemini Flash CLI for TA analysis in the Portfolio TA tab."""
    try:
        result = subprocess.run(
            ["gemini.cmd", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=90,
        )
        output = result.stdout.strip()
        if output:
            record_call("flash")
        return output
    except (subprocess.TimeoutExpired, Exception):
        return ""


def _ta_port_build_prompt(
    ticker: str,
    period_label: str,
    df: pd.DataFrame,
    patterns: list,
    ema_periods: list[int],
    show_emas: bool,
    show_bb: bool,
    bb_period: int,
    bb_std: float,
    pct: float,
) -> str:
    latest = df.iloc[-1]
    close  = float(latest["Close"])
    volume = int(latest["Volume"])

    ema_lines: list[str] = []
    if show_emas:
        for p in sorted(ema_periods):
            if len(df) >= p:
                ema_val = float(_ta_port_calc_ema(df["Close"], p).iloc[-1])
                rel     = "above" if close > ema_val else "below"
                ema_lines.append(f"  EMA({p}): ${ema_val:.2f} — price is {rel}")

    bb_line = ""
    if show_bb and len(df) >= bb_period:
        upper, mid, lower = _ta_port_calc_bollinger(df["Close"], bb_period, bb_std)
        u, m, l = float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])
        bw      = u - l
        rel_pos = (close - l) / bw if bw > 0 else 0.5
        if rel_pos > 0.8:
            pos_desc = "near upper band (potentially overbought)"
        elif rel_pos < 0.2:
            pos_desc = "near lower band (potentially oversold)"
        else:
            pos_desc = "near middle band"
        bb_line = (
            f"BB({bb_period}, {bb_std}): Upper=${u:.2f}, Mid=${m:.2f}, Lower=${l:.2f} "
            f"— price at {rel_pos:.0%} of band ({pos_desc})"
        )

    pattern_lines: list[str] = []
    for p in patterns:
        label    = _TA_PORT_PATTERN_LABELS.get(p.pattern_type, p.pattern_type)
        kl       = ", ".join(f"{k.replace('_',' ')}: ${v:.2f}" for k, v in (p.key_levels or {}).items())
        sl_str   = f"${p.stop_loss:.2f}" if p.stop_loss else "N/A"
        tp_str   = f"${p.target:.2f}"   if p.target    else "N/A"
        rr_str   = f"{p.risk_reward_ratio:.1f}:1" if p.risk_reward_ratio else "N/A"
        pattern_lines.append(
            f"  - {label} ({p.direction.upper()}, confidence={p.confidence_score:.2f})\n"
            f"    Key levels: {kl or 'N/A'} | Stop: {sl_str} | Target: {tp_str} | R/R: {rr_str}"
        )

    sign = "+" if pct >= 0 else ""

    return f"""You are an expert technical analyst. Analyze the following data for {ticker} on a {period_label} timeframe chart and provide a detailed written analysis.

=== MARKET DATA ===
Ticker: {ticker}
Timeframe: {period_label}
Current Price: ${close:.2f}
% Change: {sign}{pct:.2f}%
Volume: {volume:,}

=== EXPONENTIAL MOVING AVERAGES ===
{chr(10).join(ema_lines) if ema_lines else "No EMAs active"}

=== BOLLINGER BANDS ===
{bb_line if bb_line else "Bollinger Bands not active"}

=== DETECTED PATTERNS ({len(patterns)} total) ===
{chr(10).join(pattern_lines) if pattern_lines else "No patterns detected for this timeframe"}

=== YOUR TASK ===
Provide your analysis in this EXACT format (do not deviate from these section headers):

VERDICT: [Bullish / Bearish / Neutral] — [brief one-line reason]
CONFIDENCE: [High / Moderate / Low]

ANALYSIS:
[Write 3-5 detailed paragraphs explaining the technical picture. Reference specific patterns, EMA positions, and Bollinger Band context.]

KEY LEVELS:
- Support: [specific price levels with brief reason]
- Resistance: [specific price levels with brief reason]
- Stop zones: [specific price levels]

INVALIDATION:
[Describe what price action would invalidate the thesis.]
"""


def _ta_port_render_analysis_card(text: str) -> None:
    """Parse Gemini output and render a styled verdict card + full analysis."""
    verdict_match = re.search(r"VERDICT:\s*(.+)", text)
    verdict_line  = verdict_match.group(1).strip() if verdict_match else ""

    verdict_word = "Neutral"
    if re.search(r"\bBullish\b", verdict_line, re.IGNORECASE):
        verdict_word = "Bullish"
    elif re.search(r"\bBearish\b", verdict_line, re.IGNORECASE):
        verdict_word = "Bearish"

    if verdict_word == "Bullish":
        border_color = "#26a69a"
        badge_bg     = "#26a69a"
        badge_text   = "▲ BULLISH"
    elif verdict_word == "Bearish":
        border_color = "#ef5350"
        badge_bg     = "#ef5350"
        badge_text   = "▼ BEARISH"
    else:
        border_color = "#78909c"
        badge_bg     = "#455a64"
        badge_text   = "◆ NEUTRAL"

    reason = re.sub(r"^(Bullish|Bearish|Neutral)\s*[—\-–]\s*", "", verdict_line, flags=re.IGNORECASE).strip()

    conf_match = re.search(r"CONFIDENCE:\s*(.+)", text)
    confidence = conf_match.group(1).strip() if conf_match else ""

    sections = {}
    for section in ("ANALYSIS", "KEY LEVELS", "INVALIDATION"):
        pat   = rf"{section}:\s*\n(.*?)(?=\n(?:ANALYSIS|KEY LEVELS|INVALIDATION):|\Z)"
        match = re.search(pat, text, re.DOTALL | re.IGNORECASE)
        sections[section] = match.group(1).strip() if match else ""

    st.markdown(
        f"""
        <div style="
            border: 1px solid {border_color};
            border-left: 4px solid {border_color};
            border-radius: 8px;
            padding: 16px 20px;
            background: #161b27;
            margin-bottom: 16px;
        ">
            <div style="display:flex; align-items:center; gap:12px; margin-bottom:8px;">
                <span style="
                    background:{badge_bg}; color:white;
                    font-weight:700; font-size:14px;
                    padding:4px 12px; border-radius:4px; letter-spacing:1px;
                ">{badge_text}</span>
                {"<span style='color:#b0bec5; font-size:13px;'>Confidence: " + confidence + "</span>" if confidence else ""}
            </div>
            {"<p style='margin:4px 0 0; color:#eceff1; font-size:14px;'>" + reason + "</p>" if reason else ""}
        </div>
        """,
        unsafe_allow_html=True,
    )

    if sections["ANALYSIS"]:
        st.markdown("**Analysis**")
        for para in sections["ANALYSIS"].split("\n\n"):
            para = para.strip()
            if para:
                st.markdown(para)

    col1, col2 = st.columns(2)
    with col1:
        if sections["KEY LEVELS"]:
            st.markdown("**Key Levels**")
            st.markdown(sections["KEY LEVELS"])
    with col2:
        if sections["INVALIDATION"]:
            st.markdown("**Invalidation**")
            st.markdown(sections["INVALIDATION"])

    if not any(sections.values()):
        st.markdown(text)


@st.fragment
def _ta_ui(tickers: list[str]) -> None:
    """Technical Analysis tab fragment — scoped to portfolio tickers."""

    # ── Controls row ──────────────────────────────────────────────────────
    ctrl1, ctrl2 = st.columns([2, 6])
    with ctrl1:
        ta_port_ticker = st.selectbox(
            "Ticker",
            tickers,
            key="ta_port_ticker_select",
        )
    with ctrl2:
        ta_port_period_label = st.radio(
            "Period",
            options=list(_TA_PORT_PERIOD_CONFIG.keys()),
            index=1,  # default "3M"
            horizontal=True,
            key="ta_port_period_radio",
            label_visibility="collapsed",
        )

    # ── Indicator toggles ─────────────────────────────────────────────────
    ind_col1, ind_col2, ind_col3 = st.columns(3)
    with ind_col1:
        ta_port_show_emas = st.checkbox("EMAs (9, 21, 50)", value=True, key="ta_port_show_emas")
    with ind_col2:
        ta_port_show_bb = st.checkbox("Bollinger Bands (20, 2.0)", value=True, key="ta_port_show_bb")
    with ind_col3:
        ta_port_show_patterns = st.checkbox("Pattern Detection", value=False, key="ta_port_show_patterns")

    ta_port_ema_periods = [9, 21, 50]
    ta_port_bb_period   = 20
    ta_port_bb_std      = 2.0

    if not ta_port_ticker:
        st.info("No tickers available in portfolio.")
        return

    period, interval = _TA_PORT_PERIOD_CONFIG[ta_port_period_label]

    with st.spinner(f"Loading {ta_port_ticker} · {ta_port_period_label}…"):
        ta_df = _ta_port_fetch_ohlcv(ta_port_ticker, period, interval)

    if ta_df is None or ta_df.empty:
        st.error(f"No data returned for **{ta_port_ticker}**. Check the symbol and try again.")
        return

    # ── Price summary strip ───────────────────────────────────────────────
    latest = ta_df.iloc[-1]
    prev   = ta_df.iloc[-2] if len(ta_df) > 1 else latest
    pct    = (latest["Close"] - prev["Close"]) / prev["Close"] * 100
    sign   = "+" if pct >= 0 else ""

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Close",  f"${latest['Close']:.2f}", f"{sign}{pct:.2f}%")
    m2.metric("Open",   f"${latest['Open']:.2f}")
    m3.metric("High",   f"${latest['High']:.2f}")
    m4.metric("Low",    f"${latest['Low']:.2f}")
    m5.metric("Volume", f"{int(latest['Volume']):,}")

    # ── Pattern detection (run before chart so overlays are ready) ────────
    ta_patterns: list = []
    if ta_port_show_patterns:
        with st.spinner("Running pattern detection…"):
            ta_patterns = _ta_port_detect_patterns(ta_port_ticker, period, interval)

    # ── Chart ─────────────────────────────────────────────────────────────
    ta_fig = _ta_port_build_chart(
        ta_df, ta_port_ticker,
        ta_port_show_emas, ta_port_ema_periods,
        ta_port_show_bb, ta_port_bb_period, ta_port_bb_std,
        patterns=ta_patterns,
        show_patterns=ta_port_show_patterns,
    )
    st.plotly_chart(ta_fig, use_container_width=True)

    st.caption(
        f"{len(ta_df):,} bars · {interval} interval · "
        f"{ta_df.index[0].strftime('%Y-%m-%d')} → {ta_df.index[-1].strftime('%Y-%m-%d')} · "
        "Data via yfinance · Cached 60s"
    )

    # ── Pattern Detection results table ───────────────────────────────────
    if ta_port_show_patterns and ta_patterns:
        st.markdown("---")
        st.subheader("🔍 Pattern Detection")
        st.caption(
            f"{len(ta_patterns)} pattern{'s' if len(ta_patterns) != 1 else ''} detected · "
            "Sorted by confidence · Chart annotations show patterns >= 0.55"
        )
        rows = []
        for p in ta_patterns:
            label    = _TA_PORT_PATTERN_LABELS.get(p.pattern_type, p.pattern_type)
            clabel   = _ta_port_confidence_label(p.confidence_score)
            dir_icon = "↑" if p.direction == "bullish" else "↓"
            rr_str   = f"{p.risk_reward_ratio:.1f}:1" if p.risk_reward_ratio else "—"
            entry    = f"${p.entry_price:.2f}"  if p.entry_price  else "—"
            sl_str   = f"${p.stop_loss:.2f}"    if p.stop_loss    else "—"
            tp_str   = f"${p.target:.2f}"       if p.target       else "—"
            vol_str  = "✓" if p.volume_confirmed else "—"
            rows.append({
                "Pattern":    label,
                "Dir":        dir_icon,
                "Confidence": f"{p.confidence_score:.2f}",
                "Level":      clabel,
                "Entry":      entry,
                "Stop":       sl_str,
                "Target":     tp_str,
                "R/R":        rr_str,
                "Vol":        vol_str,
                "Notes":      p.notes or "—",
            })
        tbl = pd.DataFrame(rows)
        st.dataframe(
            tbl,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Pattern":    st.column_config.TextColumn("Pattern",    width="medium"),
                "Dir":        st.column_config.TextColumn("Dir",        width="small"),
                "Confidence": st.column_config.TextColumn("Confidence", width="small"),
                "Level":      st.column_config.TextColumn("Level",      width="small"),
                "Entry":      st.column_config.TextColumn("Entry",      width="small"),
                "Stop":       st.column_config.TextColumn("Stop",       width="small"),
                "Target":     st.column_config.TextColumn("Target",     width="small"),
                "R/R":        st.column_config.TextColumn("R/R",        width="small"),
                "Vol":        st.column_config.TextColumn("Vol ✓",      width="small"),
                "Notes":      st.column_config.TextColumn("Notes",      width="large"),
            },
        )

    # ── AI Analysis section ───────────────────────────────────────────────
    st.markdown("---")
    st.subheader("🤖 AI Analysis")

    ta_port_cache_key = f"ta_port_ai_{ta_port_ticker}_{ta_port_period_label}"
    ta_cached   = st.session_state.get(ta_port_cache_key)
    ta_is_stale = True
    if ta_cached:
        ta_is_stale = (time.time() - ta_cached["ts"]) > 300

    hdr_col, btn_col = st.columns([5, 1])
    with hdr_col:
        if ta_cached and not ta_is_stale:
            age_s   = int(time.time() - ta_cached["ts"])
            age_str = f"{age_s // 60}m {age_s % 60}s ago" if age_s >= 60 else f"{age_s}s ago"
            st.caption(f"Gemini technical analysis · Cached {age_str} · Click Run to refresh")
        else:
            st.caption(
                "Gemini Flash analysis — references EMA alignment, "
                "Bollinger Band context, and detected patterns."
            )
    with btn_col:
        ta_run_btn = st.button(
            "Run AI Analysis",
            key="ta_port_ai_run_btn",
            use_container_width=True,
            type="primary",
        )

    if ta_run_btn:
        ta_prompt = _ta_port_build_prompt(
            ta_port_ticker, ta_port_period_label, ta_df, ta_patterns,
            ta_port_ema_periods, ta_port_show_emas,
            ta_port_show_bb, ta_port_bb_period, ta_port_bb_std, pct,
        )
        with st.spinner("Gemini is analyzing the technical picture…"):
            ta_raw = _ta_port_run_gemini(ta_prompt)
        if ta_raw:
            st.session_state[ta_port_cache_key] = {"ts": time.time(), "text": ta_raw}
            ta_cached   = st.session_state[ta_port_cache_key]
            ta_is_stale = False
        else:
            st.error("Gemini returned no response. Check the CLI is available and try again.")
            return

    if ta_cached and not ta_is_stale:
        _ta_port_render_analysis_card(ta_cached["text"])
    elif not ta_run_btn:
        st.info(
            "Click **Run AI Analysis** to generate a Gemini Flash breakdown of "
            "the EMA alignment and Bollinger Band position."
        )

# ---------------------------------------------------------------------------
# WSB / Reddit Sentiment DB helpers
# ---------------------------------------------------------------------------

def _wsb_get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_WSB_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_WSB_DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(_WSB_SCHEMA_PATH) as fh:
        conn.executescript(fh.read())
    return conn


def _wsb_is_summary_fresh(analyzed_at: str) -> bool:
    try:
        dt = datetime.fromisoformat(analyzed_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt < timedelta(hours=_WSB_SUMMARY_TTL_HOURS)
    except Exception:
        return False


def _wsb_get_cached_summary(ticker: str) -> dict | None:
    try:
        conn = _wsb_get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM wsb_ticker_summaries WHERE ticker = ?",
                (ticker.upper(),),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _wsb_get_cached_posts(ticker: str) -> list[dict]:
    try:
        conn = _wsb_get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM wsb_posts WHERE ticker = ? ORDER BY score DESC LIMIT 5",
                (ticker.upper(),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _wsb_save_post(post: dict) -> None:
    try:
        conn = _wsb_get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO wsb_posts
                   (post_id, ticker, title, body, author, score, num_comments,
                    created_utc, url, permalink, fetched_at,
                    sentiment_score, sentiment_label, analyzed_at)
                   VALUES
                   (:post_id, :ticker, :title, :body, :author, :score, :num_comments,
                    :created_utc, :url, :permalink, :fetched_at,
                    :sentiment_score, :sentiment_label, :analyzed_at)""",
                post,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _wsb_save_summary(
    ticker: str,
    subreddits: list[str],
    sentiment_score: float,
    sentiment_label: str,
    summary: str,
    post_ids: list[str],
) -> None:
    try:
        conn = _wsb_get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO wsb_ticker_summaries
                   (ticker, subreddits, sentiment_score, sentiment_label,
                    summary, post_ids, analyzed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ticker.upper(),
                    json.dumps(subreddits),
                    sentiment_score,
                    sentiment_label,
                    summary,
                    json.dumps(post_ids),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _load_reddit_sentiment(ticker: str) -> dict:
    """Fetch Reddit sentiment for ticker from cache or live.

    Returns a dict with keys:
        summary_row (dict|None), posts (list[dict]),
        from_cache (bool), error (str|None).
    Catches all exceptions so a failure for one ticker cannot break the page.
    """
    _default = {"summary_row": None, "posts": [], "from_cache": False, "error": None}
    try:
        cached_summary = _wsb_get_cached_summary(ticker)
        if cached_summary and _wsb_is_summary_fresh(cached_summary.get("analyzed_at", "")):
            return {
                "summary_row": cached_summary,
                "posts": _wsb_get_cached_posts(ticker),
                "from_cache": True,
                "error": None,
            }

        posts, subreddits = fetch_top_posts_for_ticker(ticker)
        if not posts:
            return {**_default, "error": f"No Reddit posts found for {ticker}."}

        analyzed_posts: list[dict] = []
        for post in posts:
            try:
                s = analyze_sentiment(post["title"], post["body"], ticker)
            except Exception:
                s = {"sentiment_score": 0.0, "sentiment_label": "neutral"}
            full = {
                **post,
                "sentiment_score": s["sentiment_score"],
                "sentiment_label": s["sentiment_label"],
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            }
            _wsb_save_post(full)
            analyzed_posts.append(full)

        try:
            batch = analyze_batch_sentiment(posts, ticker)
        except Exception:
            scores = [p.get("sentiment_score", 0.0) for p in analyzed_posts]
            avg = sum(scores) / len(scores) if scores else 0.0
            batch = {
                "sentiment_score": avg,
                "sentiment_label": "positive" if avg > 0.1 else "negative" if avg < -0.1 else "neutral",
                "summary": "",
            }

        _wsb_save_summary(
            ticker=ticker,
            subreddits=subreddits,
            sentiment_score=batch["sentiment_score"],
            sentiment_label=batch["sentiment_label"],
            summary=batch.get("summary", ""),
            post_ids=[p["post_id"] for p in posts],
        )
        return {
            "summary_row": _wsb_get_cached_summary(ticker),
            "posts": analyzed_posts,
            "from_cache": False,
            "error": None,
        }
    except Exception as exc:
        return {**_default, "error": str(exc)}


# ---------------------------------------------------------------------------
# WSB render helpers
# ---------------------------------------------------------------------------

_WSB_SENTIMENT_COLOR = {"positive": "#00c853", "negative": "#ff1744", "neutral": "#ffd600"}
_WSB_SENTIMENT_ICON  = {"positive": "▲", "negative": "▼", "neutral": "●"}


def _render_wsb_post(post: dict, idx: int) -> None:
    label   = post.get("sentiment_label", "neutral")
    icon    = _WSB_SENTIMENT_ICON.get(label, "●")
    sub     = post.get("subreddit", "")
    sub_tag = f"[r/{sub}] " if sub else ""
    score_str = f"{post['score']:,}" if post.get("score") is not None else "—"
    header  = f"{icon} {sub_tag}{post.get('title', '')[:80]}"

    with st.expander(header, expanded=(idx == 0)):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Upvotes",   score_str)
        c2.metric("Comments",  post.get("num_comments", "—"))
        c3.metric("Sentiment", label.capitalize())
        c4.metric("Score",     f"{post.get('sentiment_score', 0.0):.2f}")

        body = post.get("body", "")
        if body:
            st.markdown(body[:600] + ("…" if len(body) > 600 else ""))

        permalink = post.get("permalink", "")
        if permalink:
            st.markdown(f"[Open on Reddit ↗]({permalink})")
        st.caption(
            f"Posted by u/{post.get('author', '?')} · "
            f"r/{post.get('subreddit', '?')} · "
            f"{'From DB cache' if post.get('analyzed_at') else 'Just analyzed'}"
        )


def _render_wsb_ticker_result(ticker: str, entry: dict) -> None:
    error = entry.get("error")
    if error:
        st.warning(f"**{ticker}**: {error}")
        return

    summary_row = entry.get("summary_row")
    posts       = entry.get("posts", [])
    from_cache  = entry.get("from_cache", False)

    if not summary_row and not posts:
        st.info(f"No Reddit data available for **{ticker}**.")
        return

    if summary_row:
        label   = summary_row.get("sentiment_label", "neutral")
        score   = summary_row.get("sentiment_score", 0.0)
        summary = summary_row.get("summary", "")
        color   = _WSB_SENTIMENT_COLOR.get(label, "#ffd600")
        icon    = _WSB_SENTIMENT_ICON.get(label, "●")

        try:
            subreddits_searched = json.loads(summary_row.get("subreddits", "[]"))
        except (json.JSONDecodeError, TypeError):
            subreddits_searched = []
        subreddit_tags = " · ".join(f"r/{s}" for s in subreddits_searched)

        st.markdown(
            f"""
            <div style="background:linear-gradient(135deg,{color}22,{color}11);
                        border-left:4px solid {color};border-radius:6px;
                        padding:14px 18px;margin-bottom:12px">
              <span style="color:{color};font-size:1.3rem;font-weight:700">
                {icon} {label.capitalize()} &mdash; Score: {score:+.2f}
              </span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if summary:
            st.markdown(
                f"""
                <div style="background:#1a1a2e;border-left:4px solid {color};
                            border-radius:6px;padding:14px 18px;margin-bottom:12px">
                  <p style="color:#ddd;font-size:0.92rem;margin:0;line-height:1.6">{summary}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

        if subreddit_tags:
            st.caption(f"Searched: {subreddit_tags}")

    if posts:
        n_pos    = sum(1 for p in posts if p.get("sentiment_label") == "positive")
        n_neg    = sum(1 for p in posts if p.get("sentiment_label") == "negative")
        avg_sc   = sum(p.get("sentiment_score", 0.0) for p in posts) / len(posts)

        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("Posts Found", len(posts))
        mc2.metric("▲ Bullish",  n_pos)
        mc3.metric("▼ Bearish",  n_neg)
        mc4.metric("Avg Score",  f"{avg_sc:.2f}")
        st.markdown("")

        for i, post in enumerate(posts):
            _render_wsb_post(post, i)

    if from_cache and summary_row:
        analyzed_at = summary_row.get("analyzed_at", "")
        if analyzed_at:
            st.caption(
                f"Cached from {analyzed_at[:16]} UTC · "
                f"Re-runs after {_WSB_SUMMARY_TTL_HOURS}h"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_ticker(position: dict) -> str:
    for field in _TICKER_FIELD_CANDIDATES:
        val = position.get(field, "")
        if val and isinstance(val, str):
            return val.upper().strip()
    return ""


_QTY_FIELD_CANDIDATES = [
    "position", "qty", "quantity", "positionQty", "position_qty",
    "holdingQty", "holding_qty", "sharesHeld", "shares_held", "shares",
]

_COST_FIELD_CANDIDATES = [
    "costPrice", "cost_price", "avgCost", "avg_cost", "averageCost",
    "average_cost", "costBasis", "cost_basis", "avgUnitCost",
    "avg_unit_cost", "averagePrice", "average_price",
]

_MV_FIELD_CANDIDATES = [
    "marketValue", "market_value", "mktValue", "mkt_value",
    "positionValue", "position_value", "currentValue", "current_value",
]


def _extract_position_qty_cost(position: dict) -> tuple[float, float, float]:
    """Extract (quantity, avg_cost_per_share, market_value) from a Webull position dict.

    Tries each candidate field name in order; returns 0.0 for any value not found.
    All returned values are floats (never None).

    Field candidates tried in order:
        quantity   : position, qty, quantity, positionQty, position_qty,
                     holdingQty, holding_qty, sharesHeld, shares_held, shares
        avg_cost   : costPrice, cost_price, avgCost, avg_cost, averageCost,
                     average_cost, costBasis, cost_basis, avgUnitCost,
                     avg_unit_cost, averagePrice, average_price
        market_val : marketValue, market_value, mktValue, mkt_value,
                     positionValue, position_value, currentValue, current_value
    """
    def _first_float(d: dict, candidates: list) -> float:
        for field in candidates:
            val = d.get(field)
            if val is not None:
                try:
                    f = float(val)
                    if f != 0.0:
                        return f
                except (TypeError, ValueError):
                    continue
        return 0.0

    qty = _first_float(position, _QTY_FIELD_CANDIDATES)
    avg_cost = _first_float(position, _COST_FIELD_CANDIDATES)
    market_val = _first_float(position, _MV_FIELD_CANDIDATES)
    return qty, avg_cost, market_val


def _get_portfolio_tickers(positions: list) -> list:
    """Extract unique uppercase ticker strings from a list of position dicts.

    Iterates each position and tries each key in _TICKER_FIELD_CANDIDATES in order.
    Returns a list of unique, non-empty, uppercased ticker strings. Preserves
    insertion order (first occurrence of each ticker wins).
    """
    seen: set = set()
    result: list = []
    for pos in positions:
        t = _extract_ticker(pos)
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _find_overlapping_funds(portfolio_tickers: list) -> list:
    """Find concentrated hedge funds that hold any ticker in portfolio_tickers.

    Calls get_all_funds_from_db() to read from the smart cache DB. For each fund,
    checks which of its holdings' tickers are in the portfolio. Returns only funds
    with at least one overlapping holding, sorted by overlap_count descending.

    Args:
        portfolio_tickers: List of uppercase ticker strings (e.g. ['AAPL', 'TSLA']).

    Returns:
        List of dicts, each with keys:
            name (str), cik (str), report_period (str), filing_date (str),
            total_value (float), overlapping_holdings (list of HoldingRow dicts),
            overlap_count (int).
    """
    if not portfolio_tickers:
        return []

    portfolio_set = set(t.upper() for t in portfolio_tickers)

    try:
        refresh_hedge_fund_db()
        all_funds = get_all_funds_from_db()
    except Exception:
        return []

    overlapping = []
    for fund in all_funds:
        holdings = fund.get("holdings", [])
        matches = [
            h for h in holdings
            if str(h.get("ticker", "")).upper() in portfolio_set
        ]
        if not matches:
            continue
        overlapping.append({
            "name": fund.get("name", ""),
            "cik": fund.get("cik", ""),
            "report_period": fund.get("report_period", ""),
            "filing_date": fund.get("filing_date", ""),
            "total_value": fund.get("total_value", 0.0),
            "overlapping_holdings": matches,
            "overlap_count": len(matches),
        })

    overlapping.sort(key=lambda x: x["overlap_count"], reverse=True)
    return overlapping


def _render_hedge_fund_analysis(result: dict) -> None:
    """Render the structured Gemini hedge fund intelligence analysis.

    Displays:
    - Portfolio signal banner (overall_stance + confidence)
    - cross_ticker_themes as inline tags
    - Portfolio summary text
    - Per-ticker expandable cards (conviction_level, ownership_type, inferred_thesis, key_signal)
    - Flags warning block (if any flags present)

    Args:
        result: Dict returned by run_hedge_fund_analysis() with keys:
                per_ticker, portfolio_signal, flags.
    """
    if result is None:
        st.error("Analysis failed — no response from Gemini.")
        return
    if "_error" in result:
        st.error(f"Gemini error: {result['_error']}")
        return

    ps = result.get("portfolio_signal", {})
    stance = ps.get("overall_stance", "mixed")
    conf = ps.get("confidence", "medium")
    themes = ps.get("cross_ticker_themes", [])
    summary = ps.get("summary", "")

    _STANCE_COLOR = {
        "bullish": "#00c853",
        "bearish": "#ff1744",
        "mixed": "#ffd600",
        "defensive": "#ff6d00",
    }
    _STANCE_ICON = {
        "bullish": "▲",
        "bearish": "▼",
        "mixed": "●",
        "defensive": "◆",
    }
    color = _STANCE_COLOR.get(stance, "#ffd600")
    icon = _STANCE_ICON.get(stance, "●")

    # Portfolio signal banner
    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,{color}22,{color}11);
                    border-left:4px solid {color};border-radius:6px;
                    padding:14px 18px;margin-bottom:12px">
          <span style="color:{color};font-size:1.5rem;font-weight:700">
            {icon} SMART MONEY: {stance.upper()}
          </span>
          &nbsp;&nbsp;
          <span style="color:#aaa;font-size:0.9rem">Confidence: {conf.capitalize()}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Theme tags
    if themes:
        tags_html = "".join(
            f'<span style="background:#1e1e2e;border:1px solid #555;border-radius:12px;'
            f'padding:2px 10px;font-size:0.78rem;margin-right:6px;display:inline-block">{t}</span>'
            for t in themes
        )
        st.markdown(tags_html, unsafe_allow_html=True)
        st.markdown("")

    # Portfolio summary
    if summary:
        st.markdown(
            f"""
            <div style="background:#1a1a2e;border-left:4px solid {color};
                        border-radius:6px;padding:16px 20px;margin-bottom:16px">
              <p style="color:#ddd;font-size:0.95rem;margin:0;line-height:1.7">{summary}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Per-ticker expandable cards
    per_ticker = result.get("per_ticker", {})
    if per_ticker:
        st.markdown("**Per-Ticker Smart Money Signals**")
        _CONV_COLOR = {"high": "#00c853", "medium": "#ffd600", "low": "#aaa"}
        _OT_LABEL = {
            "bullish_equity": "Bullish Equity",
            "hedged": "Hedged",
            "speculative_put": "Speculative Put",
            "mixed": "Mixed",
        }
        for ticker_sym, entry in per_ticker.items():
            conv = entry.get("conviction_level", "medium")
            ot = entry.get("ownership_type", "bullish_equity")
            thesis = entry.get("inferred_thesis", "")
            key_signal = entry.get("key_signal", "")
            fund_count = entry.get("fund_count", 1)
            conv_color = _CONV_COLOR.get(conv, "#aaa")
            ot_label = _OT_LABEL.get(ot, ot.replace("_", " ").title())

            with st.expander(
                f"**{ticker_sym}** — {ot_label} · {conv.capitalize()} Conviction · {fund_count} fund{'s' if fund_count != 1 else ''}",
                expanded=False,
            ):
                c1, c2 = st.columns([1, 3])
                with c1:
                    st.markdown(
                        f'<span style="color:{conv_color};font-weight:700;font-size:1.1rem">'
                        f'{conv.upper()} CONVICTION</span>',
                        unsafe_allow_html=True,
                    )
                    st.caption(ot_label)
                with c2:
                    if key_signal:
                        st.markdown(
                            f'<div style="background:#1e1e2e;border-radius:6px;padding:10px 14px;'
                            f'font-size:0.88rem;color:#eee">'
                            f'<strong>Key Signal:</strong> {key_signal}</div>',
                            unsafe_allow_html=True,
                        )
                if thesis:
                    st.markdown(f"**Inferred Thesis:** {thesis}")

    # Flags warning block
    flags = result.get("flags", [])
    if flags:
        st.markdown("")
        flag_lines = "\n".join(f"- {f}" for f in flags)
        st.warning(f"**Notable Positioning Flags:**\n{flag_lines}")


def _render_hedge_fund_overlap(positions: list) -> None:
    """Render the Hedge Fund Overlap section for the given positions list.

    Shows a table of overlapping holdings inside an st.expander for each
    concentrated hedge fund that holds any ticker from the current portfolio.
    Displays a friendly info message when no overlaps are found.
    Also renders a Smart Money Analysis section powered by Gemini 2.5 Pro.

    Args:
        positions: Raw list of position dicts from get_positions(account_id).
    """
    portfolio_tickers = _get_portfolio_tickers(positions)

    if not portfolio_tickers:
        st.info("No ticker symbols found in your positions — cannot check hedge fund overlap.")
        return

    with st.spinner("Checking hedge fund holdings…"):
        overlapping = _find_overlapping_funds(portfolio_tickers)

    if not overlapping:
        st.info(
            "No concentrated hedge funds (< 15 positions) hold any of your current positions "
            "based on the most recent 13F-HR filings in the database. "
            "The database is populated from SEC EDGAR during the 45-day filing window after each quarter end."
        )
        return

    # Count unique portfolio tickers with overlapping funds
    _overlapping_tickers = set()
    for _f in overlapping:
        for _h in _f.get("overlapping_holdings", []):
            _t = str(_h.get("ticker", "")).upper()
            if _t:
                _overlapping_tickers.add(_t)
    st.info(
        f"Found {len(overlapping)} concentrated fund{'s' if len(overlapping) != 1 else ''} "
        f"holding {len(_overlapping_tickers)} of your position{'s' if len(_overlapping_tickers) != 1 else ''}."
    )

    # ── Smart Money Analysis ───────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Smart Money Analysis")
    st.caption(
        "Gemini 2.5 Pro infers investment theses and portfolio-level signals from the 13F data above. "
        "Results cached 4 hours."
    )

    if "hf_analysis" not in st.session_state:
        st.session_state.hf_analysis = None

    hf_run_col, hf_hint_col = st.columns([2, 8])
    with hf_run_col:
        hf_run_clicked = st.button(
            "▶ Run Hedge Fund Intelligence",
            use_container_width=True,
            key="hf_gemini_btn",
        )
    with hf_hint_col:
        st.caption("Uses Gemini 2.5 Pro · ~60–180s · Results cached 4 hours · analyzes all overlapping funds")

    _trigger_hf = hf_run_clicked or st.session_state.pop("analyze_all_hf", False)

    if _trigger_hf:
        # Clear session state to force fresh analysis or cache check
        st.session_state.hf_analysis = None
        cached = get_latest_hedge_fund_analysis(portfolio_tickers)
        if cached is not None:
            st.session_state.hf_analysis = {**cached, "from_cache": True}
        else:
            with st.spinner("Gemini 2.5 Pro analyzing hedge fund positioning…"):
                result = run_hedge_fund_analysis(overlapping, portfolio_tickers)
            if result is not None and "_error" not in result:
                save_hedge_fund_analysis(portfolio_tickers, result)
            st.session_state.hf_analysis = {**(result or {}), "from_cache": False}

    if st.session_state.hf_analysis is not None:
        hf_entry = st.session_state.hf_analysis
        from_cache = hf_entry.get("from_cache", False)
        if from_cache:
            st.info("Serving cached analysis (< 4 hours old). Click the button again to force a refresh.")
        _render_hedge_fund_analysis(hf_entry)
    else:
        # Check DB on page load (without waiting for button click)
        auto_cached = get_latest_hedge_fund_analysis(portfolio_tickers)
        if auto_cached is not None:
            st.session_state.hf_analysis = {**auto_cached, "from_cache": True}
            st.info("Serving cached analysis (< 4 hours old). Click the button above to force a refresh.")
            _render_hedge_fund_analysis(auto_cached)


# ---------------------------------------------------------------------------
# MPT Analysis
# ---------------------------------------------------------------------------

def _render_mpt_analysis(positions: list) -> None:
    """Render the Modern Portfolio Theory analysis section for the given positions list.

    Shows:
    1. Pre-computed metrics table (return, volatility, Sharpe, HHI, SPY comparison)
    2. Correlation heatmap via st.dataframe with background_gradient styling
    3. Per-ticker metrics table (annualized return, volatility, beta, current weight, suggested weight)
    4. Run MPT Analysis button + cache/freshness controls
    5. Gemini JSON result: per-ticker cards, portfolio metrics, action items

    Args:
        positions: Raw list of position dicts from get_positions(account_id).
    """
    from math import sqrt as _sqrt

    st.markdown("### Modern Portfolio Theory Analysis")
    st.caption(
        "Pre-computes covariance, correlation, Sharpe ratio, beta, and optimal weights in Python, "
        "then Gemini 2.5 Pro interprets the results. Results cached 4 hours."
    )

    portfolio_tickers = _get_portfolio_tickers(positions)
    if not portfolio_tickers:
        st.info("No ticker symbols found in positions — cannot run MPT analysis.")
        return

    # ── Session state init ────────────────────────────────────────────────
    if "mpt_analysis" not in st.session_state:
        st.session_state.mpt_analysis = None

    # ── Auto-load from DB on page load ────────────────────────────────────
    if st.session_state.mpt_analysis is None:
        auto_cached = get_latest_mpt_analysis(portfolio_tickers)
        if auto_cached is not None:
            st.session_state.mpt_analysis = {**auto_cached, "from_cache": True}

    # ── Button + hint ─────────────────────────────────────────────────────
    mpt_run_col, mpt_hint_col = st.columns([2, 8])
    with mpt_run_col:
        mpt_run_clicked = st.button(
            "▶ Run MPT Analysis",
            use_container_width=True,
            key="mpt_gemini_btn",
        )
    with mpt_hint_col:
        st.caption(
            "Uses Gemini 2.5 Pro · ~60–180s · Results cached 4 hours · "
            "analyzes correlation, Sharpe, beta, and optimal weights"
        )

    _trigger_mpt = mpt_run_clicked or st.session_state.pop("analyze_all_mpt", False)

    if _trigger_mpt:
        st.session_state.mpt_analysis = None
        cached = get_latest_mpt_analysis(portfolio_tickers)
        if cached is not None:
            st.session_state.mpt_analysis = {**cached, "from_cache": True}
        else:
            with st.spinner("Pre-computing MPT metrics and calling Gemini 2.5 Pro…"):
                raw_result = run_mpt_analysis(positions)
            if raw_result is not None and "_error" not in raw_result:
                metrics_to_save = raw_result.pop("_metrics", {})
                save_mpt_analysis(portfolio_tickers, raw_result, metrics_to_save)
                st.session_state.mpt_analysis = {
                    "result": raw_result,
                    "metrics": metrics_to_save,
                    "analyzed_at": "",
                    "from_cache": False,
                }
            else:
                # On error, still show pre-computed metrics if available
                metrics_on_error = raw_result.pop("metrics", {}) if raw_result else {}
                st.session_state.mpt_analysis = {
                    "result": raw_result or {"_error": "Analysis failed."},
                    "metrics": metrics_on_error,
                    "analyzed_at": "",
                    "from_cache": False,
                }

    # ── Render ────────────────────────────────────────────────────────────
    mpt_entry = st.session_state.mpt_analysis
    if mpt_entry is None:
        return

    from_cache = mpt_entry.get("from_cache", False)
    analyzed_at = mpt_entry.get("analyzed_at", "")
    result = mpt_entry.get("result", {})
    metrics = mpt_entry.get("metrics", {})

    if from_cache and analyzed_at:
        st.info("Serving cached analysis (< 4 hours old). Click the button again to force a refresh.")

    # ── Error handling ────────────────────────────────────────────────────
    if result and "_error" in result:
        st.error(f"Gemini error: {result['_error']}")
        # Still show pre-computed metrics if available
        if metrics and "tickers" in metrics:
            _render_mpt_metrics_tables(metrics)
        return

    if not result:
        return

    # ── Pre-computed metrics display ──────────────────────────────────────
    if metrics and "tickers" in metrics:
        _render_mpt_metrics_tables(metrics)

    # ── Gemini result: portfolio-level banner ─────────────────────────────
    ma = result.get("mpt_analysis", {})
    pm = result.get("portfolio_metrics", {})

    _SCORE_COLOR = {
        "excellent": "#00c853",
        "good": "#69f0ae",
        "fair": "#ffd600",
        "poor": "#ff1744",
    }
    _SCORE_ICON = {
        "excellent": "★",
        "good": "◆",
        "fair": "●",
        "poor": "▼",
    }
    overall_score = ma.get("overall_score", "fair")
    rebal_priority = ma.get("rebalancing_priority", "moderate")
    frontier_pos = ma.get("efficient_frontier_position", "below_frontier")
    score_color = _SCORE_COLOR.get(overall_score, "#ffd600")
    score_icon = _SCORE_ICON.get(overall_score, "●")

    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,{score_color}22,{score_color}11);
                    border-left:4px solid {score_color};border-radius:6px;
                    padding:14px 18px;margin-bottom:12px">
          <span style="color:{score_color};font-size:1.5rem;font-weight:700">
            {score_icon} MPT SCORE: {overall_score.upper()}
          </span>
          &nbsp;&nbsp;
          <span style="color:#aaa;font-size:0.9rem">
            Rebalancing: {rebal_priority.capitalize()} · {frontier_pos.replace('_', ' ').title()}
          </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Key inefficiencies as tags ─────────────────────────────────────────
    inefficiencies = ma.get("key_inefficiencies", [])
    if inefficiencies:
        tags_html = "".join(
            f'<span style="background:#1e1e2e;border:1px solid #555;border-radius:12px;'
            f'padding:2px 10px;font-size:0.78rem;margin-right:6px;margin-bottom:4px;'
            f'display:inline-block">{item}</span>'
            for item in inefficiencies
        )
        st.markdown(tags_html, unsafe_allow_html=True)
        st.markdown("")

    # ── MPT summary ───────────────────────────────────────────────────────
    summary = ma.get("summary", "")
    if summary:
        st.markdown(
            f"""
            <div style="background:#1a1a2e;border-left:4px solid {score_color};
                        border-radius:6px;padding:16px 20px;margin-bottom:16px">
              <p style="color:#ddd;font-size:0.95rem;margin:0;line-height:1.7">{summary}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── Per-ticker expandable cards ────────────────────────────────────────
    per_ticker = result.get("per_ticker", {})
    if per_ticker:
        st.markdown("**Per-Ticker MPT Assessment**")
        _RISK_COLOR = {"high": "#ff1744", "medium": "#ff6d00", "low": "#00c853"}
        _REC_LABEL = {"reduce": "Reduce", "hold": "Hold", "increase": "Increase"}
        _REC_COLOR = {"reduce": "#ff1744", "hold": "#aaa", "increase": "#00c853"}
        for ticker_sym, entry in per_ticker.items():
            risk = entry.get("risk_assessment", "medium")
            corr_risk = entry.get("correlation_risk", "medium")
            rec = entry.get("recommendation", "hold")
            rationale = entry.get("rationale", "")
            ann_ret = entry.get("annualized_return_pct", 0.0)
            ann_vol = entry.get("annualized_volatility_pct", 0.0)
            beta_val = entry.get("beta", 1.0)
            curr_wt = entry.get("weight_current_pct", 0.0)
            sug_wt = entry.get("weight_suggested_pct", 0.0)
            risk_color = _RISK_COLOR.get(risk, "#aaa")
            rec_color = _REC_COLOR.get(rec, "#aaa")
            rec_label = _REC_LABEL.get(rec, rec.capitalize())

            with st.expander(
                f"**{ticker_sym}** — {rec_label} · {risk.capitalize()} Risk · "
                f"Ret: {ann_ret:.1f}% · Vol: {ann_vol:.1f}% · β {beta_val:.2f}",
                expanded=False,
            ):
                c1, c2, c3 = st.columns([1, 1, 3])
                with c1:
                    st.markdown(
                        f'<span style="color:{risk_color};font-weight:700;font-size:1rem">'
                        f'{risk.upper()} RISK</span>',
                        unsafe_allow_html=True,
                    )
                    st.caption(f"Corr Risk: {corr_risk.capitalize()}")
                with c2:
                    st.markdown(
                        f'<span style="color:{rec_color};font-weight:700;font-size:1rem">'
                        f'{rec_label.upper()}</span>',
                        unsafe_allow_html=True,
                    )
                    wt_delta = sug_wt - curr_wt
                    delta_color = "#00c853" if wt_delta > 0 else ("#ff1744" if wt_delta < 0 else "#aaa")
                    st.markdown(
                        f'<span style="font-size:0.8rem;color:#aaa">Curr: {curr_wt:.1f}% → Opt: {sug_wt:.1f}% </span>'
                        f'<span style="font-size:0.8rem;color:{delta_color};font-weight:700">({wt_delta:+.1f}%)</span>',
                        unsafe_allow_html=True,
                    )
                with c3:
                    if rationale:
                        st.markdown(
                            f'<div style="background:#1e1e2e;border-radius:6px;padding:10px 14px;'
                            f'font-size:0.88rem;color:#eee">{rationale}</div>',
                            unsafe_allow_html=True,
                        )

    # ── Action items ──────────────────────────────────────────────────────
    action_items = result.get("action_items", [])
    if action_items:
        st.markdown("")
        st.markdown("**Rebalancing Action Items**")
        for item in action_items:
            ticker_sym = item.get("ticker", "")
            action_text = item.get("action", "")
            reason_text = item.get("reason", "")
            if ticker_sym and action_text:
                _action_lower = action_text.lower()
                if "reduce" in _action_lower or "sell" in _action_lower or "trim" in _action_lower:
                    _item_color = "#ff1744"
                elif "increase" in _action_lower or "add" in _action_lower or "buy" in _action_lower:
                    _item_color = "#00c853"
                else:
                    _item_color = "#aaa"
                st.markdown(
                    f'<div style="background:#1e1e2e;border-left:3px solid {_item_color};'
                    f'border-radius:4px;padding:8px 14px;margin-bottom:6px;font-size:0.88rem">'
                    f'<strong style="color:{_item_color}">{ticker_sym}</strong>: {action_text}'
                    + (f'<br><span style="color:#aaa">{reason_text}</span>' if reason_text else "")
                    + "</div>",
                    unsafe_allow_html=True,
                )


def _render_mpt_metrics_tables(metrics: dict) -> None:
    """Render the pre-computed MPT metrics tables and correlation heatmap.

    Shows three sub-sections:
    1. Portfolio-level summary metrics (4 st.metric widgets)
    2. Per-ticker metrics as a styled DataFrame
    3. Correlation heatmap as a styled DataFrame with background_gradient

    Args:
        metrics: The dict returned by _compute_mpt_metrics() with keys:
                 tickers, annualized_returns, annualized_vols, betas,
                 weights, portfolio_return, portfolio_volatility, portfolio_sharpe,
                 hhi, correlation_matrix, max_sharpe_weights.
    """
    tickers = metrics.get("tickers", [])
    if not tickers:
        return

    st.markdown("**Pre-Computed MPT Metrics**")

    # ── Portfolio-level summary ───────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Expected Annual Return",
        f"{metrics['portfolio_return'] * 100:.2f}%",
        help="Weighted average of each ticker's 1-year annualized return",
    )
    col2.metric(
        "Portfolio Volatility",
        f"{metrics['portfolio_volatility'] * 100:.2f}%",
        help="Annualized portfolio standard deviation accounting for correlations",
    )
    col3.metric(
        "Sharpe Ratio",
        f"{metrics['portfolio_sharpe']:.3f}",
        help="(Return − 5% risk-free rate) / Volatility. >1.0 is good.",
    )
    col4.metric(
        "HHI Concentration",
        f"{metrics['hhi']:.4f}",
        delta=f"equal-weight baseline: {1.0 / len(tickers):.4f}" if tickers else None,
        delta_color="off",
        help="Herfindahl index of weights. Lower = more diversified. Equal-weight N stocks = 1/N.",
    )

    # ── Per-ticker table ──────────────────────────────────────────────────
    rows = []
    for t in tickers:
        rows.append({
            "Ticker": t,
            "Ann. Return %": round(metrics["annualized_returns"].get(t, 0.0) * 100, 2),
            "Ann. Volatility %": round(metrics["annualized_vols"].get(t, 0.0) * 100, 2),
            "Beta (vs SPY)": round(metrics["betas"].get(t, 1.0), 3),
            "Current Weight %": round(metrics["weights"].get(t, 0.0) * 100, 2),
            "Optimal Weight %": round(metrics["max_sharpe_weights"].get(t, 0.0) * 100, 2),
        })
    _mpt_row_h = 35
    _mpt_hdr_h = 38
    if rows:
        ticker_df = pd.DataFrame(rows)
        st.dataframe(ticker_df, use_container_width=True, hide_index=True,
                     height=_mpt_hdr_h + _mpt_row_h * len(ticker_df))

    # ── Correlation heatmap ───────────────────────────────────────────────
    if len(tickers) >= 2:
        st.markdown("**Correlation Heatmap** (1-year daily returns)")
        corr_data = metrics.get("correlation_matrix", {})
        corr_df = pd.DataFrame(
            [[corr_data.get(t1, {}).get(t2, 0.0) for t2 in tickers] for t1 in tickers],
            index=tickers,
            columns=tickers,
        )
        def _corr_color(val):
            try:
                v = float(val)
            except (TypeError, ValueError):
                return ""
            # red for high positive correlation, green for low/negative
            if v >= 0.7:
                return "background-color: #c0392b; color: white"
            if v >= 0.4:
                return "background-color: #e67e22; color: white"
            if v >= 0.1:
                return "background-color: #f39c12; color: black"
            if v >= -0.1:
                return "background-color: #27ae60; color: white"
            return "background-color: #2980b9; color: white"

        styled_corr = corr_df.style.map(_corr_color).format("{:.3f}")
        st.dataframe(styled_corr, use_container_width=True,
                     height=_mpt_hdr_h + _mpt_row_h * len(corr_df))


# ---------------------------------------------------------------------------
# Market Pulse constants + helpers
# ---------------------------------------------------------------------------

_MACRO_SENTIMENT_ICON = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}

_CATEGORY_DISPLAY = {
    "monetary_policy": "Monetary Policy",
    "geopolitical": "Geopolitical",
    "macro_economy": "Macro Economy",
    "energy_commodities": "Energy & Commodities",
    "sector_financials": "Sector: Financials",
    "sector_technology": "Sector: Technology",
    "sector_energy": "Sector: Energy",
}

_MACRO_CAT_DISPLAY = {
    "bullish_for_equities": "Bullish for Equities",
    "bearish_for_equities": "Bearish for Equities",
    "mixed": "Mixed Impact",
    "sector_specific": "Sector Specific",
    "neutral": "Neutral",
}


def _impact_icon(level: int) -> str:
    if 1 <= level <= 3:
        return "📰"
    if 4 <= level <= 6:
        return "⚡"
    return "🚨"


def _format_pub_date(utc_val) -> str:
    try:
        if isinstance(utc_val, (int, float)):
            dt = datetime.fromtimestamp(utc_val, tz=datetime.now().astimezone().tzinfo)
            return dt.strftime("%b %d, %Y")
        return str(utc_val)[:10] if utc_val else "—"
    except Exception:
        return "—"


def _render_macro_card_portfolio(analysis: dict, idx: int) -> None:
    title = analysis.get("title", "No title")
    url = analysis.get("article_url", "")
    source = analysis.get("source", "")
    published = _format_pub_date(analysis.get("published_utc"))
    sentiment_score = analysis.get("sentiment_score", 0.0)
    sentiment_label = analysis.get("sentiment_label", "neutral")
    summary = analysis.get("summary", "")
    impact_level = analysis.get("impact_level", 0)
    key_themes = analysis.get("key_themes", [])
    affected_sectors = analysis.get("affected_sectors", [])
    macro_category = analysis.get("macro_category", "neutral")

    with st.expander(f"{_impact_icon(impact_level)} {title}", expanded=False):
        col1, col2 = st.columns([3, 1])
        with col1:
            meta = f"{source} · {published}"
            if affected_sectors:
                meta += " · " + " ".join(f"`{s}`" for s in affected_sectors)
            st.caption(meta)
            if summary:
                st.markdown(summary)
            if url:
                st.markdown(f"[Open article ↗]({url})")
            if key_themes:
                st.markdown(" ".join(f"`{t}`" for t in key_themes))
        with col2:
            icon = _MACRO_SENTIMENT_ICON.get(sentiment_label, "")
            st.markdown(f"**{icon} {sentiment_label.capitalize()}** ({sentiment_score:+.1f})")
            st.markdown(f"Impact **{impact_level}/10**")
            st.markdown(_MACRO_CAT_DISPLAY.get(macro_category, macro_category))


def _render_market_pulse_section() -> None:
    """Render the Market Pulse macro news section on the portfolio page."""
    st.markdown("---")
    st.subheader("🌍 Market Pulse")
    st.caption("Macro, geopolitical, and sector-wide news that moves markets.")

    mp_col1, mp_col2, mp_col3 = st.columns([2, 2, 1])
    with mp_col1:
        cat_options = ["All"] + list(_CATEGORY_DISPLAY.values())
        selected_cat_label = st.selectbox("Category", cat_options, key="mp_portfolio_cat")
        cat_reverse = {v: k for k, v in _CATEGORY_DISPLAY.items()}
        selected_cat = cat_reverse.get(selected_cat_label)
    with mp_col2:
        min_impact = st.slider("Min Impact Level", 1, 10, 4, key="mp_portfolio_impact")
    with mp_col3:
        st.markdown("")
        st.markdown("")
        refresh_clicked = st.button("🔄 Refresh", key="mp_portfolio_refresh", use_container_width=True)

    is_fresh = macro_news_cache.is_category_fresh(selected_cat, max_age_minutes=120)
    if not is_fresh or refresh_clicked:
        with st.spinner("Fetching macro news…"):
            raw = macro_news_fetcher.fetch_macro_articles(selected_cat)
        seen = macro_news_cache.get_article_urls_seen(selected_cat or "all")
        new_articles = [a for a in raw if a["article_url"] not in seen]
        if new_articles:
            with st.spinner(f"Analyzing {len(new_articles)} new articles with Gemini…"):
                for feed_cat, batch_iter in groupby(new_articles, key=lambda a: a["feed_category"]):
                    batch = list(batch_iter)[:5]
                    result = macro_news_analyzer.analyze_macro_articles(batch, feed_cat)
                    for article in batch:
                        macro_news_cache.save_macro_analysis(article, result)

    analyses = macro_news_cache.get_recent_analyses(
        category=selected_cat,
        hours=48,
        limit=20,
    )
    analyses = [a for a in analyses if a.get("impact_level", 0) >= min_impact]

    if not analyses:
        st.info("No macro news in cache yet — click 🔄 Refresh to fetch.")
        return

    for i, analysis in enumerate(analyses):
        _render_macro_card_portfolio(analysis, i)


def _sentiment_color(label: str) -> str:
    return {"positive": "#2ecc71", "negative": "#e74c3c"}.get(label, "#95a5a6")


def _impact_bar(level: int) -> str:
    filled = min(max(level, 0), 10)
    color = "#e74c3c" if filled >= 7 else "#f39c12" if filled >= 4 else "#2ecc71"
    return (
        f'<div style="background:#333;border-radius:4px;height:8px;width:100%">'
        f'<div style="background:{color};border-radius:4px;height:8px;width:{filled * 10}%"></div>'
        f"</div>"
    )


def _render_analysis(ticker: str, result: dict) -> None:
    label = result["sentiment_label"]
    score = result["sentiment_score"]
    color = _sentiment_color(label)
    impact = result["impact_level"]
    themes = result.get("key_themes", [])
    summary = result.get("summary", "")
    is_specific = result.get("is_stock_specific", True)

    badge = "Stock-Specific" if is_specific else "Sector-Level Fallback"
    badge_color = "#3498db" if is_specific else "#9b59b6"

    st.markdown(
        f"""
        <div style="border:1px solid #333;border-radius:8px;padding:16px;margin-bottom:12px">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
            <span style="font-size:1.4rem;font-weight:700;color:{color}">
              {label.upper()} &nbsp; {score:+.2f}
            </span>
            <span style="background:{badge_color};color:#fff;padding:2px 8px;
                         border-radius:12px;font-size:0.75rem">{badge}</span>
          </div>
          <div style="margin-bottom:8px">
            <span style="font-size:0.8rem;color:#aaa">Impact Level {impact}/10</span>
            {_impact_bar(impact)}
          </div>
          {"<div style='margin-bottom:10px'>" + "".join(
              f'<span style="background:#1e1e2e;border:1px solid #555;border-radius:12px;'
              f'padding:2px 10px;font-size:0.75rem;margin-right:6px;display:inline-block">{t}</span>'
              for t in themes
          ) + "</div>" if themes else ""}
          <p style="color:#ccc;font-size:0.9rem;line-height:1.5;margin:0">{summary}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _run_analysis_for_ticker(ticker: str, previous_analysis: dict | None = None) -> dict:
    """Fetch articles, scrape, and call Gemini for *ticker*.

    Returns the session-state cache dict:
    {
        "result": <analyze_articles() return dict>,
        "article_count": int,
        "sector": str,
        "is_fallback": bool,
        "analyzed_at": str ISO timestamp,
        "from_db": bool,
    }
    Also persists the result to the SQLite DB via save_analysis().
    """
    articles = fetch_news(ticker, limit=20)
    scraped: list[dict] = []
    for art in articles[:_MAX_ARTICLES_TO_SCRAPE]:
        url = art.get("article_url", "")
        content = scrape_article(url) if url else ""
        if content.startswith(("HTTP", "Access denied", "Request failed", "Could not")):
            content = art.get("description", "")
        scraped.append({
            "title": art.get("title", ""),
            "content": content,
            "source": (
                art.get("publisher", {}).get("name", "")
                if isinstance(art.get("publisher"), dict)
                else str(art.get("publisher", ""))
            ),
        })

    is_fallback = False
    sector = ""
    if len(scraped) < _MIN_TICKER_ARTICLES:
        sector, etf = get_sector_info(ticker)
        if etf:
            sector_articles = fetch_news(etf, limit=10)
            for art in sector_articles[:_MAX_ARTICLES_TO_SCRAPE]:
                url = art.get("article_url", "")
                content = scrape_article(url) if url else ""
                if content.startswith(("HTTP", "Access denied", "Request failed", "Could not")):
                    content = art.get("description", "")
                scraped.append({
                    "title": art.get("title", ""),
                    "content": content,
                    "source": (
                        art.get("publisher", {}).get("name", "")
                        if isinstance(art.get("publisher"), dict)
                        else str(art.get("publisher", ""))
                    ),
                })
            is_fallback = True

    result = analyze_articles(scraped, ticker, sector=sector, is_sector_fallback=is_fallback, previous_analysis=previous_analysis)
    save_analysis(
        ticker=ticker,
        result_dict=result,
        article_count=len(articles),
        sector=sector,
        is_fallback=is_fallback,
        articles=articles,
    )
    from datetime import datetime, timezone
    analyzed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "result": result,
        "article_count": len(articles),
        "sector": sector,
        "is_fallback": is_fallback,
        "analyzed_at": analyzed_at,
        "from_db": False,
    }


def _load_or_analyze(ticker: str) -> dict:
    """Return a session-cache dict for *ticker*, using DB cache when valid.

    Decision tree:
    1. Check st.session_state.news_results — return immediately if present
       (in-session memoization; no DB hit needed).
    2. Query DB for latest analysis via get_latest_analysis().
    3. Fetch fresh article list (fast — already @st.cache_data(ttl=900)).
    4. Call has_new_articles() to detect staleness.
    5a. If NOT stale: populate session_state from DB row and return.
    5b. If stale (or no DB row): call _run_analysis_for_ticker() which
        runs Gemini and saves the new result to DB.
    """
    ticker = ticker.upper()

    # Layer 1: in-session memoization
    if ticker in st.session_state.news_results:
        return st.session_state.news_results[ticker]

    # Layer 2: DB lookup
    cached_db = get_latest_analysis(ticker)
    articles = fetch_news(ticker, limit=20)

    if cached_db is not None and not has_new_articles(ticker, articles):
        # No new articles — serve DB result
        session_entry = {
            "result": {
                "sentiment_score": cached_db["sentiment_score"],
                "sentiment_label": cached_db["sentiment_label"],
                "summary": cached_db["summary"],
                "impact_level": cached_db["impact_level"],
                "key_themes": cached_db["key_themes"],
                "is_stock_specific": cached_db["is_stock_specific"],
            },
            "article_count": cached_db["article_count"],
            "sector": cached_db["sector"] or "",
            "is_fallback": cached_db["is_fallback"],
            "analyzed_at": cached_db["analyzed_at"],
            "from_db": True,
        }
        st.session_state.news_results[ticker] = session_entry
        return session_entry

    # Layer 3: new articles exist (or first-ever analysis) — run Gemini
    session_entry = _run_analysis_for_ticker(ticker, previous_analysis=cached_db)
    st.session_state.news_results[ticker] = session_entry
    return session_entry


# ---------------------------------------------------------------------------
# AI Insights rendering helpers
# ---------------------------------------------------------------------------

_HEALTH_COLOR = {
    "excellent": "#00c853",
    "good":      "#69f0ae",
    "fair":      "#ffd600",
    "poor":      "#ff1744",
}
_HEALTH_ICON = {"excellent": "★", "good": "◆", "fair": "●", "poor": "▼"}
_URGENCY_COLOR = {"immediate": "#ff1744", "this_week": "#ffd600", "this_month": "#69f0ae"}
_ACTION_COLOR = {
    "Buy": "#00c853", "Sell": "#ff1744", "Trim": "#ff6d00",
    "Hold": "#aaa", "Hedge": "#ffd600", "Rebalance": "#42a5f5", "Watch": "#ab47bc",
}
_SEVERITY_COLOR = {"high": "#ff1744", "medium": "#ffd600", "low": "#69f0ae"}


def _render_ai_insights(result: dict) -> None:
    """Render AI Insights from portfolio_insights_agent result dict."""
    if "_error" in result:
        st.error(f"Gemini error: {result['_error']}")
        return

    health = result.get("portfolio_health", {})
    score  = health.get("score", "fair")
    one_liner = health.get("one_liner", "")
    summary   = health.get("summary", "")
    color = _HEALTH_COLOR.get(score, "#ffd600")
    icon  = _HEALTH_ICON.get(score, "●")

    # Health banner
    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,{color}22,{color}11);
                    border-left:4px solid {color};border-radius:8px;
                    padding:14px 20px;margin-bottom:14px">
          <span style="color:{color};font-size:1.4rem;font-weight:700">
            {icon} PORTFOLIO HEALTH: {score.upper()}
          </span>
          {"&nbsp;&nbsp;<span style='color:#ccc;font-size:0.95rem'>" + one_liner + "</span>" if one_liner else ""}
        </div>
        """,
        unsafe_allow_html=True,
    )
    if summary:
        st.markdown(
            f'<div style="background:#1a1a2e;border-left:4px solid {color};border-radius:6px;'
            f'padding:14px 18px;margin-bottom:16px">'
            f'<p style="color:#ddd;font-size:0.93rem;margin:0;line-height:1.6">{summary}</p></div>',
            unsafe_allow_html=True,
        )

    # Cross-signal themes
    themes = result.get("cross_signal_themes", [])
    if themes:
        tags_html = "".join(
            f'<span style="background:#1e1e2e;border:1px solid #555;border-radius:12px;'
            f'padding:3px 11px;font-size:0.78rem;margin-right:6px;margin-bottom:4px;'
            f'display:inline-block">{t}</span>'
            for t in themes
        )
        st.markdown(tags_html, unsafe_allow_html=True)
        st.markdown("")

    # Top 3 Actions + Key Risks side by side
    col_act, col_risk = st.columns(2)

    with col_act:
        st.markdown("**🎯 Top Actions**")
        for item in result.get("top_actions", [])[:3]:
            ticker   = item.get("ticker", "?")
            action   = item.get("action", "?")
            urgency  = item.get("urgency", "this_month")
            rationale = item.get("rationale", "")
            catalyst  = item.get("catalyst", "")
            ac = _ACTION_COLOR.get(action, "#aaa")
            uc = _URGENCY_COLOR.get(urgency, "#aaa")
            urgency_label = urgency.replace("_", " ").title()
            st.markdown(
                f'<div style="background:#1e1e2e;border-left:4px solid {ac};border-radius:6px;'
                f'padding:10px 14px;margin-bottom:8px">'
                f'<strong style="color:{ac}">{ticker}</strong>'
                f'&nbsp;<span style="background:{ac}33;color:{ac};padding:1px 7px;border-radius:10px;'
                f'font-size:0.78rem;font-weight:700">{action}</span>'
                f'&nbsp;<span style="background:{uc}33;color:{uc};padding:1px 7px;border-radius:10px;'
                f'font-size:0.72rem">{urgency_label}</span>'
                + (f'<br><span style="color:#ccc;font-size:0.85rem;line-height:1.4">{rationale}</span>' if rationale else "")
                + (f'<br><span style="color:#888;font-size:0.78rem">📍 {catalyst}</span>' if catalyst else "")
                + '</div>',
                unsafe_allow_html=True,
            )

    with col_risk:
        st.markdown("**⚠️ Key Risks**")
        for item in result.get("key_risks", [])[:3]:
            risk     = item.get("risk", "?")
            severity = item.get("severity", "medium")
            detail   = item.get("detail", "")
            rc = _SEVERITY_COLOR.get(severity, "#aaa")
            st.markdown(
                f'<div style="background:#1e1e2e;border-left:4px solid {rc};border-radius:6px;'
                f'padding:10px 14px;margin-bottom:8px">'
                f'<strong style="color:{rc}">{risk}</strong>'
                f'&nbsp;<span style="background:{rc}33;color:{rc};padding:1px 7px;border-radius:10px;'
                f'font-size:0.72rem">{severity.upper()}</span>'
                + (f'<br><span style="color:#ccc;font-size:0.85rem">{detail}</span>' if detail else "")
                + '</div>',
                unsafe_allow_html=True,
            )

    # Smart money divergence
    divergence = result.get("smart_money_divergence", "")
    if divergence and divergence.lower() not in ("none detected", "none", ""):
        st.warning(f"**Smart Money vs. Retail Divergence:** {divergence}")


# ---------------------------------------------------------------------------
# Account loading (mirrors 7_positions.py)
# ---------------------------------------------------------------------------
if not is_configured():
    st.error("Webull API credentials not configured.")
    st.markdown("Add `WEBULL_APP_KEY` and `WEBULL_APP_SECRET` to your `.env` file and restart.")
    st.stop()

env_ids = get_env_account_ids()

with st.spinner("Fetching account list…"):
    account_list_result = _cached_account_list()

id_to_label: dict[str, str] = {}
if isinstance(account_list_result, list):
    for account in account_list_result:
        aid = (
            account.get("accountId")
            or account.get("account_id")
            or account.get("accountNo")
            or account.get("id")
            or ""
        )
        label = account.get("account_label") or aid
        if aid:
            id_to_label[aid] = label

if env_ids:
    account_ids = env_ids
elif isinstance(account_list_result, dict) and "error" in account_list_result:
    st.error(f"API error: {account_list_result['error']}")
    st.stop()
elif not account_list_result:
    st.warning("No accounts returned. Check your credentials.")
    st.stop()
else:
    account_ids = list(id_to_label.keys())

accounts: dict[str, dict] = {}
with st.spinner("Fetching account balances…"):
    for aid in account_ids:
        balance = _cached_balance(aid)
        if isinstance(balance, dict) and "error" not in balance:
            label = id_to_label.get(aid) or aid
            accounts[label] = balance

if not accounts:
    st.warning("No balance data returned. Check your credentials.")
    st.stop()

label_to_id: dict[str, str] = {}
for aid in account_ids:
    lbl = id_to_label.get(aid) or aid
    label_to_id[lbl] = aid

# ── Account selector (compact row) ────────────────────────────────────────────
_acct_col, _spacer_col = st.columns([2, 5])
with _acct_col:
    selected_label = st.selectbox("Account", list(accounts.keys()), label_visibility="collapsed")
selected_account_id = label_to_id.get(selected_label, selected_label)

# ---------------------------------------------------------------------------
# KPI Dashboard Header
# ---------------------------------------------------------------------------
selected_balance = accounts[selected_label]

def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None

_net_liq   = _to_float(selected_balance.get("total_net_liquidation_value"))
_day_pnl   = _to_float(selected_balance.get("total_day_profit_loss"))
_unreal    = _to_float(selected_balance.get("total_unrealized_profit_loss"))
_cash      = _to_float(selected_balance.get("total_cash_balance"))
_mkt_val   = _to_float(selected_balance.get("total_market_value"))

_kpi1, _kpi2, _kpi3, _kpi4, _kpi5 = st.columns(5)
with _kpi1:
    st.metric(
        "Portfolio Value",
        f"${_net_liq:,.2f}" if _net_liq is not None else "N/A",
        help="Total net liquidation value",
    )
with _kpi2:
    _day_pct = (
        f"{_day_pnl / (_mkt_val - _day_pnl) * 100:+.2f}%"
        if _day_pnl is not None and _mkt_val and (_mkt_val - _day_pnl) != 0
        else None
    )
    st.metric(
        "Day P&L",
        f"${_day_pnl:+,.2f}" if _day_pnl is not None else "N/A",
        delta=_day_pct,
    )
with _kpi3:
    st.metric(
        "Unrealized P&L",
        f"${_unreal:+,.2f}" if _unreal is not None else "N/A",
    )
with _kpi4:
    st.metric(
        "Cash",
        f"${_cash:,.2f}" if _cash is not None else "N/A",
    )
with _kpi5:
    st.metric(
        "Market Value",
        f"${_mkt_val:,.2f}" if _mkt_val is not None else "N/A",
    )

# Placeholder: positions count updated after fetch below

# ---------------------------------------------------------------------------
# Positions fetch (needed for all sections)
# ---------------------------------------------------------------------------

with st.spinner("Fetching positions…"):
    positions_result = _cached_positions(selected_account_id)

if isinstance(positions_result, dict) and "error" in positions_result:
    st.error(f"API error: {positions_result['error']}")
    st.stop()
elif not positions_result:
    st.info("No positions found for this account.")
    st.stop()

if isinstance(positions_result, dict):
    positions_result = [positions_result]

# Extract tickers from positions
tickers: list[str] = []
for pos in positions_result:
    t = _extract_ticker(pos)
    if t and t not in tickers:
        tickers.append(t)

df_pos = pd.DataFrame(positions_result)

# Drop unwanted columns before renaming
_drop_raw = ("positionid", "instrumenttype")
_drop_cols = [c for c in df_pos.columns if c.lower().replace("_", "") in _drop_raw]
df_pos = df_pos.drop(columns=_drop_cols, errors="ignore")

# Convert proportion to percentage string
for _col in [c for c in df_pos.columns if c.lower() == "proportion"]:
    df_pos[_col] = (df_pos[_col].astype(float) * 100).round(2).astype(str) + "%"

# Reorder: symbol leftmost, currency rightmost
_sym_candidates = {"symbol", "ticker", "tickersymbol", "stocksymbol", "sym"}
_sym_col = next((c for c in df_pos.columns if c.lower().replace("_", "") in _sym_candidates), None)
_cur_col = next((c for c in df_pos.columns if c.lower() == "currency"), None)
_middle = [c for c in df_pos.columns if c not in (_sym_col, _cur_col)]
df_pos = df_pos[[c for c in ([_sym_col] + _middle + [_cur_col]) if c is not None]]

df_pos.columns = [c.replace("_", " ").title() for c in df_pos.columns]

def _color_pnl(val):
    try:
        v = float(val)
        if v > 0:
            return "color: #2ecc71"
        if v < 0:
            return "color: #e74c3c"
    except (TypeError, ValueError):
        pass
    return ""

# ---------------------------------------------------------------------------
# Compact summary helpers for Dashboard tab
# ---------------------------------------------------------------------------

def _plotly_portfolio_layout(**overrides) -> dict:
    """Dark-theme Plotly layout dict for all new Dashboard-tab charts.

    Uses the same color constants as the TA tab:
        paper_bgcolor = _TA_PORT_BG       (#0e1117)
        plot_bgcolor  = _TA_PORT_PLOT_BG  (#161b27)
        gridcolor     = _TA_PORT_GRID     (#1f2937)

    Accepts and merges any keyword overrides (same API as components/ui.plotly_dark_layout).
    """
    base: dict = dict(
        template="plotly_dark",
        paper_bgcolor=_TA_PORT_BG,
        plot_bgcolor=_TA_PORT_PLOT_BG,
        font=dict(family="Inter, system-ui, sans-serif", color="#e8eaf0", size=12),
        xaxis=dict(gridcolor=_TA_PORT_GRID, linecolor=_TA_PORT_GRID, zerolinecolor=_TA_PORT_GRID),
        yaxis=dict(gridcolor=_TA_PORT_GRID, linecolor=_TA_PORT_GRID, zerolinecolor=_TA_PORT_GRID),
        margin=dict(l=48, r=24, t=48, b=40),
        legend=dict(
            bgcolor="rgba(0,0,0,0)",
            bordercolor=_TA_PORT_GRID,
            font=dict(size=11),
        ),
        hoverlabel=dict(
            bgcolor=_TA_PORT_PLOT_BG,
            bordercolor="#2a3a60",
            font=dict(size=12),
        ),
    )
    base.update(overrides)
    return base


def _build_allocation_charts(
    positions: list,
) -> tuple[go.Figure, go.Figure]:
    """Build a position-weight donut and a sector-allocation donut from live positions.

    Parameters
    ----------
    positions : Raw list of position dicts from get_positions(account_id).

    Returns
    -------
    (donut_weights, donut_sectors) — two Plotly Figure objects.
    Both use the dashboard dark theme. Returns two empty figures if no data.

    Notes
    -----
    Market value is extracted via _extract_position_qty_cost().
    Sector is fetched via _fetch_sector() (cached 24h, graceful fallback).
    Labels with weight < 2% are grouped into "Other" in the weights donut.
    """
    # ── Collect market values ──────────────────────────────────────────────
    ticker_mv: dict[str, float] = {}
    for pos in positions:
        t = _extract_ticker(pos)
        if not t:
            continue
        _, _, mv = _extract_position_qty_cost(pos)
        if mv > 0:
            ticker_mv[t] = ticker_mv.get(t, 0.0) + mv

    if not ticker_mv:
        empty = go.Figure()
        empty.update_layout(
            **_plotly_portfolio_layout(
                annotations=[{
                    "text": "No market value data",
                    "xref": "paper", "yref": "paper",
                    "x": 0.5, "y": 0.5, "showarrow": False,
                    "font": {"size": 14, "color": "#556080"},
                }],
                xaxis_visible=False, yaxis_visible=False,
            )
        )
        return empty, empty

    total_mv = sum(ticker_mv.values())

    # ── Position weights donut — group <2% into "Other" ───────────────────
    labels_w: list[str] = []
    values_w: list[float] = []
    other_mv = 0.0
    for t, mv in sorted(ticker_mv.items(), key=lambda x: x[1], reverse=True):
        pct = mv / total_mv * 100
        if pct >= 2.0:
            labels_w.append(t)
            values_w.append(round(pct, 2))
        else:
            other_mv += mv
    if other_mv > 0:
        labels_w.append("Other")
        values_w.append(round(other_mv / total_mv * 100, 2))

    _DONUT_COLORS = [
        "#4f8ef7", "#26a69a", "#ffd600", "#ff6d00", "#ab47bc",
        "#00bcd4", "#ef5350", "#66bb6a", "#42a5f5", "#ff7043",
        "#26c6da", "#d4e157", "#ec407a", "#7e57c2", "#29b6f6",
    ]
    fig_weights = go.Figure(go.Pie(
        labels=labels_w,
        values=values_w,
        hole=0.55,
        textinfo="percent",
        textfont=dict(size=10),
        marker=dict(
            colors=_DONUT_COLORS[:len(labels_w)],
            line=dict(color="#0e1117", width=2),
        ),
        hovertemplate="<b>%{label}</b><br>Weight: %{value:.1f}%<extra></extra>",
    ))
    fig_weights.update_layout(
        **_plotly_portfolio_layout(
            height=260,
            margin=dict(l=8, r=8, t=32, b=8),
            showlegend=True,
            legend=dict(
                orientation="v",
                yanchor="middle",
                y=0.5,
                xanchor="left",
                x=1.0,
                font=dict(size=10),
                bgcolor="rgba(0,0,0,0)",
            ),
        )
    )

    # ── Sector allocation donut ────────────────────────────────────────────
    sector_mv: dict[str, float] = {}
    for t, mv in ticker_mv.items():
        sector = _fetch_sector(t)
        sector_mv[sector] = sector_mv.get(sector, 0.0) + mv

    labels_s = list(sector_mv.keys())
    values_s = [round(v / total_mv * 100, 2) for v in sector_mv.values()]

    fig_sectors = go.Figure(go.Pie(
        labels=labels_s,
        values=values_s,
        hole=0.55,
        textinfo="percent",
        textfont=dict(size=10),
        marker=dict(
            colors=_DONUT_COLORS[:len(labels_s)],
            line=dict(color="#0e1117", width=2),
        ),
        hovertemplate="<b>%{label}</b><br>Allocation: %{value:.1f}%<extra></extra>",
    ))
    fig_sectors.update_layout(
        **_plotly_portfolio_layout(
            height=260,
            margin=dict(l=8, r=8, t=32, b=8),
            showlegend=True,
            legend=dict(
                orientation="v",
                yanchor="middle",
                y=0.5,
                xanchor="left",
                x=1.0,
                font=dict(size=10),
                bgcolor="rgba(0,0,0,0)",
            ),
        )
    )

    return fig_weights, fig_sectors


def _build_performance_chart(
    positions: list,
    period: str,
) -> go.Figure:
    """Build a normalized portfolio-vs-SPY performance line chart.

    The portfolio return is reconstructed by weighting each holding's historical
    close prices by its current share quantity (not cost). This is a
    *current-holdings-held-throughout* approximation — no transaction history is
    available from the Webull SDK.

    Parameters
    ----------
    positions : Raw position dicts from get_positions().
    period    : yfinance period string ("1mo", "3mo", "6mo", "1y").

    Returns
    -------
    Plotly Figure. Returns an empty figure with an explanatory message on error.
    """
    # ── Extract ticker -> quantity map ─────────────────────────────────────
    ticker_qty: dict[str, float] = {}
    for pos in positions:
        t = _extract_ticker(pos)
        if not t:
            continue
        qty, _, mv = _extract_position_qty_cost(pos)
        # Prefer qty; fall back to deriving from market value if qty is 0
        if qty <= 0 and mv > 0:
            # qty unknown — use market value as proxy for weighting
            qty = mv
        if qty > 0:
            ticker_qty[t] = ticker_qty.get(t, 0.0) + qty

    if not ticker_qty:
        fig = go.Figure()
        fig.update_layout(
            **_plotly_portfolio_layout(
                annotations=[{
                    "text": "No position data available",
                    "xref": "paper", "yref": "paper",
                    "x": 0.5, "y": 0.5, "showarrow": False,
                    "font": {"size": 14, "color": "#556080"},
                }],
                xaxis_visible=False, yaxis_visible=False,
            )
        )
        return fig

    all_tickers = sorted(ticker_qty.keys()) + ["SPY"]
    close_df = get_batch_history(tuple(all_tickers), period=period)

    if close_df is None or close_df.empty:
        fig = go.Figure()
        fig.update_layout(
            **_plotly_portfolio_layout(
                annotations=[{
                    "text": "Price history unavailable (offline?)",
                    "xref": "paper", "yref": "paper",
                    "x": 0.5, "y": 0.5, "showarrow": False,
                    "font": {"size": 14, "color": "#556080"},
                }],
                xaxis_visible=False, yaxis_visible=False,
            )
        )
        return fig

    # ── Compute portfolio value curve ──────────────────────────────────────
    # Only use tickers that have actual close data
    available = [t for t in ticker_qty if t in close_df.columns]
    if not available:
        fig = go.Figure()
        fig.update_layout(
            **_plotly_portfolio_layout(
                annotations=[{
                    "text": "No matching price data for portfolio tickers",
                    "xref": "paper", "yref": "paper",
                    "x": 0.5, "y": 0.5, "showarrow": False,
                    "font": {"size": 14, "color": "#556080"},
                }],
                xaxis_visible=False, yaxis_visible=False,
            )
        )
        return fig

    port_value: pd.Series = pd.Series(0.0, index=close_df.index)
    for t in available:
        qty = ticker_qty[t]
        port_value = port_value + close_df[t].ffill() * qty

    port_value = port_value.dropna()
    if port_value.empty or port_value.iloc[0] == 0:
        fig = go.Figure()
        fig.update_layout(
            **_plotly_portfolio_layout(
                annotations=[{
                    "text": "Insufficient data to build performance curve",
                    "xref": "paper", "yref": "paper",
                    "x": 0.5, "y": 0.5, "showarrow": False,
                    "font": {"size": 14, "color": "#556080"},
                }],
                xaxis_visible=False, yaxis_visible=False,
            )
        )
        return fig

    # Normalize to % return from first trading day in the period
    port_pct = (port_value / port_value.iloc[0] - 1.0) * 100.0

    fig = go.Figure()

    # Portfolio line
    fig.add_trace(go.Scatter(
        x=port_pct.index,
        y=port_pct.values,
        mode="lines",
        name="Portfolio",
        line=dict(color="#4f8ef7", width=2.5),
        hovertemplate="<b>Portfolio</b><br>%{x|%Y-%m-%d}<br>Return: %{y:.2f}%<extra></extra>",
    ))

    # SPY benchmark line
    if "SPY" in close_df.columns:
        spy_series = close_df["SPY"].dropna()
        if not spy_series.empty and spy_series.iloc[0] != 0:
            spy_pct = (spy_series / spy_series.iloc[0] - 1.0) * 100.0
            fig.add_trace(go.Scatter(
                x=spy_pct.index,
                y=spy_pct.values,
                mode="lines",
                name="SPY",
                line=dict(color="#ffd600", width=1.5, dash="dash"),
                hovertemplate="<b>SPY</b><br>%{x|%Y-%m-%d}<br>Return: %{y:.2f}%<extra></extra>",
            ))

    # Zero reference line
    fig.add_hline(y=0, line_dash="dot", line_color="#556080", line_width=1, opacity=0.5)

    final_ret = float(port_pct.iloc[-1]) if len(port_pct) > 0 else 0.0
    title_str = f"Portfolio Performance — {final_ret:+.1f}%"

    fig.update_layout(
        **_plotly_portfolio_layout(
            meta=title_str,
            height=360,
            margin=dict(l=48, r=24, t=36, b=40),
            xaxis_title=None,
            yaxis_title="Return (%)",
            yaxis_ticksuffix="%",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            hovermode="x unified",
        )
    )

    return fig


def _compute_risk_stats(
    positions: list,
    period: str,
    close_df: pd.DataFrame,
) -> dict:
    """Compute portfolio risk and return stats from a close-price DataFrame.

    Parameters
    ----------
    positions : Raw position dicts (needed for market-value weights).
    period    : Period label string for display only ("1M", "3M", "6M", "1Y").
    close_df  : DataFrame of daily close prices (output of get_batch_history).
                Must include "SPY" column and at least one portfolio ticker.

    Returns
    -------
    dict with keys:
        beta          (float)  : portfolio beta vs SPY
        ann_vol       (float)  : annualized portfolio volatility %
        sharpe        (float)  : Sharpe ratio (risk-free = _RISK_FREE_RATE)
        max_drawdown  (float)  : max drawdown % over the period (negative number)
        top_conc      (float)  : weight % of the single largest position
        top_ticker    (str)    : ticker of the single largest position
        n_days        (int)    : number of trading days in the window
    Returns a dict of None values if computation fails.
    """
    _EMPTY = dict(beta=None, ann_vol=None, sharpe=None,
                  max_drawdown=None, top_conc=None, top_ticker="?", n_days=0)
    try:
        # ── Determine weights by market value ─────────────────────────────
        ticker_mv: dict[str, float] = {}
        for pos in positions:
            t = _extract_ticker(pos)
            if not t:
                continue
            _, _, mv = _extract_position_qty_cost(pos)
            if mv > 0:
                ticker_mv[t] = ticker_mv.get(t, 0.0) + mv

        if not ticker_mv:
            return _EMPTY

        total_mv = sum(ticker_mv.values())
        available = [t for t in ticker_mv if t in close_df.columns]
        if not available:
            return _EMPTY

        weights_vec: dict[str, float] = {
            t: ticker_mv.get(t, 0.0) / total_mv for t in available
        }

        # ── Daily returns ─────────────────────────────────────────────────
        port_value = pd.Series(0.0, index=close_df.index)
        for t in available:
            qty_proxy = ticker_mv[t]  # use MV as qty proxy for weighting
            port_value = port_value + close_df[t].ffill() * (qty_proxy / close_df[t].iloc[0] if close_df[t].iloc[0] > 0 else 0)

        port_value = port_value.dropna()
        if len(port_value) < 5:
            return _EMPTY

        port_returns = port_value.pct_change().dropna()
        n_days = len(port_returns)

        # ── Annualized volatility ─────────────────────────────────────────
        ann_vol = float(port_returns.std() * np.sqrt(252) * 100)

        # ── Beta vs SPY ───────────────────────────────────────────────────
        beta = None
        if "SPY" in close_df.columns:
            spy_returns = close_df["SPY"].pct_change().dropna()
            aligned = pd.DataFrame({"port": port_returns, "spy": spy_returns}).dropna()
            if len(aligned) >= 5 and aligned["spy"].var() > 0:
                beta = float(aligned["port"].cov(aligned["spy"]) / aligned["spy"].var())

        # ── Sharpe ratio ──────────────────────────────────────────────────
        mean_daily = float(port_returns.mean())
        ann_return = mean_daily * 252
        if ann_vol > 0:
            sharpe = (ann_return - _RISK_FREE_RATE) / (ann_vol / 100)
        else:
            sharpe = 0.0

        # ── Max drawdown ──────────────────────────────────────────────────
        cumulative = (1 + port_returns).cumprod()
        rolling_max = cumulative.cummax()
        drawdowns = (cumulative - rolling_max) / rolling_max
        max_drawdown = float(drawdowns.min() * 100)

        # ── Top concentration ─────────────────────────────────────────────
        top_ticker = max(weights_vec, key=lambda t: weights_vec[t])
        top_conc = weights_vec[top_ticker] * 100

        return dict(
            beta=round(beta, 2) if beta is not None else None,
            ann_vol=round(ann_vol, 1),
            sharpe=round(sharpe, 2),
            max_drawdown=round(max_drawdown, 1),
            top_conc=round(top_conc, 1),
            top_ticker=top_ticker,
            n_days=n_days,
        )
    except Exception:
        return dict(beta=None, ann_vol=None, sharpe=None,
                    max_drawdown=None, top_conc=None, top_ticker="?", n_days=0)


def _build_enhanced_positions_df(
    positions: list,
    close_df: pd.DataFrame,
) -> pd.DataFrame:
    """Build the enhanced positions DataFrame for the new positions table.

    Columns (in order):
        Symbol, Qty, Avg Cost, Last Price, Market Value,
        Weight %, Day P&L $, Day P&L %, Total P&L $, Total P&L %, Sparkline

    Parameters
    ----------
    positions : Raw position dicts from get_positions().
    close_df  : DataFrame of daily close prices from get_batch_history (30-day window).
                Must contain at least one portfolio ticker column.

    Returns
    -------
    DataFrame sorted by Market Value descending. Returns an empty DataFrame if no data.

    Notes
    -----
    - Sparkline column contains a list of recent close floats (up to 30 values).
    - Day P&L is approximated as (last_price - prev_close) * qty when position dict
      does not carry a day_pnl field directly.
    - Falls back gracefully: any field that cannot be computed is left as None/NaN.
    """
    _DAY_PNL_CANDIDATES = [
        "dayPnl", "day_pnl", "dayProfit", "day_profit",
        "todayProfit", "today_profit", "dailyPnl", "daily_pnl",
    ]
    _TOTAL_PNL_CANDIDATES = [
        "unrealizedPnl", "unrealized_pnl", "unrealizedProfitLoss",
        "unrealized_profit_loss", "totalPnl", "total_pnl",
        "profitLoss", "profit_loss", "pnl",
    ]
    _LAST_PRICE_CANDIDATES = [
        "lastPrice", "last_price", "currentPrice", "current_price",
        "markPrice", "mark_price", "closePrice", "close_price",
    ]

    def _first_float(d: dict, candidates: list) -> float | None:
        for f in candidates:
            v = d.get(f)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    # ── Compute total market value for weight calculation ──────────────────
    ticker_mv: dict[str, float] = {}
    for pos in positions:
        t = _extract_ticker(pos)
        if not t:
            continue
        _, _, mv = _extract_position_qty_cost(pos)
        if mv > 0:
            ticker_mv[t] = ticker_mv.get(t, 0.0) + mv
    total_mv = sum(ticker_mv.values())

    rows = []
    for pos in positions:
        sym = _extract_ticker(pos)
        if not sym:
            continue

        qty, avg_cost, mv = _extract_position_qty_cost(pos)

        # Last price: try position dict first, then from close_df
        last_price = _first_float(pos, _LAST_PRICE_CANDIDATES)
        if last_price is None and sym in close_df.columns:
            col = close_df[sym].dropna()
            last_price = float(col.iloc[-1]) if not col.empty else None

        # Market value: use extracted mv, or compute from qty * last_price
        if mv <= 0 and qty > 0 and last_price is not None:
            mv = qty * last_price

        # Weight %
        weight_pct = (mv / total_mv * 100) if total_mv > 0 and mv > 0 else None

        # Day P&L $: try direct field, then compute from close_df (last - prev close)
        day_pnl = _first_float(pos, _DAY_PNL_CANDIDATES)
        if day_pnl is None and sym in close_df.columns and qty > 0:
            col = close_df[sym].dropna()
            if len(col) >= 2:
                day_pnl = float((col.iloc[-1] - col.iloc[-2]) * qty)

        day_pnl_pct = None
        if day_pnl is not None and mv is not None and mv > 0:
            prev_mv = mv - day_pnl if day_pnl is not None else None
            if prev_mv and prev_mv > 0:
                day_pnl_pct = day_pnl / prev_mv * 100

        # Total (unrealized) P&L $
        total_pnl = _first_float(pos, _TOTAL_PNL_CANDIDATES)
        if total_pnl is None and qty > 0 and avg_cost > 0 and last_price is not None:
            total_pnl = (last_price - avg_cost) * qty

        total_pnl_pct = None
        if total_pnl is not None and qty > 0 and avg_cost > 0:
            cost_basis = qty * avg_cost
            if cost_basis > 0:
                total_pnl_pct = total_pnl / cost_basis * 100

        # Sparkline: last 30 trading-day closes
        sparkline: list[float] = []
        if sym in close_df.columns:
            col = close_df[sym].dropna()
            sparkline = [float(v) for v in col.iloc[-30:].tolist() if not pd.isna(v)]

        rows.append({
            "Symbol":        sym,
            "Qty":           qty if qty > 0 else None,
            "Avg Cost":      avg_cost if avg_cost > 0 else None,
            "Last Price":    last_price,
            "Market Value":  mv if mv > 0 else None,
            "Weight %":      weight_pct,
            "Day P&L $":     day_pnl,
            "Day P&L %":     day_pnl_pct,
            "Total P&L $":   total_pnl,
            "Total P&L %":   total_pnl_pct,
            "Sparkline":     sparkline if sparkline else None,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Sort by Market Value descending (None/NaN values go last)
    df = df.sort_values("Market Value", ascending=False, na_position="last")
    df = df.reset_index(drop=True)
    return df


def _render_positions_table_styled(df: pd.DataFrame) -> None:
    _pnl_keywords = ("profit", "loss", "return", "change", "rate")
    _pnl_cols = [c for c in df.columns if any(kw in c.lower() for kw in _pnl_keywords)]
    styled = df.style
    if _pnl_cols:
        styled = styled.map(_color_pnl, subset=_pnl_cols)
    st.dataframe(styled, use_container_width=True, hide_index=True,
                 height=38 + 35 * len(df))


def _render_news_compact_summary() -> None:
    if not st.session_state.get("news_results"):
        st.caption("No news analyzed yet — use the 📰 News tab to run analysis.")
        return
    rows = []
    for _t, _e in st.session_state.news_results.items():
        _r = _e.get("result", {})
        rows.append({
            "Ticker":    _t,
            "Sentiment": _r.get("sentiment_label", "?").upper(),
            "Score":     round(_r.get("sentiment_score", 0.0), 2),
            "Impact":    f"{_r.get('impact_level', 0)}/10",
            "Cache":     "✓" if _e.get("from_db") else "✗",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                 height=38 + 35 * len(rows))


def _render_options_compact_summary() -> None:
    results = st.session_state.get("options_results", {})
    rows = []
    for _sk, _e in results.items():
        if "_error" in _e:
            continue
        _parts = _sk.split("|")
        rows.append({
            "Ticker":     _parts[0] if _parts else _sk,
            "Expiry":     _parts[1] if len(_parts) > 1 else "",
            "Bias":       _e.get("directional_bias", "neutral").upper(),
            "Confidence": _e.get("confidence", "?").capitalize(),
            "P/C OI":     f"{_e.get('metrics', {}).get('pcr_oi', 0):.3f}"
                          if _e.get("metrics", {}).get("pcr_oi") is not None else "—",
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                     height=38 + 35 * len(rows))
    else:
        st.caption("No options analyzed yet — use the ⚡ Options tab to run analysis.")


def _render_reddit_compact_summary() -> None:
    results = st.session_state.get("wsb_results", {})
    rows = []
    for _t, _e in results.items():
        _sr = _e.get("summary_row") or {}
        rows.append({
            "Ticker":    _t,
            "Sentiment": _sr.get("sentiment_label", "?").upper(),
            "Score":     round(_sr.get("sentiment_score", 0.0), 2),
            "Cache":     "✓" if _e.get("from_cache") else "✗",
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                     height=38 + 35 * len(rows))
    else:
        st.caption("No Reddit sentiment analyzed yet — use the 📡 Reddit tab.")


# ---------------------------------------------------------------------------
# Session state init
# ---------------------------------------------------------------------------
if "news_results"    not in st.session_state: st.session_state.news_results    = {}
if "options_results" not in st.session_state: st.session_state.options_results = {}
if "wsb_results"     not in st.session_state: st.session_state.wsb_results     = {}
if "hf_analysis"     not in st.session_state: st.session_state.hf_analysis     = None
if "mpt_analysis"    not in st.session_state: st.session_state.mpt_analysis    = None
if "ai_insights"     not in st.session_state: st.session_state.ai_insights     = None

if not tickers:
    st.warning("Could not extract ticker symbols from position data.")
    st.stop()

# ---------------------------------------------------------------------------
# Options Analysis — constants and functions (module-level for @st.fragment)
# ---------------------------------------------------------------------------

_RISK_FREE_RATE = 0.045
_N_CONTRACTS = 10


def _bs_greeks(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> dict:
    zero = dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)
    if T <= 1e-6 or sigma <= 1e-6 or S <= 0 or K <= 0:
        return zero
    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    pdf_d1 = norm.pdf(d1)
    gamma = pdf_d1 / (S * sigma * sqrt_T)
    vega = S * pdf_d1 * sqrt_T / 100
    if opt_type == "call":
        delta = norm.cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrt_T) - r * K * np.exp(-r * T) * norm.cdf(d2)) / 365
        rho = K * T * np.exp(-r * T) * norm.cdf(d2) / 100
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrt_T) + r * K * np.exp(-r * T) * norm.cdf(-d2)) / 365
        rho = -K * T * np.exp(-r * T) * norm.cdf(-d2) / 100
    return dict(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


def _build_options_display_df(raw: pd.DataFrame, current_price: float, expiry: str, opt_type: str) -> pd.DataFrame:
    expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    T = max((expiry_dt - datetime.today()).days / 365.0, 1.0 / 365)
    n = _N_CONTRACTS
    if opt_type == "call":
        itm = raw[raw["strike"] < current_price].nlargest(n, "strike")
        otm = raw[raw["strike"] >= current_price].nsmallest(n, "strike")
    else:
        itm = raw[raw["strike"] > current_price].nsmallest(n, "strike")
        otm = raw[raw["strike"] <= current_price].nlargest(n, "strike")

    def _f(v) -> float:
        return float(v) if pd.notna(v) and v else 0.0

    def _i(v) -> int:
        return int(v) if pd.notna(v) and v else 0

    rows = []
    for _, row in pd.concat([itm, otm]).sort_values("strike").iterrows():
        iv = _f(row.get("impliedVolatility"))
        g = _bs_greeks(current_price, float(row["strike"]), T, _RISK_FREE_RATE, iv, opt_type)
        rows.append({
            "ITM":       bool(row.get("inTheMoney", False)),
            "Strike":    float(row["strike"]),
            "Bid":       _f(row.get("bid")),
            "Ask":       _f(row.get("ask")),
            "Last":      _f(row.get("lastPrice")),
            "Volume":    _i(row.get("volume")),
            "Open Int.": _i(row.get("openInterest")),
            "IV %":      round(iv * 100, 2),
            "Delta":     round(g["delta"], 4),
            "Gamma":     round(g["gamma"], 6),
            "Theta":     round(g["theta"], 4),
            "Vega":      round(g["vega"], 4),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=60)
def _fetch_price_and_expirations(sym: str):
    t = yf.Ticker(sym)
    try:
        price = float(t.fast_info["last_price"])
    except Exception:
        price = None
    return price, list(t.options)


@st.cache_data(ttl=60)
def _fetch_chain(sym: str, exp: str):
    chain = yf.Ticker(sym).option_chain(exp)
    return chain.calls, chain.puts


_OPT_BIAS_COLOR = {"bullish": "#00c853", "bearish": "#ff1744", "neutral": "#ffd600"}
_OPT_BIAS_ICON  = {"bullish": "▲", "bearish": "▼", "neutral": "●"}


def _render_options_analysis(result: dict, spot: float) -> None:
    if result is None:
        st.error("Analysis failed — no response from Gemini.")
        return
    if "_error" in result:
        st.error(f"Gemini error: {result['_error']}")
        return

    bias     = result.get("directional_bias", "neutral")
    strength = result.get("bias_strength", "moderate")
    conf     = result.get("confidence", "medium")
    metrics  = result.get("metrics", {})
    color    = _OPT_BIAS_COLOR.get(bias, "#ffd600")
    icon     = _OPT_BIAS_ICON.get(bias, "●")

    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,{color}22,{color}11);
                    border-left:4px solid {color};border-radius:6px;
                    padding:14px 18px;margin-bottom:12px">
          <span style="color:{color};font-size:1.5rem;font-weight:700">
            {icon} {bias.upper()} — {strength.capitalize()} Conviction
          </span>
          &nbsp;&nbsp;
          <span style="color:#aaa;font-size:0.9rem">Confidence: {conf.capitalize()}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    m1, m2, m3, m4, m5 = st.columns(5)
    pcr_oi       = metrics.get("pcr_oi")
    pcr_vol      = metrics.get("pcr_vol")
    max_pain_val = metrics.get("max_pain")
    iv_skew_val  = metrics.get("iv_skew")
    net_gex_val  = metrics.get("net_gex")

    m1.metric("P/C OI Ratio",   f"{pcr_oi:.3f}"  if pcr_oi  is not None else "N/A",
              help="Put OI ÷ call OI across full chain. >1 = more puts outstanding.")
    m2.metric("P/C Vol Ratio",  f"{pcr_vol:.3f}" if pcr_vol is not None else "N/A",
              help="Put volume ÷ call volume today.")
    m3.metric("Max Pain",       f"${max_pain_val:.2f}" if max_pain_val is not None else "N/A",
              delta=f"{max_pain_val - spot:+.2f} from spot" if max_pain_val is not None else None,
              help="Strike maximizing combined OI dollar loss to buyers at expiry.")
    m4.metric("IV Skew",        f"{iv_skew_val*100:+.2f}%" if iv_skew_val is not None else "N/A",
              help="OTM put avg IV − OTM call avg IV. Positive = bearish fear premium.")
    m5.metric("Net Dealer GEX", f"${net_gex_val/1e6:.2f}M" if net_gex_val is not None else "N/A",
              help="Positive = dealers long gamma (pinning). Negative = vol amplification.")

    st.markdown("---")

    sections = [
        ("📊 Implied Volatility",          "iv_analysis",             True),
        ("⚖️ Put/Call Ratio",               "pcr_analysis",            True),
        ("🎯 Max Pain",                     "max_pain_analysis",       True),
        ("⚡ Gamma Exposure & Flows",       "gamma_exposure_analysis", False),
        ("🏦 Key Price Levels",             "key_levels",              False),
        ("🚨 Unusual Activity",             "unusual_activity",        False),
        ("⚠️ Risk Factors",                 "risk_factors",            False),
    ]
    for label, field, expanded in sections:
        with st.expander(label, expanded=expanded):
            val = result.get(field, "")
            text = "\n".join(f"- {item}" for item in val) if isinstance(val, list) else str(val).strip()
            if text:
                st.markdown(text)
            else:
                st.caption("No data returned.")

    summary = result.get("summary", "").strip()
    if summary:
        st.markdown(
            f"""
            <div style="background:#1a1a2e;border-left:4px solid {color};
                        border-radius:6px;padding:16px 20px;margin-top:12px">
              <p style="color:#ddd;font-size:0.95rem;margin:0;line-height:1.7">{summary}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )


# ── Options Analysis helpers ───────────────────────────────────────────────────

_OPT_COL_CONFIG = {
    "ITM":       st.column_config.CheckboxColumn("ITM"),
    "Strike":    st.column_config.NumberColumn("Strike",    format="$%.2f"),
    "Bid":       st.column_config.NumberColumn("Bid",       format="$%.2f"),
    "Ask":       st.column_config.NumberColumn("Ask",       format="$%.2f"),
    "Last":      st.column_config.NumberColumn("Last",      format="$%.2f"),
    "Volume":    st.column_config.NumberColumn("Volume",    format="%d"),
    "Open Int.": st.column_config.NumberColumn("Open Int.", format="%d"),
    "IV %":      st.column_config.NumberColumn("IV %",      format="%.2f%%"),
    "Delta":     st.column_config.NumberColumn("Delta",     format="%.4f"),
    "Gamma":     st.column_config.NumberColumn("Gamma",     format="%.6f"),
    "Theta":     st.column_config.NumberColumn("Theta",     format="%.4f"),
    "Vega":      st.column_config.NumberColumn("Vega",      format="%.4f"),
}

if "options_results" not in st.session_state:
    st.session_state.options_results = {}


def _opt_session_key(ticker: str, expiry: str, opt_type: str) -> str:
    return f"{ticker}|{expiry}|{opt_type}"


def _load_or_analyze_options(
    ticker: str,
    expiry: str,
    opt_type: str,
    price: float,
    calls_df,
    puts_df,
    calls_display_df,
    puts_display_df,
) -> dict:
    """Return options analysis from session state → DB cache → Gemini (in that order)."""
    sess_key = _opt_session_key(ticker, expiry, opt_type)

    if sess_key in st.session_state.options_results:
        return st.session_state.options_results[sess_key]

    cached_db = get_latest_options_analysis(ticker, expiry, opt_type)
    if cached_db is not None and is_options_analysis_fresh(cached_db.get("analyzed_at", "")):
        entry = {**cached_db, "from_db": True}
        st.session_state.options_results[sess_key] = entry
        return entry

    result = run_options_analysis(ticker, price, expiry, opt_type, calls_df, puts_df, calls_display_df, puts_display_df)
    if result and "_error" not in result:
        save_options_analysis(ticker, expiry, opt_type, price, result)
    from datetime import datetime as _dt, timezone as _tz
    analyzed_at = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S")
    entry = {**result, "analyzed_at": analyzed_at, "from_db": False}
    st.session_state.options_results[sess_key] = entry
    return entry


_PERF_PERIOD_MAP: dict[str, str] = {
    "1M": "1mo",
    "3M": "3mo",
    "6M": "6mo",
    "1Y": "1y",
}


@st.fragment
def _dashboard_visuals_ui(positions: list, tickers: list[str]) -> None:
    """Dashboard tab visual section — period selector, performance chart, allocation donuts,
    risk stats strip, and enhanced positions table.

    Wrapped in @st.fragment so the period selector only reruns this section,
    not the full portfolio page.

    Parameters
    ----------
    positions : Raw position dicts from get_positions().
    tickers   : Unique ticker strings from the portfolio (for display purposes only).
    """
    if not positions:
        st.info("No position data available.")
        return

    # ── Period selector ────────────────────────────────────────────────────
    _perf_col, _spacer = st.columns([3, 5])
    with _perf_col:
        perf_period_label = st.radio(
            "Performance Period",
            options=list(_PERF_PERIOD_MAP.keys()),
            index=3,           # default "1Y"
            horizontal=True,
            key="dash_perf_period",
        )
    perf_period_yf = _PERF_PERIOD_MAP[perf_period_label]

    # ── Fetch price data (single batched call for all charts) ──────────────
    ticker_keys = tuple(sorted(t for t in tickers if t)) + ("SPY",)
    with st.spinner("Loading price data…"):
        close_df_perf = get_batch_history(ticker_keys, period=perf_period_yf)

    # Also fetch 1-month data for sparklines (always 1mo regardless of period selector)
    with st.spinner("Loading sparkline data…"):
        close_df_spark = get_batch_history(ticker_keys, period="1mo")

    # ── Row 1: Performance chart (left) + Allocation donuts (right) ────────
    _chart_col, _donut_col = st.columns([2, 1])

    with _chart_col:
        if close_df_perf is not None and not close_df_perf.empty:
            perf_fig = _build_performance_chart(positions, perf_period_yf)
            section_header(perf_fig.layout.meta or "Portfolio Performance")
            st.plotly_chart(perf_fig, use_container_width=True)
            st.caption(
                "Current holdings held throughout period · normalized to first trading day · SPY for reference"
            )
        else:
            st.info("Price history unavailable. Check your internet connection.")

    with _donut_col:
        fig_weights, fig_sectors = _build_allocation_charts(positions)
        section_header("Position Weights")
        st.plotly_chart(fig_weights, use_container_width=True)
        section_header("Sector Allocation")
        st.plotly_chart(fig_sectors, use_container_width=True)

    st.markdown("---")

    # ── Row 2: Risk stats strip ────────────────────────────────────────────
    if close_df_perf is not None and not close_df_perf.empty:
        risk = _compute_risk_stats(positions, perf_period_label, close_df_perf)
        _r1, _r2, _r3, _r4, _r5 = st.columns(5)

        # Beta: warn if > 1.5
        _beta_val = risk["beta"]
        _beta_delta = "⚠ High market exposure" if (_beta_val is not None and _beta_val > 1.5) else None
        _r1.metric(
            "Portfolio Beta",
            f"{_beta_val:.2f}" if _beta_val is not None else "N/A",
            delta=_beta_delta,
            delta_color="inverse",
            help="Weighted beta vs SPY over selected period. ⚠ shown when beta > 1.5",
        )

        _r2.metric(
            "Ann. Volatility",
            f"{risk['ann_vol']:.1f}%" if risk["ann_vol"] is not None else "N/A",
            help="Annualized portfolio standard deviation",
        )

        _r3.metric(
            "Sharpe Ratio",
            f"{risk['sharpe']:.2f}" if risk["sharpe"] is not None else "N/A",
            help=f"(Ann. return − {_RISK_FREE_RATE*100:.1f}% risk-free) / Ann. vol",
        )

        # Max drawdown: warn if < -20%
        _dd_val = risk["max_drawdown"]
        _dd_delta = "⚠ Severe drawdown" if (_dd_val is not None and _dd_val < -20.0) else None
        _r4.metric(
            "Max Drawdown",
            f"{_dd_val:.1f}%" if _dd_val is not None else "N/A",
            delta=_dd_delta,
            delta_color="inverse",
            help="Maximum peak-to-trough decline over selected period. ⚠ shown when worse than -20%",
        )

        # Top concentration: warn if > 20%
        if risk["top_conc"] is not None:
            _conc_delta = (
                f"⚠ Concentrated" if risk["top_conc"] > 20.0
                else risk["top_ticker"]
            )
            _conc_color = "inverse" if risk["top_conc"] > 20.0 else "off"
            _r5.metric(
                "Top Position",
                f"{risk['top_conc']:.1f}%",
                delta=f"{_conc_delta} ({risk['top_ticker']})" if risk["top_conc"] > 20.0 else _conc_delta,
                delta_color=_conc_color,
                help="Largest single-position weight by market value. ⚠ shown when > 20%",
            )
        else:
            _r5.metric("Top Position", "N/A")

        st.markdown("---")

    # ── Row 3: Enhanced positions table ───────────────────────────────────
    close_for_table = close_df_spark if (close_df_spark is not None and not close_df_spark.empty) else close_df_perf
    if close_for_table is None:
        close_for_table = pd.DataFrame()

    enh_df = _build_enhanced_positions_df(positions, close_for_table)

    if enh_df.empty:
        st.info("Could not build enhanced positions table.")
    else:
        _n_pos = len(enh_df)
        section_header(
            f"Holdings — {_n_pos} position{'s' if _n_pos != 1 else ''} · sorted by market value"
        )

        # Build column_config for st.dataframe
        _col_cfg: dict = {
            "Symbol":       st.column_config.TextColumn("Symbol",       width="small"),
            "Qty":          st.column_config.NumberColumn("Qty",        format="%.4g",  width="small"),
            "Avg Cost":     st.column_config.NumberColumn("Avg Cost",   format="$%.2f", width="small"),
            "Last Price":   st.column_config.NumberColumn("Last Price", format="$%.2f", width="small"),
            "Market Value": st.column_config.NumberColumn("Mkt Value",  format="$%.0f", width="medium"),
            "Weight %":     st.column_config.NumberColumn("Weight %",   format="%.1f%%", width="small"),
            "Day P&L $":    st.column_config.NumberColumn("Day $",      format="$%.2f", width="small"),
            "Day P&L %":    st.column_config.NumberColumn("Day %",      format="%.2f%%", width="small"),
            "Total P&L $":  st.column_config.NumberColumn("Total $",    format="$%.2f", width="small"),
            "Total P&L %":  st.column_config.NumberColumn("Total %",    format="%.2f%%", width="small"),
        }

        # Add sparkline column only if the Sparkline column has non-null list values
        _has_sparklines = enh_df["Sparkline"].notna().any()
        if _has_sparklines:
            _col_cfg["Sparkline"] = st.column_config.LineChartColumn(
                "30D",
                width="medium",
                y_min=None,
                y_max=None,
            )
        else:
            enh_df = enh_df.drop(columns=["Sparkline"])

        _row_h = 35
        _hdr_h = 38
        st.dataframe(
            enh_df,
            use_container_width=True,
            hide_index=True,
            height=_hdr_h + _row_h * len(enh_df),
            column_config=_col_cfg,
        )


# ── Options Analysis UI (fragment = reruns only this section) ─────────────────

@st.fragment
def _options_analysis_ui(tickers: list[str]) -> None:
    oa_col_a, oa_col_b = st.columns([3, 1])
    with oa_col_a:
        opt_ticker = st.selectbox(
            "Analyze options for",
            ["— Select a stock —"] + tickers,
            key="opt_ticker_select",
        )
    with oa_col_b:
        opt_analyze_all = st.button("Analyze All Positions", use_container_width=True, key="opt_analyze_all_btn")

    _trigger_all_options = opt_analyze_all or st.session_state.pop("analyze_all_options", False)
    if _trigger_all_options:
        oa_progress = st.progress(0, text="Starting options analysis…")
        oa_completed = 0

        def _analyze_options_ticker(t):
            """Worker: fetch price/chain, calc Greeks, run Gemini. Returns (sess_key, entry_or_None)."""
            from datetime import datetime as _dt_w, timezone as _tz_w
            try:
                t_price, t_expirations = _fetch_price_and_expirations(t)
            except Exception:
                return _opt_session_key(t, "", "put"), None
            if not t_expirations or t_price is None:
                return _opt_session_key(t, "", "put"), None
            nearest_expiry = t_expirations[0]
            sess_key = _opt_session_key(t, nearest_expiry, "put")
            cached_db = get_latest_options_analysis(t, nearest_expiry, "put")
            if cached_db is not None and is_options_analysis_fresh(cached_db.get("analyzed_at", "")):
                return sess_key, {**cached_db, "from_db": True}
            try:
                t_calls, t_puts = _fetch_chain(t, nearest_expiry)
                t_calls_display = _build_options_display_df(t_calls, t_price, nearest_expiry, "call")
                t_puts_display  = _build_options_display_df(t_puts,  t_price, nearest_expiry, "put")
            except Exception:
                return sess_key, None
            result = run_options_analysis(t, t_price, nearest_expiry, "put", t_calls, t_puts, t_calls_display, t_puts_display)
            if result and "_error" not in result:
                save_options_analysis(t, nearest_expiry, "put", t_price, result)
            analyzed_at = _dt_w.now(_tz_w.utc).strftime("%Y-%m-%dT%H:%M:%S")
            entry = {**result, "analyzed_at": analyzed_at, "from_db": False}
            return sess_key, entry

        with ThreadPoolExecutor(max_workers=3) as oa_executor:
            oa_futures = {oa_executor.submit(_analyze_options_ticker, t): t for t in tickers}
            for oa_future in as_completed(oa_futures):
                t = oa_futures[oa_future]
                sess_key, entry = oa_future.result()
                oa_completed += 1
                if entry is None:
                    oa_progress.progress(oa_completed / len(tickers), text=f"Skipped {t}")
                else:
                    st.session_state.options_results[sess_key] = entry
                    label = "cache" if entry.get("from_db") else "Gemini"
                    oa_progress.progress(oa_completed / len(tickers), text=f"Done ({label}): {t}")
        oa_progress.empty()

    if opt_ticker and opt_ticker != "— Select a stock —":
        try:
            opt_price, opt_expirations = _fetch_price_and_expirations(opt_ticker)
        except Exception as e:
            st.error(f"Failed to fetch options data for {opt_ticker}: {e}")
            opt_expirations = []
            opt_price = None

        if not opt_expirations:
            st.warning(f"No options listed for {opt_ticker}.")
        elif opt_price is None:
            st.warning(f"Could not retrieve current price for {opt_ticker}.")
        else:
            oa_ctrl1, oa_ctrl2, oa_ctrl3 = st.columns([2, 3, 2])
            with oa_ctrl1:
                st.metric("Current Price", f"${opt_price:,.2f}")
            with oa_ctrl2:
                opt_expiry = st.selectbox("Expiration Date", opt_expirations, key="opt_expiry_select")
            with oa_ctrl3:
                opt_contract_label = st.radio("Contract Type", ["Calls", "Puts"], horizontal=True, key="opt_contract_radio")

            opt_type = "call" if opt_contract_label == "Calls" else "put"

            try:
                opt_calls_df, opt_puts_df = _fetch_chain(opt_ticker, opt_expiry)
            except Exception as e:
                st.error(f"Error loading option chain for {opt_expiry}: {e}")
                opt_calls_df = opt_puts_df = None

            if opt_calls_df is not None:
                with st.spinner("Calculating Greeks…"):
                    opt_calls_display_df = _build_options_display_df(opt_calls_df, opt_price, opt_expiry, "call")
                    opt_puts_display_df  = _build_options_display_df(opt_puts_df,  opt_price, opt_expiry, "put")

                opt_display_df = opt_calls_display_df if opt_type == "call" else opt_puts_display_df

                if not opt_display_df.empty:
                    st.caption(
                        f"{_N_CONTRACTS} nearest ITM + {_N_CONTRACTS} nearest OTM {opt_contract_label.lower()} · "
                        f"Greeks via Black-Scholes · Risk-free rate {_RISK_FREE_RATE*100:.1f}%"
                    )
                    st.dataframe(opt_display_df, use_container_width=True, hide_index=True, column_config=_OPT_COL_CONFIG)

                    opt_sess_key = _opt_session_key(opt_ticker, opt_expiry, opt_type)

                    run_col, hint_col = st.columns([2, 8])
                    with run_col:
                        opt_run_clicked = st.button("▶ Run Gemini Analysis", use_container_width=True, key="opt_run_btn")
                    with hint_col:
                        st.caption("Uses Gemini 2.5 Pro · ~30–90s · Results cached 4 hours · analyzes both calls & puts")

                    if opt_run_clicked:
                        st.session_state.options_results.pop(opt_sess_key, None)
                        with st.spinner("Gemini 2.5 Pro analyzing the full option chain…"):
                            entry = _load_or_analyze_options(
                                opt_ticker, opt_expiry, opt_type, opt_price,
                                opt_calls_df, opt_puts_df, opt_calls_display_df, opt_puts_display_df,
                            )

                    if opt_sess_key in st.session_state.options_results:
                        entry = st.session_state.options_results[opt_sess_key]
                        analyzed_at = entry.get("analyzed_at", "")
                        from_db = entry.get("from_db", False)
                        if from_db and analyzed_at:
                            st.info(f"Cached analysis from {analyzed_at} UTC (re-runs after 4 hours).")
                        elif analyzed_at:
                            st.success(f"Freshly analyzed at {analyzed_at} UTC.")
                        _render_options_analysis(entry, opt_price)
                    else:
                        cached_db = get_latest_options_analysis(opt_ticker, opt_expiry, opt_type)
                        if cached_db is not None and is_options_analysis_fresh(cached_db.get("analyzed_at", "")):
                            entry = {**cached_db, "from_db": True}
                            st.session_state.options_results[opt_sess_key] = entry
                            st.info(f"Cached analysis from {cached_db['analyzed_at']} UTC (re-runs after 4 hours).")
                            _render_options_analysis(entry, opt_price)

    # ── All Options Results Summary ────────────────────────────────────────────

    if st.session_state.options_results:
        st.markdown("### Options Analysis Results")
        for sess_key, entry in st.session_state.options_results.items():
            if "_error" in entry:
                continue
            parts = sess_key.split("|")
            t_label = parts[0] if len(parts) > 0 else sess_key
            exp_label = parts[1] if len(parts) > 1 else ""
            type_label = "Calls" if (len(parts) > 2 and parts[2] == "call") else "Puts"
            bias = entry.get("directional_bias", "neutral").upper()
            strength = entry.get("bias_strength", "moderate").capitalize()
            conf = entry.get("confidence", "medium").capitalize()
            from_db = entry.get("from_db", False)
            cache_tag = " [cached]" if from_db else ""
            spot = entry.get("spot_price") or 0.0
            with st.expander(
                f"**{t_label}** · {exp_label} {type_label} — {bias} ({strength}){cache_tag}",
                expanded=False,
            ):
                analyzed_at = entry.get("analyzed_at", "")
                if from_db and analyzed_at:
                    st.info(f"Cached from {analyzed_at} UTC.")
                elif analyzed_at:
                    st.success(f"Analyzed at {analyzed_at} UTC.")
                st.caption(f"Confidence: {conf}")
                _render_options_analysis(entry, spot)

        if st.button("Clear Options Results", key="opt_clear_btn"):
            st.session_state.options_results = {}
            st.rerun()


# ---------------------------------------------------------------------------
# Main tab navigation — wire everything together
# ---------------------------------------------------------------------------

_tab_dash, _tab_news, _tab_opt, _tab_ta, _tab_reddit, _tab_smart, _tab_pulse = st.tabs([
    "📊 Dashboard",
    "📰 News",
    "⚡ Options & MPT",
    "📈 Technical Analysis",
    "📡 Reddit",
    "🏦 Smart Money",
    "🌍 Market Pulse",
])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1: DASHBOARD — Compact overview + AI Insights
# ═══════════════════════════════════════════════════════════════════════════════
with _tab_dash:

    # ── AI Insights section (decision-first, sits directly under KPI bar) ──────
    section_header("AI Insights")

    _ins_btn_col, _ins_run_col, _ins_cap_col = st.columns([2, 2, 4])
    with _ins_btn_col:
        if st.button("⚡ Analyze Everything", use_container_width=True, key="analyze_everything_btn"):
            st.session_state.analyze_all_news    = True
            st.session_state.analyze_all_options = True
            st.session_state.analyze_all_hf      = True
            st.session_state.analyze_all_mpt     = True
            st.session_state.analyze_all_reddit  = True
            st.rerun()
    with _ins_run_col:
        _run_insights = st.button("🤖 Run AI Insights", use_container_width=True, key="ai_insights_btn")
    with _ins_cap_col:
        st.caption("⚡ runs all 5 agents · 🤖 synthesizes with Gemini 2.5 Pro")

    if _run_insights:
        st.session_state.ai_insights = None
        _pd = {
            "balance":         selected_balance,
            "positions":       positions_result,
            "news_results":    st.session_state.news_results,
            "options_results": st.session_state.options_results,
            "wsb_results":     st.session_state.wsb_results,
            "mpt_analysis":    st.session_state.mpt_analysis,
            "hf_analysis":     st.session_state.hf_analysis,
        }
        with st.spinner("Gemini 2.5 Pro synthesizing all portfolio data… (~60–120s)"):
            _insights_result = run_portfolio_insights(_pd)
        st.session_state.ai_insights = _insights_result

    if st.session_state.ai_insights is not None:
        _render_ai_insights(st.session_state.ai_insights)
        if st.button("🔄 Refresh Insights", key="ai_refresh_btn"):
            st.session_state.ai_insights = None
            st.rerun()
    else:
        st.markdown(
            '<div style="background:#161b27;border:1px solid #1e2740;border-radius:8px;'
            'padding:14px 18px;color:#7a85a0;font-size:0.85rem;line-height:1.5">'
            'Run <strong style="color:#e8eaf0">🤖 Run AI Insights</strong> for Gemini 2.5 Pro\'s holistic view — '
            'top actions, key risks, and smart-money divergence across all signals. '
            'For best results, click <strong style="color:#e8eaf0">⚡ Analyze Everything</strong> first.'
            '</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ── Portfolio visuals: performance chart, donuts, risk strip, holdings table ──
    _dashboard_visuals_ui(positions_result, tickers)

    st.markdown("---")

    # ── Signal Matrix — one table replacing five collapsed expanders ──────────
    section_header("Signal Matrix")
    st.caption(
        "Summarizes all analysis run so far. Empty cells mean that signal has not been run yet — "
        "use the tabs above or ⚡ Analyze Everything."
    )

    # Build one row per portfolio ticker
    _sig_rows = []
    for _st_ticker in tickers:
        _row: dict = {"Ticker": _st_ticker}

        # News sentiment
        _news_entry = st.session_state.news_results.get(_st_ticker)
        if _news_entry:
            _nr = _news_entry.get("result", {})
            _nl = _nr.get("sentiment_label", "neutral")
            _ns = _nr.get("sentiment_score", 0.0)
            _news_icon = {"positive": "▲", "negative": "▼", "neutral": "●"}.get(_nl, "●")
            _row["News"] = f"{_news_icon} {_nl.capitalize()} ({_ns:+.2f})"
        else:
            _row["News"] = "—"

        # Reddit sentiment
        _wsb_entry = st.session_state.wsb_results.get(_st_ticker)
        if _wsb_entry:
            _wr = (_wsb_entry.get("summary_row") or {})
            _wl = _wr.get("sentiment_label", "neutral")
            _ws = _wr.get("sentiment_score", 0.0)
            _wsb_icon = {"positive": "▲", "negative": "▼", "neutral": "●"}.get(_wl, "●")
            _row["Reddit"] = f"{_wsb_icon} {_wl.capitalize()} ({_ws:+.2f})"
        else:
            _row["Reddit"] = "—"

        # Options bias — find any session key for this ticker
        _opt_bias = "—"
        for _ok, _oe in st.session_state.options_results.items():
            if _ok.startswith(_st_ticker + "|") and "_error" not in _oe:
                _ob = _oe.get("directional_bias", "neutral")
                _ob_icon = {"bullish": "▲", "bearish": "▼", "neutral": "●"}.get(_ob, "●")
                _opt_bias = f"{_ob_icon} {_ob.capitalize()}"
                break
        _row["Options"] = _opt_bias

        # Smart Money — count funds holding this ticker from hf_analysis
        _sm_text = "—"
        _hf_ss = st.session_state.hf_analysis
        if _hf_ss and "_error" not in _hf_ss:
            _hf_pt = _hf_ss.get("per_ticker", {})
            _hf_entry = _hf_pt.get(_st_ticker, {})
            if _hf_entry:
                _fc = _hf_entry.get("fund_count", 0)
                _ot = _hf_entry.get("ownership_type", "bullish_equity")
                _ot_short = {"bullish_equity": "Bullish", "hedged": "Hedged",
                             "speculative_put": "Put", "mixed": "Mixed"}.get(_ot, _ot)
                _sm_text = f"{_ot_short} ({_fc} fund{'s' if _fc != 1 else ''})"
        _row["Smart Money"] = _sm_text

        _sig_rows.append(_row)

    if _sig_rows:
        _sig_df = pd.DataFrame(_sig_rows)

        def _color_signal_cell(val: str) -> str:
            """Return CSS color string for a signal cell value."""
            s = str(val)
            if s == "—":
                return "color: #556080"
            if "▲" in s or "Bullish" in s:
                return "color: #2ecc71; font-weight: 600"
            if "▼" in s or "Bearish" in s or "negative" in s.lower():
                return "color: #e74c3c; font-weight: 600"
            if "neutral" in s.lower() or "Neutral" in s or "●" in s:
                return "color: #ffd600"
            return "color: #e8eaf0"

        _sig_signal_cols = ["News", "Reddit", "Options", "Smart Money"]
        _styled_sig = _sig_df.style.map(_color_signal_cell, subset=_sig_signal_cols)
        st.dataframe(
            _styled_sig,
            use_container_width=True,
            hide_index=True,
            height=38 + 35 * len(_sig_df),
            column_config={
                "Ticker":       st.column_config.TextColumn("Ticker",       width="small"),
                "News":         st.column_config.TextColumn("News",         width="medium"),
                "Reddit":       st.column_config.TextColumn("Reddit",       width="medium"),
                "Options":      st.column_config.TextColumn("Options",      width="small"),
                "Smart Money":  st.column_config.TextColumn("Smart Money",  width="medium"),
            },
        )

    # ── Details expander — per-ticker news cards + MPT card ────────────────────
    with st.expander("Details — per-ticker news & MPT analytics", expanded=False):
        # Per-ticker news detail cards
        _n_news_d = len(st.session_state.news_results)
        if _n_news_d > 0:
            section_header("News Detail")
            for _t, _ce in st.session_state.news_results.items():
                _sl = _ce["result"]["sentiment_label"].upper()
                _ss = _ce["result"]["sentiment_score"]
                _fd = _ce.get("from_db", False)
                with st.expander(
                    f"**{_t}** — {_sl} ({_ss:+.2f})" + (" [cached]" if _fd else ""),
                    expanded=False,
                ):
                    if _fd:
                        st.info(f"Cached from {_ce.get('analyzed_at','')} UTC")
                    _render_analysis(_t, _ce["result"])
        else:
            st.caption("No news analyzed yet — use the 📰 News tab.")

        # MPT card
        _mpt_ss = st.session_state.mpt_analysis
        if _mpt_ss:
            section_header("MPT & Portfolio Analytics")
            _mpt_r  = _mpt_ss.get("result", {})
            _mpt_m  = _mpt_ss.get("metrics", {})
            _mpt_ma = _mpt_r.get("mpt_analysis", {})
            _mpt_sc = _mpt_ma.get("overall_score", "fair")
            _mpt_rb = _mpt_ma.get("rebalancing_priority", "?")
            _pr     = (_mpt_m.get("portfolio_return", 0) or 0) * 100
            _pv     = (_mpt_m.get("portfolio_volatility", 0) or 0) * 100
            _sh     = _mpt_m.get("portfolio_sharpe", 0) or 0
            _msc    = _HEALTH_COLOR.get(_mpt_sc, "#ffd600")
            st.markdown(
                f'<div style="background:{_msc}22;border-left:4px solid {_msc};border-radius:6px;'
                f'padding:8px 14px;margin-bottom:10px">'
                f'<strong style="color:{_msc}">MPT: {_mpt_sc.upper()}</strong>'
                f'<span style="color:#aaa;font-size:0.83rem"> · Rebalancing: {_mpt_rb}</span></div>',
                unsafe_allow_html=True,
            )
            _mm1, _mm2, _mm3 = st.columns(3)
            _mm1.metric("Return", f"{_pr:.1f}%")
            _mm2.metric("Volatility", f"{_pv:.1f}%")
            _mm3.metric("Sharpe", f"{_sh:.2f}")
            _ais = _mpt_r.get("action_items", [])
            if _ais:
                st.caption("Rebalancing actions:")
                for _ai in _ais[:3]:
                    _aic = (
                        "#00c853" if any(w in (_ai.get("action", "")).lower() for w in ("increase", "add", "buy"))
                        else "#ff1744" if any(w in (_ai.get("action", "")).lower() for w in ("reduce", "sell", "trim"))
                        else "#aaa"
                    )
                    st.markdown(
                        f'<span style="color:{_aic};font-weight:700">{_ai.get("ticker","?")}</span>: {_ai.get("action","?")}',
                        unsafe_allow_html=True,
                    )
        else:
            st.caption("Run MPT Analysis via ⚡ Analyze Everything or the ⚡ Options & MPT tab.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: NEWS — Full news analysis
# ═══════════════════════════════════════════════════════════════════════════════
with _tab_news:
    st.subheader("📰 News Analysis")
    st.caption("Per-ticker and sector news analyzed by Gemini Flash · Cached until new articles appear")

    _na_col_a, _na_col_b = st.columns([3, 1])
    with _na_col_a:
        selected_ticker = st.selectbox("Analyze news for", ["— Select a stock —"] + tickers,
                                       key="news_ticker_select")
    with _na_col_b:
        analyze_all = st.button("Analyze All Positions", use_container_width=True, key="news_analyze_all_btn")

    if selected_ticker and selected_ticker != "— Select a stock —":
        if (st.button(f"▶ Run News Analysis for {selected_ticker}", key="news_run_single_btn")
                or selected_ticker in st.session_state.news_results):
            with st.spinner(f"Fetching and analyzing news for {selected_ticker}…"):
                _n_cached = _load_or_analyze(selected_ticker)
            _n_ac   = _n_cached["article_count"]
            _n_sec  = _n_cached["sector"]
            _n_fb   = _n_cached["is_fallback"]
            _n_fdb  = _n_cached.get("from_db", False)
            _n_at   = _n_cached.get("analyzed_at", "")
            if _n_fdb and _n_at:
                st.info(f"Cached analysis from {_n_at} UTC — no new articles detected.")
            else:
                st.success(f"Freshly analyzed at {_n_at} UTC.")
            _n_note = f"{_n_ac} articles found"
            if _n_fb and _n_sec:
                _n_note += f" — supplemented with {_n_sec} sector news"
            st.caption(_n_note)
            _render_analysis(selected_ticker, _n_cached["result"])

    _trigger_all_news = analyze_all or st.session_state.pop("analyze_all_news", False)
    if _trigger_all_news:
        _n_progress = st.progress(0, text="Starting news analysis…")
        _n_done = 0

        def _analyze_ticker(ticker):
            ticker_upper = ticker.upper()
            cached_db = get_latest_analysis(ticker_upper)
            articles = fetch_news(ticker_upper, limit=20)
            if cached_db is not None and not has_new_articles(ticker_upper, articles):
                return ticker_upper, {
                    "result": {
                        "sentiment_score": cached_db["sentiment_score"],
                        "sentiment_label": cached_db["sentiment_label"],
                        "summary": cached_db["summary"],
                        "impact_level": cached_db["impact_level"],
                        "key_themes": cached_db["key_themes"],
                        "is_stock_specific": cached_db["is_stock_specific"],
                    },
                    "article_count": cached_db["article_count"],
                    "sector": cached_db["sector"] or "",
                    "is_fallback": cached_db["is_fallback"],
                    "analyzed_at": cached_db["analyzed_at"],
                    "from_db": True,
                }
            return ticker_upper, _run_analysis_for_ticker(ticker_upper, previous_analysis=cached_db)

        with ThreadPoolExecutor(max_workers=3) as _n_exec:
            _n_futures = {_n_exec.submit(_analyze_ticker, t): t for t in tickers}
            for _n_fut in as_completed(_n_futures):
                _nt, _ne = _n_fut.result()
                st.session_state.news_results[_nt] = _ne
                _n_done += 1
                _n_lbl = "Gemini" if not _ne.get("from_db") else "cache"
                _n_progress.progress(_n_done / len(tickers), text=f"Done ({_n_lbl}): {_nt}")
        _n_progress.empty()

    if st.session_state.news_results:
        st.markdown("### Results")
        for _nt, _nc in st.session_state.news_results.items():
            _nsl  = _nc["result"]["sentiment_label"].upper()
            _nss  = _nc["result"]["sentiment_score"]
            _nfdb = _nc.get("from_db", False)
            _nat  = _nc.get("analyzed_at", "")
            _nct  = " [cached]" if _nfdb else ""
            with st.expander(f"**{_nt}** — {_nsl} ({_nss:+.2f}){_nct}", expanded=False):
                if _nfdb and _nat:
                    st.info(f"Cached from {_nat} UTC — no new articles detected.")
                elif _nat:
                    st.success(f"Freshly analyzed at {_nat} UTC.")
                _nn = f"{_nc['article_count']} articles"
                if _nc["is_fallback"] and _nc["sector"]:
                    _nn += f" · {_nc['sector']} sector fallback"
                st.caption(_nn)
                _render_analysis(_nt, _nc["result"])

        if st.button("Clear All News Results", key="news_clear_btn"):
            st.session_state.news_results = {}
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3: OPTIONS & MPT — Full options analysis + MPT
# ═══════════════════════════════════════════════════════════════════════════════
with _tab_opt:
    with st.expander("📊 Modern Portfolio Theory Analysis", expanded=False):
        _render_mpt_analysis(positions_result)
    st.markdown("---")
    st.subheader("⚡ Options Analysis")
    st.caption("Full option chain with Greeks, Gemini 2.5 Pro analysis of IV, P/C ratios, max pain, and GEX")

# Options functions are defined below at module level and called via _options_analysis_ui(tickers)
# which Streamlit will wire up to the correct tab context via the @st.fragment decorator.

# ═══════════════════════════════════════════════════════════════════════════════
# Options @st.fragment (must be called at module level, outside with-tab block)
# Call happens after TAB definitions to keep all fragments together.
# ═══════════════════════════════════════════════════════════════════════════════

with _tab_ta:
    st.subheader("📈 Technical Analysis")
    st.caption("Candlestick · EMA (9/21/50) · Bollinger Bands · Pattern Detection · Gemini AI")

with _tab_reddit:
    st.subheader("📡 Reddit Sentiment")
    st.caption("WSB and general finance subreddit crowd sentiment, powered by Gemini Flash")

with _tab_smart:
    st.subheader("🏦 Smart Money Analysis")
    st.caption("13F institutional positioning cross-referenced with your portfolio, analyzed by Gemini 2.5 Pro")
    _render_hedge_fund_overlap(positions_result)

with _tab_pulse:
    _render_market_pulse_section()

# Options analysis UI (fragment — renders inside tab_opt context)
with _tab_opt:
    _options_analysis_ui(tickers)

# TA analysis UI (fragment — renders inside tab_ta context)
with _tab_ta:
    _ta_ui(tickers)


@st.fragment
def _reddit_sentiment_ui(tickers: list[str]) -> None:
    if "wsb_results" not in st.session_state:
        st.session_state.wsb_results = {}

    rsent_col_a, rsent_col_b = st.columns([3, 1])
    with rsent_col_a:
        rsent_ticker = st.selectbox(
            "Analyze Reddit sentiment for",
            ["— Select a stock —"] + tickers,
            key="rsent_ticker_select",
        )
    with rsent_col_b:
        rsent_analyze_all = st.button(
            "Analyze All Positions",
            use_container_width=True,
            key="rsent_analyze_all_btn",
        )

    _trigger_all = rsent_analyze_all or st.session_state.pop("analyze_all_reddit", False)
    if _trigger_all:
        rsent_progress = st.progress(0, text="Starting Reddit sentiment analysis…")
        rsent_completed = 0

        def _fetch_wsb(t):
            return t, _load_reddit_sentiment(t)

        with ThreadPoolExecutor(max_workers=3) as rsent_exec:
            rsent_futures = {rsent_exec.submit(_fetch_wsb, t): t for t in tickers}
            for rsent_future in as_completed(rsent_futures):
                t = rsent_futures[rsent_future]
                try:
                    t_key, entry = rsent_future.result()
                except Exception as exc:
                    t_key = t
                    entry = {"summary_row": None, "posts": [], "from_cache": False, "error": str(exc)}
                rsent_completed += 1
                st.session_state.wsb_results[t_key] = entry
                tag = "cache" if entry.get("from_cache") else ("error" if entry.get("error") else "Gemini")
                rsent_progress.progress(rsent_completed / len(tickers), text=f"Done ({tag}): {t_key}")
        rsent_progress.empty()

    if rsent_ticker and rsent_ticker != "— Select a stock —":
        run_col, hint_col = st.columns([2, 8])
        with run_col:
            rsent_run = st.button(
                "▶ Run Reddit Analysis",
                use_container_width=True,
                key="rsent_run_btn",
            )
        with hint_col:
            st.caption(f"Uses Reddit API + Gemini · ~30–90s · Results cached {_WSB_SUMMARY_TTL_HOURS}h")

        if rsent_run:
            st.session_state.wsb_results.pop(rsent_ticker, None)
            with st.spinner(f"Fetching Reddit posts and analyzing sentiment for {rsent_ticker}…"):
                entry = _load_reddit_sentiment(rsent_ticker)
            st.session_state.wsb_results[rsent_ticker] = entry

        if rsent_ticker in st.session_state.wsb_results:
            entry = st.session_state.wsb_results[rsent_ticker]
            if entry.get("from_cache"):
                st.info(f"Serving cached analysis (< {_WSB_SUMMARY_TTL_HOURS}h old). Click ▶ to force refresh.")
            elif not entry.get("error"):
                st.success("Fresh analysis complete.")
            _render_wsb_ticker_result(rsent_ticker, entry)

    if st.session_state.wsb_results:
        st.markdown("### Reddit Sentiment Results")
        for t, entry in st.session_state.wsb_results.items():
            summary_row = entry.get("summary_row")
            error       = entry.get("error")
            if error:
                exp_label = f"**{t}** — ERROR"
            elif summary_row:
                lbl = summary_row.get("sentiment_label", "neutral").upper()
                sc  = summary_row.get("sentiment_score", 0.0)
                exp_label = f"**{t}** — {lbl} ({sc:+.2f})"
            else:
                exp_label = f"**{t}** — NO DATA"
            if entry.get("from_cache"):
                exp_label += " [cached]"

            with st.expander(exp_label, expanded=False):
                _render_wsb_ticker_result(t, entry)

        if st.button("Clear Reddit Results", key="wsb_clear_btn"):
            st.session_state.wsb_results = {}
            st.rerun()


# Reddit sentiment UI — call inside reddit tab (function now defined above)
with _tab_reddit:
    _reddit_sentiment_ui(tickers)
