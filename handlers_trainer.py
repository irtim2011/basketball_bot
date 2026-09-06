from interaction import ack
import asyncio
import os
import re
from datetime import date, datetime
from aiogram import Router, F
from aiogram.filters import Command, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
import db
import events
import utils
from config import TRAINER_IDS, TIMEZONE, WEEKDAY_RU
from ui import inline, menu, calendar_keyboard, hours_keyboard
from export_table import build_xlsx
from handlers_menu import show_schedule
from interaction import WizardGuard, wizard_prompt, NAV
import background
import texts
import google_sheet

router = Router(name='trainer')

class IsTrainer(BaseFilter):
    async def __call__(self, event):
        return event.from_user.id in TRAINER_IDS

router.message.filter(IsTrainer())
router.callback_query.filter(IsTrainer())
router.callback_query.outer_middleware(WizardGuard())

class Training(StatesGroup):
    day = State()
    time = State()
    repeat = State()
    confirm = State()

class Roster(StatesGroup):
    add = State()

class Deletion(StatesGroup):
    confirm = State()

async def start_editor(message, state, slot_id=None):
    await state.clear()
    await state.update_data(slot_id=slot_id)
    await state.set_state(Training.day)
    now = utils.today()
    await wizard_prompt(message, state, 'Выберите дату первой тренировки.\nПосле времени можно включить повтор каждую неделю.',
                         reply_markup=calendar_keyboard(now.year, now.month))

@router.message(Command('training', 'add_schedule'))
@router.message(F.text == '➕ Тренировка')
async def training(message: Message, state: FSMContext):
    await start_editor(message, state)

@router.callback_query(Training.day, F.data.startswith('cal:'))
async def calendar_page(callback: CallbackQuery):
    await ack(callback)
    year, month = map(int, callback.data.split(':')[1].split('-'))
    if not 2020 <= year <= 2100 or not 1 <= month <= 12:
        return
    await callback.message.edit_reply_markup(reply_markup=calendar_keyboard(year, month))

