# Plan: Portfolio Page — Show All Positions + Technical Analysis Tab

## Overview

This plan makes two changes to `stock-dashboard/pages/9_portfolio.py`:

1. **Fix the Dashboard tab Positions section** to show ALL positions in the main table instead of top-5 only, removing the duplicated expander and the "Top 5 of N" caption.
2. **Add a new "📈 Technical Analysis" tab** between "⚡ Options & MPT" and "📡 Reddit", containing a full candlestick chart with EMAs, Bollinger Bands, pattern detection, and Gemini AI analysis, all scoped to the user's portfolio tickers.

When complete, the Portfolio page will have 7 tabs. All new functions are prefixed `_ta_port_` to avoid collision with the identically-named functions in `10_technical_analysis.py`. All Streamlit session-state keys in the new tab are prefixed `ta_port_`.

---

## Files Involved

| File | Action | What changes |
|------|--------|--------------|
| `stock-dashboard/pages/9_portfolio.py` | **Modify** | Add 4 imports; add TA constants block; add 9 helper functions; change tab list (add new tab variable); add tab body + fragment wiring; fix positions section (lines 2174–2184) |

No new files. No database changes. No new pip packages (all libraries already used in `10_technical_analysis.py` which is already on the same venv).

---

## Prerequisites

None. `plotly`, `yfinance`, `analytics.patterns`, and `data.gemini_tracker` are already installed/importable in the project venv.

---

## Step-by-Step Implementation

---

### Step 1: Add missing imports at the top of `9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Lines 1–16 (the existing import block). Insert the three new import lines immediately after the existing `from scipy.stats import norm` line (currently line 15), so they appear as lines 16–18 in the modified file.

**Action:** Replace this exact block:

```python
from scipy.stats import norm
```

with:

```python
from scipy.stats import norm
from plotly.subplots import make_subplots
import plotly.graph_objects as go
from analytics.patterns import DetectedPattern, PatternDetectionEngine
from data.gemini_tracker import record_call
```

**Why:** The new TA tab functions use `make_subplots`, `go.Figure`, `PatternDetectionEngine`, and `record_call`. These are not currently imported in `9_portfolio.py`. `DetectedPattern` is needed for the type hint in `_ta_port_run_gemini`.

---

### Step 2: Add TA constants block after the existing constants section

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** The existing constants block ends around line 80 (the `_WSB_SUMMARY_TTL_HOURS` line). Insert the new TA constants block immediately after line 80, before the WSB/Reddit DB helpers section that starts with the comment `# WSB / Reddit Sentiment DB helpers`.

**Action:** Insert the following block between `_WSB_SUMMARY_TTL_HOURS = 4` and `# ---------------------------------------------------------------------------`:

```python
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
```

**Why:** All chart-drawing and annotation functions reference these module-level constants by name. Prefixing them `_TA_PORT_` avoids any future import-time collision if the two files are ever loaded in the same process context, and makes it instantly clear which constants belong to which feature.

---

### Step 3: Add the `_ta_port_confidence_label` helper function

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Immediately after the constants block added in Step 2 (after `_TA_PORT_PLOT_BG  = "#161b27"`), still before the `# WSB / Reddit Sentiment DB helpers` comment.

**Action:** Insert:

```python

def _ta_port_confidence_label(score: float) -> str:
    if score >= 0.85: return "Very High"
    if score >= 0.70: return "High"
    if score >= 0.55: return "Moderate"
    if score >= 0.40: return "Low"
    return "Very Low"
```

**Why:** Used in `_ta_port_build_chart` and pattern table rendering to convert a 0–1 float into a human-readable label. Copied verbatim from `10_technical_analysis.py`'s `_confidence_label` function with a `_ta_port_` prefix.

---

### Step 4: Add the OHLCV fetcher and indicator calculation helpers

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Immediately after `_ta_port_confidence_label` (after its closing brace), still in the same block, before `# WSB / Reddit Sentiment DB helpers`.

**Action:** Insert:

```python

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
```

**Why:** These are the data layer for the TA tab. `_ta_port_fetch_ohlcv` is decorated with `@st.cache_data(ttl=60)` so repeated period changes don't re-hit yfinance within the same minute.

