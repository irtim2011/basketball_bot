"""Paste/forward a dated schedule, review the differences, then confirm once."""
from datetime import date, timedelta
import logging
import sqlite3
from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from handlers_trainer import router
from interaction import ack, wizard_prompt, NAV
from ui import inline, menu
from config import WEEKDAY_SHORT_RU
from schedule_parser import parse_schedule
from schedule_import import build_plan, apply_plan
import google_sheet
import texts
import utils

log = logging.getLogger(__name__)


class ScheduleImport(StatesGroup):
    text = State()
    review = State()
    confirm = State()


def entry_label(entry):
    day = date.fromisoformat(entry['date'])
    end = entry.get('end_time')
    return f"{day:%d.%m.%Y}, {WEEKDAY_SHORT_RU[day.weekday()]} {entry['time']}" + (f'–{end}' if end else '')


async def prompt_text(message, state, correcting=False):
    await state.set_state(ScheduleImport.text)
    example = utils.today() + timedelta(days=1)
    intro = ('Пришлите исправленный список целиком — он заменит предыдущий черновик.'
             if correcting else 'Пришлите или перешлите текст расписания одним сообщением.')
    await wizard_prompt(message, state,
        '📋 Расписание из сообщения\n\n' + intro + '\nПо одной тренировке в строке, например:\n\n'
        f'{example:%d.%m.%Y}, {WEEKDAY_SHORT_RU[example.weekday()]} 19:30–21:30\n'
        f'{example + timedelta(days=2):%d.%m.%Y} 20:00–22:00\n\n'
        f'Конец и день недели можно не указывать. Без года использую {utils.today().year}. '
        f'Время — {texts.zone_label()}.\n'
        'Сначала покажу даты и отличия от текущего расписания. Сохранение — только после проверки.',
        reply_markup=inline([[('Отмена', 'cancel')]]))


@router.message(Command('import_schedule'))
@router.message(F.text == '📋 Вставить расписание')
async def start_import(message: Message, state: FSMContext):
    await state.clear()
    await prompt_text(message, state)


