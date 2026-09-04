"""Readable Russian dates and basketball messages shared by every screen."""
from datetime import datetime, timedelta
from config import TIMEZONE, WEEKDAY_RU, WEEKDAY_SHORT_RU, POLL_OFFSET_MINUTES
import events
import utils

MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
          'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')
WEEKLY = ('Каждый понедельник', 'Каждый вторник', 'Каждую среду',
          'Каждый четверг', 'Каждую пятницу', 'Каждую субботу', 'Каждое воскресенье')

def date_label(day):
    return f'{day.day} {MONTHS[day.month - 1]} {day.year}'

def zone_label():
    return 'по Москве' if TIMEZONE == 'Europe/Moscow' else f'({TIMEZONE})'

def when(start):
    return f'📅 {WEEKDAY_RU[start.weekday()]}, {date_label(start)}\n🕒 Начало в {start:%H:%M} {zone_label()}'

def next_start(slot, now=None):
    now = now or utils.now()
    if slot['starts_on']:
        first = utils.TZ.localize(datetime.fromisoformat(slot['starts_on']))
        now = max(now, first - timedelta(seconds=1))
    return next(events.occurrences(slot, now), None)

def schedule_text(slot, start):
    if slot['training_date']:
        return f'🏀 Разовая тренировка\n{when(start)}'
    return (f'🔁 {WEEKLY[slot["weekday"]]}\n'
            f'🕒 Начало в {slot["time"]} {zone_label()}\n'
            f'📅 Ближайшая: {date_label(start)}')

def short_when(start):
    return f'{WEEKDAY_SHORT_RU[start.weekday()]}, {start.day} {MONTHS[start.month - 1]} в {start:%H:%M}'

def poll_text(start, answer=None):
    text = f'🏀 Собираемся на баскетбол!\n\n{when(start)}\n'
    if answer is None:
        return text + '\n🔥 Пора размяться, прокачать бросок и сыграть с командой!\nТы с нами? Жми кнопку ниже 👇'
    if answer == 'yes':
        return text + '\n✅ Ты в составе! Ждём на площадке 💪🏀\nОтвет можно изменить до начала тренировки.'
    return text + '\n❌ Записал: не придёшь. Увидимся на следующей тренировке! 🏀\nЕсли планы изменятся — нажми «Приду» до начала.'

def reminder_label():
    if POLL_OFFSET_MINUTES == 180:
        return 'за 3 часа'
    return f'за {POLL_OFFSET_MINUTES} мин.'
