# Plan: Hedge Fund Intelligence Agent

## Overview

This plan adds an AI-powered Hedge Fund Intelligence Agent to the Portfolio page (`9_portfolio.py`). Instead of raw tables, the agent aggregates 13F filing data across all overlapping hedge funds per ticker and calls Gemini 2.5 Pro to infer investment theses, conviction levels, and a portfolio-level smart money signal. Results are cached in `portfolio.db` with a 4-hour TTL to avoid burning Pro quota.

The feature adds one new data module (`data/hedge_fund_agent.py`), extends `data/portfolio_cache.py` with two new cache functions, extends `db/portfolio_schema.sql` with one new table, and adds a new render section below the existing expander UI in `_render_hedge_fund_overlap`.

---

## Files to Create

- `stock-dashboard/data/hedge_fund_agent.py` — Gemini Pro runner, prompt builder, JSON parser, and `run_hedge_fund_analysis()` public function.

## Files to Modify

- `stock-dashboard/db/portfolio_schema.sql` — Add `hedge_fund_analysis` table and index.
- `stock-dashboard/data/portfolio_cache.py` — Add `save_hedge_fund_analysis()` and `get_latest_hedge_fund_analysis()`.
- `stock-dashboard/pages/9_portfolio.py` — Add `_render_hedge_fund_analysis()` helper; update `_render_hedge_fund_overlap()` to add the Smart Money Analysis section below the existing expander loop.

---

## Database Changes

**Table:** `hedge_fund_analysis` in `db/portfolio.db`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Row ID |
| `ticker_key` | `TEXT NOT NULL` | Sorted, comma-joined portfolio tickers — uniquely identifies a portfolio snapshot |
| `result_json` | `TEXT NOT NULL` | Full JSON blob of the analysis result dict |
| `analyzed_at` | `TEXT NOT NULL` | ISO UTC timestamp `"YYYY-MM-DDTHH:MM:SS"` |

**Index:** `idx_hfa_ticker_key` on `(ticker_key, analyzed_at)` — used in the WHERE + ORDER BY lookup.

---

## Prerequisites & Dependencies

No new pip packages required. All imports (`subprocess`, `re`, `json`, `os`, `sqlite3`, `datetime`) are stdlib or already present in the project.

---

## Step-by-Step Implementation

---

### Step 1: Add `hedge_fund_analysis` table to `db/portfolio_schema.sql`

**File:** `stock-dashboard/db/portfolio_schema.sql`

**Location:** Append to the end of the file (after line 60, after the last `CREATE INDEX` statement).

**Action:** Add the following SQL:

```sql
CREATE TABLE IF NOT EXISTS hedge_fund_analysis (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker_key  TEXT NOT NULL,
    result_json TEXT NOT NULL,
    analyzed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hfa_ticker_key
    ON hedge_fund_analysis (ticker_key, analyzed_at);
```

**Why:** The `portfolio_cache.py` module reads this schema file via `init_db()` which calls `conn.executescript(ddl)`. Adding the DDL here ensures the table is created automatically on the next import of `portfolio_cache.py` without any manual migration step.

---

### Step 2: Add cache functions to `data/portfolio_cache.py`

**File:** `stock-dashboard/data/portfolio_cache.py`

**Location:** Insert the two new functions after the `is_options_analysis_fresh` function (currently ending at line 342), and before the `# Initialize DB tables on import` comment block (currently at line 345).

**Action:** Insert the following code block between `is_options_analysis_fresh` and the `# Initialize DB tables on import` section:

