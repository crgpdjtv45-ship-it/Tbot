import sys
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.api.bybit_client import BybitClient
from src.analytics.screener import PairScreener

client = BybitClient(testnet=False)
screener = PairScreener(client)

pairs_to_test = [
    ('XAUUSDT', 'XAGUSDT', 'Commodities: Gold vs Silver'),
    ('SPYUSDT', 'QQQUSDT', 'Indices: S&P 500 vs NASDAQ'),
    ('NVDAUSDT', 'AMDSTOCKUSDT', 'Stocks: Nvidia vs AMD'),
    ('EURUSDUSDT', 'GBPUSDUSDT', 'Forex: EUR/USD vs GBP/USD'),
    ('COINUSDT', 'MSTRUSDT', 'Crypto Stocks: Coinbase vs MicroStrategy')
]

print("\n--- TRADFI COINTEGRATION ON BYBIT LINEAR PERPETUALS ---")
for a, b, desc in pairs_to_test:
    res = screener.analyze_pair(a, b, interval='240', total_bars=720)
    if res:
        c = "✅ ДА" if res["is_cointegrated"] else "⚠️ НЕТ"
        hl = f"{res['half_life_days']:.1f}д" if res['half_life_days'] else "N/A"
        print(f"{desc} ({a}/{b}):")
        print(f"   Корреляция: {res['correlation']:.3f} | Коинтеграция: {c} (p={res['p_value']:.4f}) | Half-Life: {hl} | Z-Score: {res['current_z_score']:+.2f}")
    else:
        print(f"❌ {desc} ({a}/{b}): Недостаточно данных.")
