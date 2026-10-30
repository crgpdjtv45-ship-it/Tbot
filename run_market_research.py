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
import pandas as pd

from src.api.bybit_client import BybitClient
from src.analytics.screener import PairScreener
from src.backtesting.engine import PairBacktestEngine
from src.utils.helpers import load_yaml_config

def run_market_research():
    print("\n" + "="*80)
    print(" 🚀 BYBIT STATISTICAL ARBITRAGE — QUANTITATIVE MARKET RESEARCH & RANKING")
    print("="*80)
    print(" Сканирование всех секторов рынка, проверка коинтеграции и бэктест доходности...\n")

    # Load pairs database
    pairs_file = Path("configs/pairs.json")
    if not pairs_file.exists():
        logger.error("configs/pairs.json not found!")
        return
        
    with open(pairs_file, "r", encoding="utf-8") as f:
        pairs_data = json.load(f)
    candidates = pairs_data.get("candidate_pairs", [])
    logger.info(f"Найдено {len(candidates)} пар в базе данных.")

    client = BybitClient(testnet=False)
    screener = PairScreener(client)

    timeframe = "240"     # 4h candles (optimal for fee-to-spread ratio)
    lookback_days = 180   # 6 months of historical data
    bars_per_day = 6      # 24 / 4 = 6
    total_bars = lookback_days * bars_per_day

    research_results = []

    for idx, item in enumerate(candidates, 1):
        sym_a = item["leg1"]
        sym_b = item["leg2"]
        sector = item.get("sector", "Crypto")
        pair_name = f"{sym_a}/{sym_b}"

        print(f"\n[{idx}/{len(candidates)}] 🔍 Исследование пары {pair_name} ({sector})...")
        
        # 1. Fetch candles
        df_a = client.get_historical_klines(sym_a, interval=timeframe, total_bars=total_bars)
        df_b = client.get_historical_klines(sym_b, interval=timeframe, total_bars=total_bars)

        if df_a.empty or df_b.empty:
            logger.warning(f"Недостаточно данных для {pair_name}, пропускаем.")
            continue

        # 2. Cointegration screen
        screen_res = screener.analyze_pair(sym_a, sym_b, interval=timeframe, total_bars=total_bars)
        if not screen_res:
            continue

        is_coint = screen_res["is_cointegrated"]
        p_val = screen_res["p_value"]
        corr = screen_res["correlation"]
        hl_days = screen_res["half_life_days"]

        # 3. Historical backtest simulation with Bybit fees and risk stops
        engine = PairBacktestEngine(
            initial_capital=10000.0,
            allocated_pct=25.0,
            leverage=2.0,
            entry_z=2.0,
            exit_z=0.2,
            stop_z=3.8,
            max_loss_pct=4.0,
            max_holding_bars=36,
            fee_rate=0.00055,
            slippage=0.0005,
            z_window=48
        )

        try:
            bt_res = engine.run(sym_a, sym_b, df_a, df_b)
            metrics = bt_res["metrics"]
            
            research_results.append({
                "sector": sector,
                "pair": pair_name,
                "leg1": sym_a,
                "leg2": sym_b,
                "is_cointegrated": is_coint,
                "p_value": p_val,
                "corr": corr,
                "half_life_days": hl_days,
                "trades": metrics["total_trades"],
                "win_rate_pct": metrics["win_rate_pct"],
                "net_profit": metrics["net_profit"],
                "return_pct": metrics["total_return_pct"],
                "profit_factor": metrics["profit_factor"],
                "max_drawdown_pct": metrics["max_drawdown_pct"],
                "sharpe_ratio": metrics["sharpe_ratio"]
            })
            logger.info(f"-> {pair_name}: Return={metrics['total_return_pct']:+.2f}% | WinRate={metrics['win_rate_pct']:.1f}% | Sharpe={metrics['sharpe_ratio']:.2f}")
        except Exception as e:
            logger.error(f"Ошибка бэктеста для {pair_name}: {e}")

    if not research_results:
        print("Нет результатов исследования.")
        return

    # Create summary DataFrame
    df_all = pd.DataFrame(research_results)

    # Sort leaderboard by Sharpe ratio and Return
    df_sorted = df_all.sort_values(by=["return_pct", "sharpe_ratio"], ascending=[False, False]).reset_index(drop=True)

    # Display Leaderboard
    display_df = df_sorted[[
        "sector", "pair", "corr", "p_value", "is_cointegrated",
        "trades", "win_rate_pct", "return_pct", "max_drawdown_pct", "sharpe_ratio"
    ]].copy()

    display_df.columns = [
        "Сектор", "Пара", "Корр", "ADF p", "Коинт?",
        "Сделок", "Винрейт %", "Доход %", "Просадка %", "Шарп"
    ]

    print("\n" + "="*85)
    print(" 🏆 ИТОГОВЫЙ РЕЙТИНГ ПАР ПО ДОХОДНОСТИ И НАДЕЖНОСТИ (180 ДНЕЙ, 4H)")
    print("="*85)
    print(tabulate(display_df, headers="keys", tablefmt="grid", showindex=False))

    # Export Top Pairs into configs/top_pairs.json
    top_candidates = df_sorted[df_sorted["return_pct"] > 0].head(5)
    if top_candidates.empty:
        top_candidates = df_sorted.head(3)

    top_export = []
    for _, row in top_candidates.iterrows():
        top_export.append({
            "sector": row["sector"],
            "leg1": row["leg1"],
            "leg2": row["leg2"],
            "return_pct": row["return_pct"],
            "win_rate_pct": row["win_rate_pct"],
            "sharpe": row["sharpe_ratio"]
        })

    out_file = Path("configs/top_pairs.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"top_pairs": top_export}, f, indent=2, ensure_ascii=False)

    print(f"\n💾 Топ-{len(top_export)} лучших пар автоматически сохранены в: {out_file.resolve()}")
    print("\n💡 Рекомендация: Эти пары можно напрямую подключить к главному торговому циклу бота!")

if __name__ == "__main__":
    run_market_research()