```python
# ---------------------------------------------------------------------------
# Hedge Fund Analysis cache helpers
# ---------------------------------------------------------------------------

_HF_TTL_HOURS = 4  # same TTL as options analysis


def _make_ticker_key(portfolio_tickers: list) -> str:
    """Return a stable string key for a set of portfolio tickers."""
    return ",".join(sorted(t.upper() for t in portfolio_tickers if t))


def save_hedge_fund_analysis(portfolio_tickers: list, result_dict: dict) -> None:
    """Persist a hedge fund intelligence analysis result for this portfolio snapshot.

    Parameters
    ----------
    portfolio_tickers : List of ticker strings currently in the portfolio.
    result_dict       : The dict returned by run_hedge_fund_analysis().
    """
    ticker_key = _make_ticker_key(portfolio_tickers)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO hedge_fund_analysis (ticker_key, result_json, analyzed_at)
            VALUES (?, ?, ?)
            """,
            (ticker_key, json.dumps(result_dict), now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_hedge_fund_analysis(portfolio_tickers: list) -> dict | None:
    """Return the most recent hedge fund analysis for this portfolio snapshot, or None.

    Returns None when no row exists or when the most recent row is older than
    _HF_TTL_HOURS hours (4 hours, same TTL as options analysis).

    Parameters
    ----------
    portfolio_tickers : List of ticker strings currently in the portfolio.
    """
    ticker_key = _make_ticker_key(portfolio_tickers)
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT result_json, analyzed_at
            FROM   hedge_fund_analysis
            WHERE  ticker_key = ?
            ORDER  BY analyzed_at DESC
            LIMIT  1
            """,
            (ticker_key,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    analyzed_at_str = row["analyzed_at"]
    # Check TTL — reuse the same logic as is_options_analysis_fresh
    if not is_options_analysis_fresh(analyzed_at_str):
        return None

    try:
        return json.loads(row["result_json"])
    except (json.JSONDecodeError, TypeError):
        return None
```

**Why:** `save_hedge_fund_analysis` and `get_latest_hedge_fund_analysis` follow the exact same pattern as `save_options_analysis` / `get_latest_options_analysis` above them — INSERT rows with a JSON blob and ISO UTC timestamp, read back the most recent row, TTL-check using the existing `is_options_analysis_fresh` helper. The `_make_ticker_key` helper sorts and uppercases tickers before joining, ensuring that `["AAPL","TSLA"]` and `["TSLA","AAPL"]` map to the same cache key.

---

### Step 3: Create `stock-dashboard/data/hedge_fund_agent.py`

**File:** `stock-dashboard/data/hedge_fund_agent.py` (new file — does not exist yet)

**Action:** Create the file with the following exact content:

