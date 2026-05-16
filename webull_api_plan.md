# Webull Official OpenAPI Integration Plan

## 1. Task Summary

Rewrite `stock-dashboard/data/webull_positions.py` to use the official Webull OpenAPI SDK
(`webull-openapi-python-sdk`) instead of the broken third-party `webull-python-sdk-trade`
package. The official SDK package installs under the `webullsdktrade` / `webullsdkcore`
namespace (not `webull`), exposes `ApiClient` from `webullsdkcore.client`, `TradeClient`
from `webullsdktrade.api`, and calls account v2 endpoints at base URL `api.webull.com`.
The SDK handles HMAC-SHA1 request signing automatically — no manual signing code is needed.

After execution, `pages/7_positions.py` will load without import errors, the positions table
will populate with real data, and account balance metrics will display correctly. No changes
are required to `pages/7_positions.py` because all public function signatures remain identical.

---

## 2. Files Involved

| File | Status | Change |
|------|--------|--------|
| `stock-dashboard/data/webull_positions.py` | **MODIFIED** | Complete rewrite to use official SDK |
| `stock-dashboard/requirements.txt` | **MODIFIED** | Add `webull-openapi-python-sdk`, remove SDK comment |
| `stock-dashboard/.env.example` | No change needed | Already correct |
| `stock-dashboard/pages/7_positions.py` | No change needed | All public function signatures stay the same |

---

## 3. Prerequisites and Dependencies

### 3a. Remove old broken package (if installed)

Run this from inside `stock-dashboard/` with the venv activated:

```powershell
.\venv\Scripts\Activate.ps1
pip uninstall webull-python-sdk-trade webull-python-sdk-core -y
```

### 3b. Install the correct official SDK

```powershell
pip install webull-openapi-python-sdk
```

This single meta-package installs all sub-packages:
- `webull-python-sdk-core` (provides `webullsdkcore`)
- `webull-python-sdk-trade` (provides `webullsdktrade`)

### 3c. Verify the install

```powershell
python -c "from webullsdkcore.client import ApiClient; from webullsdktrade.api import TradeClient; print('OK')"
```

Expected output: `OK`

### 3d. Environment variables

No new `.env` variables are required. The four existing variables are sufficient:
```
WEBULL_APP_KEY=...
WEBULL_APP_SECRET=...
WEBULL_ACCOUNT_ID=...
WEBULL_REGION_ID=us
```

The `WEBULL_REGION_ID` must be `us` (not `US`, not `United States`). The SDK endpoint
resolver maps the string `"us"` to `api.webull.com`.

---

## 4. Step-by-Step Implementation

### Step 1: Update `requirements.txt`

**File:** `stock-dashboard/requirements.txt`

**Action:** Replace the comment block about Webull and add the correct package.

Find this text (lines 10-12):
```
# Webull OpenAPI — signing implemented directly via requests (SDK incompatible with Python 3.13)
# webull-python-sdk-core and webull-python-sdk-trade are NOT installed
google-genai>=0.8.0
```

Replace it with:
```
webull-openapi-python-sdk>=0.1.18
google-genai>=0.8.0
```

**Why:** The comment said the SDK was incompatible with Python 3.13, but `webull-openapi-python-sdk`
0.1.18 (released September 2025) officially supports Python 3.8–3.13. The comment was written
before the package reached compatibility. The single meta-package name is the correct install target.

### Step 2: Rewrite `data/webull_positions.py` completely

**File:** `stock-dashboard/data/webull_positions.py`

**Action:** Delete all existing content and replace with the following complete file.

