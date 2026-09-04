"""Validate configuration and Telegram connectivity without consuming updates."""
import asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramUnauthorizedError
from config import BOT_TOKEN, TRAINER_IDS
import utils

async def check():
    if not BOT_TOKEN or not TRAINER_IDS:
        raise SystemExit('Заполните BOT_TOKEN и TRAINER_IDS в ~/.config/training-bot/bot.env')
    bot = Bot(BOT_TOKEN)
    try:
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise SystemExit('У бота настроен webhook. Остановите предыдущую установку и удалите webhook перед запуском.')
        print(f'Настройки проверены. Бот: https://t.me/{me.username}')
    except TelegramUnauthorizedError:
        raise SystemExit('Telegram отклонил BOT_TOKEN.')
    finally:
        await bot.session.close()

if __name__ == '__main__':
    asyncio.run(check())