---

### Step 5: Add the pattern detection cached function

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Immediately after `_ta_port_calc_bollinger`, before `# WSB / Reddit Sentiment DB helpers`.

**Action:** Insert:

```python

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
```

**Why:** Pattern detection is expensive (scans 20+ pattern types). Caching for 5 minutes prevents it from re-running on every minor widget interaction.

---

### Step 6: Add the pattern geometry overlay helpers

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Immediately after `_ta_port_detect_patterns`, before `# WSB / Reddit Sentiment DB helpers`.

**Action:** Insert the following three functions verbatim (they are copied from `10_technical_analysis.py` lines 134–390 with the prefix `_ta_port_` added to the two functions that are called externally, and internal cross-calls updated):

```python

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
```

**Why:** These are the geometric overlay helpers that draw trendlines, markers, and shaded regions on the chart for detected patterns. They call `_ta_port_bar_at_offset` internally (not the unprefixed version from `10_technical_analysis.py`). The `_ta_port_add_pattern_overlays` function is called by `_ta_port_build_chart`.

---

### Step 7: Add the chart builder function

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Immediately after `_ta_port_add_pattern_overlays`, before `# WSB / Reddit Sentiment DB helpers`.

**Action:** Insert:

```python

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
```

**Why:** This builds the complete candlestick + volume figure. It references all the `_TA_PORT_` constants and the `_ta_port_calc_*` functions added in earlier steps.

---

### Step 8: Add the Gemini analysis helper for the TA tab

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Immediately after `_ta_port_build_chart`, before `# WSB / Reddit Sentiment DB helpers`.

**Action:** Insert:

```python

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
```

**Why:** `_ta_port_run_gemini` follows the exact subprocess + `record_call` pattern used throughout the project (same as `wsb_sentiment.py`, same as `options_agent.py`). `_ta_port_build_prompt` constructs the structured prompt. `_ta_port_render_analysis_card` parses the structured response and renders the verdict badge card.

---

### Step 9: Add the `@st.fragment` function `_ta_ui`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** After `_ta_port_render_analysis_card` (still before `# WSB / Reddit Sentiment DB helpers`). This is the last function in the new TA constants/helpers block.

**Action:** Insert:

```python

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
```

**Why:** This is the complete self-contained UI function for the new tab. Using `@st.fragment` is the established pattern in this file (see `_options_analysis_ui` at line 1924 and `_reddit_sentiment_ui` at line 2398). Scoping the period options to 1M/3M/6M/1Y (omitting 1D/5D/2Y/5Y from the original page) keeps the UI focused for portfolio-level analysis. The default period index is `1` which maps to "3M" as specified.

---

### Step 10: Add the new tab to the tab list

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Line 2096 (after all the new helper functions have been added, this line number will have shifted). Find the line that contains the `st.tabs(` call. It currently reads exactly:

```python
_tab_dash, _tab_news, _tab_opt, _tab_reddit, _tab_smart, _tab_pulse = st.tabs([
    "📊 Dashboard",
    "📰 News",
    "⚡ Options & MPT",
    "📡 Reddit",
    "🏦 Smart Money",
    "🌍 Market Pulse",
])
```

**Action:** Replace that entire block with:

```python
_tab_dash, _tab_news, _tab_opt, _tab_ta, _tab_reddit, _tab_smart, _tab_pulse = st.tabs([
    "📊 Dashboard",
    "📰 News",
    "⚡ Options & MPT",
    "📈 Technical Analysis",
    "📡 Reddit",
    "🏦 Smart Money",
    "🌍 Market Pulse",
])
```

**Why:** Adds `_tab_ta` as the 4th tab variable and "📈 Technical Analysis" as the 4th tab label, inserted between "⚡ Options & MPT" and "📡 Reddit" exactly as specified.

---

### Step 11: Add the tab body for "📈 Technical Analysis"

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** After the `with _tab_opt:` block and before the `# Options @st.fragment` comment that precedes `with _tab_reddit:`. The current code in this region looks like:

