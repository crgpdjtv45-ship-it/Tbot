import os
import time
import json
import urllib.request
from pathlib import Path
from typing import Optional, Dict, Any, List
from loguru import logger

# Inject native OS certificate store on Windows
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

try:
    from aiogram import Bot, Dispatcher, types
    from aiogram.filters import Command
    from aiogram.enums import ParseMode
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    AIOGRAM_AVAILABLE = True
except ImportError:
    AIOGRAM_AVAILABLE = False

CONFIG_URL_PATH = Path("configs/tma_url.txt")

class TelegramReporter:
    """
    Институциональный Telegram-интерфейс для Bybit StatArb робота:
    - Кнопка открытия полноэкранного терминала (Mini App).
    - Уведомления об открытии и закрытии арбитражных сделок в реальном времени.
    - Команды /start, /status, /trades, /stop.
    """
    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        enabled: bool = False,
        order_manager = None,
        risk_manager = None,
        bybit_client = None,
        screener = None,
        candidate_pairs: Optional[List[Dict[str, str]]] = None,
        app_state_getter = None
    ):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.enabled = bool(enabled and self.bot_token and self.chat_id and AIOGRAM_AVAILABLE)
        
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.bybit_client = bybit_client
        self.screener = screener
        self.candidate_pairs = candidate_pairs or []
        self.app_state_getter = app_state_getter
        
        self.bot = None
        self.dp = None
        
        if self.enabled:
            try:
                self.bot = Bot(token=self.bot_token)
                self.dp = Dispatcher()
                self._register_handlers()
                self._reset_menu_button()
                logger.info("TelegramReporter успешно инициализирован.")
            except Exception as e:
                logger.error(f"Ошибка инициализации Telegram Bot: {e}")
                self.enabled = False

    def _get_tma_url(self) -> str:
        tma_url = os.environ.get("RENDER_EXTERNAL_URL", "https://tbot-statarb.onrender.com")
        if CONFIG_URL_PATH.exists():
            try:
                content = CONFIG_URL_PATH.read_text(encoding="utf-8").strip()
                if content and not content.endswith(".lhr.life"):
                    tma_url = content
            except Exception:
                pass
        return tma_url

    def _reset_menu_button(self):
        try:
            payload = {"menu_button": {"type": "default"}}
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.bot_token}/setChatMenuButton",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception:
            pass

    def _get_main_keyboard(self) -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton(text="🌐 Открыть сайт терминала", url=self._get_tma_url())
            ]
        ]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    def _is_authorized(self, user_id: int) -> bool:
        return str(user_id) == self.chat_id

    def send_sync_message(self, text: str, reply_markup=None) -> bool:
        """Потокобезопасная отправка сообщения в Telegram через синхронный REST API."""
        if not self.bot_token or not self.chat_id:
            return False
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML"
            }
            if reply_markup:
                # If reply_markup is dict or aiogram object
                if hasattr(reply_markup, "model_dump"):
                    payload["reply_markup"] = reply_markup.model_dump(exclude_none=True)
                elif isinstance(reply_markup, dict):
                    payload["reply_markup"] = reply_markup

            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    def _register_handlers(self):
        if not self.dp:
            return

        @self.dp.message(Command("start"))
        async def start_handler(message: types.Message):
            if not self._is_authorized(message.chat.id):
                return

            text = (
                "<b>Bybit StatArb • Торговый терминал</b>\n\n"
                "• <b>Стратегия:</b> Статистический арбитраж (дельта-нейтрально)\n"
                "• <b>Инструменты:</b> 11 TradFi + 5 Криптовалют\n"
                "• <b>Депозит:</b> $10,000.00 USDT (Paper Trading)\n"
                "• <b>Статус:</b> Активен\n\n"
                "Для управления и мониторинга нажмите кнопку ниже:"
            )
            await message.reply(text, reply_markup=self._get_main_keyboard(), parse_mode=ParseMode.HTML)

        @self.dp.message(Command("status"))
        async def status_handler(message: types.Message):
            if not self._is_authorized(message.chat.id):
                return

            state = self.app_state_getter() if self.app_state_getter else {}
            balance = state.get("balance", 10000.0)
            pnl = state.get("unrealized_pnl", 0.0)
            active_cnt = state.get("active_positions_count", 0)
            is_paused = state.get("is_kill_switch_active", False)
            stats = state.get("stats", {})

            pnl_sign = "+" if pnl >= 0 else ""
            status_text = "На паузе" if is_paused else "Активна"
            wr = stats.get("win_rate", 0.0)
            pf = stats.get("profit_factor", 0.0)
            closed_cnt = stats.get("total_trades", len(state.get("closed_trades", [])))
            realized_pnl = stats.get("total_realized_pnl", 0.0)
            realized_sign = "+" if realized_pnl >= 0 else ""

            text = (
                "<b>Текущий статус:</b>\n"
                f"• <b>Авто-торговля:</b> {status_text}\n"
                f"• <b>Баланс депозита:</b> ${balance:,.2f} USDT\n"
                f"• <b>Нереализованный P&L:</b> {pnl_sign}${pnl:,.2f} USDT\n"
                f"• <b>Зафиксированный P&L:</b> {realized_sign}${realized_pnl:,.2f} USDT\n"
                f"• <b>Винрейт:</b> {wr}% ({closed_cnt} сделок, PF {pf})\n"
                f"• <b>Открытых позиций:</b> {active_cnt}\n"
                f"• <b>Мониторинг инструментов:</b> 16 пар"
            )
            await message.reply(text, reply_markup=self._get_main_keyboard(), parse_mode=ParseMode.HTML)

        @self.dp.message(Command("trades"))
        async def trades_handler(message: types.Message):
            if not self._is_authorized(message.chat.id):
                return

            state = self.app_state_getter() if self.app_state_getter else {}
            stats = state.get("stats", {})
            trades = state.get("closed_trades", [])

            total = stats.get("total_trades", len(trades))
            wr = stats.get("win_rate", 0.0)
            pf = stats.get("profit_factor", 0.0)
            net_pnl = stats.get("total_realized_pnl", 0.0)
            sign = "+" if net_pnl >= 0 else ""

            text = (
                "<b>📊 Журнал закрытых сделок (Bybit StatArb):</b>\n"
                f"• <b>Всего сделок:</b> {total}\n"
                f"• <b>Винрейт:</b> {wr}%\n"
                f"• <b>Profit Factor:</b> {pf}\n"
                f"• <b>Зафиксированный P&L:</b> {sign}${net_pnl:,.2f} USDT\n\n"
                "<b>Последние результаты:</b>\n"
            )

            for i, t in enumerate(trades[:6], 1):
                clean_pair = t.get("pair", "").replace("USDT", "")
                pnl_str = t.get("profit_usd", "$0")
                pct_str = t.get("profit_pct", "0%")
                ez = t.get("entry_z", 0.0)
                xz = t.get("exit_z", 0.0)
                dur = t.get("duration", "")
                ez_str = f"{ez:+.2f}σ" if isinstance(ez, (int, float)) else str(ez)
                xz_str = f"{xz:+.2f}σ" if isinstance(xz, (int, float)) else str(xz)
                text += f"{i}. <b>{clean_pair}</b>: <code>{pnl_str} ({pct_str})</code> | Z: {ez_str} → {xz_str} ({dur})\n"

            text += "\n<i>Детальная таблица и динамика доступны в терминале:</i>"
            await message.reply(text, reply_markup=self._get_main_keyboard(), parse_mode=ParseMode.HTML)

        @self.dp.message(Command("stop"))
        async def stop_handler(message: types.Message):
            if not self._is_authorized(message.chat.id):
                return

            if self.risk_manager:
                self.risk_manager.trigger_kill_switch("Command /stop from Telegram")
            if self.order_manager:
                self.order_manager.close_all_positions(reason="Telegram /stop")

            text = (
                "🛑 <b>ЭКСТРЕННАЯ ОСТАНОВКА</b>\n"
                "─────────────────────────\n"
                "Все открытые позиции закрыты по текущей рыночной цене.\n"
                "Автопилот переведен на паузу.\n\n"
                "Для возобновления откройте терминал и включите переключатель автопилота."
            )
            await message.reply(text, reply_markup=self._get_main_keyboard(), parse_mode=ParseMode.HTML)

        @self.dp.message()
        async def any_message(message: types.Message):
            if not self._is_authorized(message.chat.id):
                return

            welcome_text = (
                "🤖 <b>Bybit StatArb • Квантовый терминал</b>\n\n"
                "Для мониторинга сделок, спредов и настроек перейдите в веб-терминал:"
            )
            await message.reply(welcome_text, reply_markup=self._get_main_keyboard(), parse_mode=ParseMode.HTML)

    def notify_entry(self, pos: Dict[str, Any]):
        # Отключено: бот не отправляет автоматических уведомлений в чат
        return

    def notify_exit(self, pos: Dict[str, Any], pnl: float, reason: str, balance: float):
        # Отключено: бот не отправляет автоматических уведомлений в чат
        return
