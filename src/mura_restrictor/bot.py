"""Remove forwards based on the sender in the group, not the original author."""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.error import NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    token: str
    username: str = "username"
    user_id: int | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Укажите TELEGRAM_BOT_TOKEN в .env или переменных окружения.")
        username = os.getenv("TARGET_USERNAME", "username").strip().lstrip("@").casefold()
        raw_id = os.getenv("TARGET_USER_ID", "").strip()
        try:
            user_id = int(raw_id) if raw_id else None
        except ValueError:
            raise ValueError("TARGET_USER_ID должен быть положительным числом.") from None
        if user_id is not None and user_id <= 0:
            raise ValueError("TARGET_USER_ID должен быть положительным числом.")
        if user_id is None and not username:
            raise ValueError("Укажите TARGET_USERNAME или TARGET_USER_ID.")
        return cls(token, username, user_id)


def should_delete(message: Message, settings: Settings) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        return False
    if message.forward_origin is None or message.sender_chat is not None:
        return False
    sender = message.from_user
    if sender is None:
        return False
    if settings.user_id is not None:
        return sender.id == settings.user_id
    return (sender.username or "").casefold() == settings.username.casefold()


async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    settings = context.application.bot_data["settings"]
    if message is None or not should_delete(message, settings):
        return
    for attempt in range(3):
        try:
            await message.delete()
            logger.info("Удалено сообщение %s в чате %s", message.message_id, message.chat_id)
            return
        except (RetryAfter, NetworkError) as exc:
            if attempt == 2:
                logger.error("Удаление не выполнено после 3 попыток: %s", type(exc).__name__)
                return
            delay = exc.retry_after if isinstance(exc, RetryAfter) else 2 ** attempt
            if isinstance(delay, timedelta):
                delay = delay.total_seconds()
            await asyncio.sleep(float(delay))
        except TelegramError as exc:
            # Do not log exception text or updates: they may contain secrets/content.
            logger.error(
                "Не удалось удалить сообщение %s в чате %s (%s). Проверьте права бота.",
                message.message_id, message.chat_id, type(exc).__name__,
            )
            return


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка обработки обновления: %s", type(context.error).__name__)


def build_application(settings: Settings) -> Application:
    app = Application.builder().token(settings.token).build()
    app.bot_data["settings"] = settings
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.FORWARDED, moderate))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP request URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    logger.info("Бот запущен")
    build_application(settings).run_polling(allowed_updates=["message", "edited_message"])