```python
"""
webull_positions.py
-------------------
Fetches live account positions and balance from the Webull Official OpenAPI.

Authentication is handled automatically by the SDK using HMAC-SHA1 request
signing with APP_KEY and APP_SECRET. No token file management is needed.

SDK package: webull-openapi-python-sdk (installs webullsdkcore + webullsdktrade)
Base URL:    api.webull.com  (resolved automatically from REGION_ID="us")
"""

import os
from dotenv import load_dotenv

load_dotenv()

APP_KEY = os.getenv("WEBULL_APP_KEY", "")
APP_SECRET = os.getenv("WEBULL_APP_SECRET", "")
ACCOUNT_ID = os.getenv("WEBULL_ACCOUNT_ID", "")
REGION_ID = os.getenv("WEBULL_REGION_ID", "us")

# These constants are kept so pages/7_positions.py can import them without change.
# The SDK does not use file-based tokens; these values are retained for UI display only.
TOKEN_FILE = os.path.join("conf", "webull_token_info.json")
TOKEN_MAX_DAYS = 15
TOKEN_WARN_DAYS = 13


class WebullAuthError(Exception):
    """Raised when the API returns HTTP 401 or 403."""


# ── Credentials check ─────────────────────────────────────────────────────────

def is_configured() -> bool:
    """Return True if both APP_KEY and APP_SECRET are set in the environment."""
    return bool(APP_KEY and APP_SECRET)


# ── Client factory ─────────────────────────────────────────────────────────────

def _make_client():
    """
    Build and return a TradeClient using the official SDK.

    Import paths:
      - ApiClient is in webullsdkcore.client   (from webull-python-sdk-core)
      - TradeClient is in webullsdktrade.api   (from webull-python-sdk-trade)

    ApiClient(app_key, app_secret, region_id) automatically:
      - Resolves the endpoint from data/endpoints.json ("us" -> api.webull.com)
      - Signs every outbound request with HMAC-SHA1 using APP_KEY/APP_SECRET
      - Sets required headers: x-app-key, x-signature, x-signature-algorithm,
        x-signature-version, x-signature-nonce, x-timestamp
    """
    from webullsdkcore.client import ApiClient
    from webullsdktrade.api import TradeClient

    api_client = ApiClient(APP_KEY, APP_SECRET, REGION_ID)
    return TradeClient(api_client)


# ── Token status stubs ─────────────────────────────────────────────────────────
#
# The official SDK does NOT use file-based tokens. The SDK authenticates each
# request independently via HMAC signing — there is no "pending approval" flow
# and no 15-day token expiry.
#
# These functions are kept with the same signatures as before so that
# pages/7_positions.py requires zero changes.

def get_token_age_days() -> int | None:
    """Always returns None — the SDK has no file-based token."""
    return None


def get_token_status() -> str:
    """
    Returns 'missing' if credentials are absent, 'active' otherwise.
    The 'pending' and 'expired' states no longer apply with the SDK.
    """
    if not is_configured():
        return "missing"
    return "active"


def delete_token() -> None:
    """No-op — the SDK has no token file to delete."""
    pass


# ── Account list ───────────────────────────────────────────────────────────────

def get_account_list() -> list[dict]:
    """
    Fetches the list of accounts associated with the API key.

    Endpoint:  GET /openapi/account/list
    Base URL:  https://api.webull.com
    Full URL:  https://api.webull.com/openapi/account/list

    Note from SDK docs: "Currently available only to individual brokerage customers
    in Webull Japan and institutional brokerage clients in Webull Hong Kong."
    US retail accounts may receive an empty list or a 403 from this endpoint.
    The UI in pages/7_positions.py only calls this when ACCOUNT_ID is not set,
    so this is a fallback discovery path.

    Returns a list of dicts with keys: account_id, account_type, currency.
    Returns [] on any error.
    """
    if not is_configured():
        return []
    try:
        client = _make_client()
        res = client.account_v2.get_account_list()
        if res.status_code != 200:
            return []
        data = res.json()
        # The response may be a list directly, or a dict with a "data" or "items" key.
        if isinstance(data, list):
            accounts_raw = data
        elif isinstance(data, dict):
            accounts_raw = data.get("data") or data.get("items") or data.get("accounts") or []
        else:
            accounts_raw = []
        return [
            {
                "account_id":   str(a.get("account_id") or a.get("id") or ""),
                "account_type": str(a.get("account_type") or a.get("type") or ""),
                "currency":     str(a.get("currency") or ""),
            }
            for a in accounts_raw
            if isinstance(a, dict)
        ]
    except Exception:
        return []


# ── Positions ──────────────────────────────────────────────────────────────────

def get_positions() -> list[dict]:
    """
    Fetches all open positions for the configured account using cursor pagination.

    Endpoint:  GET /openapi/account/positions
    Base URL:  https://api.webull.com
    Full URL:  https://api.webull.com/openapi/account/positions
    Query params: account_id (required)

    Pagination: The response contains a "has_next" boolean. When True, pass the
    "instrument_id" of the last item in the current page as "last_instrument_id"
    in the next request. The SDK method get_account_position() accepts:
      - account_id (str, required)
      - page_size  (int, default 10, max 100)
      - last_instrument_id (str or None, default None)

    Response structure (per holding object):
      instrument_id          — internal Webull instrument identifier (int or str)
      instrument             — ticker symbol string, e.g. "AAPL"
      instrument_type        — e.g. "STK", "OPT"
      quantity               — number of shares held (numeric)
      total_cost             — total cost basis in account currency (numeric)
      market_value           — current market value (numeric)
      unrealized_profit_loss — unrealized P&L (numeric)
      unrealized_profit_loss_rate — P&L as a decimal fraction, e.g. 0.05 = 5%

    Returns a list of normalized dicts with keys:
      ticker, quantity, cost_basis, market_value, unrealized_pnl, unrealized_pnl_pct

    Raises WebullAuthError on HTTP 401 or 403.
    Returns [] on any other error.
    """
    if not is_configured() or not ACCOUNT_ID:
        return []

    positions = []
    last_instrument_id = None

    try:
        client = _make_client()

        while True:
            res = client.account_v2.get_account_position(
                ACCOUNT_ID,
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

            # Response is a dict with "has_next" (bool) and "holdings" (list).
            if isinstance(page_data, list):
                # Fallback: some versions return the list directly without pagination wrapper
                holdings = page_data
                has_next = False
            else:
                holdings = page_data.get("holdings") or []
                has_next = bool(page_data.get("has_next", False))

            for h in holdings:
                # unrealized_profit_loss_rate is a decimal fraction (0.05 = 5%).
                # Multiply by 100 to get a percentage for display.
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

            # Advance cursor to the last instrument_id on this page.
            last_instrument_id = str(holdings[-1].get("instrument_id", ""))
            if not last_instrument_id:
                break

    except WebullAuthError:
        raise
    except Exception:
        return []

    return positions


# ── Account summary ────────────────────────────────────────────────────────────

def get_account_summary() -> dict:
    """
    Fetches the account balance/summary for the configured account.

    Endpoint:  GET /openapi/account/balance
    Base URL:  https://api.webull.com
    Full URL:  https://api.webull.com/openapi/account/balance
    Query params: account_id (required), total_asset_currency (optional, default "HKD")

    For US accounts, pass total_asset_currency="USD" to get USD-denominated totals.
    The SDK method get_account_balance() accepts:
      - account_id            (str, required)
      - total_asset_currency  (str, optional — defaults to "HKD" in the SDK)

    Response top-level fields (confirmed from SDK demo get_account_info.py):
      total_asset             — total account value
      total_market_value      — total market value of all positions
      total_cash              — total cash balance
      total_profit_loss       — unrealized total P&L
      history_profit_loss     — realized/historical P&L
      margin_utilization_rate — margin usage rate (decimal fraction)

    Returns a dict with human-readable keys matching the existing UI labels:
      "Total Value", "Market Value", "Cash Balance", "Total P&L"

    Raises WebullAuthError on HTTP 401 or 403.
    Returns {} on any other error.
    """
    if not is_configured() or not ACCOUNT_ID:
        return {}
    try:
        client = _make_client()
        res = client.account_v2.get_account_balance(
            ACCOUNT_ID,
            total_asset_currency="USD",
        )

        if res.status_code in (401, 403):
            raise WebullAuthError(
                f"Webull API returned {res.status_code}: {res.text}"
            )
        if res.status_code != 200:
            return {}

        b = res.json()

        # Some SDK versions wrap the balance in a "data" key.
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

**Why:** This rewrites every function to use the correct SDK namespace (`webullsdkcore`,
`webullsdktrade`) and the correct import paths (`webullsdkcore.client.ApiClient`,
`webullsdktrade.api.TradeClient`). The old code used `webull.core.client.ApiClient` and
`webull.trade.trade_client.TradeClient`, which are from a different, unrelated third-party
package. The response field names (`instrument`, `quantity`, `total_cost`, `market_value`,
`unrealized_profit_loss`, `unrealized_profit_loss_rate`, `total_asset`, `total_market_value`,
`total_cash`, `total_profit_loss`) are confirmed directly from the SDK demo file
`webull-python-sdk-demos/webullsdkdemos/trade/get_account_info.py` in the official GitHub repo.

---

## 5. Database Changes

None. This feature uses no SQLite tables.

---

## 6. UI/UX Specification

No changes to `pages/7_positions.py` are required. Here is why each piece of the existing UI
still works without modification:

| UI Element | Uses | Still works because |
|-----------|------|---------------------|
| `is_configured()` guard | Returns `bool` | Signature unchanged |
| `get_token_status()` | Returns `"missing"` or `"active"` | Never returns `"pending"` or `"expired"` now, so the warning/stop blocks are never reached — this is correct behavior |
| `get_token_age_days()` | Returns `int | None` | Now always returns `None`, causing the sidebar to show "Token age unknown." — acceptable since the SDK has no token expiry |
| `delete_token()` | Called on button press | Now a no-op — acceptable |
| `get_account_list()` | Returns `list[dict]` | Same keys: `account_id`, `account_type`, `currency` |
| `get_positions()` | Returns `list[dict]` | Same keys: `ticker`, `quantity`, `cost_basis`, `market_value`, `unrealized_pnl`, `unrealized_pnl_pct` |
| `get_account_summary()` | Returns `dict` | Same keys: `"Total Value"`, `"Market Value"`, `"Cash Balance"`, `"Total P&L"` |
| `WebullAuthError` | Caught in try/except | Class definition preserved |
| `TOKEN_MAX_DAYS`, `TOKEN_WARN_DAYS` | Imported constants | Still exported from module |

**Optional UI improvement (not required):** The sidebar currently shows "Token age unknown."
because `get_token_age_days()` returns `None`. This is accurate — the SDK authenticates per-
request and has no expiry. The display is harmless. If desired, the sidebar text can be updated
in `pages/7_positions.py` to say "SDK auth — no token expiry" by changing the `days_left is None`
branch, but this is cosmetic only.

---

## 7. Testing Checklist

Perform these tests in order after implementation. Run the Streamlit app from `stock-dashboard/`
with `streamlit run dashboard.py` (venv activated).

### Test 1: SDK import check (before running the app)

**Action:** Run in the venv:
```powershell
python -c "from webullsdkcore.client import ApiClient; from webullsdktrade.api import TradeClient; print('SDK imports OK')"
```
**Expected:** Prints `SDK imports OK`
**Failure:** `ModuleNotFoundError` — means `pip install webull-openapi-python-sdk` was not run
or the wrong venv is active.

### Test 2: Module import check

**Action:**
```powershell
python -c "from data.webull_positions import is_configured, get_positions, get_account_summary, get_account_list, get_token_status, get_token_age_days, delete_token, TOKEN_MAX_DAYS, TOKEN_WARN_DAYS, WebullAuthError; print('Module imports OK')"
```
Run this from the `stock-dashboard/` directory with the venv active.
**Expected:** Prints `Module imports OK`
**Failure:** `ImportError` or `SyntaxError` — check the rewritten file for typos.

### Test 3: Credentials check

**Action:**
```powershell
python -c "from data.webull_positions import is_configured; print(is_configured())"
```
**Expected:** `True`
**Failure:** `False` — means `.env` is missing `WEBULL_APP_KEY` or `WEBULL_APP_SECRET`.
Check that `.env` exists in `stock-dashboard/` and contains both keys.

### Test 4: Account balance call

**Action:**
```powershell
python -c "from data.webull_positions import get_account_summary; print(get_account_summary())"
```
**Expected:** A non-empty dict like `{'Total Value': 12345.67, 'Market Value': 9876.54, 'Cash Balance': 2469.13, 'Total P&L': 150.00}`
**Failure — empty dict `{}`:** The API call failed silently. Enable debug logging:
```powershell
python -c "
import logging
logging.basicConfig(level=logging.DEBUG)
from data.webull_positions import get_account_summary
print(get_account_summary())
"
```
Look for the HTTP response code in the debug output. A `403` means the API key is not yet
authorized in the Webull mobile app (Profile > Settings > API Management > Authorize).
A `404` on the endpoint means the account does not have access to v2 endpoints — try
switching to `client.account` (v1) methods instead (see Note A below).

### Test 5: Positions call

**Action:**
```powershell
python -c "from data.webull_positions import get_positions; import pprint; pprint.pprint(get_positions())"
```
**Expected:** A list of dicts, each with keys `ticker`, `quantity`, `cost_basis`,
`market_value`, `unrealized_pnl`, `unrealized_pnl_pct`.
**Failure — empty list `[]` with no exception:** Run the debug logging variant from Test 4
pattern, but for `get_positions()`, and check the HTTP status code.

### Test 6: Streamlit page loads

**Action:** Open the app in a browser and click "Portfolio Positions" (page 7).
**Expected:** The page loads without a red error banner. Either the positions table shows
data, or a blue info box reads "No open positions found for this account."
**Failure:** Red error "Webull rejected the request" — this is `WebullAuthError`, meaning
the API returned 401 or 403. The API key needs authorization in the Webull mobile app.

### Test 7: Sidebar token display

**Action:** While on page 7, look at the sidebar "API Token" section.
**Expected:** Shows "Token age unknown." (this is correct — the SDK has no file-based token)
**Failure:** Any Python traceback.

### Test 8: Account list fallback (optional, requires temporarily blanking ACCOUNT_ID)

**Action:** Temporarily comment out `WEBULL_ACCOUNT_ID` in `.env`, restart the app, go to
page 7.
**Expected:** The page shows "No `WEBULL_ACCOUNT_ID` set. Fetching your accounts to find it."
followed by either a table of accounts or "Could not fetch account list."
**Action after:** Uncomment `WEBULL_ACCOUNT_ID` in `.env` and restart.

---

## 8. Note A — v1 vs v2 Endpoint Fallback

The SDK's `account_v2` namespace uses v2 endpoints:
- `GET /openapi/account/list`
- `GET /openapi/account/positions`
- `GET /openapi/account/balance`

The SDK's `account` namespace (v1) uses v1 endpoints:
- `GET /account/profile`
- `GET /account/balance`
- `GET /account/positions`

The SDK comment in the `get_account_list()` docstring notes that `/openapi/account/list`
may return 403 for US retail accounts. If Tests 4 and 5 return empty results with a 403 on
the v2 endpoints, switch `_make_client()` usage in `get_positions()` and `get_account_summary()`
from `client.account_v2` to `client.account`:

```python
# In get_positions(), replace:
res = client.account_v2.get_account_position(ACCOUNT_ID, page_size=100, last_instrument_id=last_instrument_id)
# With:
res = client.account.get_account_position(ACCOUNT_ID, page_size=100, last_instrument_id=last_instrument_id)

