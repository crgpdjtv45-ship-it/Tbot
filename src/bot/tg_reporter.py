"""Optional Telegram controls for the same PAPER runtime as the dashboard.
Credentials are supplied by environment variables; construction has no network side effects.
"""
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import Command


class TelegramReporter:
    def __init__(self, bot_token='', chat_id='', enabled=False, **kwargs):
        self.chat_id = str(chat_id)
        self.enabled = bool(enabled and bot_token and chat_id)
        self.runtime = None
        self.app_state_getter = kwargs.get('app_state_getter')
        self.bot = Bot(token=bot_token) if self.enabled else None
        self.dp = Dispatcher() if self.enabled else None
        if self.enabled:
            self._register_handlers()

    def _is_authorized(self, chat_id):
        return str(chat_id) == self.chat_id

    def _register_handlers(self):
        @self.dp.message(Command('start', 'status', 'trades', 'stop', 'resume'))
        async def control(message):
            if not self._is_authorized(message.chat.id):
                return
            if self.runtime is None:
                await message.answer('PAPER runtime is not ready')
                return
            command = message.text.split()[0].split('@')[0]
            if command == '/stop':
                result = await asyncio.to_thread(self.runtime.stop)
                await message.answer(f"PAPER paused. Closed: {len(result['closed'])}. Waiting for fresh quotes: {len(result['pending'])}.")
            elif command == '/resume':
                try:
                    await asyncio.to_thread(self.runtime.resume)
                    await message.answer('PAPER resumed. Entries require fresh quotes and an eligible model.')
                except ValueError as exc:
                    await message.answer(str(exc))
            else:
                state = self.runtime.snapshot()
                if command == '/trades':
                    rows = state['recent_trades'][:10]
                    text = '\n'.join(f"{t['pair']}: {t['pnl_num']:+.2f} USDT ({t['exit_reason']})" for t in rows) or 'No closed trades'
                else:
                    text = (f"PAPER | {'PAUSED' if state['is_kill_switch_active'] else 'ACTIVE'}\n"
                            f"Data ready: {state['ready']}\nBalance: {state['balance']:.2f} USDT\n"
                            f"Open PnL: {state['unrealized_pnl']:+.2f} USDT\n"
                            f"Positions: {state['active_positions_count']}\nClosed: {state['stats']['total_trades']}")
                await message.answer(text)
