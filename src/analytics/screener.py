import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional
from statsmodels.tsa.stattools import adfuller, coint
from loguru import logger

from src.api.bybit_client import BybitClient
from src.analytics.hedge_ratio import calculate_ols_hedge_ratio, KalmanHedgeRatio
from src.analytics.spread_calculator import calculate_spread, calculate_zscore, calculate_half_life

class PairScreener:
    """
    Quantitative Screener for Cointegrated Pairs on Bybit Linear Perpetuals.
    Evaluates:
      1. Cointegration p-value (Engle-Granger / Augmented Dickey-Fuller)
      2. Pearson Correlation
      3. Dynamic and Static Hedge Ratio
      4. Ornstein-Uhlenbeck Half-Life
      5. Current Z-Score
    """
    def __init__(self, client: BybitClient):
        self.client = client

    def analyze_pair(
        self,
        symbol_a: str,
        symbol_b: str,
        interval: str = "60",
        total_bars: int = 1440,
        z_score_window: int = 48
    ) -> Optional[Dict[str, Any]]:
        """
        Analyzes a single candidate pair (symbol_a, symbol_b).
        """
        import time
        time.sleep(0.3)  # Rate-limit cooldown to avoid Bybit IP burst 403
        logger.info(f"Analyzing candidate pair {symbol_a} - {symbol_b} ({total_bars} bars, {interval}m)...")
        
        # 1. Fetch historical data
        df_a = self.client.get_historical_klines(symbol_a, interval=interval, total_bars=total_bars)
        df_b = self.client.get_historical_klines(symbol_b, interval=interval, total_bars=total_bars)
        
        if df_a.empty or df_b.empty:
            logger.warning(f"Insufficient data for {symbol_a} or {symbol_b}")
            return None
            
        # 2. Merge on timestamp to ensure exact synchrony
        merged = pd.merge(
            df_a[["timestamp", "close"]].rename(columns={"close": "close_a"}),
            df_b[["timestamp", "close"]].rename(columns={"close": "close_b"}),
            on="timestamp",
            how="inner"
        ).sort_values("timestamp").reset_index(drop=True)
        
        if len(merged) < 100:
            logger.warning(f"Not enough overlapping bars ({len(merged)}) for {symbol_a}-{symbol_b}")
            return None
            
        price_a = merged["close_a"]
        price_b = merged["close_b"]
        
        # 3. Correlation
        correlation = float(price_a.corr(price_b))
        
        # 4. OLS Hedge Ratio
        alpha_ols, beta_ols = calculate_ols_hedge_ratio(price_a, price_b)
        
        # 5. Kalman Dynamic Hedge Ratio
        kalman = KalmanHedgeRatio(delta=1e-4, vt=1e-3)
        kalman_df = kalman.fit_series(price_a, price_b)
        current_beta_kalman = float(kalman_df["beta"].iloc[-1])
        current_alpha_kalman = float(kalman_df["alpha"].iloc[-1])
        
        # Spread using Kalman
        spread_series = kalman_df["spread"]
        
        # 6. Augmented Dickey-Fuller Cointegration Test on spread
        # H0: non-stationary (no mean reversion). Low p-value (< 0.05) rejects H0 -> cointegrated!
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            adf_res = adfuller(spread_series.dropna(), autolag="AIC")
        adf_stat = float(adf_res[0])
        p_value = float(adf_res[1])
        crit_values = adf_res[4]
        
        # 7. Ornstein-Uhlenbeck Half-Life
        half_life_bars = calculate_half_life(spread_series)
        bar_hours = int(interval) / 60.0 if interval.isdigit() else 1.0
        half_life_days = (half_life_bars * bar_hours) / 24.0 if np.isfinite(half_life_bars) else float('inf')
        
        # 8. Current Z-Score
        z_scores = calculate_zscore(spread_series, window=z_score_window)
        current_z_score = float(z_scores.iloc[-1]) if not pd.isna(z_scores.iloc[-1]) else 0.0
        
        # Current prices
        last_price_a = float(price_a.iloc[-1])
        last_price_b = float(price_b.iloc[-1])
        
        is_cointegrated = bool(p_value < 0.05 and half_life_days < 14.0)
        
        return {
            "pair": f"{symbol_a}/{symbol_b}",
            "symbol_a": symbol_a,
            "symbol_b": symbol_b,
            "price_a": last_price_a,
            "price_b": last_price_b,
            "correlation": round(correlation, 4),
            "p_value": round(p_value, 5),
            "adf_stat": round(adf_stat, 3),
            "is_cointegrated": is_cointegrated,
            "beta_ols": round(beta_ols, 4),
            "beta_kalman": round(current_beta_kalman, 4),
            "half_life_bars": round(half_life_bars, 1),
            "half_life_days": round(half_life_days, 2) if np.isfinite(half_life_days) else None,
            "current_z_score": round(current_z_score, 3),
            "total_bars": len(merged)
        }

    def screen_pair_list(
        self,
        candidate_pairs: List[Dict[str, str]],
        interval: str = "60",
        total_bars: int = 1440
    ) -> pd.DataFrame:
        """
        Screens a list of pairs and returns a ranked summary DataFrame.
        """
        results = []
        for item in candidate_pairs:
            sym_a = item["leg1"]
            sym_b = item["leg2"]
            sector = item.get("sector", "Crypto")
            
            res = self.analyze_pair(sym_a, sym_b, interval=interval, total_bars=total_bars)
            if res:
                res["sector"] = sector
                results.append(res)
                
        if not results:
            return pd.DataFrame()
            
        df = pd.DataFrame(results)
        # Rank by p_value ascending (most cointegrated first)
        df = df.sort_values(by=["is_cointegrated", "p_value"], ascending=[False, True]).reset_index(drop=True)
        return df
