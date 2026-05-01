"""Sentiment features: Risk Reversal, Unusual Options Activity."""

import logging
import numpy as np
import pandas as pd
from analytics.helpers import interpolate_iv_at_delta

logger = logging.getLogger(__name__)


def risk_reversal(
    enriched_chain: pd.DataFrame,
    expiration: str,
    deltas: list = None,
) -> dict:
    """Risk Reversal (25-delta call - 25-delta put).

    Args:
        enriched_chain: Enriched chain
        expiration: ISO date
        deltas: Delta magnitudes to compute (default: [0.25, 0.10])

    Returns:
        dict: Risk reversal values, sentiment classification
    """
    if deltas is None:
        deltas = [0.25, 0.10]

    exp_chain = enriched_chain[enriched_chain['expiration'] == expiration]

    if exp_chain.empty:
        return {
            'rr_25': None,
            'rr_10': None,
            'iv_25c': None,
            'iv_25p': None,
            'iv_10c': None,
            'iv_10p': None,
            'expiration': expiration,
            'sentiment': None,
            'warnings': ['Empty chain for expiration'],
        }

    calls = exp_chain[exp_chain['option_type'] == 'call']
    puts = exp_chain[exp_chain['option_type'] == 'put']

    warnings = []
    results = {}

    for delta in deltas:
        iv_c = interpolate_iv_at_delta(calls, delta, 'call')
        iv_p = interpolate_iv_at_delta(puts, delta, 'put')

        if iv_c is None or iv_p is None:
            warnings.append(f"Could not interpolate IV at {delta}-delta")
            rr = None
        else:
            rr = iv_c - iv_p

        if delta == 0.25:
            results['rr_25'] = rr
            results['iv_25c'] = iv_c
            results['iv_25p'] = iv_p
        elif delta == 0.10:
            results['rr_10'] = rr
            results['iv_10c'] = iv_c
            results['iv_10p'] = iv_p

    # Sentiment classification
    rr_25 = results.get('rr_25')
    if rr_25 is None:
        sentiment = None
    elif rr_25 > 0.02:
        sentiment = 'bullish'
    elif rr_25 < -0.02:
        sentiment = 'bearish'
    else:
        sentiment = 'neutral'

    return {
        'rr_25': results.get('rr_25'),
        'rr_10': results.get('rr_10'),
        'iv_25c': results.get('iv_25c'),
        'iv_25p': results.get('iv_25p'),
        'iv_10c': results.get('iv_10c'),
        'iv_10p': results.get('iv_10p'),
        'expiration': expiration,
        'sentiment': sentiment,
        'warnings': warnings,
    }


def unusual_activity(
    enriched_chain: pd.DataFrame,
    spike_multiplier: float = 3.0,
    min_volume: int = 500,
    top_n: int = 25,
    historical_avg_volume: pd.DataFrame | None = None,
    exclude_dte_days: int = 1,
) -> dict:
    """Unusual options activity detection.

    Args:
        enriched_chain: Enriched chain
        spike_multiplier: Volume spike threshold
        min_volume: Minimum volume to flag
        top_n: Top N contracts to return
        historical_avg_volume: Historical avg volume (optional)
        exclude_dte_days: Exclude contracts with DTE <= this

    Returns:
        dict: Unusual contracts, counts, stats
    """
    if enriched_chain.empty:
        return {
            'n_unusual': 0,
            'total_unusual_notional': 0.0,
            'dominant_type': 'none',
            'rule_counts': {'volume_spike': 0, 'oi_breach': 0, 'both': 0},
            'using_proxy_avg': False,
            'unusual_df': pd.DataFrame(),
        }

    df = enriched_chain.copy()

    # Exclude near-expiry contracts
    df = df[df['tte'] * 365.25 > exclude_dte_days]

    if df.empty:
        return {
            'n_unusual': 0,
            'total_unusual_notional': 0.0,
            'dominant_type': 'none',
            'rule_counts': {'volume_spike': 0, 'oi_breach': 0, 'both': 0},
            'using_proxy_avg': False,
            'unusual_df': pd.DataFrame(),
        }

    # Average volume baseline
    if historical_avg_volume is None:
        avg_volume = df['volume'].median()
        using_proxy = True
    else:
        df = df.merge(
            historical_avg_volume.rename('avg_volume'),
            left_on=['strike', 'option_type', 'expiration'],
            right_index=True,
            how='left'
        )
        avg_volume = df['avg_volume'].fillna(df['volume'].median())
        using_proxy = False

    # Rule 1: Volume spike
    df['is_volume_spike'] = df['volume'] > spike_multiplier * avg_volume

    # Rule 2: OI breach
    df['is_oi_breach'] = (
        (df['volume'] > df['openinterest']) &
        (df['is_otm']) &
        (df['volume'] > min_volume)
    )

    # Combined flag
    df['is_unusual'] = df['is_volume_spike'] | df['is_oi_breach']

    # Score for ranking
    df['score'] = df['volume'] / np.maximum(df['openinterest'], 1) * (1 + df['is_oi_breach'].astype(int))

    # Notional value
    df['notional'] = df['volume'] * df['lastprice'] * 100

    # Get unusual contracts
    unusual = df[df['is_unusual']].copy()

    if unusual.empty:
        return {
            'n_unusual': 0,
            'total_unusual_notional': 0.0,
            'dominant_type': 'none',
            'rule_counts': {'volume_spike': 0, 'oi_breach': 0, 'both': 0},
            'using_proxy_avg': using_proxy,
            'unusual_df': pd.DataFrame(),
        }

    unusual = unusual.sort_values('score', ascending=False).head(top_n)

    # Statistics
    both_flags = ((unusual['is_volume_spike']) & (unusual['is_oi_breach'])).sum()
    spike_only = ((unusual['is_volume_spike']) & ~(unusual['is_oi_breach'])).sum()
    oi_only = (~(unusual['is_volume_spike']) & (unusual['is_oi_breach'])).sum()

    call_count = (unusual['option_type'] == 'call').sum()
    put_count = (unusual['option_type'] == 'put').sum()
    dominant_type = 'call' if call_count > put_count else 'put'

    return {
        'n_unusual': len(unusual),
        'total_unusual_notional': unusual['notional'].sum(),
        'dominant_type': dominant_type,
        'rule_counts': {
            'volume_spike': spike_only + both_flags,
            'oi_breach': oi_only + both_flags,
            'both': both_flags,
        },
        'using_proxy_avg': using_proxy,
        'unusual_df': unusual[[
            'strike', 'option_type', 'expiration', 'volume', 'openinterest',
            'score', 'notional', 'is_volume_spike', 'is_oi_breach', 'delta'
        ]],
    }
