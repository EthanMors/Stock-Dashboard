"""Volatility features: IV Rank, Skew, Term Structure, Expected Move."""

import logging
import numpy as np
import pandas as pd
from analytics.helpers import (
    find_atm_strike,
    interpolate_iv_at_delta,
    build_iv_surface,
    nearest_expiration,
    time_to_expiration,
)

logger = logging.getLogger(__name__)


def iv_rank_percentile(
    current_iv: float,
    historical_iv: pd.Series,
    lookback_days: int = 252,
    warn_if_short: bool = True,
) -> dict:
    """IV Rank and IV Percentile calculation.

    Args:
        current_iv: Today's ATM IV
        historical_iv: Series of daily IVs
        lookback_days: Lookback window
        warn_if_short: Warn if fewer than 200 days

    Returns:
        dict: IV rank, percentile, regime, etc.
    """
    if pd.isna(current_iv):
        return {
            'iv_rank': None,
            'iv_percentile': None,
            'iv_high': None,
            'iv_low': None,
            'current_iv': current_iv,
            'history_days': 0,
            'regime': None,
        }

    # Use recent history
    hist = historical_iv.dropna().tail(lookback_days)

    if len(hist) < 30:
        return {
            'iv_rank': None,
            'iv_percentile': None,
            'iv_high': None,
            'iv_low': None,
            'current_iv': current_iv,
            'history_days': len(hist),
            'regime': None,
            'insufficient_history': True,
        }

    iv_high = hist.max()
    iv_low = hist.min()

    # IV Rank
    if iv_high == iv_low:
        iv_rank = 50.0
        history_degenerate = True
    else:
        iv_rank = (current_iv - iv_low) / (iv_high - iv_low) * 100
        history_degenerate = False

    # IV Percentile
    iv_percentile = (hist < current_iv).sum() / len(hist) * 100

    # Regime classification
    if iv_rank < 25:
        regime = 'low'
    elif iv_rank < 50:
        regime = 'normal'
    elif iv_rank < 75:
        regime = 'elevated'
    else:
        regime = 'high'

    result = {
        'iv_rank': iv_rank,
        'iv_percentile': iv_percentile,
        'iv_high': iv_high,
        'iv_low': iv_low,
        'current_iv': current_iv,
        'history_days': len(hist),
        'regime': regime,
    }

    if history_degenerate:
        result['history_degenerate'] = True

    if warn_if_short and len(hist) < 200:
        logger.warning(f"Only {len(hist)} days of history; IV Rank may be unreliable")

    return result


def iv_skew(
    enriched_chain: pd.DataFrame,
    expiration: str,
    target_delta: float = 0.25,
    normalize: bool = True,
) -> dict:
    """IV Skew (25-delta put vs call).

    Args:
        enriched_chain: Enriched chain
        expiration: ISO date
        target_delta: Delta magnitude (0.25 for 25-delta)
        normalize: Normalize by ATM IV

    Returns:
        dict: skew values, IVs at each delta, warnings
    """
    exp_chain = enriched_chain[enriched_chain['expiration'] == expiration]

    if exp_chain.empty:
        return {
            'skew_raw': None,
            'skew_normalized': None,
            'iv_25p': None,
            'iv_25c': None,
            'iv_atm': None,
            'expiration': expiration,
            'warnings': ['Empty chain for expiration'],
        }

    calls = exp_chain[exp_chain['option_type'] == 'call']
    puts = exp_chain[exp_chain['option_type'] == 'put']

    # Get IVs at target delta
    iv_25c = interpolate_iv_at_delta(calls, target_delta, 'call')
    iv_25p = interpolate_iv_at_delta(puts, target_delta, 'put')

    warnings = []

    if iv_25c is None or iv_25p is None:
        warnings.append(f"Could not interpolate IV at {target_delta}-delta")

    # ATM IV
    spot = exp_chain['spot'].iloc[0] if 'spot' in exp_chain.columns else None
    if spot is None:
        # Infer from moneyness
        atm_rows = exp_chain[exp_chain['moneyness'].between(0.98, 1.02)]
        if atm_rows.empty:
            iv_atm = None
        else:
            iv_atm = atm_rows['mid_iv'].mean()
    else:
        atm_strike = find_atm_strike(exp_chain['strike'], spot)
        atm_rows = exp_chain[exp_chain['strike'] == atm_strike]
        iv_atm = atm_rows['mid_iv'].mean() if not atm_rows.empty else None

    skew_raw = None
    skew_normalized = None

    if iv_25p is not None and iv_25c is not None:
        skew_raw = iv_25p - iv_25c

        if normalize and iv_atm is not None and iv_atm > 0:
            skew_normalized = skew_raw / iv_atm
        elif normalize:
            warnings.append("Could not normalize: ATM IV missing")

    return {
        'skew_raw': skew_raw,
        'skew_normalized': skew_normalized,
        'iv_25p': iv_25p,
        'iv_25c': iv_25c,
        'iv_atm': iv_atm,
        'expiration': expiration,
        'warnings': warnings,
    }


