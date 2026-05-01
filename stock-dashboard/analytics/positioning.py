"""Positioning features: Put/Call Ratio, Max Pain, OI by Strike, Volume-to-OI."""

import logging
import numpy as np
import pandas as pd
from analytics.helpers import find_atm_strike

logger = logging.getLogger(__name__)

PCR_BEARISH_THRESHOLD = 1.2
PCR_BULLISH_THRESHOLD = 0.7


def _sum_by_type(
    chain: pd.DataFrame,
    column: str,
    option_type: str,
    expiration: str | None = None,
) -> float:
    """Sum a column by option type, optionally filtered by expiration."""
    filtered = chain[chain['option_type'] == option_type]
    if expiration:
        filtered = filtered[filtered['expiration'] == expiration]

    return filtered[column].sum()


def put_call_ratio(
    enriched_chain: pd.DataFrame,
    method: str = "volume",
    per_expiration: bool = False,
) -> dict | pd.Series:
    """Put/Call ratio (sentiment indicator).

    Args:
        enriched_chain: Enriched option chain
        method: 'volume' or 'open_interest'
        per_expiration: If True, return Series per expiration; else aggregate dict

    Returns:
        dict: {'pcr': float, 'method': str, 'call_total': float, 'put_total': float}
        or pd.Series: index=expirations, values=PCR ratios
    """
    if enriched_chain.empty:
        return {'pcr': np.nan, 'method': method, 'call_total': 0, 'put_total': 0}

    if per_expiration:
        expirations = enriched_chain['expiration'].unique()
        ratios = {}

        for exp in expirations:
            put_sum = _sum_by_type(enriched_chain, method, 'put', exp)
            call_sum = _sum_by_type(enriched_chain, method, 'call', exp)

            if call_sum == 0:
                ratio = float('inf')
            else:
                ratio = put_sum / call_sum if put_sum > 0 else 0.0

            ratios[exp] = ratio

        return pd.Series(ratios, name=f'pcr_{method}')

    else:
        put_total = _sum_by_type(enriched_chain, method, 'put')
        call_total = _sum_by_type(enriched_chain, method, 'call')

        if call_total == 0:
            pcr = float('inf')
        else:
            pcr = put_total / call_total if put_total > 0 else 0.0

        return {
            'pcr': pcr,
            'method': method,
            'call_total': call_total,
            'put_total': put_total,
        }


def max_pain(
    enriched_chain: pd.DataFrame,
    expiration: str,
    contract_multiplier: int = 100,
) -> dict:
    """Max pain calculation.

    Args:
        enriched_chain: Enriched chain
        expiration: ISO date string
        contract_multiplier: Shares per contract (typically 100)

    Returns:
        dict: {'max_pain_strike': float, 'pain_curve': DataFrame, 'expiration': str}
    """
    exp_chain = enriched_chain[enriched_chain['expiration'] == expiration]

    if exp_chain.empty:
        return {
            'max_pain_strike': None,
            'pain_curve': pd.DataFrame(),
            'expiration': expiration,
        }

    calls = exp_chain[exp_chain['option_type'] == 'call']
    puts = exp_chain[exp_chain['option_type'] == 'put']

    strikes = sorted(exp_chain['strike'].unique())
    if len(strikes) == 0:
        return {
            'max_pain_strike': None,
            'pain_curve': pd.DataFrame(),
            'expiration': expiration,
        }

    pain_data = []

    for target_strike in strikes:
        call_pain = 0.0
        put_pain = 0.0

        # Call pain
        for _, call_row in calls.iterrows():
            intrinsic = max(target_strike - call_row['strike'], 0) * contract_multiplier
            call_pain += call_row['openinterest'] * intrinsic

        # Put pain
        for _, put_row in puts.iterrows():
            intrinsic = max(put_row['strike'] - target_strike, 0) * contract_multiplier
            put_pain += put_row['openinterest'] * intrinsic

        total_pain = call_pain + put_pain
        pain_data.append({
            'strike': target_strike,
            'call_pain': call_pain,
            'put_pain': put_pain,
            'total_pain': total_pain,
        })

    pain_df = pd.DataFrame(pain_data)

    if pain_df.empty or pain_df['total_pain'].sum() == 0:
        return {
            'max_pain_strike': None,
            'pain_curve': pain_df,
            'expiration': expiration,
        }

    max_pain_strike = pain_df.loc[pain_df['total_pain'].idxmin(), 'strike']

    return {
        'max_pain_strike': max_pain_strike,
        'pain_curve': pain_df,
        'expiration': expiration,
    }


