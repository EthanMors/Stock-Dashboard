# Plan: Portfolio Dashboard Tab Upgrade

## Overview

This plan upgrades the Dashboard tab (`_tab_dash`) inside `stock-dashboard/pages/9_portfolio.py`
to be a visually rich, data-dense portfolio overview. The current Dashboard tab shows an AI
Insights section, a plain styled positions dataframe, and compact summary tables. After this
plan is executed, the Dashboard tab will contain: a donut chart of position weights + a sector
breakdown, a historical portfolio-vs-SPY performance chart with a period selector, a risk stats
strip (beta, volatility, Sharpe, max drawdown, concentration), an enhanced positions table with
sparklines, and the existing compact AI summaries demoted to expanders at the bottom. All other
tabs remain completely untouched.

**Expected outcome:** When the Portfolio page loads and the user clicks the 📊 Dashboard tab,
they see (top to bottom): KPI bar (existing, unchanged) → two-column row with performance chart
(left, ~2/3 width) and allocation donut (right, ~1/3 width) → risk stats strip → enhanced
positions table with sparklines → existing AI Insights section → existing compact summary
expanders. All new heavy computation is wrapped in a `@st.fragment` called
`_dashboard_visuals_ui()` that owns the period selector; changing the period reruns only that
fragment without re-executing the full page.

---

## Files Involved

| File | Action | What changes |
|------|--------|--------------|
| `stock-dashboard/pages/9_portfolio.py` | **Modify** | Add 4 helper functions, 1 cached data function, 1 `@st.fragment` function, and restructure the `_tab_dash` block |
| `stock-dashboard/data/fetcher.py` | **Modify** | Add `get_batch_history()` — batched `yf.download` for multiple tickers + SPY, cached 15 min |

No new files. No new packages. No database changes. No schema changes.

---

## Prerequisites & Dependencies

No new pip packages. All dependencies (`yfinance`, `plotly`, `numpy`, `pandas`, `streamlit`) are
already in `requirements.txt` and installed in the venv.

---

## Critical Constraints (must be respected throughout every step)

1. **No new external APIs and no API-key SDK usage.** yfinance and existing modules only. No new
   AI calls anywhere in this plan.
2. **Do not break existing tabs, session_state keys, or the existing KPI bar logic.** All
   changes are additive inside the Dashboard tab only.
3. **All new helper functions go in `pages/9_portfolio.py`** with `_`-prefixed names following
   the existing convention, except `get_batch_history()` which is a pure data fetcher and goes
   in `data/fetcher.py`.
4. **Every yfinance call wrapped in `try/except` returning empty DataFrame/None** with
   `st.info`/`st.warning` fallbacks so the page never crashes offline.
5. **Webull position field tolerance:** Ticker is already extracted via `_extract_ticker()`.
   Quantity and cost must be extracted tolerantly — the plan specifies exact field names and
   fallback logic (see Step 3).
6. **Windows, Python 3.13, Streamlit.** Follow exact code-style of the file.

---

## Step-by-Step Implementation

### Step 1: Add `get_batch_history()` to `data/fetcher.py`

**File:** `stock-dashboard/data/fetcher.py`

**Location:** After the last function in the file (currently ending at line 63, after the
`get_earnings_history()` function).

**Action:** Append the following new function to the bottom of the file:

```python
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
```

**Why:** The performance chart and sparklines both need multi-ticker daily close data. A single
batched call is dramatically faster than N individual `yf.Ticker(t).history()` calls. The
`@st.cache_data(ttl=900)` matches the existing TTL used in `data/portfolio_cache.py` for
options analysis. The function lives in `fetcher.py` because it is a pure data fetcher with no
Streamlit state (following the architecture rule: pages call `data/` modules for data).

---

### Step 2: Add the import for `get_batch_history` in `pages/9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Find the existing import block at lines 1-52. There is already an import group for
fetcher-related things. The existing imports do NOT import from `data.fetcher` at all. Add a new
import line immediately after line 52 (after `from data.portfolio_insights_agent import
run_portfolio_insights`), inserting it as a new line:

**Find this exact line (line 52):**
```python
from data.portfolio_insights_agent import run_portfolio_insights
```

