# Plan: Near-Eligible Positions + Cash-Based Strategy Capability Panel

## Overview

The "Covered Options Strategy Desk" section (bottom of the Dashboard tab in
`pages/9_portfolio.py`) currently only shows positions with >= 100 shares
(covered-call eligible). This plan adds two purely computational, display-time
enhancements, with no new Gemini calls and no new DB tables:

1. A **"Almost There" near-eligible table** showing positions with 80-99
   shares, how many more shares are needed to reach a full 100-share lot, and
   the estimated cash cost to complete that lot at today's price.
2. A **"What Your Account Can Do Right Now" cash-capability panel** that uses
   the account's available cash (the same source as the "Cash" KPI metric at
   the top of the page) to tell the user, in plain English and with
   ✅/❌ indicators, which option strategies (covered call, cash-secured put,
   collar, wheel) their current cash + positions can support, and which
   near-eligible tickers their cash could "complete" into a full lot.

When fully executed: `data/covered_options_agent.py` gains two new pure
functions (`find_near_eligible_positions`, `assess_strategy_capabilities`) and
one new module constant (`NEAR_ELIGIBLE_MIN_SHARES = 80`). `pages/9_portfolio.py`
imports these, computes the near-eligible list alongside the existing eligible
list, renders the near-eligible table under both the eligible table and the
"no eligible positions" message, and renders the new capability panel at the
bottom of the Covered Options Strategy Desk section, using the existing `_cash`
variable already computed at the top of the page.

## Files Involved

- **`stock-dashboard/data/covered_options_agent.py`** (modify) — add
  `NEAR_ELIGIBLE_MIN_SHARES` constant, `find_near_eligible_positions()`, and
  `assess_strategy_capabilities()`. No changes to any existing function.
- **`stock-dashboard/pages/9_portfolio.py`** (modify) — add the two new names
  to the existing `from data.covered_options_agent import (...)` block; add a
  `_render_near_eligible_section()` helper and calls to it in both branches of
  the existing eligible/not-eligible `if/else`; add the new capability panel
  after the whole eligible/not-eligible block, still inside the Covered
  Options Strategy Desk section and inside `with _tab_dash:`.

No other files are touched. No new pip packages. No env vars. No DB schema
changes (this is purely computed at render time from data already in memory).

## Prerequisites & Dependencies

None. No new packages, no new environment variables, no database migrations.

---

## Step-by-Step Implementation

### Step 1: Add `NEAR_ELIGIBLE_MIN_SHARES` constant to `data/covered_options_agent.py`

**File**: `stock-dashboard/data/covered_options_agent.py`

**Location**: Immediately after the `_LAST_PRICE_FIELD_CANDIDATES` block and
immediately before `def _extract_ticker(position: dict) -> str:`. The exact
current text at that location (lines 60-65) is:

```python
_LAST_PRICE_FIELD_CANDIDATES = [
    "lastPrice", "last_price", "currentPrice", "current_price", "price",
]


def _extract_ticker(position: dict) -> str:
```

**Action**: Replace that block with:

```python
_LAST_PRICE_FIELD_CANDIDATES = [
    "lastPrice", "last_price", "currentPrice", "current_price", "price",
]

# Minimum share count to be shown in the "near-eligible" (Almost There) table.
# Positions with shares in [NEAR_ELIGIBLE_MIN_SHARES, 100) are close to
# covered-call eligibility (100 shares) but not yet there.
NEAR_ELIGIBLE_MIN_SHARES = 80


def _extract_ticker(position: dict) -> str:
```

**Why**: Centralizes the near-eligibility threshold as a named, configurable
module constant rather than a magic number, matching the codebase convention
of `_CAPS_CASE` module-level constants (see `_TARGET_DTES` further down in
this same file).

---

### Step 2: Add `find_near_eligible_positions()` to `data/covered_options_agent.py`

**File**: `stock-dashboard/data/covered_options_agent.py`

