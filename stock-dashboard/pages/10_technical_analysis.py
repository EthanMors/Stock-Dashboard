import re
import subprocess
import time

import streamlit as st
import pandas as pd
import yfinance as yf
from plotly.subplots import make_subplots
import plotly.graph_objects as go

from components.gemini_usage_bar import render_gemini_usage_bar
from analytics.patterns import DetectedPattern, PatternDetectionEngine
from data.gemini_tracker import record_call

st.set_page_config(page_title="Technical Analysis", page_icon="📈", layout="wide")
render_gemini_usage_bar()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PERIOD_CONFIG: dict[str, tuple[str, str]] = {
    "1D":  ("1d",  "5m"),
    "5D":  ("5d",  "15m"),
    "1M":  ("1mo", "1d"),
    "3M":  ("3mo", "1d"),
    "6M":  ("6mo", "1d"),
    "1Y":  ("1y",  "1d"),
    "2Y":  ("2y",  "1wk"),
    "5Y":  ("5y",  "1wk"),
}

_EMA_PALETTE = [
    "#FF6B6B",  # coral
    "#4ECDC4",  # teal
    "#45B7D1",  # sky blue
    "#FFEAA7",  # yellow
    "#A29BFE",  # lavender
    "#FD79A8",  # pink
    "#55EFC4",  # mint
    "#FDCB6E",  # orange-yellow
]

_UP   = "#26a69a"
_DOWN = "#ef5350"

