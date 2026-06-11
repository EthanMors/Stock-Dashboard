# Plan: Portfolio UI Fixes — Hedge Fund Agent, Analyze Everything Button, Strip Raw Fund Listings

## Overview

Three targeted changes to `stock-dashboard/pages/9_portfolio.py` and the `.gemini/agents/` directory:

1. **Create `hedge-fund-analyst.md`** — A Gemini agent spec that teaches the model how to reason about 13F overlap data for a long equity portfolio.
2. **Add "Analyze Everything" button** — A full-width button near the top of the page (after the positions dataframe) that sets three `session_state` flags causing news, options, and hedge fund analyses to all auto-trigger on the same page rerun.
3. **Strip raw fund expanders** — Remove the `st.caption` fund/ticker count and the `for fund in overlapping:` expander loop from `_render_hedge_fund_overlap`; replace with a single `st.info()` count line, then immediately show the Smart Money Analysis section. Wire the HF button to auto-trigger when the `analyze_all_hf` flag is set.

## Files to Create

- `stock-dashboard/.gemini/agents/hedge-fund-analyst.md` — New Gemini agent definition for 13F hedge fund reasoning.

## Files to Modify

- `stock-dashboard/pages/9_portfolio.py` — Three surgical edits:
  - Add "Analyze Everything" button (after line 838, before News Analysis header).
  - Modify news `analyze_all` trigger block (line 882) to also fire on `analyze_all_news` flag.
  - Modify `_options_analysis_ui` fragment's `opt_analyze_all` trigger (line 1170) to also fire on `analyze_all_options` flag.
  - Rewrite the body of `_render_hedge_fund_overlap` (lines 296–394) to remove the caption and for-loop expanders; add `st.info()` count; auto-trigger HF analysis on `analyze_all_hf` flag.

## Files to Delete

None.

## Prerequisites & Dependencies

No new pip packages. No environment variable changes. No database schema changes.

---

## Step-by-Step Implementation

---

### Step 1: Create `stock-dashboard/.gemini/agents/hedge-fund-analyst.md`

**File**: `stock-dashboard/.gemini/agents/hedge-fund-analyst.md`  
**Action**: Create this file with the following exact content. Do not add or remove any lines.

