import sys
import uvicorn
from pathlib import Path

# Fix Windows console UTF-8 encoding
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.webapp.server import create_app
from src.api.bybit_client import BybitClient
from src.analytics.screener import PairScreener
from src.execution.order_manager import OrderManager
from src.risk.risk_manager import RiskManager
from src.utils.helpers import load_yaml_config

def main():
    print("\n" + "="*70)
    print(" 🚀 BYBIT STATARBITRAGE — TELEGRAM MINI APP & WEB DASHBOARD")
    print("="*70)
    print(" Запуск веб-сервера Telegram Mini App...")
    print(" Доступ в браузере: http://localhost:8000")
    print("="*70 + "\n")

    config = load_yaml_config("configs/config.example.yaml")
    cfg_file = Path("configs/config.yaml")
    if cfg_file.exists():
        config = load_yaml_config(str(cfg_file))

    bybit_cfg = config.get("bybit", {})
    client = BybitClient(
        api_key=bybit_cfg.get("api_key", ""),
        api_secret=bybit_cfg.get("api_secret", ""),
        testnet=bybit_cfg.get("testnet", True)
    )
    order_mgr = OrderManager(client)
    risk_mgr = RiskManager(client)
    screener = PairScreener(client)

    app = create_app(
        bybit_client=client,
        order_manager=order_mgr,
        risk_manager=risk_mgr,
        screener=screener
    )

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

if __name__ == "__main__":
    main()
