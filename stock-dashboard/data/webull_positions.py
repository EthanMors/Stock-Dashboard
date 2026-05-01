import os
import pickle
from dotenv import load_dotenv
from webull import webull

load_dotenv()

WEBULL_EMAIL = os.getenv("WEBULL_EMAIL", "")
WEBULL_PASSWORD = os.getenv("WEBULL_PASSWORD", "")
WEBULL_DEVICE_NAME = os.getenv("WEBULL_DEVICE_NAME", "Stock-Dashboard")
CREDENTIALS_FILE = "webull_credentials.json"


def connect() -> webull:
    """Authenticate and return a logged-in webull client (read-only session)."""
    wb = webull()

    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "rb") as f:
                creds = pickle.load(f)
            wb.api_login(
                access_token=creds.get("accessToken", ""),
                refresh_token=creds.get("refreshToken", ""),
                token_expire=creds.get("tokenExpireTime", ""),
                uuid=creds.get("uuid", ""),
            )
            if wb.is_logged_in():
                return wb
            result = wb.refresh_login(save_token=True)
            if "accessToken" in result and wb.is_logged_in():
                return wb
        except Exception:
            pass

    email = WEBULL_EMAIL or input("Webull email: ").strip()
    password = WEBULL_PASSWORD or input("Webull password: ").strip()

    wb.get_mfa(email)
    mfa_code = input("Enter MFA code: ").strip()

    result = wb.login(
        username=email,
        password=password,
        device_name=WEBULL_DEVICE_NAME,
        mfa=mfa_code,
        save_token=True,
    )

    if "accessToken" not in result:
        raise RuntimeError(f"Webull login failed: {result.get('msg', result)}")

    return wb


def try_cached_connect() -> webull | None:
    """Try to connect using only cached credentials; return None if MFA is required."""
    wb = webull()

    if not os.path.exists(CREDENTIALS_FILE):
        return None

    try:
        with open(CREDENTIALS_FILE, "rb") as f:
            creds = pickle.load(f)
        wb.api_login(
            access_token=creds.get("accessToken", ""),
            refresh_token=creds.get("refreshToken", ""),
            token_expire=creds.get("tokenExpireTime", ""),
            uuid=creds.get("uuid", ""),
        )
        if wb.is_logged_in():
            return wb
        result = wb.refresh_login(save_token=True)
        if "accessToken" in result and wb.is_logged_in():
            return wb
    except Exception:
        pass

    return None


def send_mfa(email: str) -> webull:
    """Create a webull client and send an MFA code to the given email."""
    wb = webull()
    wb.get_mfa(email)
    return wb


def login_with_mfa(
    wb: webull,
    email: str,
    password: str,
    mfa_code: str,
) -> webull:
    """Complete login with MFA code; raise RuntimeError on failure."""
    result = wb.login(
        username=email,
        password=password,
        device_name=WEBULL_DEVICE_NAME,
        mfa=mfa_code,
        save_token=True,
    )
    if "accessToken" not in result:
        raise RuntimeError(result.get("msg", "Login failed"))
    return wb


def get_positions(wb: webull) -> list[dict]:
    """Return current holdings as a list of position dicts."""
    try:
        raw = wb.get_positions()
    except (KeyError, TypeError):
        return []

    if not raw:
        return []

    positions = []
    for item in raw:
        ticker = item.get("ticker", {})
        positions.append({
            "ticker": ticker.get("symbol", ""),
            "name": ticker.get("name", ""),
            "quantity": float(item.get("position", 0) or 0),
            "cost_basis": float(item.get("costPrice", 0) or 0),
            "current_price": float(item.get("lastPrice", 0) or 0),
            "market_value": float(item.get("marketValue", 0) or 0),
            "unrealized_pnl": float(item.get("unrealizedProfitLoss", 0) or 0),
            "unrealized_pnl_pct": float(item.get("unrealizedProfitLossRate", 0) or 0) * 100,
        })
    return positions


def get_account_summary(wb: webull) -> dict:
    """Return portfolio totals as a flat dict of {key: float}."""
    try:
        portfolio = wb.get_portfolio()
    except (KeyError, TypeError):
        return {}
    return {k: float(v) if v else 0.0 for k, v in portfolio.items()}