# In get_account_summary(), replace:
res = client.account_v2.get_account_balance(ACCOUNT_ID, total_asset_currency="USD")
# With:
res = client.account.get_account_balance(ACCOUNT_ID, total_asset_currency="USD")
```

The response field names are identical between v1 and v2.

---

## 9. Rollback Plan

If the new implementation causes worse breakage and you need to revert:

1. Restore the original `data/webull_positions.py` from git:
   ```powershell
   cd "C:\Users\ethan\Downloads\Stock Dashboard"
   git checkout stock-dashboard/data/webull_positions.py
   ```

2. Restore `requirements.txt`:
   ```powershell
   git checkout stock-dashboard/requirements.txt
   ```

3. Uninstall the new SDK package (optional):
   ```powershell
   cd stock-dashboard
   .\venv\Scripts\Activate.ps1
   pip uninstall webull-openapi-python-sdk -y
   ```

The app will return to its previous state where `pages/7_positions.py` loads but data
calls silently fail due to the wrong SDK namespace.

---

## Appendix: SDK Architecture Reference

This appendix documents the SDK internals for future debugging. Do not implement any of this
manually — the SDK handles it automatically.

### Request Signing (HMAC-SHA1)

Every request to `api.webull.com` is signed. The SDK sets these HTTP headers automatically:

| Header | Value |
|--------|-------|
| `x-app-key` | The APP_KEY string |
| `x-timestamp` | Current UTC time as `YYYY-MM-DDTHH:MM:SSZ` |
| `x-signature-algorithm` | `"HMAC-SHA1"` |
| `x-signature-version` | `"1.0"` |
| `x-signature-nonce` | A UUID5 derived from hostname + uuid1 |
| `x-signature` | HMAC-SHA1 of the sign string (see below), base64-encoded |

### Sign String Construction

The string that gets HMAC-SHA1 signed is constructed as follows:

```
URL-encode( URI + "&" + sorted(key=value pairs from headers+query) + "&" + MD5_HEX_UPPER(JSON_body) )
```

Where:
- `URI` is the path, e.g. `/openapi/account/positions`
- `key=value pairs` includes all six `x-*` headers plus `host` and all query params, keys sorted alphabetically, joined with `&`
- `MD5_HEX_UPPER` is the uppercase MD5 hex digest of the compact JSON-encoded request body (empty string `""` for GET requests with no body)
- The HMAC key is `APP_SECRET + "&"`

The signature is set in the `x-signature` header.
