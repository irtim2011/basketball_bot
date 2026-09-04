"""Debounced Google Sheets mirror for the bot-owned attendance tab."""
import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import re

from config import GOOGLE_CREDENTIALS_FILE, GOOGLE_SHEET_ID, GOOGLE_SHEET_NAME, GOOGLE_SYNC_DISABLED

log = logging.getLogger(__name__)
BASE_HEADERS = ["ID участника", "Telegram ID", "ФИО", "Телеграм", "Телефон"]
_task = None
_dirty = False
_missing_credentials_logged = False


def sheet_url():
    return f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit"


def configured():
    return bool(
        not GOOGLE_SYNC_DISABLED
        and GOOGLE_SHEET_ID
        and GOOGLE_SHEET_NAME
        and Path(GOOGLE_CREDENTIALS_FILE).is_file()
    )


def _date_label(iso_date):
    return datetime.fromisoformat(iso_date).strftime("%d.%m.%Y")


def _parse_date_label(value):
    try:
        return datetime.strptime(str(value).strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _weekday(value):
    return ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][_parse_date_label(value).weekday()]


def _four_digits(value):
    raw = str(value or "").strip()
    return raw if re.fullmatch(r"\d{4}", raw) else None


def merge_grid(existing, dates, bot_rows):
    """Merge bot data into the tab while retaining imported historical rows."""
    if existing and any(existing[0]):
        header = list(existing[0])
        if header[:5] != BASE_HEADERS:
            raise ValueError("В листе Посещения_bot изменены первые пять заголовков")
        unexpected = [value for value in header[5:] if value and not _parse_date_label(value)]
        if unexpected:
            raise ValueError("После колонки Телефон в Посещения_bot должны быть только даты")
    else:
        header = BASE_HEADERS[:]

    date_values = {_parse_date_label(value) for value in header[5:] if _parse_date_label(value)}
    date_values.update(datetime.fromisoformat(value).date() for value in dates)
    labels = [value.strftime("%d.%m.%Y") for value in sorted(date_values)]
    old_labels = [str(value).strip() for value in header[5:]]

    records = []
    for original in existing[2:] if len(existing) > 2 else []:
        if not any(str(value).strip() for value in original):
            continue
        padded = list(original) + [""] * max(0, len(header) - len(original))
        marks = {label: padded[index + 5] for index, label in enumerate(old_labels) if label}
        records.append({"base": padded[:5], "marks": marks})

    by_code = {}
    by_telegram = {}
    for index, record in enumerate(records):
        code = _four_digits(record["base"][0])
        telegram = str(record["base"][1] or "").strip()
        if code:
            if code in by_code:
                raise ValueError(f"Дублируется ID {code} в Посещения_bot")
            by_code[code] = index
        if telegram:
            if telegram in by_telegram:
                raise ValueError(f"Дублируется Telegram ID {telegram} в Посещения_bot")
            by_telegram[telegram] = index

    adoptions = []
    seen_codes = set()
    seen_telegrams = set()
    for participant in bot_rows:
        code = _four_digits(participant["participant_id"])
        if not code:
            raise ValueError("База вернула некорректный ID участника")
        telegram = str(participant.get("telegram_id") or "").strip()
        if code in seen_codes or (telegram and telegram in seen_telegrams):
            raise ValueError("В базе дублируется ID участника")
        seen_codes.add(code)
        if telegram:
            seen_telegrams.add(telegram)

        if telegram and telegram in by_telegram:
            index = by_telegram[telegram]
            historical_code = _four_digits(records[index]["base"][0])
            if historical_code and historical_code != code:
                code = historical_code
                adoptions.append((participant["internal_id"], int(historical_code)))
        elif code in by_code:
            index = by_code[code]
        else:
            index = len(records)
            records.append({"base": [code, "", "", "", ""], "marks": {}})
            by_code[code] = index

        record = records[index]
        record["base"] = [
            code,
            telegram,
            participant.get("full_name") or "",
            "@" + participant["username"] if participant.get("username") else "",
            participant.get("phone") or "",
        ]
        for iso_date in dates:
            record["marks"][_date_label(iso_date)] = participant.get("marks", {}).get(iso_date, "")

    grid = [BASE_HEADERS + labels, ["", "", "", "", "День недели"] + [_weekday(label) for label in labels]]
    grid.extend(record["base"] + [record["marks"].get(label, "") for label in labels] for record in records)
    return grid, adoptions


def _column_name(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _worksheet():
    import gspread
    from gspread.http_client import BackOffHTTPClient
    client = gspread.service_account(
        filename=GOOGLE_CREDENTIALS_FILE, http_client=BackOffHTTPClient
    )
    return client.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_SHEET_NAME)


def _sync_blocking(dates, rows):
    worksheet = _worksheet()
    existing = worksheet.get_all_values(pad_values=False)
    grid, adoptions = merge_grid(existing, dates, rows)
    required_rows = max(len(grid), worksheet.row_count)
    required_cols = max(len(grid[0]), worksheet.col_count)
    if required_rows != worksheet.row_count or required_cols != worksheet.col_count:
        worksheet.resize(rows=required_rows, cols=required_cols)
    last = f"{_column_name(len(grid[0]))}{len(grid)}"
    worksheet.update(values=grid, range_name=f"A1:{last}", raw=True)
    return adoptions


def check_blocking():
    credentials = Path(GOOGLE_CREDENTIALS_FILE)
    if not credentials.is_file():
        raise FileNotFoundError(f"Нет файла {credentials}")
    client_email = json.loads(credentials.read_text(encoding="utf-8")).get("client_email", "")
    worksheet = _worksheet()
    header = worksheet.row_values(1)
    if header[:5] != BASE_HEADERS:
        raise ValueError("Лист найден, но его первые пять заголовков изменены")
    return {"client_email": client_email, "title": worksheet.title, "url": sheet_url()}


async def sync_now():
    import db
    import events
    dates, rows = await events.summary()
    adoptions = await asyncio.to_thread(_sync_blocking, dates, rows)
    for participant_id, public_id in adoptions:
        if not await db.adopt_public_id(participant_id, public_id):
            log.error("Could not adopt Google Sheet participant ID %s", public_id)


async def _worker():
    global _dirty
    while _dirty:
        _dirty = False
        await asyncio.sleep(0.5)
        try:
            await sync_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Google Sheet synchronization failed")


def queue():
    global _dirty, _task, _missing_credentials_logged
    if not configured():
        if not _missing_credentials_logged:
            log.warning("Google Sheet sync is waiting for %s", GOOGLE_CREDENTIALS_FILE)
            _missing_credentials_logged = True
        return False
    _dirty = True
    if _task is None or _task.done():
        _task = asyncio.create_task(_worker(), name="google-sheet-sync")
    return True


async def close():
    global _task
    if _task and not _task.done():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _task = None
