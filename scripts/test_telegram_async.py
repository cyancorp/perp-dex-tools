import os
import asyncio
import importlib.util
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
HELPERS_PATH = PROJECT_ROOT / 'helpers'

spec = importlib.util.spec_from_file_location('helpers.telegram_bot', HELPERS_PATH / 'telegram_bot.py')
helpers_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers_module)  # type: ignore
TelegramBot = helpers_module.TelegramBot

async def send_test_message():
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in environment.')
        return

    def _send():
        with TelegramBot(token, chat_id) as bot:
            bot.send_text('Telegram alert test: direct send from scripts/test_telegram_async.py')

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send)
    print('Sent test Telegram message.')

if __name__ == '__main__':
    asyncio.run(send_test_message())
