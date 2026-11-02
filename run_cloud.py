import os
import sys
import threading
import uvicorn
from loguru import logger

from src.utils.helpers import load_yaml_config
from src.api.bybit_client import BybitClient
from src.execution.order_manager import OrderManager
from src.risk.risk_manager import RiskManager
from src.analytics.screener import PairScreener
from src.bot.tg_reporter import TelegramReporter
from src.webapp.server import create_app

def run_fastapi(app, port):
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

def main():
    logger.info("Starting Bybit StatArb Unified Cloud Service...")
    cfg = load_yaml_config("configs/config.yaml")

    bybit_cfg = cfg.get("bybit", {})
    client = BybitClient(
        api_key=bybit_cfg.get("api_key", ""),
        api_secret=bybit_cfg.get("api_secret", ""),
        testnet=bybit_cfg.get("testnet", True),
        category=bybit_cfg.get("category", "linear")
    )

    risk_cfg = cfg.get("risk", {})
    risk_manager = RiskManager(
        client=client,
        max_open_pairs=risk_cfg.get("max_open_pairs", 3),
        capital_per_pair_pct=risk_cfg.get("capital_per_pair_pct", 20.0),
        leverage=risk_cfg.get("leverage", 2),
        max_account_drawdown_pct=risk_cfg.get("max_account_drawdown_pct", 10.0),
        slippage_tolerance_pct=risk_cfg.get("slippage_tolerance_pct", 0.2)
    )

    order_manager = OrderManager(client=client)
    screener = PairScreener(client=client)

    tg_cfg = cfg.get("telegram", {})
    tg_reporter = TelegramReporter(
        bot_token=tg_cfg.get("bot_token", ""),
        chat_id=tg_cfg.get("chat_id", ""),
        enabled=tg_cfg.get("enabled", True),
        order_manager=order_manager,
        risk_manager=risk_manager,
        bybit_client=client,
        screener=screener
    )

    # FastApi App with Telegram alerts integration
    app = create_app(
        bybit_client=client,
        order_manager=order_manager,
        risk_manager=risk_manager,
        screener=screener,
        tg_reporter=tg_reporter
    )

    # Connect app state getter to Telegram bot
    tg_reporter.app_state_getter = getattr(app, "get_state", None)

    # Render supplies port via environment variable PORT
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Serving Telegram Mini App on port {port}")

    # Reset Telegram Menu button to standard and register commands
    try:
        import urllib.request, json
        token = tg_cfg.get("bot_token")
        if token:
            # 1. Reset menu button from web_app to default
            data = json.dumps({"menu_button": {"type": "default"}}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/setChatMenuButton",
                data=data,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10)
            logger.info("✅ Reset Telegram Menu Button to default")

            # 2. Register bot commands in Telegram menu
            commands_payload = json.dumps({
                "commands": [
                    {"command": "start", "description": "Запустить терминал и получить ссылку"},
                    {"command": "status", "description": "Баланс, открытые позиции и статистика"},
                    {"command": "trades", "description": "Журнал закрытых сделок и результаты"},
                    {"command": "stop", "description": "Экстренная остановка всех позиций"}
                ]
            }).encode()
            cmd_req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/setMyCommands",
                data=commands_payload,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(cmd_req, timeout=10)
            logger.info("✅ Registered Telegram commands: /start, /status, /trades, /stop")
    except Exception as e:
        logger.warning(f"Could not configure Telegram menu/commands: {e}")

    # Start Telegram bot polling in background thread if enabled
    if tg_reporter.enabled and tg_reporter.bot and tg_reporter.dp:
        def start_telegram_listener():
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            logger.info("🤖 Starting Telegram Bot polling loop in cloud background...")
            try:
                loop.run_until_complete(tg_reporter.dp.start_polling(tg_reporter.bot))
            except Exception as tg_err:
                logger.error(f"Telegram polling error: {tg_err}")

        tg_thread = threading.Thread(target=start_telegram_listener, daemon=True)
        tg_thread.start()

    # Start FastAPI in main thread
    run_fastapi(app, port)

if __name__ == "__main__":
    main()
