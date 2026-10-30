import sys
import yaml
import asyncio
from pathlib import Path

# Fix Windows console UTF-8 encoding
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from pybit.unified_trading import HTTP
import aiohttp

def print_header():
    print("\n" + "="*70)
    print(" 🛠 МАСТЕР НАСТРОЙКИ: BYBIT TESTNET & TELEGRAM БОТ")
    print("="*70)
    print(" Этот помощник поможет настроить и проверить ваши ключи Bybit и Telegram.\n")

def test_bybit(api_key: str, api_secret: str, testnet: bool = True) -> bool:
    print("\n🔍 Проверка подключения к Bybit API...")
    try:
        session = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)
        res = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        if res.get("retCode") == 0:
            print("  ✅ Успешное подключение к Bybit!")
            coins = res.get("result", {}).get("list", [{}])[0].get("coin", [])
            for c in coins:
                if c.get("coin") == "USDT":
                    print(f"  💰 Текущий баланс USDT: {c.get('equity')} USDT")
            return True
        else:
            print(f"  ❌ Ошибка Bybit API: {res.get('retMsg')} (Код: {res.get('retCode')})")
            return False
    except Exception as e:
        print(f"  ❌ Исключение при подключении: {e}")
        return False

async def test_telegram(bot_token: str, chat_id: str) -> bool:
    print("\n🔍 Проверка отправки сообщения в Telegram...")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "🤖 <b>Bybit StatArb Bot:</b> Тестовое сообщение!\nСвязь с Telegram настроена успешно. Клавиатура и алерты готовы к работе.",
        "parse_mode": "HTML"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=10) as resp:
                data = await resp.json()
                if data.get("ok"):
                    print("  ✅ Тестовое сообщение успешно доставлено в ваш Telegram!")
                    return True
                else:
                    print(f"  ❌ Ошибка Telegram: {data.get('description')}")
                    return False
    except Exception as e:
        print(f"  ❌ Исключение при отправке Telegram: {e}")
        return False

def main():
    print_header()

    cfg_template_path = Path("configs/config.example.yaml")
    with open(cfg_template_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 1. Bybit Settings
    print("--- 1. НАСТРОЙКА BYBIT ---")
    print("Рекомендуется начать с Bybit Testnet (демо с виртуальными деньгами): https://testnet.bybit.com")
    use_testnet_input = input("Использовать Testnet? [Y/n] (по умолчанию: Y): ").strip().lower()
    is_testnet = False if use_testnet_input == 'n' else True
    
    api_key = input("Введите Bybit API Key (или Enter для пропуска): ").strip()
    api_secret = input("Введите Bybit API Secret (или Enter для пропуска): ").strip()

    if api_key and api_secret:
        test_bybit(api_key, api_secret, testnet=is_testnet)
        config["bybit"]["api_key"] = api_key
        config["bybit"]["api_secret"] = api_secret
    config["bybit"]["testnet"] = is_testnet

    # 2. Telegram Settings
    print("\n--- 2. НАСТРОЙКА TELEGRAM ---")
    print("1. Токен бота создается через @BotFather в Telegram.")
    print("2. Ваш Chat ID можно узнать через бота @userinfobot.")
    
    tg_token = input("Введите Telegram Bot Token (или Enter для пропуска): ").strip()
    tg_chat_id = input("Введите ваш Telegram Chat ID (или Enter для пропуска): ").strip()

    if tg_token and tg_chat_id:
        tg_ok = asyncio.run(test_telegram(tg_token, tg_chat_id))
        if tg_ok:
            config["telegram"]["enabled"] = True
            config["telegram"]["bot_token"] = tg_token
            config["telegram"]["chat_id"] = tg_chat_id

    # Save to configs/config.yaml
    out_cfg = Path("configs/config.yaml")
    with open(out_cfg, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    print("\n" + "="*70)
    print(f" 🎉 Настройки успешно сохранены в: {out_cfg.resolve()}")
    print(" Теперь вы можете запустить бота командой:")
    print("   .\\.venv\\Scripts\\python.exe main.py")
    print("="*70 + "\n")

if __name__ == "__main__":
    main()
