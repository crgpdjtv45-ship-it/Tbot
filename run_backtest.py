import sys
import argparse
from pathlib import Path

# Fix Windows console UTF-8 encoding
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from tabulate import tabulate
from loguru import logger
import matplotlib.pyplot as plt

from src.api.bybit_client import BybitClient
from src.backtesting.engine import PairBacktestEngine
from src.utils.helpers import load_yaml_config

def main():
    parser = argparse.ArgumentParser(description="Bybit Statistical Arbitrage Backtester")
    parser.add_argument("--pair", type=str, default="SOLUSDT/AVAXUSDT", help="Pair to backtest, e.g. SOLUSDT/AVAXUSDT")
    parser.add_argument("--days", type=int, default=60, help="Lookback days (default: 60)")
    parser.add_argument("--timeframe", type=str, default="60", help="Candle timeframe in minutes (default: 60)")
    parser.add_argument("--capital", type=float, default=10000.0, help="Initial capital in USDT (default: 10000)")
    parser.add_argument("--entry-z", type=float, default=2.0, help="Z-score entry threshold (default: 2.0)")
    parser.add_argument("--exit-z", type=float, default=0.2, help="Z-score exit threshold (default: 0.2)")
    parser.add_argument("--stop-z", type=float, default=3.8, help="Z-score stop loss threshold (default: 3.8)")
    parser.add_argument("--plot", action="store_true", help="Save equity curve chart as image")
    args = parser.parse_args()

    symbols = args.pair.split("/")
    if len(symbols) != 2:
        print("Error: Pair format must be LEG1/LEG2, e.g. SOLUSDT/AVAXUSDT")
        return
        
    symbol_a, symbol_b = symbols[0].strip().upper(), symbols[1].strip().upper()

    print("\n" + "="*70)
    print(f" BYBIT STATARBITRAGE BACKTEST — {symbol_a} / {symbol_b}")
    print("="*70)
    print(f" Lookback: {args.days} days | Timeframe: {args.timeframe}m | Capital: ${args.capital:,.2f}")
    print(f" Strategy: Entry |Z| >= {args.entry_z} | Exit |Z| <= {args.exit_z} | Stop |Z| >= {args.stop_z}")
    print("="*70 + "\n")

    client = BybitClient(testnet=False)
    
    # Calculate correct bars per day
    tf_minutes = int(args.timeframe) if args.timeframe.isdigit() else 60
    bars_per_day = max(1, 1440 // tf_minutes)
    total_bars = int(args.days * bars_per_day)
    logger.info(f"Downloading historical candles for {symbol_a} and {symbol_b} ({total_bars} bars, {bars_per_day} bars/day)...")
    
    df_a = client.get_historical_klines(symbol_a, interval=args.timeframe, total_bars=total_bars)
    df_b = client.get_historical_klines(symbol_b, interval=args.timeframe, total_bars=total_bars)
    
    if df_a.empty or df_b.empty:
        logger.error(f"Failed to load historical candles for {symbol_a} or {symbol_b}")
        return

    engine = PairBacktestEngine(
        initial_capital=args.capital,
        allocated_pct=25.0,     # Allocate 25% of account per pair
        leverage=2.0,           # Conservative 2x leverage
        entry_z=args.entry_z,
        exit_z=args.exit_z,
        stop_z=args.stop_z,
        fee_rate=0.00055,       # Bybit taker fee (0.055%)
        slippage=0.0005,        # 0.05% slippage
        z_window=48
    )

    logger.info("Executing quantitative backtest simulation...")
    results = engine.run(symbol_a, symbol_b, df_a, df_b)
    metrics = results["metrics"]
    trades = results["trades"]

    # Metrics display
    metrics_table = [
        ["Initial Capital", f"${metrics['initial_capital']:,.2f}"],
        ["Final Equity", f"${metrics['final_equity']:,.2f}"],
        ["Net Profit ($)", f"${metrics['net_profit']:,.2f}"],
        ["Total Return (%)", f"{metrics['total_return_pct']:+.2f}%"],
        ["Total Trades", metrics["total_trades"]],
        ["Winning / Losing", f"{metrics['winning_trades']} / {metrics['losing_trades']}"],
        ["Win Rate (%)", f"{metrics['win_rate_pct']:.1f}%"],
        ["Profit Factor", f"{metrics['profit_factor']:.2f}"],
        ["Max Drawdown (%)", f"{metrics['max_drawdown_pct']:.2f}%"],
        ["Sharpe Ratio", f"{metrics['sharpe_ratio']:.2f}"],
        ["Average Trade PnL", f"${metrics['avg_trade_pnl']:+,.2f}"]
    ]

    print("\n📊 BACKTEST PERFORMANCE SUMMARY:")
    print(tabulate(metrics_table, headers=["Metric", "Value"], tablefmt="grid"))

    # Trades table (last 10 trades)
    if trades:
        print(f"\n📋 TRADE LOG (Showing {min(10, len(trades))} of {len(trades)} trades):")
        trade_rows = []
        for t in trades[-10:]:
            trade_rows.append([
                str(t["entry_time"])[:16],
                str(t["exit_time"])[:16],
                t["side"].replace("_", " "),
                f"{t['entry_z']:+.2f}",
                f"{t['exit_z']:+.2f}",
                f"${t['gross_pnl']:+,.2f}",
                f"${t['total_fees']:,.2f}",
                f"${t['net_pnl']:+,.2f}",
                f"{t['return_pct']:+.2f}%",
                t["exit_reason"]
            ])
        print(tabulate(
            trade_rows,
            headers=["Entry", "Exit", "Direction", "Entry Z", "Exit Z", "Gross PnL", "Fees", "Net PnL", "Return", "Exit Reason"],
            tablefmt="grid"
        ))

    # Optional plot saving
    if args.plot and not results["equity_curve"].empty:
        plot_path = Path("backtest_equity.png")
        plt.figure(figsize=(12, 6))
        plt.plot(results["equity_curve"].index, results["equity_curve"].values, label="Equity ($)", color="#00bcd4", linewidth=1.5)
        plt.axhline(args.capital, color="gray", linestyle="--", alpha=0.7, label="Initial Capital")
        plt.title(f"Statistical Arbitrage Backtest: {symbol_a}/{symbol_b}", fontsize=14)
        plt.xlabel("Date")
        plt.ylabel("Portfolio Equity ($)")
        plt.grid(True, alpha=0.3)
        plt.legend()
        plt.tight_layout()
        plt.savefig(plot_path)
        plt.close()
        print(f"\n📈 Equity curve saved to {plot_path.resolve()}")

if __name__ == "__main__":
    main()
