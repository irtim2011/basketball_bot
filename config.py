import os
from dotenv import load_dotenv

load_dotenv(os.getenv('ENV_FILE', '.env'))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Telegram numeric user IDs of trainers (comma separated in .env), e.g. TRAINER_IDS=111111,222222
TRAINER_IDS = {
    int(x.strip()) for x in os.getenv("TRAINER_IDS", "").split(",") if x.strip()
}

DB_PATH = os.getenv("DB_PATH", "training_bot.db")

# The bot writes only this dedicated tab. Authentication uses a Google Cloud
# service-account JSON stored outside the application release directory.
GOOGLE_SHEET_ID = os.getenv(
    "GOOGLE_SHEET_ID", "1YNdUTiRQZ5q_NFu8dlNGDqTbSV3nmwZseq4ZIwy2s_g"
).strip()
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Посещения_bot").strip()
GOOGLE_CREDENTIALS_FILE = os.path.expanduser(os.getenv(
    "GOOGLE_CREDENTIALS_FILE", "~/.config/training-bot/google-service-account.json"
))
GOOGLE_SYNC_DISABLED = os.getenv("GOOGLE_SYNC_DISABLED", "").strip() == "1"

# How many minutes before training the poll should be sent
POLL_OFFSET_MINUTES = int(os.getenv("POLL_OFFSET_MINUTES", "180"))
if not 1 <= POLL_OFFSET_MINUTES <= 10080:
    raise ValueError('POLL_OFFSET_MINUTES must be between 1 and 10080')

# IANA timezone name used for scheduling
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

WEEKDAY_ALIASES = {
    "mon": 0, "monday": 0, "пн": 0,
    "tue": 1, "tuesday": 1, "вт": 1,
    "wed": 2, "wednesday": 2, "ср": 2,
    "thu": 3, "thursday": 3, "чт": 3,
    "fri": 4, "friday": 4, "пт": 4,
    "sat": 5, "saturday": 5, "сб": 5,
    "sun": 6, "sunday": 6, "вс": 6,
}

WEEKDAY_RU = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье",
}

WEEKDAY_SHORT_RU = {
    0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс",
}
