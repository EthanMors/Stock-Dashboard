# Webull Positions Page — No-Env Account Selection Plan

## 1. Task Summary

The positions page (`pages/7_positions.py`) currently dead-ends with `st.stop()` when `WEBULL_ACCOUNT_ID` is not set in `.env` — it shows a flat account table and tells the user to copy-paste an ID and restart. This plan modifies the data layer and the UI page so that when no `ACCOUNT_ID` is in the environment, the page becomes fully interactive: it fetches all accounts, shows each account's balance metrics, offers a selectbox to choose an account, and then renders that account's positions inline. When `ACCOUNT_ID` IS set in `.env`, all existing behavior is preserved without modification.

The expected outcome after execution: launching the page with no `WEBULL_ACCOUNT_ID` in `.env` shows account balance cards, a selectbox, and a positions table — all without restarting the app or editing any file.

---

## 2. Files Involved

| File | Status | Changes |
|------|--------|---------|
| `stock-dashboard/data/webull_positions.py` | Modified | Add two new functions: `get_account_summary_for(account_id)` and `get_positions_for(account_id)` |
| `stock-dashboard/pages/7_positions.py` | Modified | Update the "no ACCOUNT_ID" branch to show balance metrics, a selectbox, and positions inline; remove `st.stop()` from that branch; add new imports |

No files are created or deleted.

---

## 3. Prerequisites & Dependencies

No new pip packages. No schema changes. No environment variable changes. The existing `webull-openapi-python-sdk` and `python-dotenv` packages already installed in the venv are sufficient.

---

## 4. Step-by-Step Implementation

### Step 1: Add `get_account_summary_for()` to `data/webull_positions.py`

**File:** `stock-dashboard/data/webull_positions.py`

**Location:** After the closing line of `get_account_summary()` (currently line 304, the last line of the file). Insert the new function immediately below, after a blank line.

**Action:** Append the following function to the end of the file:

```python
def get_account_summary_for(account_id: str) -> dict:
    """
    Fetches the account balance/summary for an explicitly supplied account_id.

    Identical to get_account_summary() except it accepts account_id as a parameter
    rather than reading ACCOUNT_ID from the environment. Used by pages/7_positions.py
    when iterating over accounts returned by get_account_list().

    Returns a dict with keys: "Total Value", "Market Value", "Cash Balance", "Total P&L".
    Raises WebullAuthError on HTTP 401 or 403.
    Returns {} on any other error.
    """
    if not is_configured() or not account_id:
        return {}
    try:
        client = _make_client()
        res = client.account_v2.get_account_balance(
            account_id,
            total_asset_currency="USD",
        )

        if res.status_code in (401, 403):
            raise WebullAuthError(
                f"Webull API returned {res.status_code}: {res.text}"
            )
        if res.status_code != 200:
            return {}

        b = res.json()

        if isinstance(b, dict) and "data" in b and isinstance(b["data"], dict):
            b = b["data"]

        return {
            "Total Value":  float(b.get("total_asset") or 0),
            "Market Value": float(b.get("total_market_value") or 0),
            "Cash Balance": float(b.get("total_cash") or 0),
            "Total P&L":    float(b.get("total_profit_loss") or 0),
        }

    except WebullAuthError:
        raise
    except Exception:
        return {}
```

**Why:** `get_account_summary()` is hardcoded to use the `ACCOUNT_ID` module-level constant read from env. This new function is structurally identical but accepts `account_id` as a parameter so the page can call it per-account when iterating the account list without any env variable set.

---

### Step 2: Add `get_positions_for()` to `data/webull_positions.py`

**File:** `stock-dashboard/data/webull_positions.py`

**Location:** After the closing line of `get_account_summary_for()` added in Step 1. Insert immediately below, after a blank line.

**Action:** Append the following function to the end of the file (after `get_account_summary_for`):

