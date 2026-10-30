import asyncio
import json
import sys
import time
from pathlib import Path

# Fix Windows console UTF-8 encoding
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Inject native OS certificate store on Windows
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

from loguru import logger

from src.api.bybit_client import BybitClient
from src.analytics.screener import PairScreener
from src.risk.risk_manager import RiskManager
from src.execution.order_manager import OrderManager
from src.bot.tg_reporter import TelegramReporter
from src.utils.helpers import load_yaml_config

async def run_bot_loop(config: dict):
    bybit_cfg = config.get("bybit", {})
    strategy_cfg = config.get("strategy", {})
    risk_cfg = config.get("risk", {})
    tg_cfg = config.get("telegram", {})

    # 1. Initialize Bybit Client
    client = BybitClient(
        api_key=bybit_cfg.get("api_key", ""),
        api_secret=bybit_cfg.get("api_secret", ""),
        testnet=bybit_cfg.get("testnet", True),
        category=bybit_cfg.get("category", "linear")
    )

    # 2. Initialize Risk Manager, Order Manager & Screener
    risk_manager = RiskManager(
        client=client,
        max_open_pairs=risk_cfg.get("max_open_pairs", 3),
        capital_per_pair_pct=risk_cfg.get("capital_per_pair_pct", 20.0),
        leverage=risk_cfg.get("leverage", 2),
        max_account_drawdown_pct=risk_cfg.get("max_account_drawdown_pct", 10.0),
        slippage_tolerance_pct=risk_cfg.get("slippage_tolerance_pct", 0.2)
    )
    order_manager = OrderManager(client=client)
    screener = PairScreener(client)

    # Load candidate pairs (prioritize verified top_pairs.json if present)
    top_file = Path("configs/top_pairs.json")
    pairs_file = Path("configs/pairs.json")
    
    if top_file.exists():
        with open(top_file, "r", encoding="utf-8") as f:
            candidates = json.load(f).get("top_pairs", [])
        logger.info(f"Загружено {len(candidates)} проверенных прибыльных пар из top_pairs.json")
    elif pairs_file.exists():
        with open(pairs_file, "r", encoding="utf-8") as f:
            candidates = json.load(f).get("candidate_pairs", [])
        logger.info(f"Загружено {len(candidates)} пар из базового списка pairs.json")
    else:
        logger.error("Файлы со списком пар не найдены!")
        return

    # 3. Initialize Interactive Telegram Reporter
    tg_reporter = TelegramReporter(
        bot_token=tg_cfg.get("bot_token", ""),
        chat_id=tg_cfg.get("chat_id", ""),
        enabled=tg_cfg.get("enabled", False),
        order_manager=order_manager,
        risk_manager=risk_manager,
        bybit_client=client,
        screener=screener,
        candidate_pairs=candidates
    )

    timeframe = str(strategy_cfg.get("timeframe", "60"))
    entry_z = float(strategy_cfg.get("entry_z_score", 2.0))
    exit_z = float(strategy_cfg.get("exit_z_score", 0.2))
    stop_z = float(strategy_cfg.get("stop_loss_z_score", 3.8))
    
    logger.info("=" * 60)
    logger.info("BYBIT STATISTICAL ARBITRAGE BOT STARTED")
    logger.info(f"Mode: {'TESTNET' if client.testnet else 'MAINNET'} | Timeframe: {timeframe}m")
    logger.info(f"Strategy: Entry |Z| >= {entry_z} | Exit |Z| <= {exit_z} | Stop |Z| >= {stop_z}")
    logger.info(f"Active Candidate Pairs: {len(candidates)}")
    logger.info("=" * 60)

    # Start Telegram polling if enabled
    if tg_reporter.enabled and tg_reporter.dp and tg_reporter.bot:
        asyncio.create_task(tg_reporter.dp.start_polling(tg_reporter.bot))
        welcome_text = (
            "🚀 <b>Bybit Statistical Arbitrage Terminal ЗАПУЩЕН!</b>\n"
            "─────────────────────────\n"
            "Терминал подключен и отслеживает сигналы в режиме реального времени.\n\n"
            "Используйте кнопки управления ниже:"
        )
        await tg_reporter.send_message(welcome_text, reply_markup=tg_reporter._get_main_keyboard())

    loop_delay_sec = 60  # Check every 60 seconds

    while True:
        try:
            # 1. Manage Active Positions (Check Exit conditions)
            active_keys = list(order_manager.active_positions.keys())
            for pair_key in active_keys:
                pos = order_manager.active_positions[pair_key]
                sym_a = pos["symbol_a"]
                sym_b = pos["symbol_b"]
                side = pos["side"]

                # Check latest Z-score
                res = screener.analyze_pair(sym_a, sym_b, interval=timeframe, total_bars=400)
                if not res:
                    continue
                curr_z = res["current_z_score"]

                should_exit = False
                reason = ""
                # Mean Reversion exit
                if side == 1 and curr_z >= -exit_z:
                    should_exit = True
                    reason = f"Mean Reversion (Z={curr_z:+.2f})"
                elif side == -1 and curr_z <= exit_z:
                    should_exit = True
                    reason = f"Mean Reversion (Z={curr_z:+.2f})"
                # Emergency statistical stop
                elif abs(curr_z) >= stop_z:
                    should_exit = True
                    reason = f"Stop Loss (Breakdown Z={curr_z:+.2f})"

                if should_exit:
                    closed = order_manager.close_pair_position(pair_key, reason=reason)
                    if closed and tg_reporter.enabled:
                        await tg_reporter.notify_exit(closed)

            # 2. Scan Candidate Pairs for New Entry Opportunities
            if risk_manager.check_can_open_position(len(order_manager.active_positions)):
                balance = client.get_wallet_balance()
                
                for candidate in candidates:
                    if not risk_manager.check_can_open_position(len(order_manager.active_positions)):
                        break

                    sym_a = candidate["leg1"]
                    sym_b = candidate["leg2"]
                    pair_key = f"{sym_a}/{sym_b}"

                    # Skip if already open
                    if pair_key in order_manager.active_positions:
                        continue

                    res = screener.analyze_pair(sym_a, sym_b, interval=timeframe, total_bars=400)
                    if not res or not res["is_cointegrated"]:
                        continue

                    curr_z = res["current_z_score"]
                    beta = res["beta_kalman"]

                    # Check entry trigger
                    side = 0
                    if curr_z <= -entry_z:
                        side = 1  # Long A, Short B
                    elif curr_z >= entry_z:
                        side = -1  # Short A, Long B

                    if side != 0:
                        action_str = "LONG A / SHORT B" if side == 1 else "SHORT A / LONG B"
                        logger.info(f"Signal triggered on {pair_key}: Z={curr_z:+.2f} -> {action_str}")
                        
                        if tg_reporter.enabled:
                            await tg_reporter.notify_signal(pair_key, curr_z, action_str)

                        # Calculate exchange-compliant quantities
                        order_params = risk_manager.calculate_order_quantities(
                            sym_a, sym_b, res["price_a"], res["price_b"], balance
                        )
                        if order_params:
                            pos = order_manager.open_pair_position(
                                symbol_a=sym_a,
                                symbol_b=sym_b,
                                side=side,
                                qty_a=order_params["qty_a"],
                                qty_b=order_params["qty_b"],
                                current_z=curr_z,
                                beta=beta
                            )
                            if pos and tg_reporter.enabled:
                                await tg_reporter.notify_entry(pos)

        except Exception as e:
            logger.error(f"Error in main bot iteration: {e}")

        await asyncio.sleep(loop_delay_sec)

def main():
    config = load_yaml_config("configs/config.example.yaml")
    # If custom config exists, load it
    cfg_path = Path("configs/config.yaml")
    if cfg_path.exists():
        config = load_yaml_config(str(cfg_path))
        
    try:
        asyncio.run(run_bot_loop(config))
    except KeyboardInterrupt:
        logger.info("Bot process stopped by user (Ctrl+C).")

if __name__ == "__main__":
    main()
