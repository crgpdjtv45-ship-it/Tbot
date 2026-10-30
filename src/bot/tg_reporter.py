import asyncio
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
    Максимально чистый Telegram интерфейс:
    - Никаких текстовых отчетов, сигналов и спама в чат.
    - Ровно одно аккуратное приветственное сообщение с единственной кнопкой '🚀 Открыть Терминал'.
    - Все управление, баланс, автопилот и сделки происходят исключительно внутри Mini App.
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
        candidate_pairs: Optional[List[Dict[str, str]]] = None
    ):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.enabled = bool(enabled and self.bot_token and self.chat_id and AIOGRAM_AVAILABLE)
        
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.bybit_client = bybit_client
        self.screener = screener
        self.candidate_pairs = candidate_pairs or []
        
        self.bot = None
        self.dp = None
        
        if self.enabled:
            try:
                self.bot = Bot(token=self.bot_token)
                self.dp = Dispatcher()
                self._register_handlers()
                logger.info("TelegramReporter успешно инициализирован в режиме чистого чата.")
            except Exception as e:
                logger.error(f"Ошибка инициализации Telegram Bot: {e}")
                self.enabled = False

    def _get_tma_url(self) -> str:
        tma_url = "https://fcae3bee8dbf58.lhr.life"
        if CONFIG_URL_PATH.exists():
            try:
                content = CONFIG_URL_PATH.read_text(encoding="utf-8").strip()
                if content:
                    tma_url = content
            except Exception:
                pass
        return tma_url

    def _get_main_keyboard(self) -> InlineKeyboardMarkup:
        """Строго одна кнопка в чате."""
        keyboard = [
            [
                InlineKeyboardButton(text="🚀 Открыть Терминал", web_app=types.WebAppInfo(url=self._get_tma_url()))
            ]
        ]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    def _is_authorized(self, user_id: int) -> bool:
        return str(user_id) == self.chat_id

    def _register_handlers(self):
        if not self.dp:
            return

        @self.dp.message()
        async def any_message_handler(message: types.Message):
            """На любую команду или сообщение отправляется только лаконичная карточка терминала."""
            if not self._is_authorized(message.chat.id):
                return

            welcome_text = (
                "🤖 <b>Bybit StatArb • Автопилот</b>\n\n"
                "Весь мониторинг, сделки и управление доступны в графическом приложении:"
            )
            await message.reply(welcome_text, reply_markup=self._get_main_keyboard(), parse_mode=ParseMode.HTML)

    # Заглушки для уведомлений - чтобы чат никогда не засорялся
    async def send_message(self, text: str, reply_markup=None):
        pass

    async def notify_signal(self, pair: str, z_score: float, action: str):
        pass

    async def notify_entry(self, pos: Dict[str, Any]):
        pass

    async def notify_exit(self, pos: Dict[str, Any]):
        pass