```python
def get_positions_for(account_id: str) -> list[dict]:
    """
    Fetches all open positions for an explicitly supplied account_id using cursor pagination.

    Identical to get_positions() except it accepts account_id as a parameter
    rather than reading ACCOUNT_ID from the environment. Used by pages/7_positions.py
    when the user selects an account from the selectbox.

    Returns a list of normalized dicts with keys:
      ticker, quantity, cost_basis, market_value, unrealized_pnl, unrealized_pnl_pct

    Raises WebullAuthError on HTTP 401 or 403.
    Returns [] on any other error.
    """
    if not is_configured() or not account_id:
        return []

    positions = []
    last_instrument_id = None

    try:
        client = _make_client()

        while True:
            res = client.account_v2.get_account_position(
                account_id,
                page_size=100,
                last_instrument_id=last_instrument_id,
            )

            if res.status_code in (401, 403):
                raise WebullAuthError(
                    f"Webull API returned {res.status_code}: {res.text}"
                )
            if res.status_code != 200:
                break

            page_data = res.json()

            if isinstance(page_data, list):
                holdings = page_data
                has_next = False
            else:
                holdings = page_data.get("holdings") or []
                has_next = bool(page_data.get("has_next", False))

            for h in holdings:
                pnl_rate_raw = h.get("unrealized_profit_loss_rate", 0) or 0
                try:
                    pnl_pct = float(pnl_rate_raw) * 100
                except (ValueError, TypeError):
                    pnl_pct = 0.0

                positions.append({
                    "ticker":             str(h.get("instrument") or ""),
                    "quantity":           float(h.get("quantity") or 0),
                    "cost_basis":         float(h.get("total_cost") or 0),
                    "market_value":       float(h.get("market_value") or 0),
                    "unrealized_pnl":     float(h.get("unrealized_profit_loss") or 0),
                    "unrealized_pnl_pct": pnl_pct,
                })

            if not has_next or not holdings:
                break

            last_instrument_id = str(holdings[-1].get("instrument_id", ""))
            if not last_instrument_id:
                break

    except WebullAuthError:
        raise
    except Exception:
        return []

    return positions
```

**Why:** `get_positions()` is hardcoded to the module-level `ACCOUNT_ID` constant. This new function accepts the account_id explicitly so the page can fetch positions for whichever account the user selects in the selectbox.

---

### Step 3: Update the imports in `pages/7_positions.py`

**File:** `stock-dashboard/pages/7_positions.py`

**Location:** Lines 1–14, the `from data.webull_positions import (...)` block.

**Action:** Replace the existing import block (lines 3–14) with the following. The only change is adding `get_account_summary_for` and `get_positions_for` to the import list.

**Before (lines 3–14):**
```python
from data.webull_positions import (
    is_configured,
    get_positions,
    get_account_summary,
    get_account_list,
    get_token_status,
    get_token_age_days,
    delete_token,
    TOKEN_MAX_DAYS,
    TOKEN_WARN_DAYS,
    WebullAuthError,
)
```

**After:**
```python
from data.webull_positions import (
    is_configured,
    get_positions,
    get_account_summary,
    get_account_list,
    get_account_summary_for,
    get_positions_for,
    get_token_status,
    get_token_age_days,
    delete_token,
    TOKEN_MAX_DAYS,
    TOKEN_WARN_DAYS,
    WebullAuthError,
)
```

**Why:** The page will call both new functions in the no-`ACCOUNT_ID` branch added in Step 4. They must be imported before use.

---

### Step 4: Replace the "no ACCOUNT_ID" branch in `pages/7_positions.py`

**File:** `stock-dashboard/pages/7_positions.py`

**Location:** Lines 57–74. This is the entire block starting with the comment `# ── Missing ACCOUNT_ID — show account list` through `st.stop()` (inclusive).

**Action:** Replace the block spanning lines 57–74 with the following. The old block is shown first, then the replacement.

**Before (lines 57–74):**
```python
# ── Missing ACCOUNT_ID — show account list ────────────────────────────────────

import os
from dotenv import load_dotenv
load_dotenv()
_account_id = os.getenv("WEBULL_ACCOUNT_ID", "")

if not _account_id:
    st.warning("No `WEBULL_ACCOUNT_ID` set. Fetching your accounts to find it.")
    with st.spinner("Fetching account list…"):
        accounts = get_account_list()
    if accounts:
        st.markdown("**Your Webull accounts:**")
        st.dataframe(pd.DataFrame(accounts), hide_index=True, use_container_width=True)
        st.info("Copy the `account_id` you want to use and add `WEBULL_ACCOUNT_ID=<value>` to your `.env` file, then restart the app.")
    else:
        st.error("Could not fetch account list. Check your credentials and token status.")
    st.stop()
```

