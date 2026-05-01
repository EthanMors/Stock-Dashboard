"""Flow features: Gamma Exposure, Charm and Vanna."""

import logging
import numpy as np
import pandas as pd
from analytics.helpers import find_atm_strike

logger = logging.getLogger(__name__)


def gamma_exposure(
    enriched_chain: pd.DataFrame,
    spot: float,
    shares_per_contract: int = 100,
    assume_dealer_short: bool = True,
) -> dict:
    """Gamma Exposure (dealer perspective).

    Args:
        enriched_chain: Enriched chain
        spot: Spot price
        shares_per_contract: Contract multiplier
        assume_dealer_short: Assume dealers are net short options

    Returns:
        dict: GEX profile, regime, flip strike
    """
    if enriched_chain.empty or spot <= 0:
        return {
            'gex_net': 0.0,
            'gex_net_mm': 0.0,
            'gex_profile': pd.DataFrame(),
            'regime': None,
            'gex_flip_strike': None,
            'call_gex_mm': 0.0,
            'put_gex_mm': 0.0,
        }

    df = enriched_chain.copy()

    # Filter out near-zero gamma
    df = df[df['gamma'].notna() & (df['gamma'].abs() > 1e-8)]

    if df.empty:
        return {
            'gex_net': 0.0,
            'gex_net_mm': 0.0,
            'gex_profile': pd.DataFrame(),
            'regime': None,
            'gex_flip_strike': None,
            'call_gex_mm': 0.0,
            'put_gex_mm': 0.0,
        }

    # Compute GEX per row
    # dealer_GEX = -OI * gamma * shares_per_contract * spot^2
    df['dollar_gamma'] = df['gamma'] * df['openinterest'] * shares_per_contract * (spot ** 2)

    if assume_dealer_short:
        df['dealer_gex'] = -df['dollar_gamma']
    else:
        df['dealer_gex'] = df['dollar_gamma']

    # Profile by strike
    gex_by_strike = df.groupby('strike')['dealer_gex'].sum()

    profile_data = []
    cumulative_gex = 0.0
    gex_flip = None

    for strike in sorted(gex_by_strike.index):
        gex_val = gex_by_strike[strike]
        cumulative_gex += gex_val

        profile_data.append({
            'strike': strike,
            'gex': gex_val / 1e6,  # Convert to millions
            'cumulative_gex': cumulative_gex / 1e6,
        })

        # Detect flip
        if gex_flip is None and cumulative_gex > 0:
            gex_flip = strike

    gex_profile = pd.DataFrame(profile_data)

    # Total GEX
    gex_net = df['dealer_gex'].sum()
    gex_net_mm = gex_net / 1e6

    # By type
    call_gex = df[df['option_type'] == 'call']['dealer_gex'].sum() / 1e6
    put_gex = df[df['option_type'] == 'put']['dealer_gex'].sum() / 1e6

    # Regime
    if gex_net > 0:
        regime = 'positive_gex'
    elif gex_net < 0:
        regime = 'negative_gex'
    else:
        regime = 'neutral'

    return {
        'gex_net': gex_net,
        'gex_net_mm': gex_net_mm,
        'gex_profile': gex_profile,
        'regime': regime,
        'gex_flip_strike': gex_flip,
        'call_gex_mm': call_gex,
        'put_gex_mm': put_gex,
    }