**Location**: Immediately after `find_covered_call_eligible_positions()` ends
and before the "Step B" section comment. The exact current text at that
location (lines 134-142) is:

```python
    eligible.sort(key=lambda d: d["market_value"], reverse=True)
    return eligible


# ---------------------------------------------------------------------------
# Step B: Technical snapshot (EMA / RSI / MACD / support-resistance / trend)
# ---------------------------------------------------------------------------

def compute_technical_snapshot(ticker: str) -> dict:
```

**Action**: Replace that block with:

```python
    eligible.sort(key=lambda d: d["market_value"], reverse=True)
    return eligible


def find_near_eligible_positions(positions: list) -> list[dict]:
    """Return one dict per position with NEAR_ELIGIBLE_MIN_SHARES <= shares < 100
    (i.e. close to covered-call eligibility but not there yet).

    Each dict has keys: ticker, shares, shares_needed (100 - shares, float),
    current_price, cost_to_complete (shares_needed * current_price).
    """
    near_eligible: list[dict] = []
    for pos in positions:
        ticker = _extract_ticker(pos)
        if not ticker:
            continue
        fields = _extract_position_fields(pos)
        shares = fields["qty"]
        if shares < NEAR_ELIGIBLE_MIN_SHARES or shares >= 100:
            continue
        shares_needed = 100 - shares
        current_price = fields["last_price"]
        cost_to_complete = shares_needed * current_price if current_price > 0 else 0.0
        near_eligible.append({
            "ticker": ticker,
            "shares": shares,
            "shares_needed": shares_needed,
            "current_price": current_price,
            "cost_to_complete": cost_to_complete,
        })
    near_eligible.sort(key=lambda d: d["cost_to_complete"])
    return near_eligible


# ---------------------------------------------------------------------------
# Step A2: Cash-based strategy capability assessment (pure Python, no Gemini)
# ---------------------------------------------------------------------------

def assess_strategy_capabilities(cash: float, eligible: list, near_eligible: list) -> dict:
    """Assess which option strategies the account can support right now, using
    only the account's available cash and already-computed eligible /
    near-eligible position lists. Pure Python math — makes NO network or
    Gemini/agy calls.

    Parameters
    ----------
    cash          : Available cash balance (float). Callers should pass 0.0
                    if the real balance is unavailable.
    eligible      : Output of find_covered_call_eligible_positions().
    near_eligible : Output of find_near_eligible_positions().

    Returns
    -------
    dict with keys:
        cash                       : float, sanitized (never negative/None)
        can_covered_call           : bool
        covered_call_tickers       : list[str]
        can_cash_secured_put       : bool
        max_affordable_put_strike  : float (floor(cash / 100), in whole dollars)
        can_collar                 : bool (needs both a covered-call lot AND
                                     enough cash for a cash-secured put)
        can_wheel                  : bool (same condition as collar — a full
                                     wheel needs both legs available)
        affordable_lot_completions : list[dict] — near_eligible entries whose
                                     cost_to_complete is <= cash, each with an
                                     added "affordable": True key
        notes                      : list[str] — plain-English strategy notes
    """
    safe_cash = float(cash) if cash and cash > 0 else 0.0

    covered_call_tickers = [p["ticker"] for p in eligible]
    can_covered_call = len(covered_call_tickers) > 0

    max_affordable_put_strike = float(int(safe_cash // 100))
    can_cash_secured_put = max_affordable_put_strike >= 5.0

    affordable_lot_completions = [
        {**p, "affordable": True}
        for p in near_eligible
        if p.get("cost_to_complete", 0.0) > 0 and p["cost_to_complete"] <= safe_cash
    ]

    can_collar = can_covered_call and can_cash_secured_put
    can_wheel = can_covered_call and can_cash_secured_put

    notes: list[str] = []
    if can_covered_call:
        notes.append(
            f"Covered calls: you already hold a full 100-share lot in "
            f"{', '.join(covered_call_tickers)} — you can sell calls against these shares today."
        )
    else:
        notes.append("Covered calls: no position currently has a full 100-share lot.")

    if can_cash_secured_put:
        notes.append(
            f"Cash-secured puts need strike × 100 in cash set aside; with ${safe_cash:,.2f} "
            f"available you could secure up to a ${max_affordable_put_strike:,.0f} strike."
        )
    else:
        notes.append(
            f"Cash-secured puts: with only ${safe_cash:,.2f} available, you can't comfortably "
            "secure even a $5 strike (needs $500+ set aside)."
        )

    if affordable_lot_completions:
        tickers_afford = ", ".join(p["ticker"] for p in affordable_lot_completions)
        notes.append(
            f"Your cash could complete the lot on: {tickers_afford} — buying the remaining "
            "shares would unlock covered calls on that ticker."
        )

    if can_wheel:
        notes.append(
            "Wheel strategy: you have both a full lot for covered calls and enough cash for a "
            "cash-secured put, so running a full wheel (sell puts, get assigned into shares, "
            "sell covered calls, get called away, repeat) is workable right now."
        )
    else:
        notes.append(
            "Wheel strategy: needs both a full 100-share lot AND enough cash for a cash-secured "
            "put — not fully available yet."
        )

    return {
        "cash": safe_cash,
        "can_covered_call": can_covered_call,
        "covered_call_tickers": covered_call_tickers,
        "can_cash_secured_put": can_cash_secured_put,
        "max_affordable_put_strike": max_affordable_put_strike,
        "can_collar": can_collar,
        "can_wheel": can_wheel,
        "affordable_lot_completions": affordable_lot_completions,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Step B: Technical snapshot (EMA / RSI / MACD / support-resistance / trend)
# ---------------------------------------------------------------------------

def compute_technical_snapshot(ticker: str) -> dict:
```

