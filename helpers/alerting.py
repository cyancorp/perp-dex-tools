import asyncio
import os
from typing import Optional

from helpers.telegram_bot import TelegramBot


async def send_telegram_message(
    message: str,
    *,
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> None:
    """Send a Telegram message asynchronously using a background thread."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    def _send() -> None:
        with TelegramBot(token, chat_id) as bot:
            bot.send_text(message)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _send)
