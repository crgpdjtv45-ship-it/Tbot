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
        from src.runtime import fit_model, PaperSettings
        import time
        try:
            settings = PaperSettings(timeframe=str(interval))
            frames = []
            for symbol in (symbol_a, symbol_b):
                frame = self.client.get_historical_klines(symbol, interval=interval, total_bars=total_bars)
                if frame.empty:
                    return None
                frame = frame[["timestamp", "close"]].copy()
                frame["timestamp"] = pd.to_datetime(frame["timestamp"]).astype("int64") / 1e9
                frame = frame.loc[frame.timestamp + int(interval) * 60 <= time.time()]
                frames.append(frame)
            model = fit_model(*frames, settings)
            pa, pb = float(frames[0].close.iloc[-1]), float(frames[1].close.iloc[-1])
            return {
                "pair": f"{symbol_a}/{symbol_b}", "symbol_a": symbol_a, "symbol_b": symbol_b,
                "price_a": pa, "price_b": pb, "correlation": model["correlation"],
                "p_value": model["p_value"], "is_cointegrated": model["is_cointegrated"],
                "alpha_ols": model["alpha"], "beta_ols": model["beta"],
                "beta_kalman": model["beta"],  # compatibility alias; this model is fixed OLS
                "half_life_days": model["half_life"],
                "half_life_bars": model["half_life"] * 1440 / int(interval) if model["half_life"] else None,
                "current_z_score": (pa - model["alpha"] - model["beta"] * pb - model["mean"]) / model["std"],
                "total_bars": min(map(len, frames)), "hedge_ratio_method": "ols",
            }
        except Exception as exc:
            logger.warning("Pair {} / {} unavailable: {}", symbol_a, symbol_b, exc)
            return None

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