```markdown
---
name: hedge-fund-analyst
description: Analyzes 13F hedge fund overlap data to infer smart-money investment theses and portfolio-level signals for a long equity retail portfolio.
kind: local
tools:
  - read_file
  - grep_search
  - run_shell_command
model: gemini-2.5-pro
max_turns: 15
---

# Hedge Fund Overlap Analyst

You are an expert institutional equity analyst specializing in 13F filing intelligence. Your job is to interpret SEC 13F-HR overlap data — concentrated hedge fund positions, position sizing, put/equity flags, and filing recency — and translate them into a clear, well-reasoned view on what "smart money" positioning implies for each stock in a retail long equity portfolio.

You always receive a structured data payload containing: (1) a list of portfolio tickers owned by the retail investor, and (2) a list of concentrated hedge funds (fewer than 15 holdings) that also own one or more of those tickers, including each fund's position sizes, portfolio weight percentages, AUM tier, and filing date. Analyze all of this data. Never treat any fund's positioning as noise simply because its AUM is smaller.

## Concentrated Fund Signal Strength

A fund with fewer than 15 holdings is operating a highly concentrated, high-conviction strategy. Every position in such a fund is a deliberate, researched bet — not an index arb or diversification filler. When analyzing concentrated funds:

- **< 5 holdings:** Ultra-concentrated. Any shared position with the retail portfolio is an extremely high-conviction signal. These managers have typically done deep fundamental work and are making a large directional bet.
- **5–10 holdings:** Very concentrated. Shared positions carry high conviction weight, especially if the position is > 5% of the fund's AUM.
- **10–15 holdings:** Moderately concentrated. Shared positions are meaningful but require corroboration from position sizing and filing recency.

Never conflate a concentrated fund with a multi-strategy fund or an index tracker. Concentrated 13F filers are the signal; diversified 500-holding funds are noise for this analysis.

## How to Infer Investment Thesis from 13F Data

For each overlapping position, use the following data points to infer the fund's thesis:

### 1. Position Size as % of Fund AUM (`pct_of_portfolio`)
- **> 15%:** Highest-conviction bet in the fund. The manager has staked a large fraction of the book on this name. Treat as extremely bullish (if equity) or extremely bearish (if put).
- **10–15%:** High conviction. Core position.
- **5–10%:** Meaningful position. Not a starter — the manager is committed.
- **< 5%:** Exploratory or watching. Lower confidence signal.

### 2. Put vs. Equity Flag (`put_call` field)
- **Blank / "None" / "Equity":** Standard long equity position. This is a bullish or neutral-constructive signal — the fund expects price appreciation or wants economic exposure.
- **"Put":** The fund purchased put options on this ticker. This is a **bearish signal** or a **hedging signal** — do NOT interpret it as bullish ownership. It means the fund is either:
  - (a) Hedging an existing long position in the same or correlated stock, or
  - (b) Making a speculative directional bet to the downside.
  - Always flag put positions clearly in your analysis. If the fund has both equity and puts in the same ticker, that is a hedged position — less directionally pure.

### 3. Number of Funds Owning the Same Stock (Cross-Fund Conviction)
- **1 fund overlap:** Noteworthy but limited signal. One fund's thesis.
- **2–3 funds overlap:** Strong confirmation. Independent managers reaching the same conclusion is meaningful.
- **4+ funds overlap:** Very high signal. Multiple independent, concentrated managers all owning the same name is rare and indicates strong consensus in the smart-money community.
- Cross-fund conviction is one of the strongest signals you can produce. Always compute it and highlight it prominently when 2+ funds overlap on the same ticker.

### 4. AUM Tier
- **> $1B AUM:** Large institutional. Their position sizes create real market impact. Meaningful signal.
- **$100M–$1B AUM:** Mid-tier institutional. Solid signal.
- **< $100M AUM:** Smaller fund. Still valid — concentrated smaller funds often have better alpha than large ones — but weight them slightly less in cross-fund conviction counts.

### 5. Filing Recency (`filing_date` and `report_period`)
- 13F filings are submitted 45 days after quarter-end. Data can be up to ~135 days stale at maximum.
- **Filed within the last 60 days:** Fresh signal. Fund likely still holds the position.
- **Filed 60–120 days ago:** Moderately fresh. Assume the thesis is likely still active unless there has been a major price dislocation.
- **Filed > 120 days ago:** Stale. Note the staleness explicitly. The fund may have exited.
- Always report the filing date in your output so the user can judge recency themselves.

## Distinguishing Bullish Conviction from Hedging and Speculation

Not all 13F positions are bullish. Apply these rules:

### Bullish Equity Conviction
- Standard equity (non-put) position in a concentrated fund.
- Position is > 5% of fund AUM.
- Multiple funds hold the same name.
- Filing is recent (< 90 days).
- **Infer:** The manager believes the stock is undervalued or has a near-term catalyst.

### Hedging (Not Directional)
- Fund holds both equity AND put positions in the same ticker.
- Put position size is small relative to the equity position (< 30% of equity notional).
- **Infer:** The manager has a long position and is buying tail protection. Bullish on the thesis, cautious on near-term volatility.

### Speculative Put (Bearish)
- Fund holds puts with NO corresponding equity position in the same ticker.
- Put position is large (> 5% of fund AUM).
- **Infer:** This is a directional bearish bet. A concentrated fund with a large put position is expressing a thesis that the stock will decline.

### Index Arbitrage / Pair Trade (Neutral Signal)
- Fund holds many tickers across the same sector with similar weight percentages.
- Both puts and equities appear together across multiple tickers in the same sector.
- **Infer:** This looks like a long/short sector pair trade or index arb, not a single-stock thesis. Flag this pattern explicitly — it reduces the signal strength of the individual ticker overlap.

## Cross-Fund Conviction Scoring

After analyzing each fund independently, compute a cross-fund conviction score for each overlapping portfolio ticker:

```
conviction_score = (number of concentrated funds holding equity) 
                 × (average pct_of_portfolio across those funds)
                 - (number of concentrated funds holding puts) 
                 × (average put pct_of_portfolio)