**After:**
```python
# ── Missing ACCOUNT_ID — interactive account selector ────────────────────────

import os
from dotenv import load_dotenv
load_dotenv()
_account_id = os.getenv("WEBULL_ACCOUNT_ID", "")

if not _account_id:
    st.info("No `WEBULL_ACCOUNT_ID` set in `.env`. Select an account below to view its positions.")

    with st.spinner("Fetching account list…"):
        accounts = get_account_list()

    if not accounts:
        st.error("Could not fetch account list. Check your credentials.")
        st.stop()

    # Build per-account balance summary. One API call per account.
    account_summaries = {}
    with st.spinner("Fetching balances…"):
        for acct in accounts:
            aid = acct["account_id"]
            try:
                account_summaries[aid] = get_account_summary_for(aid)
            except WebullAuthError:
                st.error("Webull rejected the request while fetching balances.")
                st.stop()

    # Display each account as a labeled expander with its balance metrics.
    st.subheader("Account Balances")
    for acct in accounts:
        aid = acct["account_id"]
        label = f"{aid}  —  {acct['account_type']}  ({acct['currency']})"
        with st.expander(label, expanded=True):
            summary = account_summaries.get(aid, {})
            if summary:
                cols = st.columns(len(summary))
                for col, (metric_label, value) in zip(cols, summary.items()):
                    col.metric(metric_label, f"${value:,.2f}")
            else:
                st.caption("Balance unavailable for this account.")

    st.divider()

    # Selectbox to choose which account to inspect positions for.
    account_options = {
        f"{acct['account_id']}  ({acct['account_type']}, {acct['currency']})": acct["account_id"]
        for acct in accounts
    }
    selected_label = st.selectbox(
        "Select account to view positions:",
        options=list(account_options.keys()),
        key="wb_account_selector",
    )
    selected_account_id = account_options[selected_label]

    st.subheader(f"Positions — {selected_label}")
    with st.spinner("Fetching positions…"):
        try:
            selected_positions = get_positions_for(selected_account_id)
        except WebullAuthError:
            st.error("Webull rejected the request while fetching positions.")
            st.stop()

    if not selected_positions:
        st.info("No open positions found for this account.")
    else:
        df_sel = pd.DataFrame(selected_positions).rename(columns={
            "ticker":             "Ticker",
            "quantity":           "Qty",
            "cost_basis":         "Cost Basis",
            "market_value":       "Market Value",
            "unrealized_pnl":     "Unrealized P&L",
            "unrealized_pnl_pct": "P&L %",
        })
        st.dataframe(
            df_sel,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Cost Basis":     st.column_config.NumberColumn(format="$%.2f"),
                "Market Value":   st.column_config.NumberColumn(format="$%.2f"),
                "Unrealized P&L": st.column_config.NumberColumn(format="$%.2f"),
                "P&L %":          st.column_config.NumberColumn(format="%.2f%%"),
            },
        )
        st.caption(f"{len(df_sel)} position(s) loaded.")

    st.stop()
```

**Why:** The original branch called `st.stop()` unconditionally after showing a bare account table, making the page useless without an env var. The replacement fetches each account's balance via the new `get_account_summary_for()`, renders per-account balance metrics inside expanders, shows a selectbox for the user to pick an account, calls `get_positions_for()` for the selected account, and renders the positions table with identical column formatting to the existing env-var path. `st.stop()` is retained at the very end of this branch so that when no `ACCOUNT_ID` is set, execution does not fall through into the lower half of the page (which calls `get_account_summary()` and `get_positions()` that require the env var). When `ACCOUNT_ID` IS set, this entire `if not _account_id:` block is skipped and the page proceeds exactly as before.

---

## 5. Database Changes

None. This feature makes no database changes.

---

## 6. UI/UX Specification

The no-`ACCOUNT_ID` branch renders in this order, top to bottom:

