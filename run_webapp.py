"""Run the local persistent paper bot and authenticated dashboard."""
import argparse
import os
from pathlib import Path
import uvicorn
from loguru import logger
from src.runtime import ROOT
from src.webapp.server import create_app


def main():
    parser = argparse.ArgumentParser(description="Tbot PAPER: live quotes, simulated orders")
    parser.add_argument('--host', default=os.environ.get('TBOT_HOST', '0.0.0.0' if os.environ.get('RENDER') == 'true' else '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('TBOT_DATA_DIR', ROOT / 'data')))
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    logger.add(args.data_dir / 'paper.log', rotation='10 MB', retention='14 days', encoding='utf-8')
    reporter = None
    token = os.environ.get('TBOT_TELEGRAM_TOKEN', '')
    chat = os.environ.get('TBOT_TELEGRAM_CHAT_ID', '')
    if token and chat:
        from src.bot.tg_reporter import TelegramReporter
        reporter = TelegramReporter(token, chat, enabled=True)
    app = create_app(data_dir=args.data_dir, tg_reporter=reporter)
    logger.info('PAPER mode only. Dashboard: http://{}:{}; local access token: {}', args.host, args.port, args.data_dir / 'dashboard.token')
    uvicorn.run(app, host=args.host, port=args.port, log_level='info', workers=1,
                proxy_headers=True, forwarded_allow_ips='*' if os.environ.get('RENDER') == 'true' else '127.0.0.1')


if __name__ == '__main__':
    main()
