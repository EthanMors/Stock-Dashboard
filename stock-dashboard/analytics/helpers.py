"""Shared utility functions for analytics features."""

import logging
import numpy as np
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)


def time_to_expiration(
    expiry: str | datetime,
    now: datetime | None = None,
    min_t: float = 1 / (365.25 * 24),
) -> float:
    """Convert expiration date to fractional year (T).

    Args:
        expiry: ISO date string or datetime object
        now: Reference time (defaults to datetime.utcnow())
        min_t: Minimum T value (clamped to avoid division by zero)

    Returns:
        float: Time to expiration in fractional years, >= min_t
    """
    if now is None:
        now = datetime.utcnow()

    # Parse expiry
    if isinstance(expiry, str):
        expiry_dt = pd.Timestamp(expiry, tz="UTC").to_pydatetime()
    else:
        expiry_dt = expiry

    # Ensure UTC
    if now.tzinfo is None:
        now = now.replace(tzinfo=None)
    if hasattr(expiry_dt, 'tzinfo') and expiry_dt.tzinfo is not None:
        expiry_dt = expiry_dt.replace(tzinfo=None)

    td = expiry_dt - now
    t = td.total_seconds() / (365.25 * 24 * 3600)

    if t < 0:
        logger.warning(f"Expiration {expiry} is in the past")
        return min_t

    return max(t, min_t)


def find_atm_strike(
    strikes: pd.Series | np.ndarray | list,
    spot: float,
) -> float:
    """Find the strike closest to spot price.

    Args:
        strikes: Series or array of strike prices
        spot: Current spot price

    Returns:
        float: Strike price closest to spot (lower one if tied)
    """
    if isinstance(strikes, (list, np.ndarray)):
        strikes = pd.Series(strikes)

    strikes_unique = strikes.unique()
    if len(strikes_unique) == 0:
        raise ValueError("Empty strike array")

    diff = np.abs(strikes_unique - spot)
    min_idx = np.argmin(diff)
    atm = strikes_unique[min_idx]

    # If tied, return lower
    if min_idx > 0 and diff[min_idx] == diff[min_idx - 1]:
        return min(strikes_unique[min_idx], strikes_unique[min_idx - 1])

    return float(atm)


def interpolate_iv_at_delta(
    chain_slice: pd.DataFrame,
    target_delta: float,
    option_type: str,
    min_iv: float = 1e-4,
) -> float | None:
    """Interpolate IV at a target delta.

    Args:
        chain_slice: DataFrame slice with 'delta' and 'mid_iv' columns
        target_delta: Target delta (magnitude, sign applied internally)
        option_type: 'call' or 'put'
        min_iv: Floor for IV

    Returns:
        float or None: Interpolated IV, or None if target delta outside range
    """
    # Normalize target delta
    if option_type == 'put':
        target_delta = -abs(target_delta)
    else:
        target_delta = abs(target_delta)

    # Find IV column (try multiple names)
    iv_col = None
    for col in ['mid_iv', 'impliedvolatility', 'impliedVolatility', 'implied_volatility']:
        if col in chain_slice.columns:
            iv_col = col
            break

    if iv_col is None:
        return None

    # Filter and sort
    valid = chain_slice[
        (chain_slice['delta'].notna()) &
        (chain_slice[iv_col].notna()) &
        (chain_slice[iv_col] >= min_iv)
    ].copy()

    if len(valid) < 2:
        return None

    # Sort by delta
    valid = valid.sort_values('delta')
    deltas = valid['delta'].values
    ivs = valid[iv_col].values

    # Check bounds
    if option_type == 'put':
        if target_delta < deltas.min() or target_delta > deltas.max():
            return None
    else:
        if target_delta < deltas.min() or target_delta > deltas.max():
            return None

    # Find bracketing deltas
    idx_hi = np.searchsorted(deltas, target_delta)
    if idx_hi == 0 or idx_hi >= len(deltas):
        return None

    idx_lo = idx_hi - 1
    delta_lo, delta_hi = deltas[idx_lo], deltas[idx_hi]
    iv_lo, iv_hi = ivs[idx_lo], ivs[idx_hi]

    # Linear interpolation
    w = (target_delta - delta_lo) / (delta_hi - delta_lo)
    return iv_lo + w * (iv_hi - iv_lo)


def build_iv_surface(
    enriched_chain: pd.DataFrame,
    option_type: str = "call",
) -> pd.DataFrame:
    """Build IV surface (pivoted by strike and expiration).

    Args:
        enriched_chain: Full multi-expiry enriched chain
        option_type: 'call' or 'put'

    Returns:
        pd.DataFrame: IV surface indexed by strike, columns by expiration
    """
    # Filter by option type
    filtered = enriched_chain[enriched_chain['option_type'] == option_type].copy()

    # For duplicate (strike, expiration), keep higher volume
    filtered = filtered.sort_values('volume', ascending=False)
    filtered = filtered.drop_duplicates(['strike', 'expiration'], keep='first')

    # Check minimum valid points per expiration
    exp_counts = filtered.groupby('expiration').size()
    valid_exps = exp_counts[exp_counts >= 3].index

    if len(valid_exps) < len(exp_counts):
        invalid_exps = exp_counts[exp_counts < 3].index.tolist()
        logger.warning(f"Expirations {invalid_exps} have < 3 valid IV points, excluding")

    filtered = filtered[filtered['expiration'].isin(valid_exps)]

    if filtered.empty:
        return pd.DataFrame()

    # Pivot
    surface = filtered.pivot_table(
        index='strike',
        columns='expiration',
        values='mid_iv',
        aggfunc='first',
    )

    return surface


def nearest_expiration(
    expirations: list[str],
    target_days: int = 30,
    now: datetime | None = None,
) -> str:
    """Find expiration nearest to target_days away.

    Args:
        expirations: List of ISO date strings
        target_days: Target days to expiration
        now: Reference time

    Returns:
        str: ISO date of nearest expiration
    """
    if not expirations:
        raise ValueError("Empty expiration list")

    if now is None:
        now = datetime.utcnow()

    target_seconds = target_days * 24 * 3600
    min_diff = float('inf')
    nearest = expirations[0]

    for exp_str in expirations:
        exp_dt = pd.Timestamp(exp_str, tz="UTC").to_pydatetime()
        if hasattr(exp_dt, 'tzinfo') and exp_dt.tzinfo is not None:
            exp_dt = exp_dt.replace(tzinfo=None)

        diff = abs((exp_dt - now).total_seconds() - target_seconds)
        if diff < min_diff:
            min_diff = diff
            nearest = exp_str

    return nearest


def mid_iv(
    row: pd.Series,
    risk_free_rate: float = 0.05,
    spot: float | None = None,
) -> tuple[float | None, str]:
    """Compute mid-market implied volatility.

    Args:
        row: Option chain row (Series)
        risk_free_rate: Risk-free rate (unused but kept for signature compatibility)
        spot: Spot price (unused but kept for signature compatibility)

    Returns:
        tuple: (iv_value, source_label) where source is 'mid' or 'last'
    """
    # Try to get bid/ask IVs (yfinance doesn't directly expose these,
    # so we fall back to impliedVolatility which is market-derived)
    implied_vol = row.get('impliedVolatility')

    if pd.isna(implied_vol) or implied_vol == 0:
        return None, 'missing'

    return float(implied_vol), 'last'