@router.callback_query(Training.day, F.data.startswith('date:'))
async def select_date(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    day = date.fromisoformat(callback.data.split(':')[1])
    if day < utils.today():
        await callback.message.answer('Выберите сегодняшнюю или будущую дату.')
        return
    await state.update_data(day=day.isoformat())
    await state.set_state(Training.time)
    await wizard_prompt(callback.message, state, f'{day:%d.%m.%Y}. Выберите час или напишите время ЧЧ:ММ.\nЧасовой пояс: {TIMEZONE}',
                                  reply_markup=hours_keyboard())

@router.callback_query(Training.time, F.data.startswith('hour:'))
async def hour(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    h = int(callback.data.split(':')[1])
    if h not in range(24):
        return
    await callback.message.edit_reply_markup(reply_markup=inline([
        [(f'{h:02d}:{m:02d}', f'time:{h:02d}:{m:02d}') for m in (0,15,30,45)],
        [('Выбрать другой час','hours'), ('Отмена','cancel')]]))

@router.callback_query(Training.time, F.data == 'hours')
async def hours(callback: CallbackQuery):
    await ack(callback)
    await callback.message.edit_reply_markup(reply_markup=hours_keyboard())

async def set_time(message, state, raw):
    time_str = utils.parse_time_str(raw)
    if not time_str:
        await message.answer('Введите время ЧЧ:ММ, например 19:30.')
        return
    data = await state.get_data()
    start = utils.TZ.localize(datetime.fromisoformat(data['day']+'T'+time_str))
    if start <= utils.now():
        await message.answer('Это время уже прошло. Выберите более позднее время или начните заново: /training.')
        return
    await state.update_data(time=time_str)
    await state.set_state(Training.repeat)
    await wizard_prompt(message, state, f"Тренировка {start:%d.%m.%Y %H:%M}. Как часто?",
                         reply_markup=inline([[('Один раз','repeat:once'),('Каждую неделю','repeat:weekly')],
                                              [('Отмена','cancel')]]))

@router.callback_query(Training.time, F.data.startswith('time:'))
async def select_time(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    await set_time(callback.message, state, callback.data[5:])

@router.message(Training.time, F.text, ~F.text.startswith('/'), ~F.text.in_(set(NAV)))
async def text_time(message: Message, state: FSMContext):
    await set_time(message, state, message.text)

@router.callback_query(Training.repeat, F.data.in_({'repeat:once','repeat:weekly'}))
async def repeat(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    weekly = callback.data == 'repeat:weekly'
    await state.update_data(weekly=weekly)
    await state.set_state(Training.confirm)
    data = await state.get_data()
    day = date.fromisoformat(data['day'])
    label = f"Каждую неделю: {WEEKDAY_RU[day.weekday()]}, начиная с {day:%d.%m.%Y}" if weekly else f'{day:%d.%m.%Y}'
    await wizard_prompt(callback.message, state, f"{label}, {data['time']} ({TIMEZONE}).\nСохранить?",
        reply_markup=inline([[('✅ Сохранить','save_training'),('Отмена','cancel')]]))

@router.callback_query(Training.confirm, F.data == 'save_training')
async def save_training(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    data = await state.get_data()
    day = date.fromisoformat(data['day'])
    if utils.TZ.localize(datetime.fromisoformat(data['day']+'T'+data['time'])) <= utils.now():
        await state.clear()
        await callback.message.answer('Выбранное время уже прошло. Создайте тренировку заново: /training.')
        return
    try:
        await events.save_slot(day.weekday(), data['time'], None if data['weekly'] else data['day'],
                               data.get('slot_id'), starts_on=data['day'])
    except ValueError as exc:
        await callback.message.answer(str(exc))
        return
    google_sheet.queue()
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f'🏀 Тренировка сохранена! Опрос отправится {texts.reminder_label()} до начала. Если осталось меньше — в ближайшие секунды.',
                                   reply_markup=menu(callback.from_user.id))

@router.callback_query(F.data.startswith('slot:'))
async def slot_detail(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    await state.clear()
    slot = await events.get_slot(int(callback.data.split(':')[1]))
    if not slot:
        await callback.message.answer('Тренировка уже удалена.')
        return
    start = texts.next_start(slot)
    if start is None:
        await callback.message.answer('Эта тренировка уже прошла. Обновите /schedule.')
        return
    label = texts.schedule_text(slot, start)
    note = '\n\nИзменение относится ко всей серии будущих тренировок.' if not slot['training_date'] else ''
    await callback.message.answer(label + note,
        reply_markup=inline([[('Изменить',f"edit_slot:{slot['id']}"),('Удалить',f"delete_slot:{slot['id']}")],
                              [('Отмена','cancel')]]))

@router.callback_query(F.data.startswith('edit_slot:'))
async def edit_slot(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    slot_id = int(callback.data.split(':')[1])
    if not await events.get_slot(slot_id):
        await callback.message.answer('Тренировка уже удалена.')
        return
    await start_editor(callback.message, state, slot_id)

@router.callback_query(F.data.startswith('delete_slot:'))
async def delete_slot(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    slot_id = int(callback.data.split(':')[1])
    await state.clear()
    await state.update_data(delete_slot_id=slot_id)
    await state.set_state(Deletion.confirm)
    await wizard_prompt(callback.message, state, 'Удалить тренировку? Для еженедельной — прекратятся все будущие повторы. История ответов останется.',
        reply_markup=inline([[('Да, удалить',f'delete_confirm:{slot_id}'),('Отмена','cancel')]]))

@router.callback_query(Deletion.confirm, F.data.startswith('delete_confirm:'))
async def delete_confirm(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    slot_id = int(callback.data.split(':')[1])
    if (await state.get_data()).get('delete_slot_id') != slot_id:
        return
    await events.delete_slot(slot_id)
    google_sheet.queue()
    await state.clear()
    await callback.message.edit_text('Тренировка удалена.')
    await show_schedule(callback.message, callback.from_user.id)

async def roster_page(message, page=0):
    people = await db.list_participants()
    size = 10
    page = max(0, min(page, max(0, (len(people)-1)//size)))
    rows = []
    for p in people[page*size:(page+1)*size]:
        label = f"{'🟢' if p['is_active'] else '⚪'} {p['full_name'] or ('@'+p['username'] if p['username'] else 'ID '+str(p['telegram_id']))}"
        if not p['is_registered']:
            label += ' · ждёт /start'
        rows.append([(label, f"member:{p['id']}:{page}")])
    nav=[]
    if page:
        nav.append(('‹', f'people:{page-1}'))
    if (page+1)*size < len(people):
        nav.append(('›', f'people:{page+1}'))
    if nav:
        rows.append(nav)
    rows.append([('➕ Добавить','add_member')])
    await message.answer('Участники рассылки. 🟢 включён, ⚪ выключен.\nВыберите участника.' if people else 'Список пуст.',
                         reply_markup=inline(rows))

@router.message(Command('participants'))
@router.message(F.text == '👥 Участники')
async def participants(message: Message, state: FSMContext):
    await state.clear()
    await roster_page(message)

@router.callback_query(F.data.startswith('people:'))
async def people(callback: CallbackQuery):
    await ack(callback)
    await roster_page(callback.message, int(callback.data.split(':')[1]))

@router.callback_query(F.data.startswith('member:'))
async def member(callback: CallbackQuery):
    await ack(callback)
    _, raw_id, page = callback.data.split(':')
    p = await db.get_participant(int(raw_id))
    if not p:
        return
    enabled = int(not p['is_active'])
    await callback.message.answer(
        f"{p['full_name'] or 'Не зарегистрирован'}\n@{p['username'] or '—'}\n"
        f"{p['phone'] or 'Телефон появится после регистрации'}\n"
        f"ID участника: {p['public_id']}\n"
        f"Telegram ID: {p['telegram_id'] or 'появится после /start'}\n"
        f"Рассылка: {'включена' if p['is_active'] else 'выключена'}",
        reply_markup=inline([[('Включить' if enabled else 'Выключить',f"active:{p['id']}:{enabled}:{page}")],
                              [('Назад',f'people:{page}')]]))

@router.callback_query(F.data.startswith('active:'))
async def active(callback: CallbackQuery):
    await ack(callback)
    _, raw_id, enabled, page = callback.data.split(':')
    if enabled not in {'0','1'}:
        return
    await db.set_active(int(raw_id), bool(int(enabled)))
    await callback.message.edit_text('Рассылка включена.' if enabled=='1' else 'Рассылка выключена. История сохранена.')
    await roster_page(callback.message,int(page))

async def start_add(message, state):
    await state.clear()
    await state.set_state(Roster.add)
    await message.answer('Отправьте @username или Telegram ID (например 54545234). Можно несколько через пробел.\n'
                         'Участник должен сам открыть бота и нажать /start.\n/cancel — отмена.')

@router.message(Command('add'))
@router.message(F.text == '➕ Участник')
async def add(message: Message, state: FSMContext):
    await start_add(message,state)

@router.callback_query(F.data == 'add_member')
async def add_member(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    await start_add(callback.message,state)

@router.message(Roster.add, F.text, ~F.text.startswith('/'), ~F.text.in_(set(NAV)))
async def add_names(message: Message, state: FSMContext):
    tokens = re.split(r'[\s,;]+',message.text.strip())
    if not tokens or len(tokens)>100 or any(not (t.isdecimal() and 0<int(t)<2**52) and not re.fullmatch(r'@[A-Za-z0-9_]{3,32}',t) for t in tokens):
        await message.answer('Нужны @username или положительный Telegram ID, максимум 100 за один раз.')
        return
    for token in dict.fromkeys(tokens):
        if token.isdecimal():
            await db.add_participant_by_id(int(token))
            continue
        username=utils.normalize_username(token)
        existing=await (await db._c().execute(
            'SELECT * FROM participants WHERE username=? ORDER BY is_registered DESC LIMIT 1',(username,))).fetchone()
        if existing:
            await db.set_active(existing['id'],True)
        else:
            await db.create_stub_participant(username,None)
    await state.clear()
    await message.answer('Список обновлён.',reply_markup=menu(message.from_user.id))
    await roster_page(message)

def can_access_table(message):
    """Workbook access is limited to a trainer's own private conversation."""
    user = getattr(message, 'from_user', None)
    chat = getattr(message, 'chat', None)
    return bool(user and chat and user.id in TRAINER_IDS
                and chat.type == 'private' and chat.id == user.id)


def full_workbook_link():
    return google_sheet.sheet_url() + '?gid=2056812652#gid=2056812652'


@router.message(Command('table'))
@router.message(F.text == '📊 Таблица')
async def table(message: Message, state: FSMContext):
    if not can_access_table(message):
        return
    await state.clear()
    key = ('table', message.from_user.id)
    if background.running(key):
        return
    link = full_workbook_link()
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='📊 Открыть полную таблицу', url=link)
    ]])
    if google_sheet.configured():
        await message.answer(
            'Полная таблица доступна по кнопке ниже.\n'
            'Обновляю данные и готовлю Excel со всеми листами…\n'
            + link, reply_markup=reply_markup)
    else:
        await message.answer('Полная таблица доступна по кнопке ниже.\n'
                             'Готовлю резервную таблицу посещений…\n'
                             + link, reply_markup=reply_markup)
    background.start(key, lambda: export_table(message))

async def export_table(message):
    if not can_access_table(message):
        return
    if google_sheet.configured():
        await google_sheet.sync_now()
        path = await asyncio.to_thread(google_sheet.export_workbook_xlsx)
        caption = ('🏀 Полная таблица: посещения, тарифы и номинальная доходность. '
                   'Красный статус на листе «Тарифы» показывает клиентов с посещением без тарифа.')
    else:
        dates, rows=await events.summary()
        if not rows:
            await message.answer('Пока нет зарегистрированных участников.')
            return
        path=await asyncio.to_thread(build_xlsx,dates,rows)
        caption = ('🏀 Резервная таблица ответов с 1 августа 2026. '
                   'ID участников четырёхзначные. Y — «Приду», N — «Не приду».')
    try:
        if can_access_table(message):
            await message.answer_document(FSInputFile(path),caption=caption)
    finally:
        os.unlink(path)
