import re
import sys
import time
import subprocess
import urllib.request
import json
from pathlib import Path
from loguru import logger

CONFIG_FILE = Path("configs/config.yaml")
URL_FILE = Path("configs/tma_url.txt")

def set_telegram_menu_button(token: str, url: str):
    """Обновляет кнопку меню Telegram на актуальный URL Mini App."""
    try:
        data = json.dumps({
            "menu_button": {
                "type": "web_app",
                "text": "Терминал",
                "web_app": {"url": url}
            }
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/setChatMenuButton",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode())
            if res.get("ok"):
                logger.info(f"✅ Telegram Menu Button успешно обновлен: {url}")
            else:
                logger.warning(f"Telegram API response: {res}")
    except Exception as e:
        logger.error(f"Не удалось обновить Telegram Menu Button: {e}")

def main():
    bot_token = "9157590662:AAGjN0hqhnw9bIpy0BkQ0WIOFlKBqghfFyY"
    logger.info("Запуск отказоустойчивого туннеля для Telegram Mini App...")

    while True:
        try:
            logger.info("Инициализация SSH-туннеля к localhost.run...")
            proc = subprocess.Popen(
                [
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=NUL",
                    "-o", "ServerAliveInterval=15",
                    "-o", "ServerAliveCountMax=4",
                    "-R", "80:127.0.0.1:8000",
                    "nokey@localhost.run"
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            tunnel_url = None
            for line in proc.stdout:
                line_str = line.strip()
                if "tunneled with tls termination" in line_str or ".lhr.life" in line_str:
                    match = re.search(r"https://[a-zA-Z0-9\.\-_]+\.lhr\.life", line_str)
                    if match:
                        tunnel_url = match.group(0)
                        logger.info(f"🔥 Найден активный публичный HTTPS URL: {tunnel_url}")
                        
                        # Сохраняем в файл
                        URL_FILE.write_text(tunnel_url, encoding="utf-8")
                        
                        # Обновляем Telegram Menu Button
                        set_telegram_menu_button(bot_token, tunnel_url)
                        break

            proc.wait()
            logger.warning("SSH-туннель закрылся. Переподключение через 3 секунды...")
            time.sleep(3)

        except Exception as e:
            logger.error(f"Ошибка в цикле туннеля: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