**Replace it with:**
```python
from data.portfolio_insights_agent import run_portfolio_insights
from data.fetcher import get_batch_history
```

**Why:** The new helper functions inside `9_portfolio.py` will call `get_batch_history()`. The
import must be added at the top of the file with the other imports.

---

### Step 3: Add `_extract_position_qty_cost()` helper function

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Find the existing `_extract_ticker()` function (currently at line 1212):

```python
def _extract_ticker(position: dict) -> str:
    for field in _TICKER_FIELD_CANDIDATES:
        val = position.get(field, "")
        if val and isinstance(val, str):
            return val.upper().strip()
    return ""
```

**Action:** Insert the following new function IMMEDIATELY AFTER `_extract_ticker()` (after its
closing `return ""` line), before `_get_portfolio_tickers()`:

```python
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
```

**Why:** The enhanced positions table and allocation donut need quantity, cost, and market value
from each position dict. The Webull OpenAPI can return different field names depending on the
SDK version and account type. This function tolerantly tries all known candidate names, matching
the pattern already used in `data/mpt_agent.py` (`_extract_market_value`) and `9_portfolio.py`
(`_extract_ticker`).

---

### Step 4: Add `_fetch_sector()` cached helper function

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Find the `@st.cache_data(ttl=60)` block that defines `_ta_port_fetch_ohlcv()`
(currently around line 151):

```python
@st.cache_data(ttl=60)
def _ta_port_fetch_ohlcv(ticker: str, period: str, interval: str) -> pd.DataFrame:
```

**Action:** Insert the following new cached function BEFORE `_ta_port_fetch_ohlcv()`, separated
by a blank line:

```python
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
```

**Why:** The sector-allocation donut chart needs sector labels for each holding. Using
`@st.cache_data(ttl=86400)` avoids hitting yfinance on every rerun. Graceful fallback to
"Unknown" means the chart always renders even if a ticker has no sector data (e.g., ETFs).

---

### Step 5: Add `_build_allocation_charts()` pure builder function

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Find the `_render_positions_table_styled()` function (currently around line 2451):

```python
def _render_positions_table_styled(df: pd.DataFrame) -> None:
```

**Action:** Insert the following new function BEFORE `_render_positions_table_styled()`,
separated by a blank line:

```python
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
        textinfo="label+percent",
        textfont=dict(size=11),
        marker=dict(
            colors=_DONUT_COLORS[:len(labels_w)],
            line=dict(color="#0e1117", width=2),
        ),
        hovertemplate="<b>%{label}</b><br>Weight: %{value:.1f}%<extra></extra>",
    ))
    fig_weights.update_layout(
        **_plotly_portfolio_layout(
            title=dict(text="Position Weights", font=dict(size=13)),
            height=300,
            margin=dict(l=16, r=16, t=48, b=16),
            showlegend=False,
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
        textinfo="label+percent",
        textfont=dict(size=11),
        marker=dict(
            colors=_DONUT_COLORS[:len(labels_s)],
            line=dict(color="#0e1117", width=2),
        ),
        hovertemplate="<b>%{label}</b><br>Allocation: %{value:.1f}%<extra></extra>",
    ))
    fig_sectors.update_layout(
        **_plotly_portfolio_layout(
            title=dict(text="Sector Allocation", font=dict(size=13)),
            height=300,
            margin=dict(l=16, r=16, t=48, b=16),
            showlegend=False,
        )
    )

    return fig_weights, fig_sectors
```

**Why:** Pure builder function with no Streamlit calls — it only builds figures. This separation
makes it easy to cache and test. `_plotly_portfolio_layout()` is defined in Step 6 below and
must be inserted before this function is called at runtime (Step 6 inserts it before Step 5 in
execution order; placement in the file is also before _render functions).

---

### Step 6: Add `_plotly_portfolio_layout()` theme helper

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Find the `_build_allocation_charts()` function you just inserted in Step 5.
Insert the following helper function BEFORE `_build_allocation_charts()` (i.e., between
`_render_positions_table_styled` and `_build_allocation_charts`):

```python
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
```

