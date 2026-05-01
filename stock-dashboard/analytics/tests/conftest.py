"""Pytest configuration and fixtures for analytics tests."""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta
from scipy.stats import norm

# Test baseline: April 30, 2026
TEST_DATE = datetime(2026, 4, 30, 12, 0, 0)
SPOT = 100.0
EXPIRY = "2026-06-20"  # 51 days out
RISK_FREE_RATE = 0.045


def _bs_greeks(S, K, T, r, sigma, opt_type):
    """Black-Scholes Greeks for test fixture."""
    if T <= 1e-6 or sigma <= 1e-6 or S <= 0 or K <= 0:
        return {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0, 'rho': 0, 'vanna': 0, 'charm': 0}

    sqrt_T = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    pdf_d1 = norm.pdf(d1)
    cdf_d1 = norm.cdf(d1)

    gamma = pdf_d1 / (S * sigma * sqrt_T)
    vega = S * pdf_d1 * sqrt_T / 100
    vanna = -pdf_d1 * d2 / sigma if sigma != 0 else 0
    charm = -(pdf_d1 * (2 * r * T - d2 * sigma * sqrt_T)) / (2 * T * sigma * sqrt_T) if T * sigma > 0 else 0

    if opt_type == 'call':
        delta = cdf_d1
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrt_T) - r * np.exp(-r * T) * norm.cdf(d2)) / 365
        rho = T * np.exp(-r * T) * norm.cdf(d2) / 100
    else:
        delta = cdf_d1 - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2 * sqrt_T) + r * np.exp(-r * T) * norm.cdf(-d2)) / 365
        rho = -T * np.exp(-r * T) * norm.cdf(-d2) / 100

    return {'delta': delta, 'gamma': gamma, 'theta': theta, 'vega': vega, 'rho': rho, 'vanna': vanna, 'charm': charm}


@pytest.fixture
def synthetic_chain():
    """Create a synthetic option chain for testing."""
    strikes = [80, 85, 90, 95, 100, 105, 110, 115, 120]
    T = (pd.Timestamp(EXPIRY) - pd.Timestamp(TEST_DATE)).days / 365.25

    rows = []

    for strike in strikes:
        for opt_type in ['call', 'put']:
            # Base IV: calls at 0.30, puts at 0.35 (skew)
            iv = 0.30 if opt_type == 'call' else 0.35

            # Open interest: high at 95 and 105 (walls)
            if strike in [95, 105]:
                oi = 10000
            else:
                oi = 1000

            # Volume: one special case at strike 110 calls for OI breach test
            if strike == 110 and opt_type == 'call':
                volume = 5000
            else:
                volume = np.random.randint(50, 500)

            # Compute Greeks
            greeks = _bs_greeks(SPOT, strike, T, RISK_FREE_RATE, iv, opt_type)

            # Last price (approximate)
            if opt_type == 'call':
                intrinsic = max(SPOT - strike, 0)
            else:
                intrinsic = max(strike - SPOT, 0)

            last_price = intrinsic + greeks['vega'] * 0.1  # Rough approximation
            last_price = max(0.01, last_price)

            row = {
                'strike': strike,
                'lastprice': last_price,
                'bid': max(0.01, last_price - 0.05),
                'ask': last_price + 0.05,
                'volume': volume,
                'openinterest': oi,
                'impliedvolatility': iv,
                'option_type': opt_type,
                'expiration': EXPIRY,
                'tte': T,
                'mid_iv': iv,
                'iv_source': 'last',
                'moneyness': strike / SPOT,
                'log_moneyness': np.log(strike / SPOT),
                'intrinsic': intrinsic,
                'is_otm': (strike > SPOT) if opt_type == 'call' else (strike < SPOT),
                'delta': greeks['delta'],
                'gamma': greeks['gamma'],
                'theta': greeks['theta'],
                'vega': greeks['vega'],
                'rho': greeks['rho'],
                'vanna': greeks['vanna'],
                'charm': greeks['charm'],
            }
            rows.append(row)

    df = pd.DataFrame(rows)
    return df


@pytest.fixture
def synthetic_historical_iv():
    """Create synthetic historical IV series for IV Rank/Percentile tests."""
    # 252 days of IV data, normally distributed around 0.30
    dates = pd.date_range(end=TEST_DATE, periods=252, freq='D')
    iv_values = np.random.normal(0.30, 0.05, 252)
    iv_values = np.clip(iv_values, 0.10, 0.60)  # Reasonable bounds

    return pd.Series(iv_values, index=dates, name='iv')


@pytest.fixture
def test_date():
    """Provide test baseline date."""
    return TEST_DATE