```

A high positive score (e.g., 3 funds each holding 10%+ equity) = very strong bullish smart-money signal.
A negative score (put-heavy) = net bearish smart-money positioning.
A near-zero score = mixed or hedged positioning.

Label each ticker as one of: **high conviction bullish**, **moderate conviction bullish**, **mixed/hedged**, **moderate conviction bearish**, or **high conviction bearish**.

## What Smart Money Alignment Implies for a Retail Long Equity Holder

After per-ticker analysis, synthesize a portfolio-level conclusion:

- If multiple concentrated funds are long the same names the retail investor holds, and the filing is recent, that is validation of the retail thesis. Call this out as a reinforcing signal.
- If concentrated funds hold puts on a ticker the retail investor is long, that is a warning. The investor may be on the wrong side of institutional conviction. Flag this prominently.
- If no concentrated funds overlap on a given ticker, do NOT invent a signal. State clearly that the ticker has no concentrated fund validation in the current 13F data.
- Portfolio-level stance: after all per-ticker signals, conclude with an overall portfolio signal. If the majority of positions have bullish smart-money overlap, call the portfolio "smart-money aligned." If mixed, call it "mixed." If the retail portfolio is long names that funds are hedging with puts, call it "caution warranted."

## Output Format

Respond ONLY with a single valid JSON object. No markdown fences, no explanation outside the JSON. The JSON must have this exact structure:

```json
{
  "per_ticker": {
    "AAPL": {
      "conviction_level": "high",
      "ownership_type": "bullish_equity",
      "inferred_thesis": "3 concentrated funds hold AAPL at avg 12% of AUM, signaling...",
      "key_signal": "Multi-fund convergence with recent filings (< 60 days)",
      "fund_count": 3,
      "filing_recency": "fresh"
    }
  },
  "portfolio_signal": {
    "overall_stance": "bullish",
    "confidence": "high",
    "cross_ticker_themes": ["AI infrastructure exposure", "rate-sensitive positioning"],
    "summary": "3 of your 5 positions have concentrated fund validation..."
  },
  "flags": [
    "TSLA: Millennium Management holds puts — potential hedging or bearish bet on your long position."
  ]
}
```

### Field Constraints

`per_ticker` keys: uppercase ticker symbols only.  
`conviction_level`: exactly one of `"high"`, `"medium"`, `"low"`.  
`ownership_type`: exactly one of `"bullish_equity"`, `"hedged"`, `"speculative_put"`, `"mixed"`.  
`filing_recency`: exactly one of `"fresh"` (< 60 days), `"moderate"` (60–120 days), `"stale"` (> 120 days).  
`portfolio_signal.overall_stance`: exactly one of `"bullish"`, `"bearish"`, `"mixed"`, `"defensive"`.  
`portfolio_signal.confidence`: exactly one of `"high"`, `"medium"`, `"low"`.  
`flags`: array of plain-text warning strings; empty array `[]` if no flags.

## Methodology Reasoning

- **Model (gemini-2.5-pro):** 13F analysis requires multi-step reasoning over structured tabular data, cross-referencing multiple funds, and synthesizing conflicting put/equity signals. This demands the highest-capability model.
- **Concentrated fund filter:** 13F filings from funds with 500+ holdings are nearly useless for single-stock conviction inference — they reflect index composition, not active bets. Only sub-15-holding funds are passed to this agent.
- **Put flag rule:** Treating all 13F positions as bullish is the most common retail mistake when reading hedge fund data. This agent explicitly distinguishes equity ownership (bullish) from put ownership (bearish or hedging).
- **Cross-fund conviction:** A single fund's thesis could be idiosyncratic. Multiple independent concentrated funds converging on the same name is the strongest possible 13F signal — this agent always computes and surfaces it.
- **Portfolio context:** The retail investor is a long equity holder. The most actionable insight is knowing (a) which of their positions are "smart money validated" and (b) which are being bet against via puts by the same hedge funds.
```

---

### Step 2: Add "Analyze Everything" button to `9_portfolio.py`

**File**: `stock-dashboard/pages/9_portfolio.py`  
**Location**: After line 838 (the `st.dataframe(styled, ...)` call that renders the positions table), and before line 840 (`if not tickers:`).  
**Action**: Insert the following block exactly as written, preserving the surrounding blank lines. The new content goes between the `st.dataframe` call and the `if not tickers:` guard.

Current lines 837–844 (for reference — do NOT modify these lines, only insert between them):
```python
st.dataframe(styled, use_container_width=True, hide_index=True,
             height=_header_height + _row_height * len(df_pos))

if not tickers:
    st.warning("Could not extract ticker symbols from position data — news analysis unavailable.")
    st.stop()
```

**Insert the following block between the `st.dataframe(...)` call and the `if not tickers:` line:**