```python
# Options @st.fragment (must be called at module level, outside with-tab block)
# Call happens after TAB 4 and TAB 5 definitions to keep all fragments together.
# ═══════════════════════════════════════════════════════════════════════════════

with _tab_reddit:
```

**Action:** Insert the new tab body immediately before `with _tab_reddit:` (in the block that begins with `# Options @st.fragment`). Change the comment block and insert the new `with _tab_ta:` stub so it reads:

```python
# ═══════════════════════════════════════════════════════════════════════════════
# Options @st.fragment (must be called at module level, outside with-tab block)
# Call happens after TAB definitions to keep all fragments together.
# ═══════════════════════════════════════════════════════════════════════════════

with _tab_ta:
    st.subheader("📈 Technical Analysis")
    st.caption("Candlestick · EMA (9/21/50) · Bollinger Bands · Pattern Detection · Gemini AI")

with _tab_reddit:
```

**Why:** Streamlit fragments must be called outside of `with tab:` blocks but still render into the tab they were last associated with. Following the established pattern in this file: add a minimal stub in the `with _tab_ta:` block for the subheader/caption, then call the `@st.fragment` function after all tab body declarations.

---

### Step 12: Wire the `_ta_ui` fragment call after the tab body declarations

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Find the line that currently reads:

```python
# Options analysis UI (fragment — renders inside tab_opt context)
with _tab_opt:
    _options_analysis_ui(tickers)
```

**Action:** Replace that block with:

```python
# Options analysis UI (fragment — renders inside tab_opt context)
with _tab_opt:
    _options_analysis_ui(tickers)

# TA analysis UI (fragment — renders inside tab_ta context)
with _tab_ta:
    _ta_ui(tickers)
```

**Why:** This is the exact wiring pattern used for `_options_analysis_ui` and `_reddit_sentiment_ui`. The `with _tab_ta:` context here sets Streamlit's current render target to the TA tab before calling the fragment, so all widgets inside `_ta_ui` render into the correct tab.

---

### Step 13: Fix the Dashboard tab Positions section — show ALL positions

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Find the Positions block in `_tab_dash`. It currently reads (these lines will be shifted by the additions above, but the content is unique — search for it):

```python
        # Top 5 by market value
        _mv_col_name = next((c for c in df_pos.columns if "market" in c.lower() and "value" in c.lower()), None)
        try:
            _top5_df = df_pos.nlargest(5, _mv_col_name) if _mv_col_name else df_pos.head(5)
        except Exception:
            _top5_df = df_pos.head(5)
        _render_positions_table_styled(_top5_df)
        if _n_pos > 5:
            st.caption(f"Top 5 of {_n_pos} by market value")
        with st.expander(f"📋 Full Positions Table ({_n_pos} positions)", expanded=False):
            _render_positions_table_styled(df_pos)
```

**Action:** Replace that entire block with:

```python
        # All positions
        _render_positions_table_styled(df_pos)
```

**Why:** The task requires showing ALL positions directly without any "top 5" filtering or duplicated expander. The header above this block already shows the position count and market value via `_pos_header`, so no caption is needed.

---

## Database Changes

None. No new tables, no schema files, no migrations.

---

## UI/UX Specification

### New "📈 Technical Analysis" Tab

- **Tab position:** 4th tab (index 3), between "⚡ Options & MPT" and "📡 Reddit"
- **Ticker selectbox:** `st.selectbox("Ticker", tickers, key="ta_port_ticker_select")` — no placeholder, defaults to first ticker in `tickers` list
- **Period radio:** `options=["1M", "3M", "6M", "1Y"]`, `index=1` (default "3M"), `horizontal=True`
- **Indicator checkboxes:** Three side-by-side columns — "EMAs (9, 21, 50)" (default on), "Bollinger Bands (20, 2.0)" (default on), "Pattern Detection" (default off)
- **EMA periods:** Fixed at `[9, 21, 50]` — no add/remove UI (differs from `10_technical_analysis.py` which has a full sidebar; this is intentionally simpler for the portfolio tab)
- **Chart:** 700px height, dark theme, candlestick on row 1, volume bars on row 2, right-side y-axes
- **AI Analysis button:** `key="ta_port_ai_run_btn"`, `type="primary"`, 5-min in-session cache stored at `st.session_state[f"ta_port_ai_{ticker}_{period}"]`
- **Session state keys used:** `ta_port_ticker_select`, `ta_port_period_radio`, `ta_port_show_emas`, `ta_port_show_bb`, `ta_port_show_patterns`, `ta_port_ai_run_btn`, `ta_port_ai_{ticker}_{period}`

