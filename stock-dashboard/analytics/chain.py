"""Option chain loading and enrichment pipeline."""

import logging
import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm
from analytics.helpers import time_to_expiration, mid_iv

logger = logging.getLogger(__name__)

RISK_FREE_RATE = 0.045
DIVIDEND_YIELD = 0.0


def load_raw_chain(
    symbol: str,
    expirations: list[str],
) -> pd.DataFrame:
    """Fetch and concatenate raw option chains from yfinance.

    Args:
        symbol: Ticker symbol
        expirations: List of ISO date strings

    Returns:
        pd.DataFrame: Raw chain with option_type and expiration columns
    """
    rows = []

    for exp in expirations:
        try:
            ticker = yf.Ticker(symbol)
            chain = ticker.option_chain(exp)

            # Process calls
            calls = chain.calls.copy()
            calls['option_type'] = 'call'
            calls['expiration'] = exp
            rows.append(calls)

            # Process puts
            puts = chain.puts.copy()
            puts['option_type'] = 'put'
            puts['expiration'] = exp
            rows.append(puts)

        except Exception as e:
            logger.warning(f"Failed to load chain for {symbol} {exp}: {e}")

    if not rows:
        return pd.DataFrame()

    return pd.concat(rows, ignore_index=True)


def clean_chain(raw_chain: pd.DataFrame) -> pd.DataFrame:
    """Clean raw chain: drop invalid rows, normalize columns.

    Args:
        raw_chain: Raw chain DataFrame

    Returns:
        pd.DataFrame: Cleaned chain
    """
    if raw_chain.empty:
        return raw_chain.copy()

    df = raw_chain.copy()

    # Drop rows with no meaningful data
    df = df[~(
        (df['lastPrice'] <= 0) &
        (df['volume'] == 0) &
        (df['openInterest'] == 0)
    )]

    # Normalize column names to snake_case
    df.columns = [col.lower().replace(' ', '_') for col in df.columns]

    # Coerce numeric columns
    numeric_cols = ['strike', 'bid', 'ask', 'lastprice', 'volume', 'openinterest', 'impliedvolatility']
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # Replace inf with NaN
    df = df.replace([np.inf, -np.inf], np.nan)

    # Check required columns
    required = ['strike', 'option_type', 'expiration']
    for col in required:
        if col not in df.columns:
            logger.warning(f"Missing required column: {col}")

    return df


def _bs_greeks(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> dict:
    """Compute Black-Scholes Greeks.

    Args:
        S: Spot price
        K: Strike price
        T: Time to expiration (years)
        r: Risk-free rate
        sigma: Implied volatility
        opt_type: 'call' or 'put'

    Returns:
        dict: {delta, gamma, theta, vega, rho, vanna, charm}
    """
    zero = dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0, vanna=0.0, charm=0.0)

    if T <= 1e-6 or sigma <= 1e-6 or S <= 0 or K <= 0:
        return zero

    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    pdf_d1 = norm.pdf(d1)
    cdf_d1 = norm.cdf(d1)

    gamma = pdf_d1 / (S * sigma * sqrt_T)
    vega = S * pdf_d1 * sqrt_T / 100

    # Vanna: dDelta/dIV = -pdf(d1) * d2 / sigma
    vanna = -pdf_d1 * d2 / sigma if sigma != 0 else 0.0

    # Charm: dDelta/dt
    charm_numerator = -(pdf_d1 * (2 * r * T - d2 * sigma * sqrt_T))
    charm_denominator = 2 * T * sigma * sqrt_T
    charm = charm_numerator / charm_denominator if charm_denominator != 0 else 0.0

    if opt_type == "call":
        delta = cdf_d1
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrt_T) - r * K * np.exp(-r * T) * norm.cdf(d2)) / 365
        rho = K * T * np.exp(-r * T) * norm.cdf(d2) / 100
    else:
        delta = cdf_d1 - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrt_T) + r * K * np.exp(-r * T) * norm.cdf(-d2)) / 365
        rho = -K * T * np.exp(-r * T) * norm.cdf(-d2) / 100

    return dict(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho, vanna=vanna, charm=charm)


def enrich_chain(
    clean_chain: pd.DataFrame,
    spot: float,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> pd.DataFrame:
    """Enrich chain with derived columns and Greeks.

    Args:
        clean_chain: Cleaned chain DataFrame
        spot: Current spot price
        r: Risk-free rate
        q: Dividend yield

    Returns:
        pd.DataFrame: Enriched chain
    """
    df = clean_chain.copy()

    if df.empty:
        return df

    # Time to expiration
    df['tte'] = df['expiration'].apply(lambda x: time_to_expiration(x))

    # Mid-market IV
    df[['mid_iv', 'iv_source']] = df.apply(
        lambda row: pd.Series(mid_iv(row, r, spot)),
        axis=1
    )

    # Moneyness
    df['moneyness'] = df['strike'] / spot
    df['log_moneyness'] = np.log(df['strike'] / spot)

    # Intrinsic value
    df['intrinsic'] = df.apply(
        lambda row: max(spot - row['strike'], 0) if row['option_type'] == 'call' else max(row['strike'] - spot, 0),
        axis=1
    )

    # Is OTM flag
    df['is_otm'] = df.apply(
        lambda row: row['strike'] > spot if row['option_type'] == 'call' else row['strike'] < spot,
        axis=1
    )

    # Compute Greeks
    iv_col = 'impliedvolatility' if 'impliedvolatility' in df.columns else 'implied_volatility'
    if iv_col not in df.columns:
        iv_col = 'mid_iv'

    greeks_data = []
    for _, row in df.iterrows():
        iv = row.get(iv_col, 0) if pd.notna(row.get(iv_col)) else 0
        g = _bs_greeks(spot, row['strike'], row['tte'], r, float(iv), row['option_type'])
        greeks_data.append(g)

    greeks_df = pd.DataFrame(greeks_data)
    for col in ['delta', 'gamma', 'theta', 'vega', 'rho', 'vanna', 'charm']:
        df[col] = greeks_df[col]

    return df


def get_enriched_chain(
    symbol: str,
    expirations: tuple[str, ...],
    spot: float,
    risk_free_rate: float = RISK_FREE_RATE,
    dividend_yield: float = DIVIDEND_YIELD,
) -> pd.DataFrame:
    """Fetch, clean, and enrich option chain. [Cached version].

    Args:
        symbol: Ticker symbol
        expirations: Tuple of ISO date strings (hashable)
        spot: Spot price
        risk_free_rate: Risk-free rate
        dividend_yield: Dividend yield

    Returns:
        pd.DataFrame: Enriched chain
    """
    raw = load_raw_chain(symbol, list(expirations))
    clean = clean_chain(raw)
    enriched = enrich_chain(clean, spot, risk_free_rate, dividend_yield)
    return enriched