def term_structure(
    enriched_chain: pd.DataFrame,
    spot: float,
    min_expirations: int = 3,
) -> dict:
    """Volatility term structure.

    Args:
        enriched_chain: Enriched chain (all expirations)
        spot: Spot price
        min_expirations: Minimum expirations for valid output

    Returns:
        dict: Term structure DataFrame, backwardation flag, kinks
    """
    if enriched_chain.empty:
        return {
            'term_df': pd.DataFrame(),
            'is_backwardated': False,
            'backwardation_pairs': [],
            'iv_30d': None,
            'front_iv': None,
            'back_iv': None,
        }

    expirations = sorted(enriched_chain['expiration'].unique())

    if len(expirations) < min_expirations:
        return {
            'term_df': pd.DataFrame(),
            'is_backwardated': False,
            'backwardation_pairs': [],
            'iv_30d': None,
            'front_iv': None,
            'back_iv': None,
            'insufficient_data': True,
        }

    rows = []

    for exp in expirations:
        exp_chain = enriched_chain[enriched_chain['expiration'] == exp]
        tte_days = exp_chain['tte'].iloc[0] * 365.25 if not exp_chain.empty else 0

        # Find ATM IV
        atm_strike = find_atm_strike(exp_chain['strike'], spot)
        atm_rows = exp_chain[exp_chain['strike'] == atm_strike]

        if not atm_rows.empty:
            iv_atm = atm_rows['mid_iv'].iloc[0]
        else:
            # Average two nearest
            sorted_strikes = sorted(exp_chain['strike'].unique())
            idx = next((i for i, s in enumerate(sorted_strikes) if s >= atm_strike), len(sorted_strikes) - 1)

            if idx > 0 and idx < len(sorted_strikes):
                iv_lo = exp_chain[exp_chain['strike'] == sorted_strikes[idx-1]]['mid_iv'].mean()
                iv_hi = exp_chain[exp_chain['strike'] == sorted_strikes[idx]]['mid_iv'].mean()
                iv_atm = (iv_lo + iv_hi) / 2 if not pd.isna(iv_lo) and not pd.isna(iv_hi) else None
            else:
                iv_atm = exp_chain['mid_iv'].mean()

        rows.append({
            'expiration': exp,
            'tte_days': tte_days,
            'iv_atm': iv_atm,
        })

    term_df = pd.DataFrame(rows)

    if term_df.empty or term_df['iv_atm'].isna().all():
        return {
            'term_df': term_df,
            'is_backwardated': False,
            'backwardation_pairs': [],
            'iv_30d': None,
            'front_iv': term_df['iv_atm'].iloc[0] if not term_df.empty else None,
            'back_iv': term_df['iv_atm'].iloc[-1] if not term_df.empty else None,
        }

    # Backwardation detection
    backwardation_pairs = []
    kinks = []

    for i in range(len(term_df) - 1):
        iv_curr = term_df.loc[i, 'iv_atm']
        iv_next = term_df.loc[i + 1, 'iv_atm']

        if pd.notna(iv_curr) and pd.notna(iv_next) and iv_curr > iv_next:
            backwardation_pairs.append((term_df.loc[i, 'expiration'], term_df.loc[i+1, 'expiration']))

    # Kink detection
    for i in range(1, len(term_df) - 1):
        iv_prev = term_df.loc[i-1, 'iv_atm']
        iv_curr = term_df.loc[i, 'iv_atm']
        iv_next = term_df.loc[i+1, 'iv_atm']

        if (pd.notna(iv_prev) and pd.notna(iv_curr) and pd.notna(iv_next) and
            iv_curr > iv_prev and iv_curr > iv_next):
            kinks.append(term_df.loc[i, 'expiration'])

    is_backwardated = len(backwardation_pairs) > 0

    # 30-day IV interpolation
    iv_30d = None
    for i in range(len(term_df) - 1):
        tte_lo = term_df.loc[i, 'tte_days']
        tte_hi = term_df.loc[i+1, 'tte_days']

        if tte_lo <= 30 <= tte_hi:
            iv_lo = term_df.loc[i, 'iv_atm']
            iv_hi = term_df.loc[i+1, 'iv_atm']

            if pd.notna(iv_lo) and pd.notna(iv_hi):
                w = (30 - tte_lo) / (tte_hi - tte_lo)
                iv_30d = iv_lo * (1 - w) + iv_hi * w
                break

    front_iv = term_df['iv_atm'].iloc[0] if not term_df.empty else None
    back_iv = term_df['iv_atm'].iloc[-1] if not term_df.empty else None

    result = {
        'term_df': term_df,
        'is_backwardated': is_backwardated,
        'backwardation_pairs': backwardation_pairs,
        'iv_30d': iv_30d,
        'front_iv': front_iv,
        'back_iv': back_iv,
    }

    if kinks:
        result['kinks'] = kinks

    return result


