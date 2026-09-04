"""Acknowledge callbacks before queueing; serialize each chat and collapse menu bursts."""
import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
import background

acked = ContextVar('acked', default=None)
NAV = {
    '0':'menu','📋 Показать действия':'menu','Отмена':'cancel',
    '➕ Тренировка':'training','📅 Расписание':'schedule','👥 Участники':'participants',
    '➕ Участник':'add','📊 Таблица':'table','👤 Мой профиль':'profile','📣 Опрос сейчас':'poll_now',
}
COMMANDS = {'menu','help','cancel','start','training','add_schedule','schedule','participants',
            'add','table','profile','id','poll_now','version'}
ALIASES = {'help':'menu','add_schedule':'training'}

def navigation(update):
    msg = update.message
    if not msg or not msg.text:
        return None
    text = msg.text.strip()
    if text.startswith('/'):
        cmd = text.split()[0][1:].split('@')[0]
        return ALIASES.get(cmd, cmd) if cmd in COMMANDS else None
    return NAV.get(text)

async def ack(callback, text=None, show_alert=False):
    if acked.get() != callback.id:
        try:
            await callback.answer(text, show_alert=show_alert)
        except TelegramAPIError:
            pass
    elif text and callback.message:
        await callback.message.answer(text)

@dataclass
class Lane:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    group: int = 0
    serial: int = 0
    latest: dict = field(default_factory=dict)
    waiting: int = 0
    last_nav: str = ''
    last_time: float = 0
    last_callback: str = ''
    callback_time: float = 0
    warned: float = 0

class Ingress(BaseMiddleware):
    def __init__(self):
        self.lanes = {}

    async def __call__(self, handler, update, data):
        cb = update.callback_query
        msg = update.message or (cb.message if cb else None)
        user = update.message.from_user if update.message else (cb.from_user if cb else None)
        if not msg or msg.chat.type != 'private' or not user:
            return await handler(update, data)
        bot = data['bot']
        token = None
        if cb:
            async def answer():
                try:
                    await bot.answer_callback_query(cb.id, request_timeout=3)
                except (TelegramAPIError, asyncio.TimeoutError):
                    logging.getLogger(__name__).debug('Callback acknowledgement unavailable')
            background.start(('ack',cb.id), answer)
            token = acked.set(cb.id)
        key = (bot.id, msg.chat.id, user.id)
        lane = self.lanes.setdefault(key, Lane())
        nav = navigation(update)
        now = time.monotonic()
        try:
            signature = f'{msg.message_id}:{cb.data}' if cb else ''
            if cb and signature == lane.last_callback and now-lane.callback_time < 0.8:
                return
            lane.last_callback, lane.callback_time = signature, now
            if nav and nav == lane.last_nav and now-lane.last_time < 0.8:
                return
            lane.last_nav, lane.last_time = nav or '', now
            if lane.waiting >= 32:
                if now-lane.warned > 5:
                    lane.warned = now
                    background.start(('busy',key), lambda: bot.send_message(msg.chat.id,
                        'Слишком много действий подряд. Подождите ответ и нажмите /menu.'))
                return
            lane.serial += 1
            serial = lane.serial
            if not nav:
                lane.group += 1
            group = lane.group
            if nav:
                lane.latest[group] = serial
            lane.waiting += 1
            try:
                async with lane.lock:
                    if nav and lane.latest.get(group) != serial:
                        return  # A newer menu choice superseded this queued choice.
                    return await handler(update, data)
            finally:
                lane.waiting -= 1
                if not lane.waiting:
                    lane.latest.clear()
        finally:
            if token is not None:
                acked.reset(token)
            if len(self.lanes)>4096:
                for old_key, old_lane in list(self.lanes.items()):
                    if old_key != key and not old_lane.waiting and now-old_lane.last_time>60:
                        self.lanes.pop(old_key,None)

def install(dp):
    ingress = Ingress()
    dp.update.outer_middleware.unregister(dp.fsm)
    dp.update.outer_middleware(ingress)
    dp.update.outer_middleware(dp.fsm)
    return ingress

async def wizard_prompt(message, state, text, reply_markup=None):
    sent = await message.answer(text, reply_markup=reply_markup)
    await state.update_data(wizard_message_id=sent.message_id)
    return sent

class WizardGuard(BaseMiddleware):
    async def __call__(self, handler, callback, data):
        raw = callback.data or ''
        protected = raw.startswith(('cal:','date:','hour:','time:','repeat:','manual_select:','manual_page:','delete_confirm:')) or raw in {
            'hours','save_training','manual_confirm'}
        if protected:
            state = data.get('state')
            values = await state.get_data() if state else {}
            if values.get('wizard_message_id') != callback.message.message_id:
                await ack(callback, 'Эта кнопка из предыдущего действия. Используйте последний экран или /menu.')
                return
        return await handler(callback,data)