# Pattern detection UI constants
_PATTERN_LABELS = {
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

_CONFIDENCE_COLORS = {
    "Very High": "#26a69a",
    "High":      "#66bb6a",
    "Moderate":  "#ffa726",
    "Low":       "#ef5350",
    "Very Low":  "#b0bec5",
}

def _confidence_label(score: float) -> str:
    if score >= 0.85: return "Very High"
    if score >= 0.70: return "High"
    if score >= 0.55: return "Moderate"
    if score >= 0.40: return "Low"
    return "Very Low"
_BB_LINE  = "rgba(100, 149, 237, 0.85)"
_BB_FILL  = "rgba(100, 149, 237, 0.07)"
_BB_MID   = "rgba(100, 149, 237, 0.5)"
_GRID     = "#1f2937"
_BG       = "#0e1117"
_PLOT_BG  = "#161b27"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def _fetch_ohlcv(ticker: str, period: str, interval: str) -> pd.DataFrame:
    try:
        return yf.Ticker(ticker.upper()).history(
            period=period, interval=interval, auto_adjust=True
        )
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------

def _calc_ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def _calc_bollinger(
    close: pd.Series, period: int, std_mult: float
) -> tuple[pd.Series, pd.Series, pd.Series]:
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    return sma + std_mult * std, sma, sma - std_mult * std


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def _detect_patterns(ticker: str, period: str, interval: str) -> list:
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


def _bar_at_offset(df_index: pd.Index, start_idx, offset: float):
    """Return the index label at start_idx + offset bars (clamped to df range)."""
    try:
        pos    = df_index.get_loc(start_idx)
        target = max(0, min(int(round(pos + offset)), len(df_index) - 1))
        return df_index[target]
    except Exception:
        return df_index[-1]


def _draw_pattern_geometry(
    fig: go.Figure,
    p,
    x_start,
    x_end,
    base_color: str,
    df: pd.DataFrame,
) -> None:
    """Draw pattern-specific geometric overlays on the price sub-chart (row=1).

    Uses actual diagonal lines for trendline-based patterns (flags, triangles,
    wedges) and scatter markers for peaks, shoulders, and swing points.
    """
    kl      = p.key_levels or {}
    pt      = p.pattern_type
    col     = base_color
    _TL     = "rgba(100,149,237,0.9)"   # cornflower-blue trendlines
    y_lo    = float(df["Low"].min())
    y_hi    = float(df["High"].max())

    def hline(y: float, label: str, *, dash="dot", width=1.5, lcolor=None, alpha=0.85):
        """Horizontal level line spanning the pattern's time window."""
        c = lcolor or col
        fig.add_shape(type="line", x0=x_start, y0=y, x1=x_end, y1=y,
                      line=dict(color=c, width=width, dash=dash), opacity=alpha,
                      row=1, col=1)
        fig.add_annotation(x=x_end, y=y, text=f"  {label} ${y:.2f}",
                           showarrow=False, xanchor="left",
                           font=dict(color=c, size=8),
                           bgcolor="rgba(14,17,23,0.72)", borderpad=2, row=1, col=1)

    def diag(xa, ya: float, xb, yb: float, *, dash="dot", width=1.5, lcolor=None, alpha=0.85):
        """Diagonal line between two (x, y) chart coordinates."""
        c = lcolor or col
        fig.add_shape(type="line", x0=xa, y0=ya, x1=xb, y1=yb,
                      line=dict(color=c, width=width, dash=dash), opacity=alpha,
                      row=1, col=1)

    def vline(x, *, dash="dash", width=1.0, lcolor=None, alpha=0.5):
        """Vertical line spanning the price panel's full height."""
        c = lcolor or col
        fig.add_shape(type="line", x0=x, x1=x, y0=y_lo, y1=y_hi,
                      line=dict(color=c, width=width, dash=dash), opacity=alpha,
                      row=1, col=1)

    def markers(xs, ys, symbol: str, size: int = 10, *, mcolor=None):
        """Scatter marker points plotted on the price sub-chart."""
        c = mcolor or col
        fig.add_trace(go.Scatter(
            x=list(xs), y=list(ys), mode="markers",
            marker=dict(symbol=symbol, size=size, color=c,
                        line=dict(color="white", width=1)),
            showlegend=False, hoverinfo="skip",
        ), row=1, col=1)

    # ── BOS / CHoCH ─────────────────────────────────────────────────────────
    if pt in ("bos_bullish", "choch_bullish", "bos_bearish", "choch_bearish"):
        lvl = kl.get("trigger_level") or kl.get("broken_swing")
        if lvl:
            tag = "CHoCH" if "choch" in pt else "BOS"
            hline(lvl, tag, dash="dashdot", width=2.0)
            vline(x_end, alpha=0.4)
            sym = "triangle-up" if p.direction == "bullish" else "triangle-down"
            markers([x_start], [lvl], sym, size=11)

    # ── S/R Breakout ─────────────────────────────────────────────────────────
    elif pt == "breakout_resistance":
        lvl = kl.get("resistance")
        if lvl: hline(lvl, "Resistance", dash="dot", width=1.5)
    elif pt == "breakout_support":
        lvl = kl.get("support")
        if lvl: hline(lvl, "Support", dash="dot", width=1.5)

    # ── Flag / Pennant — pole line + diagonal channel trendlines ─────────────
    elif pt in ("bull_flag", "bear_flag", "pennant_bull", "pennant_bear"):
        ph        = kl.get("pole_high")
        pl        = kl.get("pole_low")
        fh_end    = kl.get("flag_high")
        fl_end    = kl.get("flag_low")
        fh_start  = kl.get("flag_high_start")
        fl_start  = kl.get("flag_low_start")
        bar_off   = kl.get("flag_bar_offset", 1.0)
        flag_x0   = _bar_at_offset(df.index, x_start, bar_off)

        # Pole: impulse move before the consolidation
        if ph is not None and pl is not None:
            py0, py1 = (pl, ph) if p.direction == "bullish" else (ph, pl)
            diag(x_start, py0, flag_x0, py1, dash="solid", width=1.5, lcolor=col)

        # Channel trendlines (diagonal if start values stored, else horizontal)
        if fh_start is not None and fh_end is not None:
            diag(flag_x0, fh_start, x_end, fh_end, dash="dot", width=1.5, lcolor=_TL)
        elif fh_end is not None:
            hline(fh_end, "Chan. High", dash="dot", width=1.2, lcolor=_TL)

        if fl_start is not None and fl_end is not None:
            diag(flag_x0, fl_start, x_end, fl_end, dash="dot", width=1.5, lcolor=_TL)
        elif fl_end is not None:
            hline(fl_end, "Chan. Low", dash="dot", width=1.2, lcolor=_TL)

    # ── Triangles / Wedges — diagonal trendlines connecting swing extremes ────
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

    # ── Double Top / Bottom — neckline + peak markers ─────────────────────────
    elif pt in ("double_top", "double_bottom"):
        nk      = kl.get("neckline")
        lp      = kl.get("left_peak")
        rp      = kl.get("right_peak")
        p2_off  = kl.get("peak2_bar_offset", 10.0)
        peak2_x = _bar_at_offset(df.index, x_start, p2_off)

        if nk:
            hline(nk, "Neckline", dash="dashdot", width=2.0)
        if lp is not None:
            sym = "triangle-down" if pt == "double_top" else "triangle-up"
            markers([x_start, peak2_x], [lp, rp if rp is not None else lp], sym, size=12)

    # ── Head & Shoulders / Inv H&S — diagonal neckline + three markers ────────
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
            hd_x = _bar_at_offset(df.index, x_start, hd_off)
            rs_x = _bar_at_offset(df.index, x_start, rs_off)
            sym  = "triangle-down" if pt == "head_shoulders" else "triangle-up"
            markers([x_start, hd_x, rs_x], [lp, hd, rp], sym, size=10)

    # ── Cup & Handle — horizontal rim line + cup bottom marker ───────────────
    elif pt == "cup_handle":
        lr        = kl.get("cup_left_rim")
        rr        = kl.get("cup_right_rim")
        cb        = kl.get("cup_bottom")
        bot_off   = kl.get("bottom_bar_offset", 0.0)
        rim_off   = kl.get("right_rim_bar_offset", 0.0)
        if lr is not None and rr is not None:
            hline((lr + rr) / 2.0, "Cup Rim", dash="dashdot", width=2.0)
        if cb is not None:
            bot_x = _bar_at_offset(df.index, x_start, bot_off)
            markers([bot_x], [cb], "circle", size=9)

    # ── Range Consolidation Breakout — horizontal ceiling and floor ───────────
    elif pt in ("range_breakout_bull", "range_breakout_bear"):
        rh = kl.get("range_high")
        rl = kl.get("range_low")
        if rh: hline(rh, "Range High", dash="dot", width=1.5)
        if rl: hline(rl, "Range Low",  dash="dot", width=1.5)


def _add_pattern_overlays(
    fig: go.Figure,
    patterns: list,
    df: pd.DataFrame,
) -> go.Figure:
    """For each detected pattern ≥ 0.55 confidence draw:
    • a shaded region over the pattern's time span
    • pattern-specific geometric lines and markers (diagonal where applicable)
    • a label pin badge at the detection bar
    SL / TP lines drawn for the highest-confidence pattern only.
    """
    _MAX_GEOM  = 4
    geom_drawn = 0
    shown_sl   = shown_tp = False

    for p in patterns:
        if p.confidence_score < 0.55:
            continue

        label  = _PATTERN_LABELS.get(p.pattern_type, p.pattern_type)
        color  = _UP if p.direction == "bullish" else _DOWN
        clabel = _confidence_label(p.confidence_score)

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
            _draw_pattern_geometry(fig, p, x_start, x_end, color, df)
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


# ---------------------------------------------------------------------------
# Chart builder
# ---------------------------------------------------------------------------

def _build_chart(
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
        _UP if c >= o else _DOWN
        for o, c in zip(df["Open"], df["Close"])
    ]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        row_heights=[0.78, 0.22],
    )

    # ── Candlesticks ──────────────────────────────────────────────────────
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            name=ticker.upper(),
            increasing_line_color=_UP,
            decreasing_line_color=_DOWN,
            increasing_fillcolor=_UP,
            decreasing_fillcolor=_DOWN,
            line_width=1,
        ),
        row=1, col=1,
    )

    # ── Bollinger Bands ───────────────────────────────────────────────────
    if show_bb and len(df) >= bb_period:
        upper, mid, lower = _calc_bollinger(df["Close"], bb_period, bb_std)
        bb_label = f"BB({bb_period},{bb_std:.1f})"

        fig.add_trace(go.Scatter(
            x=df.index, y=upper,
            name=f"{bb_label} Upper",
            line=dict(color=_BB_LINE, width=1, dash="dot"),
        ), row=1, col=1)

        fig.add_trace(go.Scatter(
            x=df.index, y=lower,
            name=f"{bb_label} Lower",
            fill="tonexty",
            fillcolor=_BB_FILL,
            line=dict(color=_BB_LINE, width=1, dash="dot"),
        ), row=1, col=1)

        fig.add_trace(go.Scatter(
            x=df.index, y=mid,
            name=f"{bb_label} Mid",
            line=dict(color=_BB_MID, width=1),
        ), row=1, col=1)

    # ── EMAs ──────────────────────────────────────────────────────────────
    if show_emas:
        for i, period in enumerate(sorted(ema_periods)):
            if len(df) >= period:
                fig.add_trace(go.Scatter(
                    x=df.index,
                    y=_calc_ema(df["Close"], period),
                    name=f"EMA {period}",
                    line=dict(color=_EMA_PALETTE[i % len(_EMA_PALETTE)], width=1.5),
                ), row=1, col=1)

    # ── Volume bars ───────────────────────────────────────────────────────
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

    # ── Layout ────────────────────────────────────────────────────────────
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=_BG,
        plot_bgcolor=_PLOT_BG,
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

    fig.update_xaxes(gridcolor=_GRID, showgrid=True, zeroline=False)
    fig.update_yaxes(gridcolor=_GRID, showgrid=True, zeroline=False)
    fig.update_yaxes(side="right", row=1, col=1)
    fig.update_yaxes(side="right", row=2, col=1, title_text="Vol", title_font_size=10)

    if show_patterns and patterns:
        fig = _add_pattern_overlays(fig, patterns, df)

    return fig


