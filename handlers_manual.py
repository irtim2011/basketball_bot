"""Trainer-triggered poll delivery uses the same durable records as the scheduler."""
from datetime import datetime, timedelta
from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import db
import events
import utils
import texts
from handlers_trainer import router
from interaction import ack, wizard_prompt
from ui import inline
from scheduler import kick

class Manual(StatesGroup):
    choose = State()
    confirm = State()

@router.message(Command('poll_now'))
@router.message(F.text == '📣 Опрос сейчас')
async def choose(message: Message, state: FSMContext):
    await state.clear()
    now=utils.now()
    options=[]
    for slot in await db.list_schedule():
        baseline=now
        if slot['starts_on']:
            first=utils.TZ.localize(datetime.fromisoformat(slot['starts_on']))
            baseline=max(now,first-timedelta(seconds=1))
        start=next(events.occurrences(slot,baseline),None)
        if start:
            options.append((start,slot['id']))
    options.sort()
    if not options:
        await message.answer('Сначала создайте тренировку кнопкой «Тренировка», затем выберите «Опрос сейчас».')
        return
    # Keep callback data short; the complete chosen timestamp lives in the FSM.
    await state.update_data(options={str(i):{'id':sid,'start':start.isoformat()} for i,(start,sid) in enumerate(options)})
    await state.set_state(Manual.choose)
    await choose_page(message,state,0)

async def choose_page(message,state,page):
    data=await state.get_data()
    options=data['options']
    size=8
    last=max(0,(len(options)-1)//size)
    page=max(0,min(page,last))
    rows=[]
    for i in range(page*size,min((page+1)*size,len(options))):
        item=options[str(i)]
        start=datetime.fromisoformat(item['start'])
        rows.append([(texts.short_when(start),f'manual_select:{i}')])
    nav=[]
    if page: nav.append(('‹',f'manual_page:{page-1}'))
    if page<last: nav.append(('›',f'manual_page:{page+1}'))
    if nav: rows.append(nav)
    rows.append([('Отмена','cancel')])
    await wizard_prompt(message,state,'Для какой тренировки отправить опрос прямо сейчас?',reply_markup=inline(rows))

@router.callback_query(Manual.choose,F.data.startswith('manual_page:'))
async def page(callback: CallbackQuery,state:FSMContext):
    await ack(callback)
    await choose_page(callback.message,state,int(callback.data.split(':')[1]))

@router.callback_query(Manual.choose,F.data.startswith('manual_select:'))
async def select(callback: CallbackQuery,state:FSMContext):
    await ack(callback)
    data=await state.get_data()
    selected=data['options'].get(callback.data.split(':')[1])
    if not selected:
        return
    slot=await events.get_slot(selected['id'])
    start=datetime.fromisoformat(selected['start'])
    if not events.matches(slot,start) or start<=utils.now():
        await state.clear()
        await callback.message.answer('Тренировка изменилась. Откройте /poll_now заново.')
        return
    people=await db.get_active_registered_participants()
    sent=await (await db._c().execute(
        'SELECT participant_id FROM responses WHERE schedule_id=? AND starts_at=? AND message_id IS NOT NULL',
        (slot['id'],start.isoformat()))).fetchall()
    sent_ids={r['participant_id'] for r in sent}
    count=sum(p['id'] not in sent_ids for p in people)
    await state.update_data(selected=selected)
    await state.set_state(Manual.confirm)
    await wizard_prompt(callback.message,state,
        f'🏀 Отправить опрос сейчас?\n{texts.when(start)}\n\n'
        f'Новых получателей: {count}. Уже отправленные опросы не дублируются.\n'
        'Состав рассылки проверяется ещё раз при отправке.',
        reply_markup=inline([[('📣 Отправить сейчас','manual_confirm'),('Отмена','cancel')]]))

@router.callback_query(Manual.confirm,F.data=='manual_confirm')
async def confirm(callback: CallbackQuery,state:FSMContext,bot):
    await ack(callback)
    selected=(await state.get_data())['selected']
    slot=await events.get_slot(selected['id'])
    start=datetime.fromisoformat(selected['start'])
    if not events.matches(slot,start) or start<=utils.now():
        await state.clear()
        await callback.message.answer('Тренировка изменилась или уже началась. Откройте /poll_now заново.')
        return
    await events.queue_manual(slot['id'],start,callback.from_user.id)
    await state.clear()
    try:
        await callback.message.edit_text(f'Опрос на {start:%d.%m.%Y %H:%M} поставлен на отправку. Меню можно использовать.')
    finally:
        kick(bot)
