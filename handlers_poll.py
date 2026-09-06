from interaction import ack
from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.exceptions import TelegramBadRequest
import db
import events
import utils
import texts
import google_sheet
from ui import inline
router = Router(name='poll')

@router.callback_query(F.data.startswith('r:'))
async def process_answer(callback: CallbackQuery):
    await ack(callback)
    try:
        _, raw_id, answer = callback.data.split(':')
        response_id = int(raw_id)
    except (ValueError, TypeError):
        return
    if answer not in {'yes', 'no'}:
        return
    row = await (await db._c().execute(
        'SELECT r.*, p.telegram_id FROM responses r JOIN participants p ON p.id=r.participant_id WHERE r.id=?',
        (response_id,))).fetchone()
    if not row or row['telegram_id'] != callback.from_user.id:
        return
    start = datetime.fromisoformat(row['starts_at'])
    slot = await events.get_slot(row['schedule_id'])
    valid = slot and slot['time'] == start.strftime('%H:%M') and (
        slot['training_date'] == start.date().isoformat() if slot['training_date'] else slot['weekday'] == start.weekday())
    if valid and slot['starts_on'] and start.date().isoformat() < slot['starts_on']:
        valid = False
    if (row['is_cancelled'] or row['message_id'] != callback.message.message_id
            or not valid or utils.now() >= start):
        await callback.message.answer('Этот опрос закрыт: тренировка уже началась, отменена или перенесена.')
        return
    await db._c().execute('UPDATE responses SET status=?, responded_at=? WHERE id=?',
                          (answer, utils.now().isoformat(), response_id))
    await db._c().commit()
    google_sheet.queue()
    mark = '✅ Приду' if answer == 'yes' else '❌ Не приду'
    try:
        await callback.message.edit_text(
            texts.poll_text(start, answer),
            reply_markup=inline([[('✅ Приду', f'r:{response_id}:yes'), ('❌ Не приду', f'r:{response_id}:no')]]))
    except TelegramBadRequest:
        pass

@router.callback_query(F.data.startswith('att:'))
async def legacy_answer(callback: CallbackQuery):
    await ack(callback, 'Старый опрос закрыт. Ответьте на новый опрос перед тренировкой.', show_alert=True)
