"""Tests for flow features (F10, F12)."""

import pytest
import numpy as np
import pandas as pd
from analytics.flows import (
    gamma_exposure,
    charm_surface,
    vanna_surface,
    charm_vanna_summary,
)


class TestGammaExposure:
    def test_gex_returns_dict(self, synthetic_chain):
        result = gamma_exposure(synthetic_chain, spot=100.0)
        assert isinstance(result, dict)
        assert 'gex_net' in result
        assert 'gex_net_mm' in result
        assert 'gex_profile' in result
        assert 'regime' in result

    def test_gex_regime_valid(self, synthetic_chain):
        result = gamma_exposure(synthetic_chain, spot=100.0)
        assert result['regime'] in ['positive_gex', 'negative_gex', 'neutral', None]

    def test_gex_profile_is_dataframe(self, synthetic_chain):
        result = gamma_exposure(synthetic_chain, spot=100.0)
        assert isinstance(result['gex_profile'], pd.DataFrame)
        if not result['gex_profile'].empty:
            assert 'strike' in result['gex_profile'].columns
            assert 'gex' in result['gex_profile'].columns

    def test_gex_call_put_sum(self, synthetic_chain):
        result = gamma_exposure(synthetic_chain, spot=100.0)
        total = result['call_gex_mm'] + result['put_gex_mm']
        # Should approximately equal gex_net_mm (allowing for rounding)
        assert np.isclose(total, result['gex_net_mm'], atol=0.1)

    def test_zero_spot_returns_zero(self):
        df = pd.DataFrame({
            'gamma': [0.1, 0.2],
            'openinterest': [1000, 2000],
            'option_type': ['call', 'put'],
        })
        result = gamma_exposure(df, spot=0)
        assert result['gex_net'] == 0.0


class TestCharmSurface:
    def test_charm_returns_dict(self, synthetic_chain):
        result = charm_surface(synthetic_chain, spot=100.0)
        assert isinstance(result, dict)
        assert 'charm_profile' in result
        assert 'total_daily_flow_shares' in result
        assert 'flow_direction' in result

    def test_charm_direction_valid(self, synthetic_chain):
        result = charm_surface(synthetic_chain, spot=100.0)
        assert result['flow_direction'] in ['buy', 'sell', 'neutral']

    def test_charm_profile_is_dataframe(self, synthetic_chain):
        result = charm_surface(synthetic_chain, spot=100.0)
        assert isinstance(result['charm_profile'], pd.DataFrame)

    def test_charm_profile_columns(self, synthetic_chain):
        result = charm_surface(synthetic_chain, spot=100.0)
        profile = result['charm_profile']
        if not profile.empty:
            assert 'strike' in profile.columns
            assert 'charm_exposure' in profile.columns
            assert 'daily_flow_shares' in profile.columns


class TestVannaSurface:
    def test_vanna_returns_dict(self, synthetic_chain):
        result = vanna_surface(synthetic_chain, spot=100.0)
        assert isinstance(result, dict)
        assert 'vanna_profile' in result
        assert 'total_vanna_exposure' in result
        assert 'total_flow_per_vol_pct' in result
        assert 'vanna_direction' in result

    def test_vanna_direction_valid(self, synthetic_chain):
        result = vanna_surface(synthetic_chain, spot=100.0)
        assert result['vanna_direction'] in ['amplifying', 'dampening', 'neutral']

    def test_vanna_profile_is_dataframe(self, synthetic_chain):
        result = vanna_surface(synthetic_chain, spot=100.0)
        assert isinstance(result['vanna_profile'], pd.DataFrame)

    def test_vanna_profile_columns(self, synthetic_chain):
        result = vanna_surface(synthetic_chain, spot=100.0)
        profile = result['vanna_profile']
        if not profile.empty:
            assert 'strike' in profile.columns
            assert 'vanna_exposure' in profile.columns
            assert 'flow_per_vol_pct' in profile.columns


class TestCharmVannaSummary:
    def test_summary_returns_dict(self, synthetic_chain):
        result = charm_vanna_summary(synthetic_chain, spot=100.0)
        assert isinstance(result, dict)
        assert 'charm' in result
        assert 'vanna' in result

    def test_summary_charm_structure(self, synthetic_chain):
        result = charm_vanna_summary(synthetic_chain, spot=100.0)
        assert 'charm_profile' in result['charm']
        assert 'flow_direction' in result['charm']

    def test_summary_vanna_structure(self, synthetic_chain):
        result = charm_vanna_summary(synthetic_chain, spot=100.0)
        assert 'vanna_profile' in result['vanna']
        assert 'vanna_direction' in result['vanna']
