"""Debounced Google Sheets mirror for the bot-owned attendance tab."""
import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import utils

from config import GOOGLE_CREDENTIALS_FILE, GOOGLE_SHEET_ID, GOOGLE_SHEET_NAME, GOOGLE_SYNC_DISABLED

log = logging.getLogger(__name__)
BASE_HEADERS = ["ID участника", "Telegram ID", "ФИО", "Телеграм", "Телефон"]
FLAG_HEADER = "flag_active"
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
        date_start = 6 if str(header[5] if len(header) > 5 else "").strip() == FLAG_HEADER else 5
        unexpected = [value for value in header[date_start:] if value and not _parse_date_label(value)]
        if unexpected:
            raise ValueError("После flag_active в Посещения_bot должны быть только даты")
    else:
        header = BASE_HEADERS[:]
        date_start = 5

    date_values = {_parse_date_label(value) for value in header[date_start:] if _parse_date_label(value)}
    date_values.update(datetime.fromisoformat(value).date() for value in dates)
    labels = [value.strftime("%d.%m.%Y") for value in sorted(date_values)]
    old_labels = [str(value).strip() for value in header[date_start:]]

    records = []
    for original in existing[2:] if len(existing) > 2 else []:
        if not any(str(value).strip() for value in original):
            continue
        padded = list(original) + [""] * max(0, len(header) - len(original))
        marks = {label: padded[index + date_start] for index, label in enumerate(old_labels) if label}
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
            owner = str(records[index]['base'][1] or '').strip()
            if owner and owner != telegram:
                raise ValueError('Исторический ID принадлежит другому Telegram участнику')
        else:
            candidates = []
            for i, record in enumerate(records):
                canonical, quality = utils.legacy_canonical_fio(str(record['base'][2]))
                if quality == 'готово':
                    candidates.append({'canonical_name': canonical, 'index': i})
            match = utils.unique_legacy_match(participant.get('full_name') or '', candidates)
            if match is not None and not str(records[match['index']]['base'][1] or '').strip():
                index = match['index']
                code = _four_digits(records[index]['base'][0])
                if not code:
                    raise ValueError('Некорректный legacy ID')
                adoptions.append((participant['internal_id'], int(code)))
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
            mark = participant.get("marks", {}).get(iso_date, "")
            if mark:
                record["marks"][_date_label(iso_date)] = mark

    grid = [BASE_HEADERS + [FLAG_HEADER] + labels,
            ["", "", "", "", "День недели", "За последние 30 дней"] +
            [_weekday(label) for label in labels]]
    grid.extend(record["base"] + [""] + [record["marks"].get(label, "") for label in labels]
                for record in records)
    return grid, adoptions


def _column_name(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _active_formula(row, last_column):
    return (f'=IF(COUNTIFS($G$1:${last_column}$1;">="&TODAY()-30;'
            f'$G$1:${last_column}$1;"<="&TODAY();G{row}:{last_column}{row};"Y")>0;'
            '"active";"")')


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
    if len(grid) > 2:
        last_column = _column_name(len(grid[0]))
        formulas = [[_active_formula(row, last_column)] for row in range(3, len(grid) + 1)]
        worksheet.update(values=formulas, range_name=f"F3:F{len(grid)}", raw=False)
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


def export_workbook_xlsx():
    """Export the complete Google workbook, including trainer-owned sheets."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    response = AuthorizedSession(credentials).get(
        f"https://www.googleapis.com/drive/v3/files/{GOOGLE_SHEET_ID}/export",
        params={
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        },
        timeout=60,
    )
    response.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="training_full_")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(response.content)
    except Exception:
        os.unlink(path)
        raise
    return path


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