**Why:** Avoids importing `plotly_dark_layout` from `components/ui` inside the module-level
code (which would create a circular dependency concern if ui.py ever imports from pages) and
reuses the page's existing color constants (`_TA_PORT_BG`, `_TA_PORT_PLOT_BG`, `_TA_PORT_GRID`)
that are already defined at the top of the file (lines 139–141). The pattern mirrors how
`components/ui.plotly_dark_layout` works.

---

### Step 7: Add `_build_performance_chart()` function

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Insert AFTER `_build_allocation_charts()` (after its closing `return
fig_weights, fig_sectors` line), before `_render_positions_table_styled()`.

```python
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
        port_value = port_value + close_df[t].fillna(method="ffill") * qty

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
    title_str = f"Portfolio Performance — {final_ret:+.1f}% (current holdings, {period} basis)"

    fig.update_layout(
        **_plotly_portfolio_layout(
            title=dict(text=title_str, font=dict(size=13)),
            height=360,
            xaxis_title=None,
            yaxis_title="Return (%)",
            yaxis_ticksuffix="%",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            hovermode="x unified",
        )
    )

    return fig
```

**Why:** Reconstructs a portfolio equity curve from current holdings weighted by quantity, the
only data available from the Webull OpenAPI (no transaction history). Using `get_batch_history()`
from `data/fetcher.py` means a single cached HTTP call covers both the performance chart and the
sparklines (both use the same `(tickers + SPY, period)` cache key). The zero-reference line
gives immediate visual context for drawdowns.

---

### Step 8: Add `_compute_risk_stats()` function

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Insert AFTER `_build_performance_chart()` (after its closing `return fig` line),
before `_render_positions_table_styled()`.

```python
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
            port_value = port_value + close_df[t].fillna(method="ffill") * (qty_proxy / close_df[t].iloc[0] if close_df[t].iloc[0] > 0 else 0)

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
```

**Why:** Separated from the chart-building function so it can be called once and its results
used for both the metric strip display and potential future AI Insights augmentation. Uses
`_RISK_FREE_RATE = 0.045` already defined at line 2537 of `9_portfolio.py` (not redefined here;
uses the module-level constant). Uses `np` (numpy) which is already imported at line 12.

---

### Step 9: Add `_build_enhanced_positions_df()` function

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Insert AFTER `_compute_risk_stats()` (after its final `return dict(...)` line),
before `_render_positions_table_styled()`.

```python
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
```

**Why:** Builds a structured DataFrame for the new `st.dataframe` call with
`st.column_config.LineChartColumn` sparklines. Separates data preparation from rendering,
matching the existing `_render_positions_table_styled()` pattern. The sparkline list uses
close prices from the same `close_df` already fetched for the performance chart (no extra HTTP
calls). Tolerant extraction for every field so the table always renders even with incomplete
position data.

---

### Step 10: Add the main `@st.fragment` for Dashboard visuals

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Find the `_options_analysis_ui()` function definition (currently around line 2762):

```python
@st.fragment
def _options_analysis_ui(tickers: list[str]) -> None:
```

**Action:** Insert the following new fragment function BEFORE `_options_analysis_ui()`, separated
by a blank line. (This keeps all fragment definitions together.)

```python
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
            st.plotly_chart(perf_fig, use_container_width=True)
            st.caption(
                "Assumes current holdings held throughout the entire period. "
                "No transaction history is available from the Webull SDK. "
                "Portfolio return normalized to first trading day of the selected window. "
                "SPY shown for reference."
            )
        else:
            st.info("Price history unavailable. Check your internet connection.")

    with _donut_col:
        fig_weights, fig_sectors = _build_allocation_charts(positions)
        st.plotly_chart(fig_weights, use_container_width=True)
        st.plotly_chart(fig_sectors, use_container_width=True)

    st.markdown("---")

    # ── Row 2: Risk stats strip ────────────────────────────────────────────
    if close_df_perf is not None and not close_df_perf.empty:
        risk = _compute_risk_stats(positions, perf_period_label, close_df_perf)
        _r1, _r2, _r3, _r4, _r5 = st.columns(5)
        _r1.metric(
            "Portfolio Beta",
            f"{risk['beta']:.2f}" if risk["beta"] is not None else "N/A",
            help="Weighted beta vs SPY over selected period",
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
        _r4.metric(
            "Max Drawdown",
            f"{risk['max_drawdown']:.1f}%" if risk["max_drawdown"] is not None else "N/A",
            help="Maximum peak-to-trough decline over selected period",
        )
        if risk["top_conc"] is not None:
            _r5.metric(
                "Top Position",
                f"{risk['top_conc']:.1f}%",
                delta=risk["top_ticker"],
                delta_color="off",
                help="Largest single-position weight by market value",
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
        st.markdown(
            f"#### Holdings &nbsp;<span style='color:#aaa;font-size:0.85rem'>"
            f"{_n_pos} position{'s' if _n_pos != 1 else ''} · sorted by market value</span>",
            unsafe_allow_html=True,
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
```

