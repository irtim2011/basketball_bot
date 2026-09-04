import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeChat
from aiogram.exceptions import TelegramBadRequest
import background
import interaction
from instance import ConflictGuard, single_instance
from version import VERSION
import db
import google_sheet
from config import BOT_TOKEN, TRAINER_IDS
from scheduler import setup_scheduler
import handlers_menu
import handlers_trainer
import handlers_poll
import handlers_registration
import handlers_manual  # Registers handlers on the trainer router before it is attached.

async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN or not TRAINER_IDS:
        raise RuntimeError("Заполните BOT_TOKEN и TRAINER_IDS в файле настроек")
    await db.init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    interaction.install(dp)
    conflict = ConflictGuard(dp)
    bot.session.middleware(conflict)
    logging.info('Training bot version %s; database %s', VERSION, db.DB_PATH)
    # Registration and personal data are only accepted in private chats.
    dp.message.filter(F.chat.type == 'private')
    dp.callback_query.filter(F.message.chat.type == 'private')
    dp.include_router(handlers_menu.router)
    dp.include_router(handlers_trainer.router)
    dp.include_router(handlers_poll.router)
    dp.include_router(handlers_registration.router)
    dp.include_router(handlers_menu.fallback)
    commands = [BotCommand(command=k, description=v) for k, v in [
        ('start','Регистрация и начало'), ('menu','Показать действия'),
        ('schedule','Расписание'), ('profile','Мой профиль'), ('id','Мой Telegram ID'),
        ('version','Версия бота'), ('cancel','Отменить ввод')]]
    await bot.set_my_commands(commands)
    for trainer_id in TRAINER_IDS:
        try:
            await bot.set_my_commands(commands + [
                BotCommand(command='training', description='Добавить тренировку'),
                BotCommand(command='participants', description='Участники рассылки'),
                BotCommand(command='poll_now', description='Отправить опрос сейчас'),
                BotCommand(command='table', description='Таблица посещаемости')],
                scope=BotCommandScopeChat(chat_id=trainer_id))
        except TelegramBadRequest:
            logging.info("Trainer %s must open the bot first", trainer_id)
    scheduler = setup_scheduler(bot)
    google_sheet.queue()
    try:
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        scheduler.shutdown(wait=False)
        await google_sheet.close()
        await background.close()
        # APScheduler cancels its running tick when shutdown is processed.
        await asyncio.sleep(0)
        await db.close_db()
        await bot.session.close()
    if conflict.detected:
        if conflict.stop_task:
            await conflict.stop_task
        raise SystemExit(73)

if __name__ == '__main__':
    with single_instance(BOT_TOKEN):
        asyncio.run(main())
