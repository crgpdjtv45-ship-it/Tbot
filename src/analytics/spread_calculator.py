import numpy as np
import pandas as pd
from typing import Optional, Tuple
import statsmodels.api as sm

def calculate_spread(price_a: pd.Series, price_b: pd.Series, beta: float, alpha: float = 0.0) -> pd.Series:
    """
    Computes the cointegrated spread series:
    Spread = Price_A - (alpha + beta * Price_B)
    """
    return price_a - (alpha + beta * price_b)

def calculate_zscore(spread: pd.Series, window: int = 48) -> pd.Series:
    """
    Computes the rolling Z-Score of the spread:
    Z = (Spread - Rolling_Mean) / Rolling_Std
    """
    roll_mean = spread.rolling(window=window, min_periods=max(5, window // 2)).mean()
    roll_std = spread.rolling(window=window, min_periods=max(5, window // 2)).std()
    
    # Avoid zero-division by replacing 0 or near-0 std with NaN
    roll_std = roll_std.replace(0, np.nan)
    z_score = (spread - roll_mean) / roll_std
    return z_score

def calculate_half_life(spread: pd.Series) -> float:
    """
    Calculates the Mean Reversion Half-Life via Ornstein-Uhlenbeck process:
    dy(t) = -lambda * (y(t-1) - mu) * dt + sigma * dW
    Linear regression: Delta(Spread_t) = alpha + beta * Spread_{t-1}
    Half-life = -ln(2) / beta
    
    Returns:
        Half-life in number of bars. Returns inf if series does not revert to mean (beta >= 0).
    """
    clean_spread = spread.dropna()
    if len(clean_spread) < 20:
        return float('inf')
        
    lag = clean_spread.shift(1).dropna()
    delta = (clean_spread - lag).dropna()
    
    # Align indices
    lag = lag.loc[delta.index]
    
    x = sm.add_constant(lag.values)
    model = sm.OLS(delta.values, x).fit()
    
    # beta is the slope on lag
    beta = model.params[1]
    
    if beta >= 0:
        # Not mean-reverting (trending or random walk)
        return float('inf')
        
    half_life = -np.log(2) / beta
    return float(half_life)
