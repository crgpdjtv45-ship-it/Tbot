import json
import sys
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

from src.api.bybit_client import BybitClient
from src.analytics.screener import PairScreener
from src.utils.helpers import load_yaml_config

def main():
    print("\n" + "="*70)
    print(" BYBIT STATISTICAL ARBITRAGE — COINTEGRATION SCREENER")
    print("="*70 + "\n")
    
    config = load_yaml_config("configs/config.example.yaml")
    
    # Load pairs whitelist
    pairs_file = Path("configs/pairs.json")
    if not pairs_file.exists():
        logger.error("configs/pairs.json not found!")
        return
        
    with open(pairs_file, "r", encoding="utf-8") as f:
        pairs_data = json.load(f)
        
    candidates = pairs_data.get("candidate_pairs", [])
    logger.info(f"Loaded {len(candidates)} candidate pairs across sectors.")
    
    # Bybit client (public endpoints don't need real keys for kline scanning)
    client = BybitClient(testnet=False)
    screener = PairScreener(client)
    
    timeframe = str(config.get("strategy", {}).get("timeframe", "60"))
    lookback_days = config.get("strategy", {}).get("lookback_days", 60)
    tf_minutes = int(timeframe) if timeframe.isdigit() else 60
    bars_per_day = max(1, 1440 // tf_minutes)
    total_bars = int(lookback_days * bars_per_day)
    
    logger.info(f"Scanning pairs with timeframe={timeframe}m, lookback={lookback_days} days ({total_bars} bars)...")
    
    df_results = screener.screen_pair_list(candidates, interval=timeframe, total_bars=total_bars)
    
    if df_results.empty:
        logger.warning("No data retrieved for candidate pairs.")
        return
        
    # Format display columns
    display_cols = [
        "sector", "pair", "correlation", "p_value", "is_cointegrated",
        "beta_kalman", "half_life_days", "current_z_score"
    ]
    
    table_df = df_results[display_cols].copy()
    table_df.columns = [
        "Sector", "Pair", "Corr", "ADF p-val", "Cointegrated?",
        "Kalman Beta", "Half-Life (d)", "Current Z"
    ]
    
    print("\n" + tabulate(table_df, headers="keys", tablefmt="grid", showindex=False))
    
    # Opportunities highlight
    opportunities = df_results[
        (df_results["is_cointegrated"] == True) & 
        (df_results["current_z_score"].abs() >= 1.8)
    ]
    
    if not opportunities.empty:
        print("\n⚡ ACTIVE ARBITRAGE SIGNALS RIGHT NOW (|Z| >= 1.8):")
        for _, row in opportunities.iterrows():
            signal_type = "LONG A / SHORT B" if row["current_z_score"] < 0 else "SHORT A / LONG B"
            print(f"  👉 Pair: {row['pair']} | Z-Score: {row['current_z_score']} | Action: {signal_type} | Half-Life: {row['half_life_days']}d")
    else:
        print("\nℹ️  No pairs currently at extreme divergence (|Z| >= 1.8). Watching for entry opportunities.")

if __name__ == "__main__":
    main()