**Why:** `@st.fragment` scopes the period selector so clicking "1M"/"3M"/"6M"/"1Y" only reruns
this function, not the entire page (which would trigger re-fetching Webull data, re-running AI
agents, etc.). The `tickers` list is passed in by the caller so there is no circular dependency
with the module-level `tickers` variable. Both close_df calls reuse the same `@st.cache_data`
key via `get_batch_history()`.

---

### Step 11: Restructure the `_tab_dash` block

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Find the entire `with _tab_dash:` block, which currently starts at line 2946 and
ends at line 3113 (the closing of the Smart Money section under `_right`). The block looks like:

```python
with _tab_dash:

    # ── Action buttons row ────────────────────────────────────────────────────
    _bc1, _bc2, _bc3 = st.columns([2, 2, 4])
    with _bc1:
        if st.button("⚡ Analyze Everything", use_container_width=True, key="analyze_everything_btn"):
```

...and ends with:

```python
        else:
            st.info("Run Hedge Fund Analysis in the 🏦 Smart Money tab.")
```

**Action:** Replace the ENTIRE content of the `with _tab_dash:` block (everything from the
line after `with _tab_dash:` down to and including the final `st.info(...)` line) with the
following. Do NOT change the `with _tab_dash:` line itself.

```python

    # ── Action buttons row ────────────────────────────────────────────────────
    _bc1, _bc2, _bc3 = st.columns([2, 2, 4])
    with _bc1:
        if st.button("⚡ Analyze Everything", use_container_width=True, key="analyze_everything_btn"):
            st.session_state.analyze_all_news    = True
            st.session_state.analyze_all_options = True
            st.session_state.analyze_all_hf      = True
            st.session_state.analyze_all_mpt     = True
            st.session_state.analyze_all_reddit  = True
            st.rerun()
    with _bc2:
        _run_insights = st.button("🤖 Run AI Insights", use_container_width=True, key="ai_insights_btn")
    with _bc3:
        st.caption(
            "⚡ **Analyze Everything** runs all 5 agents in parallel · "
            "🤖 **AI Insights** synthesizes all results with Gemini 2.5 Pro"
        )

    # ── NEW: Allocation charts + Performance chart + Risk strip + Enhanced table ──
    _dashboard_visuals_ui(positions_result, tickers)

    st.markdown("---")

    # ── AI Insights section ───────────────────────────────────────────────────
    st.markdown("### 🤖 AI Insights")

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
        st.info(
            "Click **🤖 Run AI Insights** to get Gemini 2.5 Pro's holistic analysis combining "
            "positions, news, options flow, Reddit sentiment, hedge fund 13F data, and MPT metrics. "
            "For best results, run **⚡ Analyze Everything** first."
        )

    st.markdown("---")

    # ── Compact Signal Summaries (demoted to expanders) ───────────────────────
    with st.expander("📰 News Sentiment Summary", expanded=False):
        _n_news = len(st.session_state.news_results)
        _npos = sum(1 for e in st.session_state.news_results.values()
                    if e.get("result", {}).get("sentiment_label") == "positive")
        _nneg = sum(1 for e in st.session_state.news_results.values()
                    if e.get("result", {}).get("sentiment_label") == "negative")
        st.caption(f"{_n_news} analyzed · {_npos} positive · {_nneg} negative")
        _render_news_compact_summary()
        if _n_news > 0:
            for _t, _ce in st.session_state.news_results.items():
                _sl = _ce["result"]["sentiment_label"].upper()
                _ss = _ce["result"]["sentiment_score"]
                _fd = _ce.get("from_db", False)
                with st.expander(f"**{_t}** — {_sl} ({_ss:+.2f})" + (" [cached]" if _fd else ""), expanded=False):
                    if _fd:
                        st.info(f"Cached from {_ce.get('analyzed_at','')} UTC")
                    _render_analysis(_t, _ce["result"])

    with st.expander("⚡ Options Flow Summary", expanded=False):
        _n_opt = len([e for e in st.session_state.options_results.values() if "_error" not in e])
        st.caption(f"{_n_opt} analyzed")
        _render_options_compact_summary()

    with st.expander("📡 Reddit Sentiment Summary", expanded=False):
        _nwsb = len(st.session_state.wsb_results)
        _wpos = sum(1 for e in st.session_state.wsb_results.values() if (e.get("summary_row") or {}).get("sentiment_label") == "positive")
        _wneg = sum(1 for e in st.session_state.wsb_results.values() if (e.get("summary_row") or {}).get("sentiment_label") == "negative")
        st.caption(f"{_nwsb} analyzed · {_wpos} positive · {_wneg} negative")
        _render_reddit_compact_summary()

    with st.expander("📊 MPT & Portfolio Analytics", expanded=False):
        _mpt_ss = st.session_state.mpt_analysis
        if _mpt_ss:
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
                st.caption("**Rebalancing:**")
                for _ai in _ais[:3]:
                    _aic = "#00c853" if any(w in (_ai.get("action","")).lower() for w in ("increase","add","buy")) else "#ff1744" if any(w in (_ai.get("action","")).lower() for w in ("reduce","sell","trim")) else "#aaa"
                    st.markdown(f'<span style="color:{_aic};font-weight:700">{_ai.get("ticker","?")}</span>: {_ai.get("action","?")}', unsafe_allow_html=True)
        else:
            st.info("Run MPT Analysis via ⚡ Analyze Everything or the ⚡ Options & MPT tab.")

    with st.expander("🏦 Smart Money Summary", expanded=False):
        _hf_ss = st.session_state.hf_analysis
        if _hf_ss and "_error" not in _hf_ss:
            _hfps  = _hf_ss.get("portfolio_signal", {})
            _hfst  = _hfps.get("overall_stance", "mixed").upper()
            _hfc   = _hfps.get("confidence", "?")
            _hfcol = {"BULLISH": "#00c853", "BEARISH": "#ff1744", "MIXED": "#ffd600", "DEFENSIVE": "#ff6d00"}.get(_hfst, "#ffd600")
            _hfth  = _hfps.get("cross_ticker_themes", [])
            st.markdown(
                f'<div style="background:{_hfcol}22;border-left:4px solid {_hfcol};border-radius:6px;'
                f'padding:8px 14px;margin-bottom:6px">'
                f'<strong style="color:{_hfcol}">13F: {_hfst}</strong>'
                f'<span style="color:#aaa;font-size:0.83rem"> · Conf: {_hfc}</span></div>',
                unsafe_allow_html=True,
            )
            if _hfth:
                st.caption(" · ".join(_hfth[:3]))
        else:
            st.info("Run Hedge Fund Analysis in the 🏦 Smart Money tab.")
```

