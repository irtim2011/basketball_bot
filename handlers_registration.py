from interaction import ack
import re
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
import db
import google_sheet
import utils
from ui import menu
from keyboards import contact_keyboard

router = Router(name='registration')

class Registration(StatesGroup):
    name = State()
    phone = State()

async def begin(message, user, state, editing=False):
    await state.clear()
    existing = await db.get_participant_by_telegram_id(user.id)
    if existing and existing['is_registered'] and not editing:
        await message.answer(f"С возвращением, {existing['full_name']}! Выберите действие.",
                             reply_markup=menu(user.id))
        return
    username = utils.normalize_username(user.username)
    stub = await db.get_participant_by_username(username) if username and not existing else None
    await state.update_data(existing_id=existing['id'] if existing else (stub['id'] if stub else None),
                            username=username)
    await state.set_state(Registration.name)
    await message.answer(f'Введите ФИО строго в порядке «Фамилия Имя Отчество». Ваш Telegram ID: {user.id}.\n/cancel — отменить.')

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await begin(message, message.from_user, state)

@router.callback_query(F.data == 'profile:edit')
async def edit(callback: CallbackQuery, state: FSMContext):
    await ack(callback)
    await begin(callback.message, callback.from_user, state, editing=True)

@router.message(Registration.name, F.text)
async def name(message: Message, state: FSMContext):
    name = utils.registration_fio(message.text)
    if not name or len(name) > 150:
        await message.answer('Введите три слова кириллицей: Фамилия Имя Отчество (до 150 символов).')
        return
    await state.update_data(name=name)
    await state.set_state(Registration.phone)
    await message.answer('Отправьте свой номер кнопкой или введите его, например +79991234567.',
                         reply_markup=contact_keyboard())

@router.message(Registration.phone)
async def phone(message: Message, state: FSMContext):
    if message.contact and message.contact.user_id != message.from_user.id:
        await message.answer('Нужен ваш собственный контакт. Нажмите кнопку отправки номера.')
        return
    raw = message.contact.phone_number if message.contact else message.text or ''
    if not re.fullmatch(r'[+\d\s()\-]{7,30}', raw):
        await message.answer('Введите номер телефона, например +79991234567.')
        return
    number = utils.normalize_phone(raw)
    if not 7 <= len(number.lstrip('+')) <= 15:
        await message.answer('В номере должно быть от 7 до 15 цифр.')
        return
    data = await state.get_data()
    existing = await db.get_participant_by_telegram_id(message.from_user.id)
    participant_id = await db.register_participant(
        message.from_user.id, utils.normalize_username(message.from_user.username),
        data['name'], number, existing['id'] if existing else data.get('existing_id'))
    await state.clear()
    p = await db.get_participant(participant_id)
    google_sheet.queue()
    status = 'Рассылка включена.' if p['is_active'] else 'Тренер должен включить вас в рассылку.'
    await message.answer(
        f"Готово, {data['name']}! Ваш ID участника: {p['public_id']}. {status}",
        reply_markup=menu(message.from_user.id))