```python
import json
import re
import subprocess

from data.gemini_tracker import record_call


# ---------------------------------------------------------------------------
# Gemini Pro runner (same pattern as options_agent.py)
# ---------------------------------------------------------------------------

def _run_gemini_pro(prompt: str) -> tuple[str, str]:
    """Call Gemini 2.5 Pro via CLI. Returns (stdout, stderr). Prompt passed via stdin."""
    try:
        result = subprocess.run(
            ["gemini.cmd", "-m", "gemini-2.5-pro", "-p", ""],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )
        output = result.stdout.strip()
        if output:
            record_call("pro")
        return output, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "Timed out after 180s"
    except Exception as exc:
        return "", str(exc)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _format_value(val: float) -> str:
    """Format a dollar value into a human-readable string (B/M/K)."""
    if val >= 1e9:
        return f"${val / 1e9:.2f}B"
    if val >= 1e6:
        return f"${val / 1e6:.2f}M"
    if val >= 1e3:
        return f"${val / 1e3:.2f}K"
    return f"${val:,.0f}"


def _build_prompt(overlapping_funds: list, portfolio_tickers: list) -> str:
    """Build the LLM-optimized prompt text from overlap data.

    Parameters
    ----------
    overlapping_funds : List of fund dicts as returned by _find_overlapping_funds()
                        in 9_portfolio.py. Each dict has keys:
                        name, cik, report_period, filing_date, total_value,
                        overlapping_holdings (list of HoldingRow dicts with keys:
                        ticker, issuer, value, shares, pct_of_portfolio, put_call),
                        overlap_count.
    portfolio_tickers : List of uppercase ticker strings for the portfolio.
    """
    lines = []

    # Section 1 — Portfolio tickers
    lines.append("=== PORTFOLIO TICKERS HELD ===")
    lines.append(", ".join(portfolio_tickers))
    lines.append("")

    # Section 2 — Per-fund breakdown
    lines.append("=== OVERLAPPING FUNDS (concentrated hedge funds also holding your stocks) ===")
    lines.append("")

    for fund in overlapping_funds:
        fund_name = fund.get("name") or fund.get("cik", "Unknown")
        total_val = fund.get("total_value", 0.0)
        # Estimate position count from overlapping holdings list length
        # (total_holdings not available in the overlap dict — use overlapping only)
        holdings = fund.get("overlapping_holdings", [])

        lines.append(
            f"FUND: {fund_name}  |  AUM: {_format_value(total_val)}  "
            f"|  Report: {fund.get('report_period', 'N/A')}  "
            f"|  Filed: {fund.get('filing_date', 'N/A')}"
        )
        lines.append("  Holdings overlapping your portfolio:")
        for h in holdings:
            ticker = str(h.get("ticker", "")).upper()
            pct = h.get("pct_of_portfolio", 0.0)
            val = h.get("value", 0.0)
            shares = h.get("shares", 0.0)
            put_call = str(h.get("put_call", "")).strip().upper()
            position_type = put_call if put_call else "EQUITY"
            shares_str = f"{shares / 1e6:.2f}M" if shares >= 1e6 else f"{shares:,.0f}"
            lines.append(
                f"    - {ticker}: {pct:.1f}% of fund ({_format_value(val)}, {shares_str} shares) [{position_type}]"
            )
        lines.append("")

    # Section 3 — Cross-fund ticker summary (aggregate stats per ticker)
    lines.append("=== CROSS-FUND TICKER SUMMARY ===")

    # Build per-ticker aggregates
    ticker_stats: dict[str, dict] = {}
    for fund in overlapping_funds:
        for h in fund.get("overlapping_holdings", []):
            t = str(h.get("ticker", "")).upper()
            if not t:
                continue
            if t not in ticker_stats:
                ticker_stats[t] = {"fund_count": 0, "pct_sum": 0.0, "put_count": 0}
            ticker_stats[t]["fund_count"] += 1
            ticker_stats[t]["pct_sum"] += h.get("pct_of_portfolio", 0.0)
            put_call = str(h.get("put_call", "")).strip().upper()
            if put_call in ("PUT", "P"):
                ticker_stats[t]["put_count"] += 1

    for ticker in portfolio_tickers:
        stats = ticker_stats.get(ticker)
        if stats is None:
            continue
        fc = stats["fund_count"]
        avg_pct = stats["pct_sum"] / fc if fc > 0 else 0.0
        put_c = stats["put_count"]
        lines.append(
            f"{ticker}: owned by {fc} fund{'s' if fc != 1 else ''}  |  "
            f"avg position size: {avg_pct:.1f}% of fund  |  "
            f"put positions: {put_c} of {fc}"
        )

    lines.append("")

    # Final instruction block
    lines.append("=== YOUR ANALYSIS TASK ===")
    lines.append(
        "You are a hedge fund analyst reviewing 13F SEC filing data. "
        "Based on the fund concentration, position sizes as % of fund AUM, put/call flags, "
        "and cross-fund ticker ownership patterns above, analyze the smart money positioning "
        "across this portfolio."
    )
    lines.append("")
    lines.append(
        "For each ticker owned by at least one fund, infer:"
        "\n  1. The conviction level (high/medium/low) based on position size as % of fund and number of funds"
        "\n  2. The ownership type (bullish_equity / hedged / speculative_put / mixed)"
        "\n  3. The inferred investment thesis — what macro, sector, or company-specific thesis "
        "does this concentration imply? Reference the fund's AUM, filing date, and position weighting."
        "\n  4. The single most important key signal about smart money positioning in this stock"
    )
    lines.append("")
    lines.append(
        "Then synthesize a portfolio-level signal: what does the collective hedge fund positioning "
        "across ALL your stocks suggest about overall market stance, sector rotation, or risk?"
    )
    lines.append("")
    lines.append(
        "Flag any unusual or notable positioning: e.g. a fund with 40%+ in one of your tickers, "
        "put-heavy positioning suggesting a hedge rather than a bullish bet, very recent filing dates "
        "indicating timely positioning, or a single fund owning multiple of your tickers (concentrated overlap)."
    )
    lines.append("")
    lines.append("Rules:")
    lines.append("  - Be specific: reference actual fund names, percentages, and dollar amounts from the data")
    lines.append("  - Distinguish between a long equity position and a put/call option position")
    lines.append("  - Inferred thesis should be 2-3 sentences per ticker")
    lines.append("  - key_signal should be one sentence only")
    lines.append("  - Only include tickers in per_ticker that are actually held by at least one fund above")
    lines.append("  - cross_ticker_themes should be 2-4 thematic strings (brief phrases, not sentences)")
    lines.append("  - flags should be a list of strings, each describing one notable anomaly (or empty list)")
    lines.append("")
    lines.append(
        "Respond ONLY with a single JSON object (no markdown fences, no preamble, no trailing text):"
    )
    lines.append("""{
  "per_ticker": {
    "<TICKER>": {
      "conviction_level": "<high|medium|low>",
      "ownership_type": "<bullish_equity|hedged|speculative_put|mixed>",
      "inferred_thesis": "<2-3 sentence inference>",
      "fund_count": <integer>,
      "key_signal": "<one sentence>"
    }
  },
  "portfolio_signal": {
    "overall_stance": "<bullish|bearish|mixed|defensive>",
    "confidence": "<high|medium|low>",
    "cross_ticker_themes": ["<theme 1>", "<theme 2>"],
    "summary": "<3-4 sentence synthesis>"
  },
  "flags": ["<notable anomaly 1>", "<notable anomaly 2>"]
}""")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON parser
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> dict | None:
    """Extract and parse the JSON object from Gemini's raw stdout."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    # Validate top-level structure
    if "per_ticker" not in data or "portfolio_signal" not in data:
        return None

    # Normalize portfolio_signal fields
    ps = data.get("portfolio_signal", {})
    stance = str(ps.get("overall_stance", "mixed")).lower()
    if stance not in ("bullish", "bearish", "mixed", "defensive"):
        stance = "mixed"
    ps["overall_stance"] = stance

    conf = str(ps.get("confidence", "medium")).lower()
    if conf not in ("high", "medium", "low"):
        conf = "medium"
    ps["confidence"] = conf

    if not isinstance(ps.get("cross_ticker_themes"), list):
        ps["cross_ticker_themes"] = []
    if not isinstance(ps.get("summary"), str):
        ps["summary"] = ""
    data["portfolio_signal"] = ps

    # Normalize per_ticker entries
    for ticker, entry in data.get("per_ticker", {}).items():
        if not isinstance(entry, dict):
            continue
        conv = str(entry.get("conviction_level", "medium")).lower()
        if conv not in ("high", "medium", "low"):
            conv = "medium"
        entry["conviction_level"] = conv

        ot = str(entry.get("ownership_type", "bullish_equity")).lower()
        if ot not in ("bullish_equity", "hedged", "speculative_put", "mixed"):
            ot = "bullish_equity"
        entry["ownership_type"] = ot

        if not isinstance(entry.get("inferred_thesis"), str):
            entry["inferred_thesis"] = ""
        if not isinstance(entry.get("key_signal"), str):
            entry["key_signal"] = ""
        if not isinstance(entry.get("fund_count"), int):
            try:
                entry["fund_count"] = int(entry.get("fund_count", 1))
            except (TypeError, ValueError):
                entry["fund_count"] = 1

    # Normalize flags
    if not isinstance(data.get("flags"), list):
        data["flags"] = []

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_hedge_fund_analysis(overlapping_funds: list, portfolio_tickers: list) -> dict | None:
    """Run Gemini 2.5 Pro analysis on hedge fund overlap data.

    Builds a structured prompt from the overlapping fund/holding data, calls
    Gemini 2.5 Pro via CLI subprocess (stdin delivery), and returns a parsed
    dict. Returns None if Gemini fails or returns unparseable output.

    Parameters
    ----------
    overlapping_funds : List of fund dicts as returned by _find_overlapping_funds()
                        in 9_portfolio.py. Each dict has keys:
                        name, cik, report_period, filing_date, total_value,
                        overlapping_holdings (list of HoldingRow dicts),
                        overlap_count.
    portfolio_tickers : List of uppercase ticker strings for the portfolio.

    Returns
    -------
    dict with keys: per_ticker, portfolio_signal, flags
    Returns {"_error": str} on failure (no None so the caller can check _error).
    Returns None only if Gemini returns empty output.
    """
    if not overlapping_funds or not portfolio_tickers:
        return {"_error": "No overlapping fund data to analyze."}

    prompt = _build_prompt(overlapping_funds, portfolio_tickers)
    raw, stderr = _run_gemini_pro(prompt)

    if not raw:
        return {"_error": stderr or "Gemini returned empty output."}

    result = _parse_response(raw)
    if result is None:
        return {"_error": f"Could not parse Gemini response.\n\nRaw output:\n{raw[:500]}"}

    return result
```

