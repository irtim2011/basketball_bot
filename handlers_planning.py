"""Private, paginated planning cards. Forecasts never write attendance responses."""
from datetime import date, datetime
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest

import google_sheet
from interaction import ack
import planning
from ui import inline

router = Router(name='planning')
router.callback_query.filter(F.message.chat.type == 'private')


def render_card(card):
    title = 'План на месяц' if card['kind'] == 'month' else 'План на неделю'
    first, last = date.fromisoformat(card['period_start']), date.fromisoformat(card['period_end'])
    text = (f'🏀 {title} · {first:%d.%m}–{last:%d.%m.%Y}\n\n'
            'На какие тренировки планируете прийти? Это предварительный план.\n'
            'Каждый ответ можно менять до начала соответствующей тренировки. '
            'Ответ в основном опросе учитывается отдельно и имеет приоритет.\n\n')
    rows, lines = [], []
    for entry in card['entries']:
        start = datetime.fromisoformat(entry['starts_at'])
        label = f'{start:%d.%m %H:%M}'
        end = f"–{entry['end_time']}" if entry.get('end_time') else ''
        mark = {'yes': '✅ Приду', 'no': '❌ Не приду', 'pending': '— нет ответа'}[entry['status']]
        suffix = ' · отменена' if entry['is_cancelled'] else (' · началась' if not entry['can_answer'] else '')
        lines.append(f'{label}{end}: {mark}{suffix}')
        if entry['can_answer']:
            base = f"pa:{entry['id']}:{entry['generation']}"
            rows.append([(f'✅ {label}', f"{base}:yes:{card['page']}"),
                         (f'❌ {label}', f"{base}:no:{card['page']}")])
    text += '\n'.join(lines) if lines else 'В этом периоде пока нет тренировок.'
    text += f"\n\nСтраница {card['page']+1} из {card['pages']}. Время московское."
    nav = []
    if card['page']:
        nav.append(('‹ Назад', f"pp:{card['id']}:{card['page']-1}"))
    if card['page']+1 < card['pages']:
        nav.append(('Далее ›', f"pp:{card['id']}:{card['page']+1}"))
    if nav:
        rows.append(nav)
    rows.append([('🔄 Обновить расписание', f"pp:{card['id']}:{card['page']}")])
    return text, inline(rows)


async def _show(callback, poll_id, page):
    card = await planning.get_card(poll_id, callback.from_user.id, callback.message.message_id, page)
    if not card:
        await callback.message.answer('Эта карточка плана недоступна или уже заменена.')
        return
    text, markup = render_card(card)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass  # The identical card may already be displayed.


@router.callback_query(F.data.startswith('pp:'))
async def page(callback):
    await ack(callback)
    try:
        _, poll_id, page_number = callback.data.split(':')
        poll_id, page_number = int(poll_id), int(page_number)
    except (ValueError, TypeError):
        return
    await _show(callback, poll_id, page_number)


@router.callback_query(F.data.startswith('pa:'))
async def answer(callback):
    await ack(callback)
    try:
        _, answer_id, generation, status, page_number = callback.data.split(':')
        answer_id, generation, page_number = int(answer_id), int(generation), int(page_number)
    except (ValueError, TypeError):
        return
    result = await planning.answer_planned(answer_id, generation, callback.from_user.id,
                                           callback.message.message_id, status, callback.id)
    if not result:
        await callback.message.answer('Ответ не сохранён: тренировка началась, отменена или эта кнопка устарела. Обновите карточку плана.')
        return
    if result['changed']:
        google_sheet.queue()
    await _show(callback, result['poll_id'], page_number)
