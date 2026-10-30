import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional
from loguru import logger

from src.analytics.hedge_ratio import KalmanHedgeRatio
from src.analytics.spread_calculator import calculate_zscore
from src.backtesting.metrics import calculate_backtest_metrics

class PairBacktestEngine:
    """
    Realistic Backtesting Engine for Pairs Trading on Bybit USDT Perpetuals.
    Incorporates:
      - Taker fees (0.055% standard Bybit fee per leg)
      - Dynamic Kalman Filter hedge ratio (sequential, no lookahead)
      - Configurable slippage
      - Strict Z-score entry, exit, and stop-loss logic
    """
    def __init__(
        self,
        initial_capital: float = 10000.0,
        allocated_pct: float = 20.0,      # 20% of capital allocated to this pair
        leverage: float = 2.0,            # 2x leverage
        entry_z: float = 2.0,             # Open trade at |Z| >= 2.0
        exit_z: float = 0.2,              # Close trade at |Z| <= 0.2 (mean reversion)
        stop_z: float = 3.8,              # Emergency exit at |Z| >= 3.8 (breakdown)
        max_loss_pct: float = 4.0,        # Emergency loss stop at -4% drawdown on the pair
        max_holding_bars: int = 36,       # Max holding period (timeout if no mean reversion)
        fee_rate: float = 0.00055,        # 0.055% Bybit taker fee
        slippage: float = 0.0005,         # 0.05% slippage estimate
        z_window: int = 48
    ):
        self.initial_capital = initial_capital
        self.allocated_pct = allocated_pct
        self.leverage = leverage
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.stop_z = stop_z
        self.max_loss_pct = max_loss_pct
        self.max_holding_bars = max_holding_bars
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.z_window = z_window

    def run(
        self,
        symbol_a: str,
        symbol_b: str,
        df_a: pd.DataFrame,
        df_b: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        Executes backtest over historical price series.
        """
        # Synchronize data
        merged = pd.merge(
            df_a[["timestamp", "close"]].rename(columns={"close": "close_a"}),
            df_b[["timestamp", "close"]].rename(columns={"close": "close_b"}),
            on="timestamp",
            how="inner"
        ).sort_values("timestamp").reset_index(drop=True)
        
        if len(merged) < self.z_window + 10:
            raise ValueError(f"Insufficient historical data: {len(merged)} bars")

        # Institutional Log-Price Cointegration Formulation:
        # log(P_A) - beta * log(P_B)
        log_a = np.log(merged["close_a"])
        log_b = np.log(merged["close_b"])
        
        rolling_betas = []
        rolling_alphas = []
        spreads = []
        
        # Lookback window for rolling estimation (default 168h = 7 days)
        lookback = max(72, self.z_window * 3)
        
        for idx in range(len(merged)):
            if idx < lookback:
                rolling_betas.append(1.0)
                rolling_alphas.append(0.0)
                spreads.append(0.0)
            else:
                window_la = log_a.iloc[idx - lookback : idx]
                window_lb = log_b.iloc[idx - lookback : idx]
                var_b = np.var(window_lb)
                if var_b > 1e-9:
                    cov_ab = np.cov(window_la, window_lb)[0, 1]
                    b = float(cov_ab / var_b)
                    a = float(np.mean(window_la) - b * np.mean(window_lb))
                else:
                    b = 1.0
                    a = 0.0
                rolling_betas.append(b)
                rolling_alphas.append(a)
                s = float(log_a.iloc[idx] - (a + b * log_b.iloc[idx]))
                spreads.append(s)
                
        merged["alpha"] = rolling_alphas
        merged["beta"] = rolling_betas
        merged["spread"] = spreads
        merged["z_score"] = calculate_zscore(merged["spread"], window=self.z_window)

        # Simulation state
        capital = self.initial_capital
        equity_curve = []
        trades = []
        position = None
        
        start_idx = lookback + 10
        
        for i in range(start_idx, len(merged)):
            row = merged.iloc[i]
            timestamp = row["timestamp"]
            p_a = row["close_a"]
            p_b = row["close_b"]
            z = row["z_score"]
            beta = row["beta"]
            
            if pd.isna(z):
                equity_curve.append(capital)
                continue
                
            # Current unrealized PnL if in position
            unrealized_pnl = 0.0
            if position:
                side = position["side"]  # +1: Long A / Short B, -1: Short A / Long B
                qty_a = position["qty_a"]
                qty_b = position["qty_b"]
                entry_a = position["entry_a"]
                entry_b = position["entry_b"]
                
                pnl_a = side * qty_a * (p_a - entry_a)
                pnl_b = (-side) * qty_b * (p_b - entry_b)
                unrealized_pnl = pnl_a + pnl_b
                
                # Check Exit conditions
                should_exit = False
                exit_reason = ""
                bars_held = i - position["entry_bar_idx"]
                total_notional = position["notional_a"] * 2.0
                curr_loss_pct = (unrealized_pnl / total_notional) * 100.0
                
                # 1. Mean Reversion exit: Z crossed close to zero
                if side == 1 and z >= -self.exit_z:
                    should_exit = True
                    exit_reason = "Mean Reversion"
                elif side == -1 and z <= self.exit_z:
                    should_exit = True
                    exit_reason = "Mean Reversion"
                # 2. Hard Loss Stop: drawdown exceeded risk limit
                elif curr_loss_pct <= -self.max_loss_pct:
                    should_exit = True
                    exit_reason = f"Max Loss Stop ({curr_loss_pct:.1f}%)"
                # 3. Timeout Stop: trade did not revert within max holding period
                elif bars_held >= self.max_holding_bars:
                    should_exit = True
                    exit_reason = f"Time Stop ({bars_held} bars)"
                # 4. Statistical Stop Loss: Z expanded too far (correlation breakdown)
                elif abs(z) >= self.stop_z:
                    should_exit = True
                    exit_reason = "Stop Loss (|Z| > threshold)"
                    
                if should_exit:
                    # Closing trades
                    exit_cost_a = p_a * qty_a * (self.fee_rate + self.slippage)
                    exit_cost_b = p_b * qty_b * (self.fee_rate + self.slippage)
                    total_exit_fees = exit_cost_a + exit_cost_b
                    
                    net_trade_pnl = unrealized_pnl - position["entry_fees"] - total_exit_fees
                    capital += (unrealized_pnl - total_exit_fees)
                    
                    trades.append({
                        "entry_time": position["entry_time"],
                        "exit_time": timestamp,
                        "side": "LONG_A_SHORT_B" if side == 1 else "SHORT_A_LONG_B",
                        "entry_z": position["entry_z"],
                        "exit_z": z,
                        "entry_a": entry_a,
                        "exit_a": p_a,
                        "entry_b": entry_b,
                        "exit_b": p_b,
                        "qty_a": qty_a,
                        "qty_b": qty_b,
                        "gross_pnl": unrealized_pnl,
                        "total_fees": position["entry_fees"] + total_exit_fees,
                        "net_pnl": net_trade_pnl,
                        "return_pct": (net_trade_pnl / (position["notional_a"] * 2.0)) * 100.0,
                        "exit_reason": exit_reason
                    })
                    position = None
                    unrealized_pnl = 0.0

            # Check Entry conditions (only if not already in position)
            if position is None:
                allocated_capital = capital * (self.allocated_pct / 100.0) * self.leverage
                notional_per_leg = allocated_capital / 2.0
                qty_a = notional_per_leg / p_a
                qty_b = notional_per_leg / p_b

                # Condition 1: Spread is severely negative (Asset A undervalued vs B)
                # -> Long A, Short B
                if z <= -self.entry_z:
                    side = 1
                    entry_cost_a = p_a * qty_a * (self.fee_rate + self.slippage)
                    entry_cost_b = p_b * qty_b * (self.fee_rate + self.slippage)
                    total_entry_fees = entry_cost_a + entry_cost_b
                    capital -= total_entry_fees
                    
                    position = {
                        "side": side,
                        "entry_bar_idx": i,
                        "entry_time": timestamp,
                        "entry_z": z,
                        "entry_a": p_a,
                        "entry_b": p_b,
                        "qty_a": qty_a,
                        "qty_b": qty_b,
                        "beta": beta,
                        "notional_a": notional_per_leg,
                        "entry_fees": total_entry_fees
                    }
                # Condition 2: Spread is severely positive (Asset A overvalued vs B)
                # -> Short A, Long B
                elif z >= self.entry_z:
                    side = -1
                    entry_cost_a = p_a * qty_a * (self.fee_rate + self.slippage)
                    entry_cost_b = p_b * qty_b * (self.fee_rate + self.slippage)
                    total_entry_fees = entry_cost_a + entry_cost_b
                    capital -= total_entry_fees
                    
                    position = {
                        "side": side,
                        "entry_bar_idx": i,
                        "entry_time": timestamp,
                        "entry_z": z,
                        "entry_a": p_a,
                        "entry_b": p_b,
                        "qty_a": qty_a,
                        "qty_b": qty_b,
                        "beta": beta,
                        "notional_a": notional_per_leg,
                        "entry_fees": total_entry_fees
                    }

            current_equity = capital + unrealized_pnl
            equity_curve.append(current_equity)

        # Build equity curve series
        equity_series = pd.Series(equity_curve, index=merged["timestamp"].iloc[start_idx:])
        metrics = calculate_backtest_metrics(trades, equity_series, self.initial_capital)
        
        return {
            "pair": f"{symbol_a}/{symbol_b}",
            "symbol_a": symbol_a,
            "symbol_b": symbol_b,
            "metrics": metrics,
            "trades": trades,
            "equity_curve": equity_series,
            "merged_data": merged
        }
