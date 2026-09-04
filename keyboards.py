from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def poll_keyboard(attendance_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Приду", callback_data=f"att:{attendance_id}:yes"),
            InlineKeyboardButton(text="❌ Не приду", callback_data=f"att:{attendance_id}:no"),
        ]]
    )


def participants_keyboard(participants) -> InlineKeyboardMarkup | None:
    rows = []
    for p in participants:
        mark = "🟢" if p["is_active"] else "🔴"
        reg = "" if p["is_registered"] else " (не зарегистрирован)"
        uname = f"@{p['username']}" if p["username"] else "—"
        label = f"{mark} {p['full_name'] or '(без имени)'} {uname}{reg}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"toggle:{p['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
