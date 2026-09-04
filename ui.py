import calendar
from datetime import date
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from config import TRAINER_IDS, WEEKDAY_SHORT_RU

def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows])

def menu(user_id):
    rows = [["📅 Расписание", "👤 Мой профиль"], ["📋 Показать действия"]]
    if user_id in TRAINER_IDS:
        rows = [["➕ Тренировка", "📅 Расписание"], ["👥 Участники", "➕ Участник"],
                ["📣 Опрос сейчас", "📊 Таблица"], ["👤 Мой профиль", "📋 Показать действия"]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
                               resize_keyboard=True, is_persistent=True)

def calendar_keyboard(year, month):
    rows = [[(f"{month:02d}.{year}", 'noop')], [(WEEKDAY_SHORT_RU[i], 'noop') for i in range(7)]]
    for week in calendar.monthcalendar(year, month):
        rows.append([(str(day), f"date:{year:04d}-{month:02d}-{day:02d}") if day else ('·', 'noop') for day in week])
    previous = date(year - (month == 1), 12 if month == 1 else month - 1, 1)
    following = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    rows += [[('‹', f'cal:{previous:%Y-%m}'), ('›', f'cal:{following:%Y-%m}')], [('Отмена', 'cancel')]]
    return inline(rows)

def hours_keyboard():
    return inline([[(f'{h:02d}:00', f'hour:{h}') for h in range(i, i + 4)] for i in range(0, 24, 4)]
                  + [[('Отмена', 'cancel')]])