1. `st.info(...)` — one line explaining no env var is set.
2. `st.spinner("Fetching account list…")` — while `get_account_list()` runs.
3. `st.spinner("Fetching balances…")` — while per-account `get_account_summary_for()` calls run sequentially.
4. `st.subheader("Account Balances")` — section header.
5. One `st.expander(label, expanded=True)` per account. Each expander label is `"<account_id>  —  <account_type>  (<currency>)"`. Inside each expander: four `st.metric` calls in four `st.columns`, one per summary key ("Total Value", "Market Value", "Cash Balance", "Total P&L"), each formatted as `$X,XXX.XX`. If the balance call returned `{}`, a `st.caption("Balance unavailable for this account.")` is shown instead.
6. `st.divider()` — horizontal rule.
7. `st.selectbox("Select account to view positions:", ...)` — key `"wb_account_selector"`. Options are strings in the format `"<account_id>  (<account_type>, <currency>)"`. The selected value drives the positions fetch below.
8. `st.subheader(f"Positions — {selected_label}")` — section header echoing the selected account.
9. `st.spinner("Fetching positions…")` — while `get_positions_for()` runs.
10. Either `st.info("No open positions found for this account.")` (empty result) or a `st.dataframe(...)` with the same column config as the existing positions table at the bottom of the page (lines 131–142 of the original file).
11. `st.stop()` — prevents fall-through into the env-var path below.

When `ACCOUNT_ID` IS set in `.env`, none of the above renders. The page skips the `if not _account_id:` block entirely and the existing sidebar, spinner, metrics, and table render as before.

---

## 7. Testing Checklist

Execute from `stock-dashboard/` with the venv active (`.\venv\Scripts\Activate.ps1`) and `streamlit run dashboard.py` running.

1. **No `ACCOUNT_ID` in `.env` — accounts returned:**
   - Remove or comment out `WEBULL_ACCOUNT_ID` from `.env`, restart the app, navigate to page 7.
   - Expected: info banner appears, balances are fetched, one expander per account is shown with four metric tiles, a selectbox appears, changing the selection reloads positions inline.
   - Failure: `st.stop()` fires before the selectbox, or an `ImportError` for `get_account_summary_for`/`get_positions_for`.

2. **No `ACCOUNT_ID` in `.env` — account list empty:**
   - If `get_account_list()` returns `[]` (e.g., US retail account), expected: `st.error("Could not fetch account list. Check your credentials.")` appears and the page stops cleanly.
   - Failure: an unhandled exception or blank page.

3. **No `ACCOUNT_ID` in `.env` — account with no positions:**
   - Select an account that has zero open positions.
   - Expected: `st.info("No open positions found for this account.")` appears below the selectbox.
   - Failure: error or empty `st.dataframe` with no caption.

4. **No `ACCOUNT_ID` in `.env` — switching accounts:**
   - With multiple accounts in the selectbox, change the selection.
   - Expected: the positions section re-renders for the newly selected account without a full page reload error.
   - Failure: stale positions from the previous account shown, or Streamlit key collision error.

5. **`ACCOUNT_ID` set in `.env` — existing behavior unchanged:**
   - Set `WEBULL_ACCOUNT_ID` to a valid account ID in `.env`, restart the app, navigate to page 7.
   - Expected: sidebar shows token info, spinner fetches data, four metric tiles appear, positions table appears — identical to behavior before this change.
   - Failure: any regression in the existing flow, or the new imports cause a syntax error.

6. **Balance unavailable for one account:**
   - If one account's `get_account_summary_for()` returns `{}`, the expander for that account should show `"Balance unavailable for this account."` as a caption rather than crashing.
   - Expected: all other account expanders show metrics normally; only the failing one shows the caption.
   - Failure: `KeyError` or `TypeError` when iterating `summary.items()` on an empty dict.

7. **Auth error during balance fetch:**
   - If the API returns 401/403 during `get_account_summary_for()`, expected: `st.error("Webull rejected the request while fetching balances.")` and `st.stop()`.
   - Failure: unhandled `WebullAuthError` traceback shown to the user.

8. **Auth error during positions fetch:**
   - If the API returns 401/403 during `get_positions_for()`, expected: `st.error("Webull rejected the request while fetching positions.")` and `st.stop()`.
   - Failure: unhandled `WebullAuthError` traceback.

---

## 8. Rollback Plan

If anything goes wrong after executing Steps 1–4, revert both files to their prior state:

1. In `stock-dashboard/data/webull_positions.py`: delete everything after line 304 (the closing `except Exception: return {}` of `get_account_summary()`). The file should end at exactly that line.

2. In `stock-dashboard/pages/7_positions.py`:
   - Revert the import block (lines 3–14) to remove `get_account_summary_for` and `get_positions_for`.
   - Revert the "no ACCOUNT_ID" block (lines 57–74 in the original) back to the original text shown in the "Before" snippet in Step 4.

Both files are fully self-contained — no database rows, no schema changes, no new files — so reverting these two edits is a complete rollback.