```python

# ---------------------------------------------------------------------------
# Analyze Everything
# ---------------------------------------------------------------------------
st.markdown("---")
if st.button("⚡ Analyze Everything", use_container_width=True, key="analyze_everything_btn"):
    st.session_state.analyze_all_news = True
    st.session_state.analyze_all_options = True
    st.session_state.analyze_all_hf = True
    st.rerun()

```

The result after insertion should read (lines shown for orientation only):

```python
st.dataframe(styled, use_container_width=True, hide_index=True,
             height=_header_height + _row_height * len(df_pos))

# ---------------------------------------------------------------------------
# Analyze Everything
# ---------------------------------------------------------------------------
st.markdown("---")
if st.button("⚡ Analyze Everything", use_container_width=True, key="analyze_everything_btn"):
    st.session_state.analyze_all_news = True
    st.session_state.analyze_all_options = True
    st.session_state.analyze_all_hf = True
    st.rerun()

if not tickers:
    st.warning("Could not extract ticker symbols from position data — news analysis unavailable.")
    st.stop()
```

---

### Step 3: Modify the news "Analyze All" trigger to also fire on `analyze_all_news` flag

**File**: `stock-dashboard/pages/9_portfolio.py`  
**Location**: The `if analyze_all:` block starting at line 882 (after the "Analyze Everything" insertion the line numbers will shift by ~8; identify the block by its content, not its absolute line number).

**Find this exact block** (search by content):

```python
# Analyze all positions
if analyze_all:
    progress = st.progress(0, text="Starting analysis…")
    completed = 0
```

**Replace it with:**

```python
# Analyze all positions
_trigger_all_news = analyze_all or st.session_state.pop("analyze_all_news", False)
if _trigger_all_news:
    progress = st.progress(0, text="Starting analysis…")
    completed = 0
```

No other lines in this block change. The `with ThreadPoolExecutor` loop and everything inside it remain identical.

---

### Step 4: Modify `_options_analysis_ui` to also fire on `analyze_all_options` flag

**File**: `stock-dashboard/pages/9_portfolio.py`  
**Location**: Inside the `_options_analysis_ui` fragment function. Find the line:

```python
    if opt_analyze_all:
```

This is the only `if opt_analyze_all:` statement in the file (currently near line 1170, may shift after prior insertions). It is inside the `@st.fragment` decorated function `_options_analysis_ui`.

**Find this exact line and the line above it** (for uniqueness):

```python
    with oa_col_b:
        opt_analyze_all = st.button("Analyze All Positions", use_container_width=True, key="opt_analyze_all_btn")

    if opt_analyze_all:
        oa_progress = st.progress(0, text="Starting options analysis…")
```

**Replace with:**

```python
    with oa_col_b:
        opt_analyze_all = st.button("Analyze All Positions", use_container_width=True, key="opt_analyze_all_btn")

    _trigger_all_options = opt_analyze_all or st.session_state.pop("analyze_all_options", False)
    if _trigger_all_options:
        oa_progress = st.progress(0, text="Starting options analysis…")
```

No other lines inside `_options_analysis_ui` change.

---

### Step 5: Rewrite the body of `_render_hedge_fund_overlap` — remove caption and for-loop expanders, add count info, auto-trigger HF analysis

**File**: `stock-dashboard/pages/9_portfolio.py`  
**Location**: Inside the `_render_hedge_fund_overlap` function. The function starts at the `def _render_hedge_fund_overlap(positions: list) -> None:` line.

**Find this exact block** (lines 304–394 in the original file — identify by content, as line numbers shift after prior steps):

