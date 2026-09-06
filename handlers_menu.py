from interaction import ack
import asyncio
from datetime import datetime
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
import db
import events
import utils
from config import TRAINER_IDS, WEEKDAY_RU, TIMEZONE
from ui import menu, inline
from version import VERSION
import texts
import google_sheet

router = Router(name='menu')
fallback = Router(name='fallback')

@router.message(Command('menu', 'help', 'cancel'))
@router.message(F.text.in_({'0', '📋 Показать действия', 'Отмена'}))
async def show_menu(message: Message, state: FSMContext):
    await state.clear()
    text = ('Выберите действие кнопками ниже.\n'
            '/start — регистрация\n/menu или 0 — все действия\n'
            '/schedule — расписание\n/profile — ваши данные\n/id — Telegram ID\n/cancel — отмена ввода')
    if message.from_user.id in TRAINER_IDS:
        text += ('\n\nТренеру: добавляйте тренировки через календарь, управляйте '
                 'участниками, открывайте полную таблицу и скачивайте Excel кнопками ниже.\n'
                 '/training — добавить тренировку\n/participants — участники\n/poll_now — опрос сейчас\n/table — полная таблица и Excel')
    await message.answer(text, reply_markup=menu(message.from_user.id))

@router.callback_query(F.data == 'cancel')
async def cancel(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    await state.clear()
    await callback.message.answer('Действие отменено.', reply_markup=menu(callback.from_user.id))

@router.callback_query(F.data == 'noop')
async def noop(callback: CallbackQuery):
    await ack(callback)

@router.message(Command('profile'))
@router.message(F.text == '👤 Мой профиль')
async def profile(message: Message, state: FSMContext):
    await state.clear()
    p = await db.get_participant_by_telegram_id(message.from_user.id)
    if not p or not p['is_registered']:
        await message.answer(f'Ваш Telegram ID: {message.from_user.id}.\nДля регистрации нажмите /start.', reply_markup=menu(message.from_user.id))
        return
    username = utils.normalize_username(message.from_user.username)
    await db._c().execute('UPDATE participants SET username=? WHERE id=?', (username, p['id']))
    await db._c().commit()
    google_sheet.queue()
    telegram = '@'+username if username else 'без username'
    await message.answer(f"{p['full_name']}\nID участника: {p['public_id']}\nТелеграм: {telegram}\nTelegram ID: {message.from_user.id}\n"
                         f"Телефон: {p['phone']}\nРассылка: {'включена' if p['is_active'] else 'выключена'}",
                         reply_markup=inline([[('Изменить ФИО / телефон','profile:edit')]]))

@router.message(Command('id'))
async def my_id(message: Message):
    await message.answer(f'Ваш Telegram ID: {message.from_user.id}\nОн назначен Telegram и не меняется при смене @username.')

@router.message(Command('version'))
async def version(message: Message):
    await message.answer(f'Бот тренировок · версия {VERSION}\n📱 Команда исполнена с телефона')

async def show_schedule(message, user_id, page=0):
    upcoming = [(texts.next_start(s), s) for s in await db.list_schedule()]
    upcoming = sorted(((start, s) for start, s in upcoming if start is not None), key=lambda item: (item[0], item[1]['id']))
    slots = [s for _, s in upcoming]
    starts = {s['id']: start for start, s in upcoming}
    size = 8
    page = max(0, min(page, max(0, (len(slots)-1)//size)))
    if not slots:
        await message.answer('Расписание пока пустое.', reply_markup=menu(user_id))
        return
    rows = []
    lines = ['🏀 Расписание тренировок\nВыбирай день — встречаемся на площадке!']
    for s in slots[page*size:(page+1)*size]:
        start = starts[s['id']]
        label = ('🔁 ' if not s['training_date'] else '🏀 ') + texts.short_when(start)
        lines.append(texts.schedule_text(s, start))
        if user_id in TRAINER_IDS:
            rows.append([(label, f"slot:{s['id']}")])
    nav = []
    if page:
        nav.append(('‹ Назад',f'schedule_page:{page-1}'))
    if (page+1)*size < len(slots):
        nav.append(('Далее ›',f'schedule_page:{page+1}'))
    if nav:
        rows.append(nav)
    if user_id in TRAINER_IDS:
        lines.append('Нажмите тренировку, чтобы изменить или удалить.')
    await message.answer('\n\n'.join(lines), reply_markup=inline(rows) if rows else menu(user_id))

@router.message(Command('schedule'))
@router.message(F.text == '📅 Расписание')
async def schedule(message: Message, state: FSMContext):
    await state.clear()
    await show_schedule(message, message.from_user.id)

@router.callback_query(F.data.startswith('schedule_page:'))
async def schedule_page(callback: CallbackQuery):
    await ack(callback)
    await show_schedule(callback.message, callback.from_user.id, int(callback.data.split(':')[1]))

@fallback.message()
async def unknown(message: Message):
    await message.answer('Не понял действие. Нажмите /menu или отправьте 0 — покажу кнопки.',
                         reply_markup=menu(message.from_user.id))

@fallback.callback_query()
async def stale(callback: CallbackQuery):
    await ack(callback, 'Эта кнопка устарела. Откройте /menu.', show_alert=True)
