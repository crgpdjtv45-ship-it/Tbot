import numpy as np
import pandas as pd
from typing import Tuple, Optional
import statsmodels.api as sm

def calculate_ols_hedge_ratio(price_a: pd.Series, price_b: pd.Series) -> Tuple[float, float]:
    """
    Ordinary Least Squares (OLS) estimation:
    price_a = alpha + beta * price_b + error
    Returns:
        (alpha, beta) where beta is the hedge ratio.
    """
    x = sm.add_constant(price_b)
    model = sm.OLS(price_a, x).fit()
    alpha = float(model.params.iloc[0])
    beta = float(model.params.iloc[1])
    return alpha, beta


class KalmanHedgeRatio:
    """
    Online Dynamic Kalman Filter for adaptive Hedge Ratio estimation.
    Recursively updates state theta = [alpha, beta]^T on each new price candle.
    Prevents lookahead bias and responds smoothly to regime changes.
    """
    def __init__(self, delta: float = 1e-4, vt: float = 1e-3):
        """
        Args:
            delta: process noise parameter
            vt: measurement noise variance
        """
        self.delta = delta
        self.vt = vt
        
        # State vector [alpha, beta]^T
        self.theta = np.zeros(2)
        
        # State covariance matrix P
        self.P = np.eye(2)
        
        # Process noise covariance matrix Q
        self.Q = (self.delta / (1.0 - self.delta)) * np.eye(2)

    def update(self, price_a: float, price_b: float) -> Tuple[float, float]:
        """
        Processes a single price observation and updates alpha and beta.
        
        Args:
            price_a: Current price of Asset A (dependent)
            price_b: Current price of Asset B (independent)
            
        Returns:
            (alpha, beta) current estimated hedge parameters
        """
        # Observation vector x_t = [1, price_b]
        x = np.array([1.0, float(price_b)])
        
        # 1. Prediction step: P = P + Q
        self.P = self.P + self.Q
        
        # 2. Measurement residual: y_hat = x * theta, e = y - y_hat
        y_hat = np.dot(x, self.theta)
        error = float(price_a) - y_hat
        
        # 3. Variance of residual: F = x * P * x^T + vt
        f = np.dot(x, np.dot(self.P, x)) + self.vt
        
        # 4. Kalman Gain: K = P * x^T / F
        k = np.dot(self.P, x) / f
        
        # 5. State update: theta = theta + K * error
        self.theta = self.theta + k * error
        
        # 6. Covariance update: P = (I - K * x) * P
        self.P = self.P - np.outer(k, np.dot(x, self.P))
        
        alpha = float(self.theta[0])
        beta = float(self.theta[1])
        return alpha, beta

    def fit_series(self, series_a: pd.Series, series_b: pd.Series) -> pd.DataFrame:
        """
        Applies the Kalman filter across the full historical series.
        Returns a DataFrame with columns ['alpha', 'beta', 'spread'].
        """
        alphas = []
        betas = []
        spreads = []
        
        for pa, pb in zip(series_a, series_b):
            a, b = self.update(pa, pb)
            alphas.append(a)
            betas.append(b)
            # spread = price_a - (alpha + beta * price_b)
            spread = pa - (a + b * pb)
            spreads.append(spread)
            
        return pd.DataFrame({
            "alpha": alphas,
            "beta": betas,
            "spread": spreads
        }, index=series_a.index)