def charm_surface(
    enriched_chain: pd.DataFrame,
    spot: float,
    shares_per_contract: int = 100,
    trading_days_per_year: int = 252,
    max_charm_cap: float = 10.0,
) -> dict:
    """Charm exposure (delta decay flows).

    Args:
        enriched_chain: Enriched chain
        spot: Spot price
        shares_per_contract: Contract multiplier
        trading_days_per_year: Days per year for daily flow calc
        max_charm_cap: Cap charm at this absolute value

    Returns:
        dict: Charm profile, total daily flow, direction
    """
    if enriched_chain.empty:
        return {
            'charm_profile': pd.DataFrame(),
            'total_daily_flow_shares': 0.0,
            'flow_direction': 'neutral',
            'peak_charm_strike': None,
        }

    df = enriched_chain.copy()

    # Cap charm to prevent numerical instability
    df['charm_capped'] = df['charm'].clip(-max_charm_cap, max_charm_cap)

    # Charm exposure = -charm * OI * shares_per_contract
    df['charm_exposure'] = -df['charm_capped'] * df['openinterest'] * shares_per_contract

    # Daily flow
    df['daily_flow_shares'] = df['charm_exposure'] / trading_days_per_year

    # Profile by strike
    profile_data = []
    total_daily = 0.0

    for strike in sorted(df['strike'].unique()):
        strike_rows = df[df['strike'] == strike]
        charm_exp = strike_rows['charm_exposure'].sum()
        daily_flow = strike_rows['daily_flow_shares'].sum()

        profile_data.append({
            'strike': strike,
            'charm_exposure': charm_exp,
            'daily_flow_shares': daily_flow,
        })

        total_daily += daily_flow

    profile_df = pd.DataFrame(profile_data)

    # Direction
    if total_daily > 0:
        direction = 'buy'
    elif total_daily < 0:
        direction = 'sell'
    else:
        direction = 'neutral'

    # Peak charm strike
    peak_strike = None
    if not profile_df.empty:
        peak_idx = profile_df['daily_flow_shares'].abs().idxmax()
        peak_strike = profile_df.loc[peak_idx, 'strike']

    return {
        'charm_profile': profile_df,
        'total_daily_flow_shares': total_daily,
        'flow_direction': direction,
        'peak_charm_strike': peak_strike,
    }


def vanna_surface(
    enriched_chain: pd.DataFrame,
    spot: float,
    shares_per_contract: int = 100,
    vol_move_pct: float = 0.01,
) -> dict:
    """Vanna exposure (vol sensitivity flows).

    Args:
        enriched_chain: Enriched chain
        spot: Spot price
        shares_per_contract: Contract multiplier
        vol_move_pct: IV move to compute flow for (as decimal, e.g. 0.01 for 1%)

    Returns:
        dict: Vanna profile, total exposure, direction
    """
    if enriched_chain.empty:
        return {
            'vanna_profile': pd.DataFrame(),
            'total_vanna_exposure': 0.0,
            'total_flow_per_vol_pct': 0.0,
            'vanna_direction': 'neutral',
        }

    df = enriched_chain.copy()

    # Vanna exposure = -vanna * OI * shares_per_contract
    df['vanna_exposure'] = -df['vanna'] * df['openinterest'] * shares_per_contract

    # Flow per vol move
    df['flow_per_vol_pct'] = df['vanna_exposure'] * vol_move_pct

    # Profile by strike
    profile_data = []
    total_exposure = 0.0
    total_flow = 0.0

    for strike in sorted(df['strike'].unique()):
        strike_rows = df[df['strike'] == strike]
        vanna_exp = strike_rows['vanna_exposure'].sum()
        flow = strike_rows['flow_per_vol_pct'].sum()

        profile_data.append({
            'strike': strike,
            'vanna_exposure': vanna_exp,
            'flow_per_vol_pct': flow,
        })

        total_exposure += vanna_exp
        total_flow += flow

    profile_df = pd.DataFrame(profile_data)

    # Direction
    if total_flow > 0:
        direction = 'amplifying'
    elif total_flow < 0:
        direction = 'dampening'
    else:
        direction = 'neutral'

    return {
        'vanna_profile': profile_df,
        'total_vanna_exposure': total_exposure,
        'total_flow_per_vol_pct': total_flow,
        'vanna_direction': direction,
    }


def charm_vanna_summary(
    enriched_chain: pd.DataFrame,
    spot: float,
    shares_per_contract: int = 100,
) -> dict:
    """Combined charm and vanna summary.

    Args:
        enriched_chain: Enriched chain
        spot: Spot price
        shares_per_contract: Contract multiplier

    Returns:
        dict: Combined results from both surfaces
    """
    charm_result = charm_surface(enriched_chain, spot, shares_per_contract)
    vanna_result = vanna_surface(enriched_chain, spot, shares_per_contract)

    return {
        'charm': charm_result,
        'vanna': vanna_result,
    }