**Why:** This module follows the exact same structure as `options_agent.py` — `_run_gemini_pro`, a prompt builder, `_parse_response`, and a single public `run_*` function. The timeout is 180s (longer than options_agent's 120s) because the prompt is larger. `record_call("pro")` is called inside `_run_gemini_pro` after verifying non-empty output, matching the existing pattern.

---

### Step 4: Add `_render_hedge_fund_analysis` helper to `9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** Insert the new function directly before the existing `_render_hedge_fund_overlap` function definition. The `_render_hedge_fund_overlap` function starts at line 142 with `def _render_hedge_fund_overlap(positions: list) -> None:`. Insert the new function on the line immediately before that definition.

**Action:** Insert the following new function:

```python
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
```

**Why:** This render function mirrors `_render_options_analysis` in structure — banner with gradient/colored border using the same CSS style pattern used throughout the page, per-entity expandable cards, and a summary div. It uses only `st.markdown`, `st.expander`, `st.columns`, `st.caption`, and `st.warning` — all already imported via `streamlit as st`.

---

### Step 5: Add imports to `9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** The existing imports block at the top of the file (lines 1–34). Find this exact block:

```python
from data.portfolio_cache import (
    get_latest_analysis,
    save_analysis,
    has_new_articles,
    get_sentiment_history,
    save_options_analysis,
    get_latest_options_analysis,
    is_options_analysis_fresh,
)
```

**Action:** Replace it with:

```python
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
)
from data.hedge_fund_agent import run_hedge_fund_analysis
```

**Why:** The updated `_render_hedge_fund_overlap` function calls `get_latest_hedge_fund_analysis`, `save_hedge_fund_analysis`, and `run_hedge_fund_analysis`. These must be imported at the top of the page file following the project's existing import pattern.

---

### Step 6: Update `_render_hedge_fund_overlap` in `9_portfolio.py`

**File:** `stock-dashboard/pages/9_portfolio.py`

**Location:** The existing `_render_hedge_fund_overlap` function. It starts at what is currently line 142 with `def _render_hedge_fund_overlap(positions: list) -> None:` and ends at line 211 with `st.dataframe(overlap_df, use_container_width=True, hide_index=True)` inside the per-fund loop. The function currently ends after the `for fund in overlapping:` loop closes.

**Action:** Find the closing of the existing function body. The last lines of the function are currently:

```python
            if rows:
                overlap_df = pd.DataFrame(rows)
                st.dataframe(overlap_df, use_container_width=True, hide_index=True)
```

Immediately after those lines (still inside `_render_hedge_fund_overlap`, after the closing of the `for fund in overlapping:` loop), append the Smart Money Analysis section. The complete updated function body from `def _render_hedge_fund_overlap` onwards must be:

```python
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

**Why:** The entire function is replaced (not just appended) to avoid any diff ambiguity for the implementor. The first part of the function body is identical to the existing code — the raw fund expanders are preserved unchanged. The new Smart Money Analysis section is appended after the `for fund in overlapping:` loop closes. `st.session_state.hf_analysis` follows the same session-state pattern as `options_results` used in the options analysis section. The auto-cache check on page load ensures a fresh analysis from DB is displayed immediately without requiring the user to click the button.

---

## Testing Checklist

1. **Schema migration:** Start the app with `streamlit run dashboard.py` from `stock-dashboard/` with venv activated. Navigate to the Portfolio page. If no error appears about a missing `hedge_fund_analysis` table, the schema migration succeeded (the table is created by `init_db()` on import of `portfolio_cache.py`).

2. **Import check:** If the Portfolio page loads without an `ImportError` or `ModuleNotFoundError`, all imports in Step 5 are correct.

3. **Existing overlap UI unchanged:** Navigate to the Hedge Fund Overlap section. Verify that the per-fund expanders still render with Report Period, Filing Date, Fund Portfolio Value metrics, and the Overlapping Holdings dataframe inside each expander — exactly as before.

4. **Smart Money Analysis section visible:** Below the fund expanders, verify there is a "---" divider, a "### Smart Money Analysis" heading, a caption line, a "▶ Run Hedge Fund Intelligence" button, and a hint caption.

5. **Button click — no overlap data:** If no overlapping funds are found (the `if not overlapping: return` path), verify the Smart Money Analysis section is NOT shown (the function returns early before rendering it).

6. **Button click — cache miss:** With no prior cached analysis, click "▶ Run Hedge Fund Intelligence". Verify a spinner appears with "Gemini 2.5 Pro analyzing hedge fund positioning…", a result appears after completion, and the `from_cache` flag shows `False` (no info banner about cached analysis).

7. **Rendered result structure:** After a successful analysis, verify:
   - A colored banner appears showing "SMART MONEY: [STANCE]" with a Confidence label
   - Theme tags appear as inline styled spans
   - A dark-background summary div appears
   - "**Per-Ticker Smart Money Signals**" heading appears
   - Each ticker owned by at least one fund appears as an expander with conviction level, ownership type, inferred thesis, and key signal
   - If any flags are present, a `st.warning` block appears with the flag list

8. **Button click — cache hit:** Click the button a second time within 4 hours. Verify the info banner "Serving cached analysis (< 4 hours old)…" appears and Gemini is NOT called (no spinner).

9. **Auto-cache on page load:** Reload the Portfolio page within 4 hours of a successful analysis. Verify the analysis renders automatically without clicking the button, with the info banner "Serving cached analysis…".

10. **Gemini error path:** If Gemini is unavailable or returns garbage, verify an `st.error("Gemini error: …")` message appears inside `_render_hedge_fund_analysis` and the page does not crash.

11. **`record_call("pro")` fires:** After a successful Gemini call, check the Gemini usage bar at the top of the page — the Pro call counter should increment by 1.

12. **DB row written:** After a successful analysis, open `stock-dashboard/db/portfolio.db` with any SQLite viewer and confirm a row exists in `hedge_fund_analysis` with a valid `ticker_key` (comma-joined sorted tickers), a non-empty `result_json`, and an `analyzed_at` timestamp.

---

## Rollback Plan

If anything goes catastrophically wrong:

1. **Revert `db/portfolio_schema.sql`:** Remove the `CREATE TABLE IF NOT EXISTS hedge_fund_analysis` and `CREATE INDEX IF NOT EXISTS idx_hfa_ticker_key` blocks appended in Step 1. The existing tables are unaffected (all DDL uses `CREATE TABLE IF NOT EXISTS`).

2. **Delete `data/hedge_fund_agent.py`:** This is a new file with no dependents other than `9_portfolio.py`. Delete it entirely.

3. **Revert `data/portfolio_cache.py`:** Remove the `_HF_TTL_HOURS`, `_make_ticker_key`, `save_hedge_fund_analysis`, and `get_latest_hedge_fund_analysis` additions. The existing `init_db()` call at the bottom and all existing functions are unchanged.

4. **Revert `pages/9_portfolio.py`:**
   - Remove `save_hedge_fund_analysis` and `get_latest_hedge_fund_analysis` from the `from data.portfolio_cache import (...)` block.
   - Remove the `from data.hedge_fund_agent import run_hedge_fund_analysis` line.
   - Remove the `_render_hedge_fund_analysis` function definition.
   - Restore the original `_render_hedge_fund_overlap` body (remove everything after and including `# ── Smart Money Analysis ───────────────────────────────────────────────────────`).

5. **Drop the DB table (optional — safe to leave):** If needed, open `portfolio.db` with SQLite and run `DROP TABLE IF EXISTS hedge_fund_analysis;`. This is optional since the table is empty and causes no harm.