# ---------------------------------------------------------------------------
# Sidebar — indicator controls
# ---------------------------------------------------------------------------

def _render_sidebar() -> tuple[bool, bool, int, float, bool]:
    """Returns (show_emas, show_bb, bb_period, bb_std, show_patterns)."""
    with st.sidebar:
        st.header("Indicators")

        # ── EMA ───────────────────────────────────────────────────────────
        show_emas = st.checkbox("Exponential Moving Averages", value=True, key="ta_show_emas")

        if show_emas:
            st.markdown("**Active EMAs**")
            to_remove = []
            for i, p in enumerate(st.session_state.ta_ema_periods):
                color = _EMA_PALETTE[i % len(_EMA_PALETTE)]
                c1, c2 = st.columns([4, 1])
                c1.markdown(
                    f'<span style="color:{color};font-weight:600">━ EMA {p}</span>',
                    unsafe_allow_html=True,
                )
                if c2.button("✕", key=f"ta_rm_ema_{i}", help=f"Remove EMA {p}"):
                    to_remove.append(p)
            for p in to_remove:
                if p in st.session_state.ta_ema_periods:
                    st.session_state.ta_ema_periods.remove(p)
                    st.rerun()

            st.markdown("")
            a1, a2 = st.columns([3, 2])
            new_period = a1.number_input(
                "Add EMA period",
                min_value=2, max_value=500, value=20, step=1,
                key="ta_ema_add_val",
                label_visibility="collapsed",
            )
            if a2.button("＋ Add", key="ta_ema_add_btn", use_container_width=True):
                p = int(new_period)
                if p not in st.session_state.ta_ema_periods:
                    st.session_state.ta_ema_periods.append(p)
                    st.session_state.ta_ema_periods.sort()
                    st.rerun()

        st.markdown("---")

        # ── Bollinger Bands ───────────────────────────────────────────────
        show_bb = st.checkbox("Bollinger Bands", value=True, key="ta_show_bb")
        bb_period, bb_std = 20, 2.0
        if show_bb:
            bb_period = st.number_input(
                "Period", min_value=5, max_value=200, value=20, step=1,
                key="ta_bb_period",
            )
            bb_std = st.number_input(
                "Std Dev multiplier", min_value=0.5, max_value=5.0, value=2.0, step=0.1,
                key="ta_bb_std", format="%.1f",
            )

        st.markdown("---")

        # ── Pattern Detection ──────────────────────────────────────────────
        show_patterns = st.checkbox("Pattern Detection", value=False, key="ta_show_patterns")
        if show_patterns:
            st.caption(
                "Scans for 20+ chart patterns: BOS/CHoCH, flags, triangles, "
                "wedges, H&S, double tops/bottoms, and more. Annotates chart "
                "with detected patterns ≥ 0.55 confidence."
            )

    return show_emas, show_bb, int(bb_period), float(bb_std), show_patterns