**Why**: `find_near_eligible_positions` mirrors the exact structure of
`find_covered_call_eligible_positions` immediately above it (same helper
functions `_extract_ticker` / `_extract_position_fields`, same "skip if no
ticker" guard), so it's easy to maintain in parallel. `assess_strategy_capabilities`
is intentionally pure Python (no imports of `agy_client`, no `run_agy` calls)
per the task constraint — it only consumes numbers already computed by the two
`find_*` functions and the cash figure passed in by the page.

Also update the module docstring's "Public API" list near the top of the file
(lines 19-23) so it stays accurate:

**Location**: Lines 19-23 currently read:

```python
Public API
----------
find_covered_call_eligible_positions(positions) -> list[dict]
run_covered_options_roundtable(ticker, position, macro_indicators) -> dict
"""
```

**Action**: Replace with:

```python
Public API
----------
find_covered_call_eligible_positions(positions) -> list[dict]
find_near_eligible_positions(positions) -> list[dict]
assess_strategy_capabilities(cash, eligible, near_eligible) -> dict
run_covered_options_roundtable(ticker, position, macro_indicators) -> dict
"""
```

---

### Step 3: Import the two new functions in `pages/9_portfolio.py`

**File**: `stock-dashboard/pages/9_portfolio.py`

**Location**: Lines 57-60, the existing import block:

```python
from data.covered_options_agent import (
    find_covered_call_eligible_positions,
    run_covered_options_roundtable,
)
```

**Action**: Replace with:

```python
from data.covered_options_agent import (
    find_covered_call_eligible_positions,
    find_near_eligible_positions,
    assess_strategy_capabilities,
    run_covered_options_roundtable,
)
```

**Why**: Makes the two new pure functions available to the page without
touching any other import.

---

### Step 4: Compute the near-eligible list and add a small local render helper

**File**: `stock-dashboard/pages/9_portfolio.py`

**Location**: Immediately after the existing line (currently line 4434):

```python
    _eligible_positions = find_covered_call_eligible_positions(positions_result)

    if not _eligible_positions:
```

**Action**: Insert the following between `_eligible_positions = ...` and
`if not _eligible_positions:`, so the block becomes:

```python
    _eligible_positions = find_covered_call_eligible_positions(positions_result)
    _near_eligible_positions = find_near_eligible_positions(positions_result)

    def _render_near_eligible_section() -> None:
        """Render the 'Almost There' near-eligible table. No-op if the list is empty."""
        if not _near_eligible_positions:
            return
        st.markdown("**Almost There — Building Toward a Full Lot**")
        st.caption(
            f"Positions with {NEAR_ELIGIBLE_MIN_SHARES}-99 shares are close to covered-call "
            "eligibility (100+ shares needed). Here's what it would cost to complete the lot "
            "at today's price."
        )
        _near_rows = [
            {
                "Ticker": p["ticker"],
                "Current Shares": f"{p['shares']:.0f}",
                "Shares Needed": f"{p['shares_needed']:.0f}",
                "Current Price": f"${p['current_price']:.2f}",
                "Est. Cost to Complete Lot": f"${p['cost_to_complete']:,.2f}",
            }
            for p in _near_eligible_positions
        ]
        st.dataframe(pd.DataFrame(_near_rows), use_container_width=True, hide_index=True)

    if not _eligible_positions:
```

You must also add the `NEAR_ELIGIBLE_MIN_SHARES` name to the Step 3 import
block above, since it is referenced in the caption text here.

**Location for that additional import change**: same import block edited in
Step 3. After Step 3's edit it reads:

```python
from data.covered_options_agent import (
    find_covered_call_eligible_positions,
    find_near_eligible_positions,
    assess_strategy_capabilities,
    run_covered_options_roundtable,
)
```

**Action**: Change it to also import the constant:

```python
from data.covered_options_agent import (
    NEAR_ELIGIBLE_MIN_SHARES,
    find_covered_call_eligible_positions,
    find_near_eligible_positions,
    assess_strategy_capabilities,
    run_covered_options_roundtable,
)
```

**Why**: `_render_near_eligible_section()` is defined once and called from
both branches of the eligible/not-eligible `if/else` below (Step 5 and Step
6), avoiding code duplication while still satisfying "render it below the
eligible table (or below the no-eligible-positions message)".

---

### Step 5: Call the near-eligible renderer in the "no eligible positions" branch

**File**: `stock-dashboard/pages/9_portfolio.py`

**Location**: The `if not _eligible_positions:` branch. After Step 4's edit,
the surrounding text (previously lines 4436-4438) is:

```python
    if not _eligible_positions:
        st.info("No positions with 100+ shares were found — covered calls require at least one full lot (100 shares).")
    else:
```

**Action**: Replace with:

```python
    if not _eligible_positions:
        st.info("No positions with 100+ shares were found — covered calls require at least one full lot (100 shares).")
        _render_near_eligible_section()
    else:
```

**Why**: Satisfies "if nothing is eligible either, show the near-eligible
table below the info message; if nothing is near-eligible either, show
nothing extra" — `_render_near_eligible_section()` already no-ops when the
list is empty (Step 4).

---

### Step 6: Call the near-eligible renderer below the eligible table

**File**: `stock-dashboard/pages/9_portfolio.py`

**Location**: Inside the `else:` branch, immediately after the eligible
positions `st.dataframe(...)` call. The exact current text (previously lines
4439-4452) is:

```python
    else:
        _elig_rows = [
            {
                "Ticker": p["ticker"],
                "Shares": f"{p['shares']:.0f}",
                "Lots (100sh)": p["lots"],
                "Cost Basis": f"${p['cost_basis']:.2f}",
                "Current Price": f"${p['current_price']:.2f}",
                "Unrealized P/L": f"${p['unrealized_pl']:+,.2f} ({p['unrealized_pl_pct']:+.1f}%)",
            }
            for p in _eligible_positions
        ]
        st.dataframe(pd.DataFrame(_elig_rows), use_container_width=True, hide_index=True)

        _cod_col_a, _cod_col_b, _cod_col_c = st.columns([3, 2, 2])
```

**Action**: Replace with:

```python
    else:
        _elig_rows = [
            {
                "Ticker": p["ticker"],
                "Shares": f"{p['shares']:.0f}",
                "Lots (100sh)": p["lots"],
                "Cost Basis": f"${p['cost_basis']:.2f}",
                "Current Price": f"${p['current_price']:.2f}",
                "Unrealized P/L": f"${p['unrealized_pl']:+,.2f} ({p['unrealized_pl_pct']:+.1f}%)",
            }
            for p in _eligible_positions
        ]
        st.dataframe(pd.DataFrame(_elig_rows), use_container_width=True, hide_index=True)
        _render_near_eligible_section()

        _cod_col_a, _cod_col_b, _cod_col_c = st.columns([3, 2, 2])
```

**Why**: Places the near-eligible table directly below the eligible table,
before the "Run for a ticker" controls, matching the spec's requested
placement.

---

### Step 7: Add the cash-based capability panel at the end of the desk section

**File**: `stock-dashboard/pages/9_portfolio.py`

**Location**: This is added after the entire eligible/not-eligible `if/else`
block finishes (i.e. after all covered-call roundtable UI, results rendering,
and the per-ticker "Refresh analysis" button), and before the Dashboard tab's
`with` block ends / Tab 2 (News) begins. The exact current text at that
boundary (previously lines 4608-4617, unchanged by prior steps since they only
touched earlier lines) is:

```python
                if st.button(f"🔄 Refresh analysis for {_t}", key=f"covered_opt_refresh_{_t}"):
                    with st.status(f"Refreshing covered options roundtable for {_t}…", expanded=True):
                        _run_one_covered_options(_t, _p, force=True)
                    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: NEWS — Full news analysis
# ═══════════════════════════════════════════════════════════════════════════════
with _tab_news:
```

**Action**: Replace with:

```python
                if st.button(f"🔄 Refresh analysis for {_t}", key=f"covered_opt_refresh_{_t}"):
                    with st.status(f"Refreshing covered options roundtable for {_t}…", expanded=True):
                        _run_one_covered_options(_t, _p, force=True)
                    st.rerun()

    # ── What Your Account Can Do Right Now (cash-based capability panel) ──────
    st.markdown("---")
    st.markdown("**What Your Account Can Do Right Now**")
    if _cash is None:
        _cod_cash_available = 0.0
        st.caption("Cash balance unavailable for this account — treating available cash as $0.00 below.")
    else:
        _cod_cash_available = _cash

    _cod_caps = assess_strategy_capabilities(_cod_cash_available, _eligible_positions, _near_eligible_positions)

    _cap_col1, _cap_col2, _cap_col3, _cap_col4, _cap_col5 = st.columns(5)
    with _cap_col1:
        st.metric("Available Cash", f"${_cod_caps['cash']:,.2f}")
    with _cap_col2:
        st.markdown(f"{'✅' if _cod_caps['can_covered_call'] else '❌'}  **Covered Call**")
    with _cap_col3:
        st.markdown(f"{'✅' if _cod_caps['can_cash_secured_put'] else '❌'}  **Cash-Secured Put**")
    with _cap_col4:
        st.markdown(f"{'✅' if _cod_caps['can_collar'] else '❌'}  **Collar**")
    with _cap_col5:
        st.markdown(f"{'✅' if _cod_caps['can_wheel'] else '❌'}  **Wheel**")

    for _cod_note in _cod_caps["notes"]:
        st.caption(f"• {_cod_note}")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: NEWS — Full news analysis
# ═══════════════════════════════════════════════════════════════════════════════
with _tab_news:
```

**Important indentation note**: The new capability-panel block uses 4-space
indentation (matching `st.markdown("---")` and `section_header("Covered
Options Strategy Desk")` at the top of this section, and matching
`_eligible_positions = ...`), i.e. it sits directly inside `with _tab_dash:`
and OUTSIDE the `if not _eligible_positions: / else:` block, so it always
renders exactly once regardless of whether any position is eligible. Do not
indent it to 8 spaces (that would nest it inside the `else:` branch and skip
it whenever there are zero eligible positions).

**Why**: `_cash` is a module-level variable already computed at line ~2690
(`_cash = _to_float(selected_balance.get("total_cash_balance"))`) from the
same `selected_balance` dict used to populate the "Cash" KPI metric card at
the top of the page — reusing it guarantees the capability panel always
matches what the user sees in the KPI header. `_to_float` returns `None` when
the field is missing or unparsable, which is exactly the "balance fetch
failed" signal the task asked to degrade gracefully on; the `if _cash is
None:` branch handles that by falling back to `0.0` and showing a caption.
`_eligible_positions` and `_near_eligible_positions` are both already
computed earlier in this same `with _tab_dash:` block (Step 4) and remain in
scope here since this is flat script-level code, not a function.

---

## Database Changes

None. This feature is entirely computed at render time from data already
fetched into `positions_result` and `selected_balance` / `_cash`. No new
tables, no new columns, no migrations.

## UI/UX Specification

**Near-eligible table** ("Almost There — Building Toward a Full Lot"):
- Rendered via `st.markdown("**...**")` bold header + `st.caption(...)` +
  `st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)`
  — this exactly matches the existing eligible-positions table's rendering
  pattern a few lines above it (`st.dataframe(pd.DataFrame(_elig_rows),
  use_container_width=True, hide_index=True)`).
- Columns: `Ticker`, `Current Shares`, `Shares Needed`, `Current Price`,
  `Est. Cost to Complete Lot`.
- Sorted ascending by `cost_to_complete` (cheapest lot to complete first) —
  set inside `find_near_eligible_positions`.
- Renders nothing (no header, no caption, no table) when the list is empty —
  handled by the early `return` in `_render_near_eligible_section()`.

**Cash capability panel** ("What Your Account Can Do Right Now"):
- `st.markdown("---")` horizontal rule, then a bold `st.markdown("**...**")`
  title — same divider pattern used before "Covered Options Strategy Desk"
  itself (`st.markdown("---")` immediately followed by `section_header(...)`
  at the top of the section).
- One `st.metric` for available cash, formatted `${:,.2f}`.
- Four columns, each showing `✅` or `❌` plus a bold strategy name
  (Covered Call, Cash-Secured Put, Collar, Wheel) via `st.markdown`.
- Notes rendered as a bullet list using `st.caption(f"• {note}")` — matching
  how existing bullet-style notes are rendered elsewhere on this page (e.g.
  around `st.caption(_n_note)` in the News tab).
- If cash is unavailable (`_cash is None`), an `st.caption(...)` warning
  appears above the metric row, and `0.0` is used everywhere downstream so no
  exception is raised.

## Testing Checklist

1. **Launch the app** (`streamlit run dashboard.py` from `stock-dashboard/`
   with the venv active, or use the `launch-dashboard` skill) and navigate to
   the Portfolio page (`pages/9_portfolio.py`), Dashboard tab.
   - Expected: page loads with no traceback; scroll to the bottom to find
     "Covered Options Strategy Desk".

2. **No eligible, no near-eligible positions** (if your test account has no
   position with >= 80 shares of anything): confirm only the "No positions
   with 100+ shares..." info box appears, with no "Almost There" header/table
   below it, followed directly by the "What Your Account Can Do Right Now"
   panel.
   - Failure looks like: an empty `st.dataframe` rendering with zero rows, or
     a stray "Almost There" header with no rows underneath.

3. **Near-eligible only** (temporarily, for testing, you can lower
   `NEAR_ELIGIBLE_MIN_SHARES` or use a test account with 80-99 shares of a
   ticker): confirm the "Almost There — Building Toward a Full Lot" table
   appears with correct `Shares Needed` (100 - shares) and
   `Est. Cost to Complete Lot` (shares_needed × current_price) values, matching
   manual arithmetic against the position's displayed price.
   - Failure looks like: negative shares needed, `$0.00` cost when price is
     nonzero, or a table with a ticker that already has >= 100 shares.

4. **Eligible positions present** (>= 100 shares of at least one ticker):
   confirm the eligible table renders as before (regression check — should be
   byte-identical to pre-change behavior), any near-eligible table renders
   directly below it, and the roundtable buttons/controls still work
   unchanged (run a single roundtable and confirm it still completes).
   - Failure looks like: roundtable buttons missing, duplicated, or erroring
     due to a variable name collision from this change.

5. **Cash capability panel — normal case**: with cash > 0 in the test
   account, confirm the "Available Cash" metric matches the "Cash" KPI metric
   at the top of the page exactly (both derive from `_cash`). Confirm
   ✅/❌ icons are logically consistent: Covered Call is ✅ only if the
   eligible table has rows; Cash-Secured Put is ✅ only if cash / 100 >= 5
   (i.e. cash >= $500); Wheel and Collar are ✅ only if both of the above are
   true.
   - Failure looks like: mismatched cash figures between the KPI card and the
     panel, or a strategy marked ✅ when its precondition isn't met.

6. **Cash capability panel — cash unavailable**: simulate by temporarily
   making `get_balance` return a dict without `total_cash_balance` (or test
   against an account where the API omits it) — confirm the page shows the
   "Cash balance unavailable..." caption and the panel still renders (cash
   shown as `$0.00`) rather than raising an exception.
   - Failure looks like: a `TypeError`/`NoneType` traceback instead of the
     graceful caption + `$0.00` fallback.

7. **Notes text sanity check**: read the plain-English notes rendered at the
   bottom of the capability panel and confirm they read correctly for at
   least two scenarios — (a) some cash + at least one eligible ticker, and
   (b) zero cash + no eligible positions (should say covered calls have no
   full lot, and cash-secured puts can't secure even a $5 strike).

8. **Regression — no Gemini/agy calls added**: open the Gemini usage bar
   at the top of the page before and after interacting with the new sections;
   confirm the daily Flash/Pro call counters do NOT increment merely from
   scrolling/viewing the near-eligible table or the capability panel (they
   should only increment when you explicitly click "Run Roundtable" /
   "Analyze All Eligible" / "Refresh analysis").

## Rollback Plan

If something goes wrong after implementation:

1. In `stock-dashboard/pages/9_portfolio.py`:
   - Revert the import block (Step 3/4) back to:
     ```python
     from data.covered_options_agent import (
         find_covered_call_eligible_positions,
         run_covered_options_roundtable,
     )
     ```
   - Remove the `_near_eligible_positions = ...` line and the
     `_render_near_eligible_section()` function definition (Step 4).
   - Remove the `_render_near_eligible_section()` call added in the
     `if not _eligible_positions:` branch (Step 5).
   - Remove the `_render_near_eligible_section()` call added after the
     eligible table's `st.dataframe(...)` (Step 6).
   - Remove the entire "What Your Account Can Do Right Now" block added in
     Step 7 (everything between the `if st.button(f"🔄 Refresh analysis...")`
     block and the `# TAB 2: NEWS` comment).

2. In `stock-dashboard/data/covered_options_agent.py`:
   - Remove the `NEAR_ELIGIBLE_MIN_SHARES` constant (Step 1).
   - Remove the `find_near_eligible_positions()` function and the
     `assess_strategy_capabilities()` function, restoring the original
     `# Step B` comment to sit directly after
     `find_covered_call_eligible_positions()`'s `return eligible` line
     (Step 2).
   - Revert the module docstring's "Public API" section back to its original
     two-line form (Step 2's docstring edit).

3. Since no database schema or persisted data was touched, no DB rollback or
   data cleanup is required — deleting the code changes above fully reverts
   the feature with no residual state.

4. Run `streamlit run dashboard.py` again and confirm the Portfolio page's
   Dashboard tab loads and the Covered Options Strategy Desk section renders
   exactly as it did before this feature was added.