**Why:** This restructures the tab so new visuals (allocation, performance, risk, enhanced table)
appear first before AI Insights, and the existing compact summary tables are demoted to collapsed
expanders. The action button row and AI Insights section remain identical to the current code —
zero logic changes there. The `_dashboard_visuals_ui(positions_result, tickers)` call is placed
directly after the action buttons row, at the top of the visual content, so it renders before
the AI section.

---

## Exact Insertion Order Summary (to resolve any ordering ambiguity)

In `9_portfolio.py`, the new functions/definitions must appear in the file in this order (each
listed next to the existing landmark they are placed relative to):

1. `get_batch_history` import — added to import block (Step 2)
2. `_fetch_sector()` — inserted BEFORE `_ta_port_fetch_ohlcv` (Step 4)
3. `_extract_position_qty_cost()` + field-candidate lists — inserted AFTER `_extract_ticker()` (Step 3)
4. `_plotly_portfolio_layout()` — inserted BEFORE `_build_allocation_charts()` (Step 6)
5. `_build_allocation_charts()` — inserted BEFORE `_render_positions_table_styled()` (Step 5)
6. `_build_performance_chart()` — inserted AFTER `_build_allocation_charts()` (Step 7)
7. `_compute_risk_stats()` — inserted AFTER `_build_performance_chart()` (Step 8)
8. `_build_enhanced_positions_df()` — inserted AFTER `_compute_risk_stats()` (Step 9)
9. `_PERF_PERIOD_MAP` dict + `_dashboard_visuals_ui()` fragment — inserted BEFORE `_options_analysis_ui()` (Step 10)
10. `_tab_dash` block restructured (Step 11)

