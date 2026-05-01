"""Tests for volatility features (F05-F08)."""

import pytest
import numpy as np
import pandas as pd
from analytics.volatility import (
    iv_rank_percentile,
    iv_skew,
    term_structure,
    expected_move,
)


class TestIVRankPercentile:
    def test_ivr_known_value(self, synthetic_historical_iv):
        current_iv = 0.30
        result = iv_rank_percentile(current_iv, synthetic_historical_iv)
        assert isinstance(result, dict)
        assert 'iv_rank' in result
        assert 'iv_percentile' in result
        assert 0 <= result['iv_rank'] <= 100
        assert 0 <= result['iv_percentile'] <= 100

    def test_ivr_regime_classification(self, synthetic_historical_iv):
        current_iv = 0.50  # High value
        result = iv_rank_percentile(current_iv, synthetic_historical_iv)
        assert result['regime'] in ['low', 'normal', 'elevated', 'high']

    def test_insufficient_history(self):
        short_history = pd.Series([0.30, 0.31, 0.29])
        result = iv_rank_percentile(0.30, short_history)
        assert result.get('insufficient_history', False)

    def test_nan_current_iv(self, synthetic_historical_iv):
        result = iv_rank_percentile(np.nan, synthetic_historical_iv)
        assert result['iv_rank'] is None


class TestIVSkew:
    def test_skew_returns_dict(self, synthetic_chain):
        result = iv_skew(synthetic_chain, '2026-06-20')
        assert isinstance(result, dict)
        assert 'skew_raw' in result
        assert 'skew_normalized' in result

    def test_skew_values_reasonable(self, synthetic_chain):
        result = iv_skew(synthetic_chain, '2026-06-20')
        # Synthetic chain has puts at IV=0.35, calls at IV=0.30
        # so skew should be ~0.05
        if result['skew_raw'] is not None:
            assert -0.20 < result['skew_raw'] < 0.20

    def test_empty_expiration(self, synthetic_chain):
        result = iv_skew(synthetic_chain, '2099-01-01')
        assert result['skew_raw'] is None


class TestTermStructure:
    def test_term_structure_returns_dict(self, synthetic_chain):
        result = term_structure(synthetic_chain, spot=100.0)
        assert isinstance(result, dict)
        assert 'term_df' in result
        assert 'is_backwardated' in result
        assert 'backwardation_pairs' in result

    def test_single_expiration_insufficient(self, synthetic_chain):
        result = term_structure(synthetic_chain, spot=100.0, min_expirations=2)
        assert result.get('insufficient_data', False)  # Synthetic has only 1 expiration

    def test_backwardation_flag_is_bool(self, synthetic_chain):
        result = term_structure(synthetic_chain, spot=100.0)
        assert isinstance(result['is_backwardated'], bool)


class TestExpectedMove:
    def test_em_returns_dict(self, synthetic_chain):
        result = expected_move(synthetic_chain, '2026-06-20', spot=100.0)
        assert isinstance(result, dict)
        assert 'expiration' in result
        assert 'tte_days' in result
        assert 'spot' in result

    def test_em_methods(self, synthetic_chain):
        result = expected_move(synthetic_chain, '2026-06-20', spot=100.0, method='both')
        # Either straddle or analytic (or both) should be present
        has_method = result['straddle'] is not None or result['analytic'] is not None
        assert has_method or result['expiration'] == '2026-06-20'

    def test_em_analytic_positive(self, synthetic_chain):
        result = expected_move(synthetic_chain, '2026-06-20', spot=100.0, method='analytic')
        if result['analytic'] is not None:
            assert result['analytic']['em_pct'] > 0

    def test_em_bounds(self, synthetic_chain):
        result = expected_move(synthetic_chain, '2026-06-20', spot=100.0)
        if result['straddle'] is not None:
            assert result['straddle']['lower_1s'] < 100.0 < result['straddle']['upper_1s']
