import numpy as np
import pandas as pd
from typing import Dict, Any, List

def calculate_backtest_metrics(
    trades: List[Dict[str, Any]],
    equity_curve: pd.Series,
    initial_capital: float
) -> Dict[str, Any]:
    """
    Computes professional quantitative performance metrics for the backtest.
    """
    if equity_curve.empty:
        return {
            "initial_capital": initial_capital,
            "final_equity": initial_capital,
            "net_profit": 0.0,
            "total_return_pct": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "avg_trade_pnl": 0.0
        }

    final_equity = float(equity_curve.iloc[-1])
    net_profit = final_equity - initial_capital
    total_return_pct = (net_profit / initial_capital) * 100.0
    
    # Trade statistics
    trade_pnls = [t["net_pnl"] for t in trades]
    winning_trades = [p for p in trade_pnls if p > 0]
    losing_trades = [p for p in trade_pnls if p <= 0]
    
    total_trades = len(trades)
    win_rate = (len(winning_trades) / total_trades * 100.0) if total_trades > 0 else 0.0
    
    gross_profit = sum(winning_trades)
    gross_loss = abs(sum(losing_trades))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    
    # Drawdown
    running_max = equity_curve.cummax().clip(lower=initial_capital)
    drawdown = (equity_curve - running_max) / running_max
    max_drawdown_pct = abs(float(drawdown.min())) * 100.0
    
    # Returns series for Sharpe ratio
    returns = equity_curve.pct_change().dropna()
    if len(returns) > 1 and returns.std() > 0:
        # Annualized Sharpe (assuming hourly bars ~ 24*365 = 8760 periods/year)
        periods = 8760.
        if isinstance(equity_curve.index, pd.DatetimeIndex) and len(equity_curve) > 1:
            hours = equity_curve.index.to_series().diff().dropna().dt.total_seconds().median() / 3600
            if hours > 0:
                periods = 8760. / hours
        sharpe_ratio = float(np.sqrt(periods) * returns.mean() / returns.std())
    else:
        sharpe_ratio = 0.0
        
    avg_pnl = float(np.mean(trade_pnls)) if trade_pnls else 0.0
    
    return {
        "initial_capital": round(initial_capital, 2),
        "final_equity": round(final_equity, 2),
        "net_profit": round(net_profit, 2),
        "total_return_pct": round(total_return_pct, 2),
        "total_trades": total_trades,
        "winning_trades": len(winning_trades),
        "losing_trades": len(losing_trades),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "avg_trade_pnl": round(avg_pnl, 2)
    }
