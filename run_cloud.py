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
        max_open_pairs=risk_cfg.get("max_open_pairs", 3),
        capital_per_pair_pct=risk_cfg.get("capital_per_pair_pct", 20.0),
        max_account_drawdown_pct=risk_cfg.get("max_account_drawdown_pct", 10.0),
        initial_equity=client.get_wallet_balance()
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

    # FastApi App
    app = create_app(
        bybit_client=client,
        order_manager=order_manager,
        risk_manager=risk_manager,
        screener=screener
    )

    # Render supplies port via environment variable PORT
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Serving Telegram Mini App on port {port}")

    # Set Telegram Menu button to permanent onrender domain
    cloud_url = os.environ.get("RENDER_EXTERNAL_URL", "https://tbot-statarb.onrender.com")
    try:
        import urllib.request, json
        token = tg_cfg.get("bot_token")
        if token:
            data = json.dumps({
                "menu_button": {
                    "type": "web_app",
                    "text": "Терминал",
                    "web_app": {"url": cloud_url}
                }
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/setChatMenuButton",
                data=data,
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10)
            logger.info(f"✅ Set Telegram Menu Button to cloud URL: {cloud_url}")
    except Exception as e:
        logger.warning(f"Could not set menu button: {e}")

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