Steps 3–9 all go in the "Helper functions" section of the page (between the constants block and
the render functions block), which is the correct location per the page organization pattern in
`planner-skills.md`. Steps 4 and 2 go in the cached-data-functions section and imports section
respectively.

---

## Database Changes

None. This plan requires no SQLite schema changes.

---

## UI/UX Specification

### Period selector (inside `_dashboard_visuals_ui`)
- Widget type: `st.radio`
- Label: `"Performance Period"`
- Options: `["1M", "3M", "6M", "1Y"]`
- Default index: `3` (1Y)
- Key: `"dash_perf_period"`
- Layout: placed in left column of a `st.columns([3, 5])` split, no label beside it

### Performance chart
- Two traces: "Portfolio" (blue `#4f8ef7`, solid, width 2.5) and "SPY" (amber `#ffd600`,
  dashed, width 1.5)
- Height: 360px
- Y-axis: "Return (%)", tick suffix "%"
- Zero reference line: dotted, color `#556080`
- Legend: horizontal, anchored bottom-left above the chart
- Caption below chart: explains current-holdings-held-throughout assumption

### Allocation donuts
- Right column `[2, 1]` split — donuts take right 1/3
- Two stacked donuts: "Position Weights" (groups <2% into Other) and "Sector Allocation"
- Height: 300px each
- Hole: 0.55 (medium donut ring)
- Dark background `#0e1117`, no legend (text labels on slices)
- Color palette: `_DONUT_COLORS` list (15 colors, cycling)

### Risk stats strip
- 5 `st.metric` widgets in a single `st.columns(5)` row
- Labels: "Portfolio Beta", "Ann. Volatility", "Sharpe Ratio", "Max Drawdown", "Top Position"
- Formats: beta 2dp, vol 1dp%, sharpe 2dp, drawdown 1dp% (negative), concentration 1dp% with
  ticker as delta label

### Enhanced positions table
- `st.dataframe` with `hide_index=True`
- 11 columns (or 10 if no sparkline data): Symbol, Qty, Avg Cost, Last Price, Market Value,
  Weight %, Day P&L $, Day P&L %, Total P&L $, Total P&L %, Sparkline (30D)
- Sparkline column uses `st.column_config.LineChartColumn("30D")` — only included when data is
  available
- Sorted by Market Value descending
- Dynamic height: `38 + 35 * len(df)` rows

### Compact summaries (existing content, now in expanders)
- 5 collapsed `st.expander` blocks at the bottom of the Dashboard tab:
  "📰 News Sentiment Summary", "⚡ Options Flow Summary", "📡 Reddit Sentiment Summary",
  "📊 MPT & Portfolio Analytics", "🏦 Smart Money Summary"
- All `expanded=False` by default

---

## Testing Checklist

### 1. Syntax check (required before manual testing)
Run the following from the `stock-dashboard/` directory with the venv activated:
```
python -m py_compile pages/9_portfolio.py
python -m py_compile data/fetcher.py
```
Expected: no output (no errors). If there is a SyntaxError, fix it before proceeding.

### 2. Page loads without error
- Action: Run `streamlit run dashboard.py`, navigate to the Portfolio page.
- Expected: Page loads, KPI bar renders, no red Streamlit error boxes.
- Failure: Any exception traceback visible in the browser means a step was applied incorrectly.

### 3. Dashboard tab renders new visuals
- Action: Click the 📊 Dashboard tab.
- Expected: Period selector (1M/3M/6M/1Y radio buttons) appears. A spinner "Loading price data…"
  runs. After spinner completes, two columns appear: left column shows a line chart with
  "Portfolio" and "SPY" traces; right column shows two donut charts labeled "Position Weights"
  and "Sector Allocation".
