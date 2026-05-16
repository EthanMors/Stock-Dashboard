---
name: Webull SDK Correct Package
description: The correct pip package name and import paths for the Webull Official OpenAPI SDK
type: project
---

The correct pip package is `webull-openapi-python-sdk` (NOT `webull-python-sdk-trade` or any `webull.*` namespace).

Install: `pip install webull-openapi-python-sdk`

Import paths:
- `from webullsdkcore.client import ApiClient`
- `from webullsdktrade.api import TradeClient`

Client construction: `ApiClient(APP_KEY, APP_SECRET, "us")` — region "us" resolves to base URL `api.webull.com`

TradeClient namespaces: `client.account` (v1) and `client.account_v2` (v2)

v2 endpoints:
- Account list: `GET /openapi/account/list` — may 403 for US retail accounts
- Positions: `GET /openapi/account/positions?account_id=...`
- Balance: `GET /openapi/account/balance?account_id=...&total_asset_currency=USD`

Confirmed response field names (from official SDK demo `get_account_info.py`):
- Balance: `total_asset`, `total_market_value`, `total_cash`, `total_profit_loss`
- Positions: `instrument` (ticker), `quantity`, `total_cost`, `market_value`, `unrealized_profit_loss`, `unrealized_profit_loss_rate` (decimal fraction)

**Why:** The old code used `webull.core.client.ApiClient` which is a different unrelated third-party package. The official package uses `webullsdkcore` / `webullsdktrade` namespaces.

**How to apply:** Any future Webull integration work must use the `webullsdkcore`/`webullsdktrade` import paths and `webull-openapi-python-sdk` as the pip package name.