def oi_by_strike(
    enriched_chain: pd.DataFrame,
    spot: float,
    strike_range_pct: float = 0.20,
    expiration: str | None = None,
    wall_multiplier: float = 3.0,
) -> dict:
    """Open interest by strike with wall detection.

    Args:
        enriched_chain: Enriched chain
        spot: Spot price
        strike_range_pct: Percentage range around spot
        expiration: Optional filter to single expiration
        wall_multiplier: OI threshold multiplier

    Returns:
        dict: OI table, walls, threshold
    """
    if enriched_chain.empty:
        return {
            'oi_table': pd.DataFrame(),
            'call_walls': [],
            'put_walls': [],
            'wall_threshold': 0.0,
        }

    chain = enriched_chain.copy()
    if expiration:
        chain = chain[chain['expiration'] == expiration]

    # Filter by strike range
    lower = spot * (1 - strike_range_pct)
    upper = spot * (1 + strike_range_pct)
    chain = chain[(chain['strike'] >= lower) & (chain['strike'] <= upper)]

    if len(chain) < 5:
        lower = spot * 0.7
        upper = spot * 1.3
        chain = enriched_chain[(enriched_chain['strike'] >= lower) & (enriched_chain['strike'] <= upper)]
        logger.warning(f"Expanded strike range to ±30% for {len(chain)} contracts")

    # Aggregate by strike
    strike_oi = {}
    for _, row in chain.iterrows():
        strike = row['strike']
        if strike not in strike_oi:
            strike_oi[strike] = {'call': 0, 'put': 0}

        strike_oi[strike][row['option_type']] += row['openinterest']

    # Build table
    rows = []
    all_oi = []
    for strike in sorted(strike_oi.keys()):
        call_oi = strike_oi[strike]['call']
        put_oi = strike_oi[strike]['put']
        net_oi = call_oi - put_oi

        all_oi.extend([call_oi, put_oi])
        rows.append({
            'strike': strike,
            'call_oi': call_oi,
            'put_oi': put_oi,
            'net_oi': net_oi,
        })

    if not rows or not all_oi:
        return {
            'oi_table': pd.DataFrame(),
            'call_walls': [],
            'put_walls': [],
            'wall_threshold': 0.0,
        }

    oi_df = pd.DataFrame(rows)
    wall_threshold = np.median(all_oi) * wall_multiplier

    oi_df['is_call_wall'] = oi_df['call_oi'] > wall_threshold
    oi_df['is_put_wall'] = oi_df['put_oi'] > wall_threshold

    call_walls = oi_df[oi_df['is_call_wall']]['strike'].tolist()
    put_walls = oi_df[oi_df['is_put_wall']]['strike'].tolist()

    return {
        'oi_table': oi_df,
        'call_walls': call_walls,
        'put_walls': put_walls,
        'wall_threshold': wall_threshold,
    }


def volume_oi_ratio(
    enriched_chain: pd.DataFrame,
    voir_threshold: float = 1.0,
    voir_min_volume: int = 100,
    top_n: int = 20,
    otm_only: bool = False,
) -> dict:
    """Volume-to-OI ratio (position opening detection).

    Args:
        enriched_chain: Enriched chain
        voir_threshold: VOIR threshold
        voir_min_volume: Minimum volume to flag
        top_n: Top N rows to return
        otm_only: Filter to OTM only

    Returns:
        dict: voir_table, flagged, summary
    """
    if enriched_chain.empty:
        return {
            'voir_table': pd.DataFrame(),
            'flagged': pd.DataFrame(),
            'summary': {
                'n_flagged': 0,
                'total_flagged_volume': 0,
                'dominant_type': 'none',
            },
        }

    df = enriched_chain.copy()

    if otm_only:
        df = df[df['is_otm']]

    # Compute VOIR
    df['voir'] = df.apply(
        lambda row: float('inf') if row['openinterest'] == 0 else row['volume'] / row['openinterest'],
        axis=1
    )

    # Mark zero OI
    df['is_oi_zero'] = df['openinterest'] == 0

    # New position flag
    df['is_new_position'] = (
        (df['voir'] > voir_threshold) &
        (df['is_otm']) &
        (df['volume'] > voir_min_volume) &
        (~df['is_oi_zero'])
    )

    # Sort and limit
    df_sorted = df[~df['is_oi_zero']].sort_values('voir', ascending=False).head(top_n)

    # Flagged subset
    flagged = df[df['is_new_position']].sort_values('voir', ascending=False)

    summary_type = 'none'
    if not flagged.empty:
        call_flagged = (flagged['option_type'] == 'call').sum()
        put_flagged = (flagged['option_type'] == 'put').sum()
        summary_type = 'call' if call_flagged > put_flagged else 'put'

    return {
        'voir_table': df_sorted[[
            'strike', 'option_type', 'expiration', 'volume', 'openinterest',
            'voir', 'is_oi_zero', 'is_new_position'
        ]],
        'flagged': flagged[[
            'strike', 'option_type', 'expiration', 'volume', 'openinterest',
            'voir', 'is_new_position'
        ]] if not flagged.empty else pd.DataFrame(),
        'summary': {
            'n_flagged': len(flagged),
            'total_flagged_volume': flagged['volume'].sum() if not flagged.empty else 0,
            'dominant_type': summary_type,
        },
    }