- Failure: If the period selector is missing, Step 10 was not applied. If the chart is blank with
  "No position data available", Step 3 field extraction failed (check Webull position dict keys
  with `st.json(positions_result[0])` temporarily added to debug).

### 4. Risk stats strip appears
- Action: On the Dashboard tab, scroll below the chart/donut row.
- Expected: 5 metric widgets appear: Portfolio Beta, Ann. Volatility, Sharpe Ratio, Max Drawdown,
  Top Position. Values may be N/A if data is sparse but the metrics must render without error.
- Failure: Missing strip means `_compute_risk_stats()` threw an exception — check the function's
  `except Exception: return _EMPTY` is in place.

### 5. Enhanced positions table renders
- Action: Scroll below the risk strip.
- Expected: A bold section header "Holdings · N positions · sorted by market value" followed by
  a `st.dataframe` with Symbol, Qty, Avg Cost, Last Price, Market Value, Weight %, Day P&L $,
  Day P&L %, Total P&L $, Total P&L %, and optionally a "30D" sparkline column.
- Failure: If only "Could not build enhanced positions table." info message appears, Step 9's
  `_build_enhanced_positions_df()` returned empty — verify `_extract_position_qty_cost()` is
  extracting values from the actual Webull field names by temporarily printing
  `positions_result[0].keys()`.

### 6. Period selector reruns only the fragment
- Action: With the Dashboard tab showing the chart, click "1M" on the period radio.
- Expected: Only the fragment content (chart, donuts, risk strip, table) reruns — the KPI bar
  and button row at the top do NOT flash/reload. The chart changes to show 1-month data.
- Failure: Entire page reloading (KPI bar spinner visible) means `@st.fragment` is not working —
  verify Step 10 places `@st.fragment` decorator correctly on `_dashboard_visuals_ui`.

### 7. Compact summaries moved to expanders
- Action: Scroll to the bottom of the Dashboard tab.
- Expected: 5 collapsed expander widgets visible: 📰 News Sentiment Summary, ⚡ Options Flow
  Summary, 📡 Reddit Sentiment Summary, 📊 MPT & Portfolio Analytics, 🏦 Smart Money Summary.
  Each expander opens to show the same content as the previous layout.
- Failure: If old 2-column layout still visible (Positions + News left, MPT + Options + Reddit +
  Smart Money right), Step 11 was not applied or applied to the wrong code block.

### 8. Other tabs are unaffected
- Action: Click 📰 News, ⚡ Options & MPT, 📈 Technical Analysis, 📡 Reddit, 🏦 Smart Money,
  🌍 Market Pulse tabs in turn.
- Expected: All tabs function identically to before — no regressions, no missing UI elements.
- Failure: Any broken tab means the `with _tab_dash:` block replacement in Step 11 accidentally
  consumed code from another tab block. Verify the replacement ends exactly before the
  `# TAB 2: NEWS` comment block.

### 9. Offline / no-data resilience
- Action: Temporarily break the Webull connection (e.g., clear the env vars temporarily) or
  test when markets are closed and yfinance returns limited data.
- Expected: Dashboard tab shows `st.info` / `st.warning` messages gracefully instead of crashing.
  Specifically: "Price history unavailable. Check your internet connection." and empty-figure
  messages in place of charts.
- Failure: Red Streamlit traceback = a try/except is missing somewhere.

### 10. Sector donut shows "Unknown" for ETFs/untickered positions
- Action: If the portfolio contains an ETF (e.g., SPY, QQQ), check the sector donut.
- Expected: The ETF appears in the "Unknown" sector slice without causing an error.
- Failure: Any exception from `_fetch_sector()` = the try/except in Step 4 is not in place.

---

## Rollback Plan

If something goes catastrophically wrong:

1. Revert `stock-dashboard/pages/9_portfolio.py` to the git HEAD version:
   ```
   git checkout HEAD -- stock-dashboard/pages/9_portfolio.py
   ```

2. Revert `stock-dashboard/data/fetcher.py`:
   ```
   git checkout HEAD -- stock-dashboard/data/fetcher.py
   ```

3. Restart Streamlit:
   ```
   streamlit run dashboard.py
   ```

No database changes were made, so no SQL rollback is needed.