# ---------------------------------------------------------------------------
# Gemini AI Analysis
# ---------------------------------------------------------------------------

def _run_gemini_ta(prompt: str) -> str:
    """Call Gemini CLI for technical analysis via stdin (same pattern as wsb_sentiment.py)."""
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


def _bars_since_detected(p: DetectedPattern, df_index: pd.Index) -> int:
    """Return how many bars ago the pattern was detected (0 = current bar)."""
    try:
        pos = df_index.get_loc(p.detected_at_bar)
        return max(0, len(df_index) - 1 - int(pos))
    except Exception:
        return 10


def _recency_weight(bars_since: int) -> int:
    if bars_since <= 3:
        return 3
    if bars_since <= 8:
        return 2
    return 1


def _compute_directional_score(patterns: list, df_index: pd.Index) -> float:
    """Recency-weighted directional score: bullish=positive, bearish=negative."""
    score = 0.0
    for p in patterns:
        bars   = _bars_since_detected(p, df_index)
        weight = _recency_weight(bars)
        sign   = 1 if p.direction == "bullish" else -1
        score += sign * weight * p.confidence_score
    return score


def _build_ta_prompt(
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
                ema_val = float(_calc_ema(df["Close"], p).iloc[-1])
                rel     = "above" if close > ema_val else "below"
                ema_lines.append(f"  EMA({p}): ${ema_val:.2f} — price is {rel}")

    bb_line = ""
    if show_bb and len(df) >= bb_period:
        upper, mid, lower = _calc_bollinger(df["Close"], bb_period, bb_std)
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

    dir_score = _compute_directional_score(patterns, df.index)

    pattern_lines: list[str] = []
    for p in patterns:
        label    = _PATTERN_LABELS.get(p.pattern_type, p.pattern_type)
        bars     = _bars_since_detected(p, df.index)
        kl       = ", ".join(f"{k.replace('_',' ')}: ${v:.2f}" for k, v in (p.key_levels or {}).items())
        sl_str   = f"${p.stop_loss:.2f}" if p.stop_loss else "N/A"
        tp_str   = f"${p.target:.2f}"   if p.target    else "N/A"
        rr_str   = f"{p.risk_reward_ratio:.1f}:1" if p.risk_reward_ratio else "N/A"
        pattern_lines.append(
            f"  - {label} ({p.direction.upper()}, confidence={p.confidence_score:.2f}, "
            f"detected {bars} bars ago)\n"
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

=== DIRECTIONAL SCORE ===
Recency-weighted directional score: {dir_score:+.2f}
(Positive = bullish bias, Negative = bearish bias; patterns weighted by recency:
 last 3 bars weight=3, 4-8 bars weight=2, older weight=1; confidence-scaled)

=== YOUR TASK ===
Provide your analysis in this EXACT format (do not deviate from these section headers):

VERDICT: [Bullish / Bearish / Neutral] — [brief one-line reason]
CONFIDENCE: [High / Moderate / Low]

ANALYSIS:
[Write 3-5 detailed paragraphs explaining WHY the technical picture is bullish/bearish/neutral. Reference specific patterns by name, EMA positions relative to price, Bollinger Band context, and key price levels. Explain what each signal means for near-term price action. Be specific and analytical, not generic.]

KEY LEVELS:
- Support: [specific price levels with brief reason]
- Resistance: [specific price levels with brief reason]
- Stop zones: [specific price levels]

INVALIDATION:
[Describe exactly what price action would invalidate the bullish/bearish thesis. Reference specific levels and conditions that would signal the thesis is wrong.]
"""


def _render_analysis_card(text: str) -> None:
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


def _render_ai_analysis_section(
    patterns: list,
    ticker: str,
    period_label: str,
    df: pd.DataFrame,
    pct: float,
    ema_periods: list[int],
    show_emas: bool,
    show_bb: bool,
    bb_period: int,
    bb_std: float,
) -> None:
    st.markdown("---")
    st.subheader("🤖 AI Analysis")

    patterns_sig = "_".join(sorted(p.pattern_type for p in patterns))
    cache_key    = f"ta_ai_{ticker}_{period_label}_{hash(patterns_sig) & 0xFFFFFF}"

    cached   = st.session_state.get(cache_key)
    is_stale = True
    if cached:
        is_stale = (time.time() - cached["ts"]) > 300

    hdr_col, btn_col = st.columns([5, 1])
    with hdr_col:
        if cached and not is_stale:
            age_s   = int(time.time() - cached["ts"])
            age_str = f"{age_s // 60}m {age_s % 60}s ago" if age_s >= 60 else f"{age_s}s ago"
            st.caption(
                f"Gemini technical analysis · Cached {age_str} · "
                "Click **Run AI Analysis** to refresh"
            )
        else:
            st.caption(
                "Gemini-powered in-depth technical analysis — references all detected "
                "patterns, EMA positions, and Bollinger Band context."
            )
    with btn_col:
        run_btn = st.button(
            "Run AI Analysis",
            key="ta_ai_run_btn",
            use_container_width=True,
            type="primary",
        )

    if run_btn:
        prompt = _build_ta_prompt(
            ticker, period_label, df, patterns,
            ema_periods, show_emas, show_bb, bb_period, bb_std, pct,
        )
        with st.spinner("Gemini is analyzing the technical picture…"):
            raw = _run_gemini_ta(prompt)
        if raw:
            st.session_state[cache_key] = {"ts": time.time(), "text": raw}
            cached   = st.session_state[cache_key]
            is_stale = False
        else:
            st.error("Gemini returned no response. Check the CLI is available and try again.")
            return

    if cached and not is_stale:
        _render_analysis_card(cached["text"])
    elif not run_btn:
        st.info(
            "Click **Run AI Analysis** to generate a Gemini-powered breakdown of "
            "the detected patterns, EMA alignment, and Bollinger Band position."
        )


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def _init_state() -> None:
    st.session_state.setdefault("ta_ema_periods", [9, 21, 50, 200])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _init_state()

    st.title("📈 Technical Analysis")
    st.markdown("##### Candlestick · EMAs · Bollinger Bands · Volume · Powered by yfinance")
    st.markdown("---")

    # ── Controls row ──────────────────────────────────────────────────────
    ctrl1, ctrl2 = st.columns([2, 6])
    with ctrl1:
        ticker = st.text_input(
            "Ticker",
            value=st.session_state.get("active_ticker", "AAPL"),
            placeholder="e.g. AAPL",
            key="ta_ticker_input",
        ).upper().strip()
    with ctrl2:
        period_label = st.radio(
            "Period",
            options=list(_PERIOD_CONFIG.keys()),
            index=5,
            horizontal=True,
            key="ta_period_radio",
            label_visibility="collapsed",
        )

    show_emas, show_bb, bb_period, bb_std, show_patterns = _render_sidebar()

    if not ticker:
        st.info("Enter a ticker symbol above.")
        return

    period, interval = _PERIOD_CONFIG[period_label]

    with st.spinner(f"Loading {ticker} · {period_label}…"):
        df = _fetch_ohlcv(ticker, period, interval)

    if df is None or df.empty:
        st.error(f"No data returned for **{ticker}**. Check the symbol and try again.")
        return

    # ── Price summary strip ───────────────────────────────────────────────
    latest = df.iloc[-1]
    prev   = df.iloc[-2] if len(df) > 1 else latest
    pct    = (latest["Close"] - prev["Close"]) / prev["Close"] * 100
    sign   = "+" if pct >= 0 else ""

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Close",  f"${latest['Close']:.2f}", f"{sign}{pct:.2f}%")
    m2.metric("Open",   f"${latest['Open']:.2f}")
    m3.metric("High",   f"${latest['High']:.2f}")
    m4.metric("Low",    f"${latest['Low']:.2f}")
    m5.metric("Volume", f"{int(latest['Volume']):,}")

    # ── Pattern detection (run before chart so overlays are ready) ────────
    patterns: list = []
    if show_patterns:
        with st.spinner("Running pattern detection…"):
            patterns = _detect_patterns(ticker, period, interval)

    # ── Chart ─────────────────────────────────────────────────────────────
    fig = _build_chart(df, ticker, show_emas, st.session_state.ta_ema_periods,
                       show_bb, bb_period, bb_std,
                       patterns=patterns, show_patterns=show_patterns)
    st.plotly_chart(fig, use_container_width=True)

    # ── Footer ────────────────────────────────────────────────────────────
    st.caption(
        f"{len(df):,} bars · {interval} interval · "
        f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')} · "
        "Data via yfinance · Cached 60s"
    )

    # ── Pattern Detection section ─────────────────────────────────────────
    if show_patterns:
        _render_pattern_section(
            patterns, ticker,
            period_label=period_label,
            df=df,
            pct=pct,
            ema_periods=st.session_state.ta_ema_periods,
            show_emas=show_emas,
            show_bb=show_bb,
            bb_period=bb_period,
            bb_std=bb_std,
        )


def _render_pattern_section(
    patterns: list,
    ticker: str,
    period_label: str = "",
    df: pd.DataFrame | None = None,
    pct: float = 0.0,
    ema_periods: list | None = None,
    show_emas: bool = True,
    show_bb: bool = True,
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> None:
    """Render the Pattern Detection results table below the chart."""
    st.markdown("---")
    st.subheader("🔍 Pattern Detection")

    if not patterns:
        st.info("No patterns detected for the current timeframe. Try a longer period (3M, 6M, 1Y).")
        return

    st.caption(
        f"{len(patterns)} pattern{'s' if len(patterns) != 1 else ''} detected · "
        "Sorted by confidence · Chart annotations show patterns ≥ 0.55"
    )

    rows = []
    for p in patterns:
        label   = _PATTERN_LABELS.get(p.pattern_type, p.pattern_type)
        clabel  = _confidence_label(p.confidence_score)
        color   = _CONFIDENCE_COLORS.get(clabel, "#b0bec5")
        dir_icon = "↑" if p.direction == "bullish" else "↓"
        rr_str  = f"{p.risk_reward_ratio:.1f}:1" if p.risk_reward_ratio else "—"
        entry   = f"${p.entry_price:.2f}"   if p.entry_price  else "—"
        sl_str  = f"${p.stop_loss:.2f}"     if p.stop_loss    else "—"
        tp_str  = f"${p.target:.2f}"        if p.target       else "—"
        vol_str = "✓" if p.volume_confirmed else "—"
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

    # Detail expander for highest-confidence pattern
    if patterns:
        top = patterns[0]
        top_label = _PATTERN_LABELS.get(top.pattern_type, top.pattern_type)
        with st.expander(f"Top pattern details — {top_label} ({top.confidence_score:.2f})", expanded=False):
            cols = st.columns(3)
            cols[0].markdown(f"**Type:** {top_label}")
            cols[0].markdown(f"**Direction:** {top.direction.capitalize()}")
            cols[0].markdown(f"**Bars:** {top.pattern_bars}")
            cols[1].markdown(f"**Entry:** {f'${top.entry_price:.2f}' if top.entry_price else '—'}")
            cols[1].markdown(f"**Stop:** {f'${top.stop_loss:.2f}' if top.stop_loss else '—'}")
            cols[1].markdown(f"**Target:** {f'${top.target:.2f}' if top.target else '—'}")
            cols[2].markdown(f"**R/R:** {f'{top.risk_reward_ratio:.1f}:1' if top.risk_reward_ratio else '—'}")
            cols[2].markdown(f"**Vol confirmed:** {'Yes' if top.volume_confirmed else 'No'}")
            cols[2].markdown(f"**Timeframe:** {top.timeframe}")

            if top.key_levels:
                st.markdown("**Key Levels:**")
                kl_cols = st.columns(min(len(top.key_levels), 4))
                for col_w, (k, v) in zip(kl_cols * 10, top.key_levels.items()):
                    col_w.metric(k.replace("_", " ").title(), f"${v:.2f}")

            if top.component_scores:
                st.markdown("**Component Scores:**")
                sc_data = {k.replace("_", " ").title(): [round(v, 2)] for k, v in top.component_scores.items()}
                st.dataframe(pd.DataFrame(sc_data), hide_index=True, use_container_width=True)

            if top.notes:
                st.info(f"Notes: {top.notes}")

    # ── AI Analysis ───────────────────────────────────────────────────────────
    if df is not None:
        _render_ai_analysis_section(
            patterns=patterns,
            ticker=ticker,
            period_label=period_label,
            df=df,
            pct=pct,
            ema_periods=ema_periods or [],
            show_emas=show_emas,
            show_bb=show_bb,
            bb_period=bb_period,
            bb_std=bb_std,
        )


main()
