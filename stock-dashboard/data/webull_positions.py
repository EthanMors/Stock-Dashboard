import os
from dotenv import load_dotenv

load_dotenv()

APP_KEY = os.getenv("WEBULL_APP_KEY", "")
APP_SECRET = os.getenv("WEBULL_APP_SECRET", "")
REGION_ID = os.getenv("WEBULL_REGION_ID", "us")
ACCOUNT_ID1 = os.getenv("WEBULL_ACCOUNT_ID1", "")
ACCOUNT_ID2 = os.getenv("WEBULL_ACCOUNT_ID2", "")


def is_configured() -> bool:
    return bool(APP_KEY and APP_SECRET)


def get_env_account_ids() -> list[str]:
    return [aid for aid in [ACCOUNT_ID1, ACCOUNT_ID2] if aid]


def _make_client():
    from webull.core.client import ApiClient
    from webull.trade.trade_client import TradeClient

    api_client = ApiClient(APP_KEY, APP_SECRET, REGION_ID)
    return TradeClient(api_client)


def get_account_list():
    if not is_configured():
        return []
    try:
        client = _make_client()
        res = client.account_v2.get_account_list()
        return res.json()
    except Exception as exc:
        return {"error": str(exc)}

def get_balance(account_id: str):
    if not is_configured():
        return {}
    try:
        client = _make_client()
        res = client.account_v2.get_account_balance(account_id)
        return res.json()
    except Exception as exc:
        return {"error": str(exc)}

def get_positions(account_id: str):
    if not is_configured():
        return []
    try:
        client = _make_client()
        res = client.account_v2.get_account_position(account_id)
        return res.json()
    except Exception as exc:
        return {"error": str(exc)}