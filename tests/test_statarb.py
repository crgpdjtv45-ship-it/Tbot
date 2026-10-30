import pytest
import numpy as np
import pandas as pd

from src.utils.helpers import round_step_size, round_tick_size
from src.analytics.hedge_ratio import calculate_ols_hedge_ratio, KalmanHedgeRatio
from src.analytics.spread_calculator import calculate_spread, calculate_zscore, calculate_half_life
from src.backtesting.engine import PairBacktestEngine

def test_helpers_rounding():
    # Step size rounding (floor to step)
    assert round_step_size(1.23456, 0.01) == 1.23
    assert round_step_size(0.00789, 0.001) == 0.007
    assert round_step_size(15.9, 5.0) == 15.0

    # Tick size rounding (nearest tick)
    assert round_tick_size(123.456, 0.05) == 123.45
    assert round_tick_size(123.48, 0.05) == 123.50

def test_ols_and_kalman_hedge_ratio():
    np.random.seed(42)
    # Asset B is random walk
    b = np.cumsum(np.random.randn(200)) + 50.0
    # Asset A is 2.5 * B + stationary noise
    noise = np.random.randn(200) * 0.5
    a = 2.5 * b + 10.0 + noise
    
    series_a = pd.Series(a)
    series_b = pd.Series(b)
    
    alpha_ols, beta_ols = calculate_ols_hedge_ratio(series_a, series_b)
    assert abs(beta_ols - 2.5) < 0.1
    assert abs(alpha_ols - 10.0) < 1.0
    
    # Test Kalman filter
    kalman = KalmanHedgeRatio(delta=1e-5, vt=1e-3)
    df_res = kalman.fit_series(series_a, series_b)
    final_beta = df_res["beta"].iloc[-1]
    assert abs(final_beta - 2.5) < 0.3

def test_spread_and_zscore():
    series = pd.Series([10.0, 10.0, 10.0, 10.0, 10.0, 20.0])
    z = calculate_zscore(series, window=5)
    # Last element is outlier (+20 vs mean of 10)
    assert z.iloc[-1] > 1.5

def test_half_life():
    # Mean-reverting AR(1) process: x_t = 0.5 * x_{t-1} + e_t
    np.random.seed(42)
    x = [0.0]
    for _ in range(300):
        x.append(0.7 * x[-1] + np.random.randn())
    hl = calculate_half_life(pd.Series(x))
    # For rho = 0.7, half life = -ln(2) / ln(0.7) ~ 1.94 bars
    assert 1.0 < hl < 5.0

def test_backtest_synthetic_reversion():
    dates = pd.date_range("2026-01-01", periods=200, freq="h")
    # Perfectly correlated pair with 1 large divergence and reversion
    p_b = 100.0 + np.sin(np.linspace(0, 10, 200)) * 5
    p_a = 100.0 + np.sin(np.linspace(0, 10, 200)) * 5
    # Inject temporary deviation at bar 100-110
    p_a[100:105] += 15.0
    
    df_a = pd.DataFrame({"timestamp": dates, "close": p_a})
    df_b = pd.DataFrame({"timestamp": dates, "close": p_b})
    
    engine = PairBacktestEngine(
        initial_capital=10000.0,
        allocated_pct=20.0,
        leverage=1.0,
        entry_z=1.5,
        exit_z=0.2,
        stop_z=5.0,
        z_window=30
    )
    res = engine.run("TESTA", "TESTB", df_a, df_b)
    assert "metrics" in res
    assert "trades" in res
    assert len(res["trades"]) >= 1