@router.callback_query(F.data == 'open_import')
async def open_import(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    await state.clear()
    await prompt_text(callback.message, state)


@router.message(ScheduleImport.text, F.text, ~F.text.startswith('/'), ~F.text.in_(set(NAV)))
async def receive_text(message: Message, state: FSMContext):
    parsed = parse_schedule(message.text, utils.today())
    if parsed['errors']:
        errors = parsed['errors'][:8]
        tail = f"\nИ ещё ошибок: {len(parsed['errors']) - 8}." if len(parsed['errors']) > 8 else ''
        await message.answer('Не удалось принять весь список:\n\n' + '\n'.join(errors) + tail +
                             '\n\nИсправьте и пришлите список целиком. Расписание ещё не изменено.')
        return
    try:
        plan = await build_plan(parsed['entries'])
    except ValueError as exc:
        await message.answer(str(exc) + '\nПришлите исправленный список.')
        return
    except (sqlite3.Error, OSError, RuntimeError):
        log.exception('Could not build schedule import preview')
        await message.answer('Сейчас не удалось прочитать расписание. Пришлите список ещё раз через несколько секунд.')
        return
    await state.update_data(import_plan=plan, import_warnings=parsed['warnings'])
    await show_review(message, state)


def review_lines(plan):
    lines = []
    for item in plan['additions']:
        lines.append((item['date'], item['time'], '➕ Добавить: ' + entry_label(item)))
    for item in plan['existing']:
        tag = '🕒 Обновить окончание: ' if item.get('end_time_changed') else '✅ Уже есть: '
        lines.append((item['date'], item['time'], tag + entry_label(item)))
    for item in plan['past']:
        lines.append((item['date'], item['time'], '⏪ Прошло, пропускаю: ' + entry_label(item)))
    for item in plan['missing']:
        lines.append((item['date'], item['time'], '❓ Нет в сообщении: ' + entry_label(item)))
    return [line for _, _, line in sorted(lines)]


async def show_review(message, state, page=0):
    data = await state.get_data()
    plan = data['import_plan']
    lines = review_lines(plan)
    pages = max(1, (len(lines) + 11) // 12)
    page = max(0, min(page, pages - 1))
    updates = sum(bool(item.get('end_time_changed')) for item in plan['existing'])
    start = date.fromisoformat(plan['range_start'])
    end = date.fromisoformat(plan['range_end'])
    body = (f'🏀 Проверьте расписание · {texts.zone_label()}\n'
            f'Период сравнения: {start:%d.%m.%Y}–{end:%d.%m.%Y}\n'
            f"Добавить: {len(plan['additions'])} · Уже есть: {len(plan['existing'])}\n"
            f"Обновить окончание: {updates} · Прошло: {len(plan['past'])}\n"
            f"Нет в сообщении: {len(plan['missing'])}\n\n" +
            '\n'.join(lines[page*12:(page+1)*12]))
    if pages > 1:
        body += f'\n\nСтраница {page+1} из {pages}'
    if data.get('import_warnings'):
        body += '\n\n' + '\n'.join(data['import_warnings'][:3])
    body += '\n\nНовые тренировки добавятся на конкретные даты. Совпадения не дублируются.'
    if plan['missing']:
        body += ('\nКнопка «Добавить» сохраняет остальные тренировки. '
                 'Если список полный на этот период, можно отдельно подтвердить отмену отсутствующих дат.')
    rows = []
    nav = []
    if page:
        nav.append(('‹ Назад', f'import:page:{page-1}'))
    if page+1 < pages:
        nav.append(('Далее ›', f'import:page:{page+1}'))
    if nav:
        rows.append(nav)
    if plan['additions'] or plan['existing']:
        rows.append([('✅ Добавить / обновить', 'import:choose:add')])
        if plan['missing']:
            rows.append([(f"⚠️ Также отменить отсутствующие ({len(plan['missing'])})", 'import:choose:replace')])
    rows.append([('✏️ Исправить текст', 'import:edit'), ('Отмена', 'cancel')])
    await state.set_state(ScheduleImport.review)
    await wizard_prompt(message, state, body, reply_markup=inline(rows))


@router.callback_query(ScheduleImport.review, F.data.startswith('import:page:'))
async def review_page(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    await show_review(callback.message, state, int(callback.data.rsplit(':', 1)[1]))


@router.callback_query(F.data == 'import:edit')
async def edit_text(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    await state.clear()
    await prompt_text(callback.message, state, correcting=True)


@router.callback_query(ScheduleImport.review, F.data.in_({'import:choose:add', 'import:choose:replace'}))
async def choose_mode(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    plan = (await state.get_data())['import_plan']
    replace = callback.data.endswith(':replace')
    await state.update_data(import_cancel_missing=replace)
    await state.set_state(ScheduleImport.confirm)
    body = (f"Сохранить расписание?\n\n➕ Новых тренировок: {len(plan['additions'])}.\n"
            f"🕒 Изменений окончания: {sum(bool(e.get('end_time_changed')) for e in plan['existing'])}.\n")
    if replace:
        body += f"❌ Отменить конкретных тренировок: {len(plan['missing'])}.\n"
        body += '\n'.join(entry_label(item) for item in plan['missing'][:18])
        if len(plan['missing']) > 18:
            body += f"\nИ ещё {len(plan['missing'])-18}; весь список — в предпросмотре."
        body += '\n\nДругие даты еженедельных серий сохранятся. Опросы на отменённые даты закроются.'
    else:
        body += 'Другие тренировки останутся в расписании.'
    body += f'\n\nНовые опросы отправляются {texts.reminder_label()} до начала; если осталось меньше — после сохранения.'
    if any(item.get('end_time_changed') for item in plan['existing']):
        body += '\nНовое окончание появится в «Расписание». Текст уже отправленных опросов автоматически не изменится.'
    await wizard_prompt(callback.message, state, body,
        reply_markup=inline([[('✅ Подтвердить', 'import:save')],
                             [('← К проверке', 'import:back'), ('Отмена', 'cancel')]]))


@router.callback_query(ScheduleImport.confirm, F.data == 'import:back')
async def back(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    await show_review(callback.message, state)


@router.callback_query(ScheduleImport.confirm, F.data == 'import:save')
async def save_import(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    data = await state.get_data()
    try:
        result = await apply_plan(data['import_plan'], data['import_cancel_missing'])
    except ValueError as exc:
        await callback.message.answer(str(exc))
        # Another trainer or the clock may have changed the schedule since preview.
        await refresh_review(callback.message, state)
        return
    except (sqlite3.Error, OSError, RuntimeError):
        log.exception('Could not confirm schedule import')
        await storage_problem(callback.message, state)
        return
    await state.clear()
    google_sheet.queue()
    await callback.message.answer(
        f"🏀 Расписание сохранено!\nДобавлено: {result['added']}.\n"
        f"Совпадений без дублей: {result['existing']}.\n"
        f"Обновлено окончаний: {result['end_times_updated']}.\n"
        f"Отменено конкретных дат: {result['cancelled']}.\n\nОткройте «📅 Расписание», чтобы посмотреть результат.",
        reply_markup=menu(callback.from_user.id))


async def storage_problem(message, state):
    await state.set_state(ScheduleImport.review)
    await wizard_prompt(message, state,
        'Не удалось подтвердить результат сохранения. Черновик сохранён. '
        'Нажмите «Проверить состояние»: перечитаю расписание и покажу, что уже есть, прежде чем повторять сохранение.',
        reply_markup=inline([[('🔄 Проверить состояние', 'import:refresh')], [('Отмена', 'cancel')]]))


async def refresh_review(message, state):
    data = await state.get_data()
    try:
        plan = await build_plan(data['import_plan']['entries'])
    except (sqlite3.Error, OSError, RuntimeError):
        log.exception('Could not refresh schedule import preview')
        await storage_problem(message, state)
        return
    await state.update_data(import_plan=plan)
    await show_review(message, state)


@router.callback_query(ScheduleImport.review, F.data == 'import:refresh')
async def retry_read(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    await refresh_review(callback.message, state)


@router.message(ScheduleImport.text, ~F.text)
async def need_text(message: Message):
    await message.answer('Пришлите расписание текстом или перешлите текстовое сообщение. Фото пока не распознаю.')