def expected_move(
    enriched_chain: pd.DataFrame,
    expiration: str,
    spot: float,
    method: str = "both",
    sigma_multiples: list = None,
) -> dict:
    """Expected move (straddle + analytic methods).

    Args:
        enriched_chain: Enriched chain
        expiration: ISO date
        spot: Spot price
        method: 'straddle', 'analytic', or 'both'
        sigma_multiples: List of sigma multiples to compute

    Returns:
        dict: Expected moves and bounds
    """
    if sigma_multiples is None:
        sigma_multiples = [1.0, 2.0]

    exp_chain = enriched_chain[enriched_chain['expiration'] == expiration]

    if exp_chain.empty:
        return {
            'expiration': expiration,
            'tte_days': None,
            'spot': spot,
            'straddle': None,
            'analytic': None,
        }

    tte_days = int(exp_chain['tte'].iloc[0] * 365.25) if not exp_chain.empty else 0

    # Find ATM
    atm_strike = find_atm_strike(exp_chain['strike'], spot)
    atm_call = exp_chain[(exp_chain['strike'] == atm_strike) & (exp_chain['option_type'] == 'call')]
    atm_put = exp_chain[(exp_chain['strike'] == atm_strike) & (exp_chain['option_type'] == 'put')]

    result = {
        'expiration': expiration,
        'tte_days': tte_days,
        'spot': spot,
        'straddle': None,
        'analytic': None,
    }

    # Straddle method
    if not atm_call.empty or not atm_put.empty:
        call_price = 0.0
        put_price = 0.0

        if not atm_call.empty:
            call_row = atm_call.iloc[0]
            call_price = call_row['lastprice'] if pd.notna(call_row.get('lastprice')) else (
                (call_row.get('bid', 0) + call_row.get('ask', 0)) / 2 if call_row.get('bid', 0) > 0 else 0
            )

        if not atm_put.empty:
            put_row = atm_put.iloc[0]
            put_price = put_row['lastprice'] if pd.notna(put_row.get('lastprice')) else (
                (put_row.get('bid', 0) + put_row.get('ask', 0)) / 2 if put_row.get('bid', 0) > 0 else 0
            )

        if call_price > 0 or put_price > 0:
            em_straddle = call_price + put_price
            em_straddle_pct = em_straddle / spot * 100 if spot > 0 else 0

            straddle_result = {
                'em_dollars': em_straddle,
                'em_pct': em_straddle_pct,
            }

            for mult in sigma_multiples:
                if mult == 1.0:
                    straddle_result['upper_1s'] = spot + em_straddle
                    straddle_result['lower_1s'] = spot - em_straddle
                elif mult == 2.0:
                    straddle_result['upper_2s'] = spot + 2 * em_straddle
                    straddle_result['lower_2s'] = spot - 2 * em_straddle

            result['straddle'] = straddle_result

    # Analytic method
    atm_iv = exp_chain['mid_iv'].mean() if not exp_chain.empty else None
    tte = exp_chain['tte'].iloc[0] if not exp_chain.empty else 0

    if atm_iv is not None and atm_iv > 0 and tte > 0:
        em_analytic = spot * atm_iv * np.sqrt(tte)
        em_analytic_pct = atm_iv * np.sqrt(tte) * 100

        analytic_result = {
            'em_dollars': em_analytic,
            'em_pct': em_analytic_pct,
        }

        for mult in sigma_multiples:
            if mult == 1.0:
                analytic_result['upper_1s'] = spot * np.exp(atm_iv * np.sqrt(tte))
                analytic_result['lower_1s'] = spot * np.exp(-atm_iv * np.sqrt(tte))
            elif mult == 2.0:
                analytic_result['upper_2s'] = spot * np.exp(2 * atm_iv * np.sqrt(tte))
                analytic_result['lower_2s'] = spot * np.exp(-2 * atm_iv * np.sqrt(tte))

        result['analytic'] = analytic_result

    return result