```python
    st.caption(
        f"Checking {len(portfolio_tickers)} portfolio ticker(s) against "
        f"{len(overlapping)} concentrated fund(s) with overlapping positions."
    )

    for fund in overlapping:
        overlap_count = fund["overlap_count"]
        fund_name = fund["name"] or fund["cik"]
        header = f"{fund_name} — {overlap_count} shared position{'s' if overlap_count != 1 else ''}"

        with st.expander(header, expanded=False):
            meta_col1, meta_col2, meta_col3 = st.columns(3)
            meta_col1.metric("Report Period", fund["report_period"] or "—")
            meta_col2.metric("Filing Date", fund["filing_date"] or "—")

            total_val = fund["total_value"]
            if total_val >= 1e9:
                val_str = f"${total_val / 1e9:.2f}B"
            elif total_val >= 1e6:
                val_str = f"${total_val / 1e6:.2f}M"
            elif total_val >= 1e3:
                val_str = f"${total_val / 1e3:.2f}K"
            else:
                val_str = f"${total_val:,.0f}"
            meta_col3.metric("Fund Portfolio Value", val_str)

            st.markdown("**Overlapping Holdings**")
            rows = []
            for h in fund["overlapping_holdings"]:
                rows.append({
                    "Ticker": str(h.get("ticker", "")).upper() or "—",
                    "Issuer": str(h.get("issuer", "")) or "—",
                    "% of Fund": f"{h.get('pct_of_portfolio', 0.0):.1f}%",
                    "Value": (
                        f"${h.get('value', 0.0) / 1e6:.2f}M"
                        if h.get("value", 0.0) >= 1e6
                        else f"${h.get('value', 0.0):,.0f}"
                    ),
                    "Type": str(h.get("put_call", "")) or "Equity",
                })
            if rows:
                overlap_df = pd.DataFrame(rows)
                st.dataframe(overlap_df, use_container_width=True, hide_index=True)

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

    if hf_run_clicked:
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
```

**Replace the entire block above with:**

```python
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
```

---

## Verification Checklist

After all steps are executed, perform the following checks in the running Streamlit app (`streamlit run dashboard.py` from `stock-dashboard/`):

### Agent File Check
1. Open `stock-dashboard/.gemini/agents/hedge-fund-analyst.md` in a text editor and confirm:
   - Frontmatter has `name: hedge-fund-analyst`, `model: gemini-2.5-pro`, `max_turns: 15`.
   - The file has the `---` frontmatter delimiters at both the top and after the metadata block.
   - Body starts with `# Hedge Fund Overlap Analyst`.

### "Analyze Everything" Button Check
2. Navigate to the Portfolio page. Confirm a full-width "⚡ Analyze Everything" button appears between the positions dataframe and the `---` separator that precedes the News Analysis header.
3. Confirm the button is NOT inside any column — it spans the full width.
4. Click "⚡ Analyze Everything". Confirm the page reruns. After the rerun, confirm:
   - The news "Analyze All Positions" parallel fetch starts automatically (progress bar appears or results appear).
   - The options "Analyze All Positions" parallel fetch starts automatically within the options fragment.
   - The hedge fund "▶ Run Hedge Fund Intelligence" analysis runs automatically in the Hedge Fund Overlap section.

### News Flag Check
5. After Step 3 is applied, confirm that clicking "Analyze All Positions" (the original news button) still works independently of the "Analyze Everything" button. The original `analyze_all` variable still controls the block — only the trigger condition is widened.

### Options Flag Check
6. After Step 4 is applied, confirm that clicking "Analyze All Positions" inside the options section still works independently.

### Hedge Fund UI Check
7. Navigate to the Portfolio page and scroll to the Hedge Fund Overlap section. Confirm:
   - The `st.caption(f"Checking {len(portfolio_tickers)} portfolio ticker(s)…")` line is GONE.
   - The `for fund in overlapping:` expander loop is GONE.
   - A single `st.info(...)` line appears showing the count, e.g. "Found 4 concentrated funds holding 3 of your positions."
   - Immediately below the info line is the `---` separator and `### Smart Money Analysis` header.
   - The "▶ Run Hedge Fund Intelligence" button is present and functional.
8. Click "▶ Run Hedge Fund Intelligence" and confirm it still produces Gemini analysis output.

### Edge Case: No Overlapping Funds
9. If no concentrated funds overlap, the function still returns early at the existing `if not overlapping:` guard (line 296–303 in the original), which was NOT touched. Confirm the early-return `st.info(...)` message still appears in this case.

### Edge Case: `analyze_all_hf` already consumed
10. After clicking "⚡ Analyze Everything" once, reload the page manually (F5). Confirm the `analyze_all_hf` flag is not present (it was consumed via `pop`) and the hedge fund analysis does NOT auto-run on the fresh reload.

---

## Rollback Plan

If any step produces errors:

### Rollback Step 1 (agent file)
Delete `stock-dashboard/.gemini/agents/hedge-fund-analyst.md`. No other files are affected.

### Rollback Steps 2–5 (page file edits)
Revert `stock-dashboard/pages/9_portfolio.py` using git:
```powershell
cd "C:\Users\ethan\Downloads\Stock Dashboard"
git checkout stock-dashboard/pages/9_portfolio.py
```

This restores the file to the last committed state, undoing all four edits to the page.
