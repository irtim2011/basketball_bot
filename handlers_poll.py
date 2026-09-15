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
import planning
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
    owner = await (await db._c().execute(
        'SELECT p.telegram_id FROM responses r JOIN participants p ON p.id=r.participant_id WHERE r.id=?',
        (response_id,))).fetchone()
    if not owner or owner['telegram_id'] != callback.from_user.id:
        return
    row = await planning.record_main_answer(
        response_id, callback.from_user.id, callback.message.message_id,
        answer, callback.id, utils.now())
    if not row:
        await callback.message.answer('Этот опрос закрыт: тренировка отменена, перенесена или кнопка относится к другому сообщению.')
        return
    start = datetime.fromisoformat(row['starts_at'])
    slot = await events.get_slot(row['schedule_id'])
    google_sheet.queue()
    try:
        await callback.message.edit_text(
            texts.poll_text(start, row['status'], end=events.end_time(slot, start) if slot else None),
            reply_markup=inline([[('✅ Приду', f'r:{response_id}:yes'), ('❌ Не приду', f'r:{response_id}:no')]]))
    except TelegramBadRequest:
        pass

@router.callback_query(F.data.startswith('att:'))
async def legacy_answer(callback: CallbackQuery):
    await ack(callback, 'Старый опрос закрыт. Ответьте на новый опрос перед тренировкой.', show_alert=True)