### Modified Dashboard Tab — Positions Section

- Removes: `_top5_df`, `nlargest(5)`, `st.caption(f"Top 5 of {_n_pos}…")`, the `st.expander` with the duplicate table
- Keeps: The `st.markdown` header with `_pos_header`, `_render_positions_table_styled(df_pos)` call

---

## Testing Checklist

1. **Page loads without error:** Navigate to Portfolio in the running app. Confirm no Python traceback appears and the page renders the 7-tab navigation bar.

2. **Dashboard tab shows all positions:** Click "📊 Dashboard". In the Positions section, confirm the table shows ALL positions (not capped at 5). If the portfolio has more than 5 positions, verify rows beyond 5 appear. Confirm there is no "Top 5 of N" caption and no duplicate expander below the table.

3. **TA tab appears in correct position:** Confirm the tabs read left-to-right as: Dashboard · News · Options & MPT · Technical Analysis · Reddit · Smart Money · Market Pulse.

4. **TA tab renders chart for default ticker:** Click "📈 Technical Analysis". Confirm the candlestick chart loads for the first portfolio ticker on the 3M period. Confirm EMA and Bollinger Band lines appear on the chart.

5. **Ticker selectbox works:** Change the ticker in the selectbox to a different portfolio position. Confirm the chart re-renders for the new ticker without a full page reload (fragment isolation).

6. **Period radio works:** Select "1M". Confirm the chart re-renders with fewer bars. Select "1Y". Confirm more bars appear. All four options (1M, 3M, 6M, 1Y) should work.

7. **EMA toggle:** Uncheck "EMAs (9, 21, 50)". Confirm the EMA lines disappear from the chart. Re-check. Confirm they return.

8. **Bollinger Bands toggle:** Uncheck "Bollinger Bands (20, 2.0)". Confirm the BB lines and fill disappear. Re-check. Confirm they return.

9. **Pattern detection toggle:** Check "Pattern Detection". Confirm a spinner appears, then pattern overlays appear on the chart (shaded regions + annotations). Confirm the pattern table renders below the chart with columns: Pattern, Dir, Confidence, Level, Entry, Stop, Target, R/R, Vol, Notes.

10. **Gemini AI Analysis button:** Click "Run AI Analysis". Confirm a spinner shows "Gemini is analyzing the technical picture…". After completion, confirm a verdict card renders with a BULLISH/BEARISH/NEUTRAL badge, confidence level, and at least one of the Analysis/Key Levels/Invalidation sections. If Gemini CLI is unavailable, confirm the error message "Gemini returned no response…" is displayed.

11. **AI Analysis caching:** Click "Run AI Analysis" a second time within 5 minutes. Confirm the caption changes to show "Cached Xm Ys ago" above the button and the card re-renders immediately without calling Gemini.

12. **Other tabs unaffected:** Click through "📰 News", "⚡ Options & MPT", "📡 Reddit", "🏦 Smart Money", "🌍 Market Pulse". Confirm all still work exactly as before.

13. **Edge case — empty data:** If a ticker returns no OHLCV data from yfinance (unlikely but possible), confirm the error message "No data returned for **TICKER**" is shown and the page does not crash.

---

## Rollback Plan

If something goes catastrophically wrong after the changes are applied:

1. The only file modified is `stock-dashboard/pages/9_portfolio.py`.
2. Run `git diff stock-dashboard/pages/9_portfolio.py` to see all changes.
3. Run `git checkout stock-dashboard/pages/9_portfolio.py` to revert to the last committed version.
4. Restart the Streamlit app with `streamlit run dashboard.py`.

No database tables were added, so no SQL rollback is needed.
