"""Idempotent setup for trainer-owned purchase and finance sheets."""
import asyncio
from datetime import date, timedelta

import gspread

from config import GOOGLE_CREDENTIALS_FILE, GOOGLE_SHEET_ID
import db
import utils


GREEN = {"red": 0.05, "green": 0.40, "blue": 0.19}
RED = {"red": 0.72, "green": 0.19, "blue": 0.16}
PALE_RED = {"red": 1.0, "green": 0.80, "blue": 0.80}
PALE_YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.72}
PALE_GRAY = {"red": 0.94, "green": 0.94, "blue": 0.94}
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
WARNING = "⚠️ ЕСТЬ ПОСЕЩЕНИЕ — ЗАПОЛНИТЕ ТАРИФ"


def _client():
    from gspread.http_client import BackOffHTTPClient
    return gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE, http_client=BackOffHTTPClient)


def _sheet(book, title, rows, cols):
    try:
        sheet = book.worksheet(title)
        sheet.resize(rows=max(sheet.row_count, rows), cols=max(sheet.col_count, cols))
        return sheet
    except gspread.WorksheetNotFound:
        return book.add_worksheet(title=title, rows=rows, cols=cols)


def _header_request(sheet_id, start_row, end_row, start_col, end_col, color=GREEN):
    return {
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": start_row, "endRowIndex": end_row,
                      "startColumnIndex": start_col, "endColumnIndex": end_col},
            "cell": {"userEnteredFormat": {
                "backgroundColor": color,
                "textFormat": {"bold": True, "foregroundColor": WHITE},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
        }
    }


def _column_width(sheet_id, index, pixels):
    return {
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": index, "endIndex": index + 1},
            "properties": {"pixelSize": pixels},
            "fields": "pixelSize",
        }
    }


def _column_name(number):
    """Return a 1-based Google Sheets column number as A1 notation."""
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _has_conditional_formula(book, sheet_id, formula):
    metadata = book.fetch_sheet_metadata(
        params={"fields": "sheets(properties(sheetId),conditionalFormats)"}
    )
    for item in metadata.get("sheets", []):
        if item.get("properties", {}).get("sheetId") != sheet_id:
            continue
        for rule in item.get("conditionalFormats", []):
            values = rule.get("booleanRule", {}).get("condition", {}).get("values", [])
            if any(value.get("userEnteredValue") == formula for value in values):
                return True
    return False


def _set_tariff_alert(book):
    sheet = book.worksheet("Тарифы")
    expected = ["ID участника", "ФИО", "Тариф, ₽", "Состояние", "Источник"]
    if sheet.get("A1:E1", value_render_option="FORMULA")[0] != expected:
        raise RuntimeError("Неожиданные заголовки листа Тарифы")
    sheet.resize(rows=max(sheet.row_count, 200), cols=max(sheet.col_count, 8))
    formulas = []
    for row in range(2, 200):
        attendance_row = row + 1
        formulas.append([
            f'=IF(A{row}="";"";IF(AND(COUNTIF(\'Посещения_bot\'!G{attendance_row}:FC{attendance_row};"Y")>0;'
            f'OR(C{row}="";C{row}<=0));"{WARNING}";'
            f'IF(OR(C{row}="";C{row}<=0);"тариф не заполнен";"готово")))'
        ])
    sheet.update(values=formulas, range_name="D2:D199", raw=False)
    sheet.update(values=[
        ["Клиенты с посещением без тарифа", "Что сделать"],
        ['=COUNTIF(D2:D199;"⚠️*")',
         '=IF(G2=0;"Все тарифы заполнены";"Заполните красные строки — прибыль сейчас занижена")'],
    ], range_name="G1:H2", raw=False)
    requests = [
        _header_request(sheet.id, 0, 1, 6, 8, RED),
        _column_width(sheet.id, 3, 330),
        _column_width(sheet.id, 6, 235),
        _column_width(sheet.id, 7, 340),
    ]
    tariff_rule_formula = f'=$D2="{WARNING}"'
    if not _has_conditional_formula(book, sheet.id, tariff_rule_formula):
        requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 199,
                        "startColumnIndex": 0, "endColumnIndex": 5}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [
                {"userEnteredValue": tariff_rule_formula}]},
                "format": {"backgroundColor": PALE_RED,
                           "textFormat": {"bold": True, "foregroundColor": {"red": 0.55}}}},
        }}})
    book.batch_update({"requests": requests})


def _set_purchases(book):
    sheet = _sheet(book, "Покупки тарифов", 500, 7)
    headers = [["Дата покупки", "ID участника", "ФИО", "Куплено тренировок",
                "Тариф за тренировку, ₽", "Доход, ₽", "Комментарий"]]
    existing = sheet.get("A1:G1", value_render_option="FORMULA")
    if existing and any(existing[0]) and existing[0] != headers[0]:
        raise RuntimeError("Неожиданные заголовки листа Покупки тарифов")
    sheet.update(values=headers, range_name="A1:G1", raw=True)
    names, tariffs, incomes = [], [], []
    for row in range(2, 501):
        names.append([f'=IF(B{row}="";"";IFNA(VLOOKUP(B{row};\'Тарифы\'!A:B;2;FALSE);"ID не найден"))'])
        tariffs.append([f'=IF(B{row}="";"";IFNA(VLOOKUP(B{row};\'Тарифы\'!A:C;3;FALSE);""))'])
        incomes.append([f'=IF(OR(A{row}="";B{row}="";D{row}="";E{row}="");"";D{row}*E{row})'])
    sheet.update(values=names, range_name="C2:C500", raw=False)
    sheet.update(values=tariffs, range_name="E2:E500", raw=False)
    sheet.update(values=incomes, range_name="F2:F500", raw=False)
    requests = [
        _header_request(sheet.id, 0, 1, 0, 7),
        {"updateSheetProperties": {"properties": {"sheetId": sheet.id,
                                                    "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 500,
                                    "startColumnIndex": 0, "endColumnIndex": 7},
                        "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE"}},
                        "fields": "userEnteredFormat.verticalAlignment"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 500,
                                    "startColumnIndex": 0, "endColumnIndex": 1},
                        "cell": {"userEnteredFormat": {"backgroundColor": PALE_YELLOW,
                                                        "numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}}},
                        "fields": "userEnteredFormat(backgroundColor,numberFormat)"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 500,
                                    "startColumnIndex": 1, "endColumnIndex": 2},
                        "cell": {"userEnteredFormat": {"backgroundColor": PALE_YELLOW}},
                        "fields": "userEnteredFormat.backgroundColor"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 500,
                                    "startColumnIndex": 3, "endColumnIndex": 4},
                        "cell": {"userEnteredFormat": {"backgroundColor": PALE_YELLOW,
                                                        "numberFormat": {"type": "NUMBER", "pattern": "0"}}},
                        "fields": "userEnteredFormat(backgroundColor,numberFormat)"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 500,
                                    "startColumnIndex": 6, "endColumnIndex": 7},
                        "cell": {"userEnteredFormat": {"backgroundColor": PALE_YELLOW}},
                        "fields": "userEnteredFormat.backgroundColor"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 500,
                                    "startColumnIndex": 2, "endColumnIndex": 3},
                        "cell": {"userEnteredFormat": {"backgroundColor": PALE_GRAY}},
                        "fields": "userEnteredFormat.backgroundColor"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 500,
                                    "startColumnIndex": 4, "endColumnIndex": 6},
                        "cell": {"userEnteredFormat": {"backgroundColor": PALE_GRAY}},
                        "fields": "userEnteredFormat.backgroundColor"}},
        {"setDataValidation": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 500,
                                           "startColumnIndex": 0, "endColumnIndex": 1},
                               "rule": {"condition": {"type": "DATE_IS_VALID"}, "strict": True}}},
        {"setDataValidation": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 500,
                                           "startColumnIndex": 3, "endColumnIndex": 4},
                               "rule": {"condition": {"type": "NUMBER_GREATER", "values": [
                                   {"userEnteredValue": "0"}]}, "strict": True}}},
    ]
    for index, width in enumerate((120, 120, 230, 165, 185, 140, 260)):
        requests.append(_column_width(sheet.id, index, width))
    book.batch_update({"requests": requests})


def _set_actual_profit(book):
    sheet = _sheet(book, "Фактическая прибыль", 160, 10)
    sheet.batch_clear(["A1:J160"])
    sheet.update(values=[["Дата", "День недели", "Доход по покупкам, ₽", "Посещений за месяц",
                          "Расход, ₽", "Фактическая прибыль, ₽", "", "Параметр", "Значение", ""]],
                 range_name="A1:J1", raw=True)
    sheet.update(values=[["Аренда на одно посещение, ₽", 600]], range_name="H2:I2", raw=True)
    sheet.update(values=[["Месяц", "Посещений", "Расход, ₽"]], range_name="H4:J4", raw=True)
    daily = []
    for row in range(2, 155):
        daily.append([
            f'=DATE(2026;8;1)+ROW()-2',
            f'=TEXT(A{row};"ddd")',
            f'=SUMIF(\'Покупки тарифов\'!$A$2:$A$500;A{row};\'Покупки тарифов\'!$F$2:$F$500)',
            f'=IF(A{row}=EOMONTH(A{row};0);IFNA(VLOOKUP(EOMONTH(A{row};0);$H$5:$I$9;2;FALSE);0);0)',
            f'=D{row}*$I$2',
            f'=C{row}-E{row}',
        ])
    sheet.update(values=daily, range_name="A2:F154", raw=False)
    months = []
    first_attendance_column = 7  # G = 01.08.2026
    month_lengths = (31, 30, 31, 30, 31)
    offset = 0
    for row, month_length in zip(range(5, 10), month_lengths):
        start_column = _column_name(first_attendance_column + offset)
        end_column = _column_name(first_attendance_column + offset + month_length - 1)
        months.append([
            f'=EOMONTH(DATE(2026;8;1);ROW()-5)',
            f'=COUNTIF(\'Посещения_bot\'!{start_column}$3:{end_column}$200;"Y")',
            f'=I{row}*$I$2',
        ])
        offset += month_length
    sheet.update(values=months, range_name="H5:J9", raw=False)
    requests = [
        _header_request(sheet.id, 0, 1, 0, 6),
        _header_request(sheet.id, 0, 1, 7, 9),
        _header_request(sheet.id, 3, 4, 7, 10),
        {"updateSheetProperties": {"properties": {"sheetId": sheet.id,
                                                    "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 154,
                                    "startColumnIndex": 0, "endColumnIndex": 1},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 154,
                                    "startColumnIndex": 2, "endColumnIndex": 3},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0 ₽"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 154,
                                    "startColumnIndex": 3, "endColumnIndex": 4},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 154,
                                    "startColumnIndex": 4, "endColumnIndex": 6},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0 ₽"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 4, "endRowIndex": 9,
                                    "startColumnIndex": 7, "endColumnIndex": 8},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "mmmm yyyy"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
    ]
    for index, width in enumerate((115, 115, 180, 170, 130, 190)):
        requests.append(_column_width(sheet.id, index, width))
    for index, width in ((7, 230), (8, 125), (9, 135)):
        requests.append(_column_width(sheet.id, index, width))
    requests.extend([
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1, "endRowIndex": 2,
                                    "startColumnIndex": 8, "endColumnIndex": 9},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0 ₽"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 4, "endRowIndex": 9,
                                    "startColumnIndex": 8, "endColumnIndex": 9},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 4, "endRowIndex": 9,
                                    "startColumnIndex": 9, "endColumnIndex": 10},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0 ₽"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
    ])
    book.batch_update({"requests": requests})


def _set_analytics(book):
    tech = _sheet(book, "Аналитика_тех", 200, 8)
    tech.batch_clear(["A1:H200"])
    tech.update(values=[["ID", "ФИО", "Посещений за последние 30 дней",
                         "Посещений 30–59 дней назад", "Тариф", "flag_active",
                         "Риск ухода", "Посещений в текущем месяце"]], range_name="A1:H1", raw=True)
    rows = []
    for row in range(2, 200):
        bot_row = row + 1
        rows.append([
            f'=\'Посещения_bot\'!A{bot_row}',
            f'=\'Посещения_bot\'!C{bot_row}',
            (f'=IF(A{row}="";"";SUMPRODUCT(N(IFERROR(DATEVALUE(\'Посещения_bot\'!$G$1:$FC$1);0)>=TODAY()-29);'
             f'N(IFERROR(DATEVALUE(\'Посещения_bot\'!$G$1:$FC$1);0)<=TODAY());N(\'Посещения_bot\'!G{bot_row}:FC{bot_row}="Y")))'),
            (f'=IF(A{row}="";"";SUMPRODUCT(N(IFERROR(DATEVALUE(\'Посещения_bot\'!$G$1:$FC$1);0)>=TODAY()-59);'
             f'N(IFERROR(DATEVALUE(\'Посещения_bot\'!$G$1:$FC$1);0)<=TODAY()-30);N(\'Посещения_bot\'!G{bot_row}:FC{bot_row}="Y")))'),
            f'=IF(A{row}="";"";IFNA(INDEX(\'Тарифы\'!$C$2:$C$199;MATCH(VALUE(A{row});INDEX(IFERROR(1*\'Тарифы\'!$A$2:$A$199;0);0);0));""))',
            f'=IF(A{row}="";"";IF(C{row}>0;"active";"inactive"))',
            f'=IF(A{row}="";"";IF(AND(D{row}>=2;C{row}=0);"⚠️ перестал(а) ходить";""))',
            (f'=IF(A{row}="";"";SUMPRODUCT(N(IFERROR(DATEVALUE(\'Посещения_bot\'!$G$1:$FC$1);0)>=EOMONTH(TODAY();-1)+1);'
             f'N(IFERROR(DATEVALUE(\'Посещения_bot\'!$G$1:$FC$1);0)<=TODAY());N(\'Посещения_bot\'!G{bot_row}:FC{bot_row}="Y")))'),
        ])
    tech.update(values=rows, range_name="A2:H199", raw=False)
    book.batch_update({"requests": [
        {"updateSheetProperties": {"properties": {"sheetId": tech.id, "hidden": True},
                                   "fields": "hidden"}},
    ]})

    nominal = book.worksheet("Номинальная доходность")
    nominal.resize(rows=max(nominal.row_count, 220), cols=max(nominal.col_count, 154))
    nominal.batch_clear(["A13:K220"])
    nominal.update(values=[
        ["Активные клиенты — текущий месяц", "", "", "", "", "",
         "Клиенты перестали ходить"],
        ["ID", "ФИО", "Посещений за месяц", "Тариф, ₽", "Посещения × тариф, ₽", "",
         "ID", "ФИО", "Посещений 30–59 дней назад", "Посещений за последние 30 дней", "Статус"],
    ], range_name="A14:K15", raw=True)
    nominal.update(values=[[
        '=IFNA(FILTER(\'Аналитика_тех\'!A2:A199;\'Аналитика_тех\'!F2:F199="active");"")',
        '=IFNA(FILTER(\'Аналитика_тех\'!B2:B199;\'Аналитика_тех\'!F2:F199="active");"")',
        '=IFNA(FILTER(\'Аналитика_тех\'!H2:H199;\'Аналитика_тех\'!F2:F199="active");"")',
        '=IFNA(FILTER(\'Аналитика_тех\'!E2:E199;\'Аналитика_тех\'!F2:F199="active");"")',
        '=ARRAYFORMULA(IF(A16:A="";"";C16:C*D16:D))',
        "",
        '=IFNA(FILTER(\'Аналитика_тех\'!A2:A199;\'Аналитика_тех\'!G2:G199<>"");"")',
        '=IFNA(FILTER(\'Аналитика_тех\'!B2:B199;\'Аналитика_тех\'!G2:G199<>"");"")',
        '=IFNA(FILTER(\'Аналитика_тех\'!D2:D199;\'Аналитика_тех\'!G2:G199<>"");"")',
        '=IFNA(FILTER(\'Аналитика_тех\'!C2:C199;\'Аналитика_тех\'!G2:G199<>"");"")',
        '=IFNA(FILTER(\'Аналитика_тех\'!G2:G199;\'Аналитика_тех\'!G2:G199<>"");"")',
    ]], range_name="A16:K16", raw=False)
    nominal.update(values=[[
        "Активный клиент: хотя бы одно посещение за последние 30 дней.", "", "", "", "", "",
        "Перестали ходить: 2+ посещения 30–59 дней назад и ни одного за последние 30 дней."
    ]], range_name="A13:G13", raw=True)
    requests = [
        _header_request(nominal.id, 13, 14, 0, 5),
        _header_request(nominal.id, 13, 14, 6, 11, RED),
        _header_request(nominal.id, 14, 15, 0, 5),
        _header_request(nominal.id, 14, 15, 6, 11),
    ]
    dropout_rule_formula = '=$K16<>""'
    if not _has_conditional_formula(book, nominal.id, dropout_rule_formula):
        requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": nominal.id, "startRowIndex": 15, "endRowIndex": 220,
                        "startColumnIndex": 6, "endColumnIndex": 11}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [
                {"userEnteredValue": dropout_rule_formula}]},
                "format": {"backgroundColor": PALE_RED, "textFormat": {"bold": True}}},
        }}})
    for index, width in enumerate((90, 230, 165, 120, 185, 25, 90, 230, 170, 160, 250)):
        requests.append(_column_width(nominal.id, index, width))
    book.batch_update({"requests": requests})


def _mark_priority(value):
    text = str(value or "").strip().upper()
    return {"Y": 3, "N": 2}.get(text, 1 if text else 0)


def _canonicalize_roster(book):
    """Normalize legacy names and merge only exact, unregistered duplicates."""
    attendance = book.worksheet("Посещения_bot")
    grid = attendance.get("A1:FC200", value_render_option="FORMULA")
    if len(grid) < 2 or grid[0][:6] != ["ID участника", "Telegram ID", "ФИО", "Телеграм", "Телефон", "flag_active"]:
        raise RuntimeError("Неожиданная структура листа Посещения_bot")
    width = len(grid[0])
    expected_dates = [(date(2026, 8, 1) + timedelta(days=i)).strftime('%d.%m.%Y') for i in range(153)]
    if grid[0][6:] != expected_dates:
        raise RuntimeError('Ожидаются даты август–декабрь 2026 без пропусков')
    records, positions, redirects = [], {}, {}
    for original in grid[2:]:
        row = list(original) + [""] * (width - len(original))
        if not any(str(value).strip() for value in row[:5]):
            continue
        public_id = str(row[0]).strip()
        telegram_id = str(row[1]).strip()
        if not public_id.isdigit() or len(public_id) != 4:
            raise RuntimeError(f"Некорректный ID участника: {public_id}")
        if telegram_id:
            canonical = utils.clean_person_name(str(row[2]))
            quality = "готово" if utils.registration_fio(canonical) else "⚠️ ФИО требует уточнения"
            key = f"telegram:{telegram_id}"
        else:
            canonical, quality = utils.legacy_canonical_fio(str(row[2]))
            tokens = utils.fio_match_tokens(canonical)
            key = "name:" + "|".join(tokens) if quality == "готово" and len(tokens) >= 2 else f"id:{public_id}"
        row[2] = canonical
        if key not in positions:
            positions[key] = len(records)
            records.append({"row": row, "quality": quality})
            continue
        current = records[positions[key]]["row"]
        canonical_id = min(int(current[0]), int(public_id))
        duplicate_id = max(int(current[0]), int(public_id))
        if canonical_id != int(current[0]):
            current, row = row, current
            records[positions[key]]["row"] = current
        redirects[duplicate_id] = canonical_id
        for index in range(6, width):
            if {str(row[index]).upper(), str(current[index]).upper()} == {'Y', 'N'}:
                raise RuntimeError('Конфликт исторических отметок: требуется ручное уточнение')
            if _mark_priority(row[index]) > _mark_priority(current[index]):
                current[index] = row[index]
        for index in (1, 3, 4):
            if not current[index] and row[index]:
                current[index] = row[index]

    tariff_sheet = book.worksheet("Тарифы")
    tariff_rows = tariff_sheet.get("A2:E199", value_render_option="UNFORMATTED_VALUE")
    tariff_by_id = {}
    for source in tariff_rows:
        row = list(source) + [""] * (5 - len(source))
        if not str(row[0]).strip():
            continue
        public_id = int(str(row[0]).strip())
        canonical_id = redirects.get(public_id, public_id)
        saved = tariff_by_id.setdefault(canonical_id, ["", ""])
        if row[2] not in ("", None, 0, "0"):
            saved[0] = row[2]
        if row[4]:
            saved[1] = row[4]

    attendance.batch_clear(["A3:FC200"])
    roster_values = [item["row"] for item in records]
    if roster_values:
        attendance.update(values=roster_values, range_name=f"A3:FC{len(roster_values) + 2}", raw=True)
    rebuilt_tariffs, people = [], []
    for item in records:
        row, quality = item["row"], item["quality"]
        public_id = int(row[0])
        tariff, source = tariff_by_id.get(public_id, ["", ""])
        rebuilt_tariffs.append([public_id, row[2], tariff, "", source])
        people.append({
            "id": public_id, "fio": row[2], "telegram_id": row[1], "telegram": row[3],
            "phone": row[4], "tariff": tariff, "quality": quality,
        })
    if roster_values:
        import google_sheet
        attendance.update(values=[[google_sheet._active_formula(i, 'FC')] for i in range(3, len(roster_values)+3)],
                          range_name=f'F3:F{len(roster_values)+2}', raw=False)
    tariff_sheet.batch_clear(["A2:E199"])
    if rebuilt_tariffs:
        tariff_sheet.update(values=rebuilt_tariffs, range_name=f"A2:E{len(rebuilt_tariffs) + 1}", raw=True)
    return people, redirects


def _seed_legacy_identities(people):
    async def seed():
        await db.init_db()
        try:
            await db.upsert_legacy_identities(
                [(person["id"], person["fio"], person["quality"]) for person in people]
            )
        finally:
            await db.close_db()
            db._conn = None
    asyncio.run(seed())


def _set_client_directory(book, people):
    sheet = _sheet(book, "Справочник_клиентов", 220, 8)
    sheet.batch_clear(["A1:H220"])
    sheet.update(values=[["Выбор для покупок", "ID", "ФИО", "Telegram ID", "Телеграм",
                          "Телефон", "Тариф, ₽", "Качество ФИО"]], range_name="A1:H1", raw=True)
    values = []
    for person in sorted(people, key=lambda item: item["fio"]):
        username = person["telegram"] or "без Telegram"
        display = f'{person["fio"]} · ID {person["id"]} · {username}'
        values.append([display, person["id"], person["fio"], person["telegram_id"],
                       person["telegram"], person["phone"], person["tariff"], person["quality"]])
        person["display"] = display
    if values:
        sheet.update(values=values, range_name=f"A2:H{len(values) + 1}", raw=True)
    requests = [_header_request(sheet.id, 0, 1, 0, 8),
                {"updateSheetProperties": {"properties": {"sheetId": sheet.id,
                    "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}}]
    for index, width in enumerate((360, 80, 230, 125, 150, 150, 110, 210)):
        requests.append(_column_width(sheet.id, index, width))
    book.batch_update({"requests": requests})
    return sheet


def _set_purchase_matrix(book, people, directory):
    sheet = _sheet(book, "Покупки тарифов", 220, 160)
    previous = sheet.get("A1:FD220", value_render_option="FORMULA")
    saved = []
    if previous and previous[0] and previous[0][0] == "Выберите ФИО":
        old_dates = [str(value).strip() for value in previous[0][7:]]
        for row in previous[2:]:
            if row and str(row[0]).strip():
                counts = {label: row[index + 7] for index, label in enumerate(old_dates)
                          if label and index + 7 < len(row) and row[index + 7] not in ("", None)}
                saved.append((str(row[0]).strip(), counts))
    dates = [date(2026, 8, 1) + timedelta(days=offset) for offset in range(153)]
    labels = [value.strftime("%d.%m.%Y") for value in dates]
    headers = ["Выберите ФИО", "ID", "Telegram ID", "Телеграм", "Телефон", "Тариф, ₽",
               "Качество ФИО"] + labels
    subheaders = ["Начните вводить фамилию и выберите вариант", "", "", "", "", "", ""] + [
        ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][value.weekday()] for value in dates]
    sheet.batch_clear(["A1:FD220"])
    sheet.update(values=[headers, subheaders], range_name="A1:FD2", raw=True)
    formulas = []
    for row in range(3, 201):
        formulas.append([
            f'=IF(A{row}="";"";IFNA(VLOOKUP(A{row};\'Справочник_клиентов\'!$A$2:$H$220;2;FALSE);""))',
            f'=IF(A{row}="";"";IFNA(VLOOKUP(A{row};\'Справочник_клиентов\'!$A$2:$H$220;4;FALSE);""))',
            f'=IF(A{row}="";"";IFNA(VLOOKUP(A{row};\'Справочник_клиентов\'!$A$2:$H$220;5;FALSE);""))',
            f'=IF(A{row}="";"";IFNA(VLOOKUP(A{row};\'Справочник_клиентов\'!$A$2:$H$220;6;FALSE);""))',
            f'=IF(A{row}="";"";IFNA(VLOOKUP(A{row};\'Справочник_клиентов\'!$A$2:$H$220;7;FALSE);""))',
            f'=IF(A{row}="";"";IFNA(VLOOKUP(A{row};\'Справочник_клиентов\'!$A$2:$H$220;8;FALSE);""))',
        ])
    sheet.update(values=formulas, range_name="B3:G200", raw=False)
    input_updates = []
    if not saved:
        by_id = {person["id"]: person for person in people}
        examples = [(8006, "05.08.2026", 8), (8010, "20.08.2026", 4), (8013, "04.09.2026", 6)]
        for row_number, (public_id, label, count) in enumerate(examples, 3):
            if public_id in by_id:
                column = 8 + labels.index(label)
                input_updates.extend([
                    {"range": f"A{row_number}", "values": [[by_id[public_id]["display"]]]},
                    {"range": f"{_column_name(column)}{row_number}", "values": [[count]]},
                ])
    else:
        for row_number, (display, counts) in enumerate(saved, 3):
            input_updates.append({"range": f"A{row_number}", "values": [[display]]})
            for label, count in counts.items():
                if label in labels:
                    input_updates.append({"range": f"{_column_name(8 + labels.index(label))}{row_number}",
                                          "values": [[count]]})
    if input_updates:
        sheet.batch_update(input_updates, raw=True)
    validation = {"setDataValidation": {"range": {"sheetId": sheet.id, "startRowIndex": 2,
        "endRowIndex": 200, "startColumnIndex": 0, "endColumnIndex": 1}, "rule": {
        "condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue":
            "='Справочник_клиентов'!$A$2:$A$220"}]}, "strict": True, "showCustomUi": True}}}
    requests = [_header_request(sheet.id, 0, 1, 0, 160), validation,
        {"updateSheetProperties": {"properties": {"sheetId": sheet.id,
            "gridProperties": {"frozenRowCount": 2, "frozenColumnCount": 7}},
            "fields": "gridProperties(frozenRowCount,frozenColumnCount)"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 2, "endRowIndex": 200,
            "startColumnIndex": 0, "endColumnIndex": 1}, "cell": {"userEnteredFormat": {
            "backgroundColor": PALE_YELLOW}}, "fields": "userEnteredFormat.backgroundColor"}},
        {"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 2, "endRowIndex": 200,
            "startColumnIndex": 1, "endColumnIndex": 7}, "cell": {"userEnteredFormat": {
            "backgroundColor": PALE_GRAY}}, "fields": "userEnteredFormat.backgroundColor"}}]
    for index, width in enumerate((360, 75, 120, 145, 145, 105, 200)):
        requests.append(_column_width(sheet.id, index, width))
    for index in range(7, 160):
        requests.append(_column_width(sheet.id, index, 78))
    book.batch_update({"requests": requests})
    return sheet


def _delete_column_groups(book, sheet):
    metadata = book.fetch_sheet_metadata(params={"fields": "sheets(properties(sheetId),columnGroups(range))"})
    requests = []
    for item in metadata.get("sheets", []):
        if item.get("properties", {}).get("sheetId") == sheet.id:
            for group in reversed(item.get("columnGroups", [])):
                requests.append({"deleteDimensionGroup": {"range": group["range"]}})
    if requests:
        book.batch_update({"requests": requests})


def _weeks():
    current, end = date(2026, 8, 1), date(2026, 12, 31)
    result = []
    while current <= end:
        week_end = min(current + timedelta(days=6 - current.weekday()), end)
        days = []
        day = current
        while day <= week_end:
            days.append(day)
            day += timedelta(days=1)
        result.append(days)
        current = week_end + timedelta(days=1)
    return result


def _set_weekly_finance(book, title, nominal=False):
    sheet = _sheet(book, title, 12, 220)
    _delete_column_groups(book, sheet)
    sheet.batch_clear(["A1:HL12"])
    headers, columns = ["Показатель"], []
    source_date_index = 0
    for days in _weeks():
        summary_col = len(headers) + 1
        label = f"Неделя {days[0].strftime('%d.%m')}–{days[-1].strftime('%d.%m')}"
        headers.append(label)
        detail_start = len(headers) + 1
        for day in days:
            headers.append(day.strftime("%d.%m.%Y"))
        detail_end = len(headers)
        columns.append((summary_col, detail_start, detail_end, days, source_date_index))
        source_date_index += len(days)
    values = [headers] + [[label] + [""] * (len(headers) - 1) for label in
                          ("Доход, ₽", "Посещений", "Расход, ₽", "Прибыль, ₽")]
    requests = [_header_request(sheet.id, 0, 1, 0, len(headers)),
        {"updateSheetProperties": {"properties": {"sheetId": sheet.id,
            "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1}},
            "fields": "gridProperties(frozenRowCount,frozenColumnCount)"}},
        _column_width(sheet.id, 0, 145)]
    for summary_col, detail_start, detail_end, days, date_offset in columns:
        summary_letter = _column_name(summary_col)
        start_letter, end_letter = _column_name(detail_start), _column_name(detail_end)
        summary_index = summary_col - 1
        values[1][summary_index] = f'=SUM({start_letter}2:{end_letter}2)'
        values[2][summary_index] = f'=SUM({start_letter}3:{end_letter}3)'
        values[3][summary_index] = f'=SUM({start_letter}4:{end_letter}4)'
        values[4][summary_index] = f'=SUM({start_letter}5:{end_letter}5)'
        daily = [[], [], [], []]
        for offset, _day in enumerate(days):
            attendance_letter = _column_name(7 + date_offset + offset)
            purchase_letter = _column_name(8 + date_offset + offset)
            if nominal:
                income = (f'=SUMPRODUCT(N(\'Посещения_bot\'!{attendance_letter}$3:{attendance_letter}$199="Y");'
                          "'Тарифы'!$C$2:$C$198)")
            else:
                income = (f'=SUMPRODUCT(\'Покупки тарифов\'!{purchase_letter}$3:{purchase_letter}$200;'
                          "'Покупки тарифов'!$F$3:$F$200)")
            attendance_count = f'=COUNTIF(\'Посещения_bot\'!{attendance_letter}$3:{attendance_letter}$199;"Y")'
            daily[0].append(income)
            daily[1].append(attendance_count)
            if nominal:
                expense = f'={_column_name(detail_start + offset)}3*600'
            elif (_day + timedelta(days=1)).month != _day.month:
                first = _column_name(7 + (_day.replace(day=1) - date(2026, 8, 1)).days)
                expense = f'=COUNTIF(\'Посещения_bot\'!{first}$3:{attendance_letter}$199;"Y")*600'
            else:
                expense = '=0'
            daily[2].append(expense)
            daily[3].append(f'={_column_name(detail_start + offset)}2-{_column_name(detail_start + offset)}4')
        for row_index in range(4):
            for offset, formula in enumerate(daily[row_index]):
                values[row_index + 1][detail_start - 1 + offset] = formula
        zero_start, zero_end = detail_start - 1, detail_end
        requests.extend([
            {"addDimensionGroup": {"range": {"sheetId": sheet.id, "dimension": "COLUMNS",
                "startIndex": zero_start, "endIndex": zero_end}}},
            {"updateDimensionGroup": {"dimensionGroup": {"range": {"sheetId": sheet.id,
                "dimension": "COLUMNS", "startIndex": zero_start, "endIndex": zero_end},
                "depth": 1, "collapsed": True}, "fields": "collapsed"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet.id, "dimension": "COLUMNS",
                "startIndex": zero_start, "endIndex": zero_end}, "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser"}},
            _column_width(sheet.id, summary_col - 1, 145),
            {"repeatCell": {"range": {"sheetId": sheet.id, "startColumnIndex": summary_col - 1,
                "endColumnIndex": summary_col, "startRowIndex": 0, "endRowIndex": 5},
                "cell": {"userEnteredFormat": {"backgroundColor": GREEN,
                    "textFormat": {"foregroundColor": WHITE, "bold": True}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        ])
        for col in range(detail_start - 1, detail_end):
            requests.append(_column_width(sheet.id, col, 90))
    sheet.update(values=values, range_name=f"A1:{_column_name(len(headers))}5", raw=False)
    requests.append({"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": 1,
        "endRowIndex": 5, "startColumnIndex": 1, "endColumnIndex": len(headers)},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
        "fields": "userEnteredFormat.numberFormat"}})
    book.batch_update({"requests": requests})


def _set_client_analytics(book):
    # Reuse the tested technical formulas, then move the readable output to its own stable sheet.
    _set_analytics(book)
    nominal = book.worksheet("Номинальная доходность")
    formulas = nominal.get("A13:K220", value_render_option="FORMULA")
    analytics = _sheet(book, "Аналитика клиентов", 220, 11)
    analytics.batch_clear(["A1:K220"])
    if formulas:
        shifted = []
        for row in formulas:
            shifted.append([
                value.replace("A16:A", "A4:A").replace("C16:C", "C4:C").replace("D16:D", "D4:D")
                if isinstance(value, str) and value.startswith("=") else value
                for value in row
            ])
        analytics.update(values=shifted, range_name=f"A1:K{len(shifted)}", raw=False)
    nominal.batch_clear(["A13:K220"])
    requests = [_header_request(analytics.id, 1, 2, 0, 5),
                _header_request(analytics.id, 1, 2, 6, 11, RED),
                _header_request(analytics.id, 2, 3, 0, 5),
                _header_request(analytics.id, 2, 3, 6, 11)]
    for index, width in enumerate((90, 230, 165, 120, 185, 25, 90, 230, 170, 160, 250)):
        requests.append(_column_width(analytics.id, index, width))
    book.batch_update({"requests": requests})


def setup():
    # Rebuild derived views only. Registration data and purchases are user-owned.
    from finance_views import setup_views
    book = _client().open_by_key(GOOGLE_SHEET_ID)
    setup_views(book)
    return book





def _reset_view(book, title, rows, cols):
    """Reset a derived view in place, preserving its sheet ID and removing stale tails."""
    try:
        sheet = book.worksheet(title)
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(title=title, rows=rows, cols=cols)
    metadata = book.fetch_sheet_metadata()
    info = next(item for item in metadata['sheets'] if item['properties']['sheetId'] == sheet.id)
    requests = []
    for key in ('columnGroups', 'rowGroups'):
        for group in reversed(info.get(key, [])):
            requests.append({'deleteDimensionGroup': {'range': group['range']}})
    for merged in info.get('merges', []):
        requests.append({'unmergeCells': {'range': merged}})
    for index in reversed(range(len(info.get('conditionalFormats', [])))):
        requests.append({'deleteConditionalFormatRule': {'sheetId': sheet.id, 'index': index}})
    for band in info.get('bandedRanges', []):
        requests.append({'deleteBanding': {'bandedRangeId': band['bandedRangeId']}})
    if info.get('basicFilter'):
        requests.append({'clearBasicFilter': {'sheetId': sheet.id}})
    requests.extend([
        {'updateCells': {'range': {'sheetId': sheet.id}, 'fields':
            'userEnteredValue,userEnteredFormat,dataValidation,note,textFormatRuns'}},
        {'updateSheetProperties': {'properties': {'sheetId': sheet.id, 'hidden': False,
            'gridProperties': {'rowCount': rows, 'columnCount': cols,
                'frozenRowCount': 1, 'frozenColumnCount': 0}},
            'fields': 'hidden,gridProperties'}},
    ])
    for dimension, count in [('ROWS', rows), ('COLUMNS', cols)]:
        requests.append({'updateDimensionProperties': {'range': {'sheetId': sheet.id,
            'dimension': dimension, 'startIndex': 0, 'endIndex': count},
            'properties': {'hiddenByUser': False}, 'fields': 'hiddenByUser'}})
    book.batch_update({'requests': requests})
    return sheet


def restore_attendance_copy(book):
    from finance_views import mirror
    mirror(book)


def _month_spans():
    import calendar
    for month, label in zip(range(8, 13), ('Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь')):
        first = date(2026, month, 1)
        offset = (first - date(2026, 8, 1)).days
        yield label + ' 2026', offset, calendar.monthrange(2026, month)[1]


def _profit_colors(sheet_id, ranges):
    return [{'addConditionalFormatRule': {'index': 0, 'rule': {'ranges': ranges,
        'booleanRule': {'condition': {'type': condition, 'values': [{'userEnteredValue': '0'}]},
        'format': {'backgroundColor': color}}}}}
        for condition, color in [('NUMBER_LESS', PALE_RED), ('NUMBER_GREATER_THAN_EQ',
            {'red': .86, 'green': .95, 'blue': .88})]]


def _set_nominal_finance(book):
    sheet = _reset_view(book, 'Номинальная доходность', 154, 12)
    values = [['Дата', 'День', 'Доход, ₽', 'Посещений', 'Расход, ₽', 'Прибыль, ₽', '',
               'Месяц', 'Доход, ₽', 'Посещений', 'Расход, ₽', 'Прибыль, ₽']]
    for offset in range(153):
        row, col = offset + 2, _column_name(offset + 7)
        values.append([f'=DATE(2026;8;1)+{offset}', f'=TEXT(A{row};"ddd")',
            f'=SUMPRODUCT(N(\'Посещения_bot\'!{col}$3:{col}$199="Y");\'Аналитика_тех\'!$E$2:$E$198)',
            f'=COUNTIF(\'Посещения_bot\'!{col}$3:{col}$199;"Y")', f'=D{row}*600', f'=C{row}-E{row}'])
    for index, (label, offset, count) in enumerate(_month_spans(), 1):
        values[index] += [''] * (7 - len(values[index]))
        values[index] += [label] + [f'=SUM({col}{offset+2}:{col}{offset+count+1})' for col in 'CDEF']
    values[7] += [''] * (7-len(values[7]))
    values[7] += ['ИТОГО ПО СЕКЦИИ'] + [f'=SUM({col}2:{col}6)' for col in 'IJKL']
    sheet.update(values=values, range_name='A1:L154', raw=False)
    requests = [_header_request(sheet.id, 0, 1, 0, 6), _header_request(sheet.id, 0, 1, 7, 12),
                _header_request(sheet.id, 7, 8, 7, 12)]
    colors = [{'red': .92, 'green': .96, 'blue': 1}, {'red': .94, 'green': .98, 'blue': .92}]
    for index, (_, offset, count) in enumerate(_month_spans()):
        requests.append({'repeatCell': {'range': {'sheetId': sheet.id, 'startRowIndex': offset+1,
            'endRowIndex': offset+count+1, 'startColumnIndex': 0, 'endColumnIndex': 6},
            'cell': {'userEnteredFormat': {'backgroundColor': colors[index%2]}},
            'fields': 'userEnteredFormat.backgroundColor'}})
    for index, width in enumerate((115, 75, 125, 105, 125, 135, 30, 210, 145, 110, 140, 155)):
        requests.append(_column_width(sheet.id, index, width))
    requests.extend([
        {'repeatCell': {'range': {'sheetId': sheet.id, 'startRowIndex': 1,
            'endRowIndex': 154, 'startColumnIndex': 0, 'endColumnIndex': 1},
            'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'dd.mm.yyyy'}}},
            'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 1},
            'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP'}}, 'fields': 'userEnteredFormat.wrapStrategy'}},
    ])
    for start, end in [(2, 6), (8, 12)]:
        requests.append({'repeatCell': {'range': {'sheetId': sheet.id, 'startRowIndex': 1,
            'endRowIndex': 154, 'startColumnIndex': start, 'endColumnIndex': end},
            'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}}},
            'fields': 'userEnteredFormat.numberFormat'}})
    requests.extend(_profit_colors(sheet.id, [{'sheetId': sheet.id, 'startRowIndex': 1,
        'endRowIndex': end, 'startColumnIndex': col, 'endColumnIndex': col+1} for col,end in [(5,154),(11,8)]]))
    book.batch_update({'requests': requests})


def _set_monthly_profit(book):
    sheet = _reset_view(book, 'Фактическая прибыль', 8, 5)
    values = [['Месяц', 'Доход по покупкам, ₽', 'Посещений', 'Аренда, ₽', 'Фактическая прибыль, ₽']]
    for row, (label, offset, count) in enumerate(_month_spans(), 2):
        income = '+'.join(f'SUMPRODUCT(\'Покупки тарифов\'!{_column_name(8+i)}$3:{_column_name(8+i)}$200;'
            "'Покупки тарифов'!$F$3:$F$200)" for i in range(offset, offset+count))
        first, last = _column_name(7+offset), _column_name(6+offset+count)
        values.append([label, '='+income, f'=COUNTIF(\'Посещения_bot\'!{first}$3:{last}$199;"Y")',
                       f'=C{row}*600', f'=B{row}-D{row}'])
    values += [[], ['ИТОГО'] + [f'=SUM({col}2:{col}6)' for col in 'BCDE']]
    sheet.update(values=values, range_name='A1:E8', raw=False)
    requests = [_header_request(sheet.id, 0, 1, 0, 5), _header_request(sheet.id, 7, 8, 0, 5)]
    for index, width in enumerate((185, 210, 120, 155, 230)):
        requests.append(_column_width(sheet.id, index, width))
    requests.append({'repeatCell': {'range': {'sheetId': sheet.id, 'startRowIndex': 1,
        'endRowIndex': 8, 'startColumnIndex': 1, 'endColumnIndex': 5},
        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}}},
        'fields': 'userEnteredFormat.numberFormat'}})
    requests.extend(_profit_colors(sheet.id, [{'sheetId': sheet.id, 'startRowIndex': 1,
        'endRowIndex': 8, 'startColumnIndex': 4, 'endColumnIndex': 5}]))
    book.batch_update({'requests': requests})

if __name__ == "__main__":
    spreadsheet = setup()
    print(spreadsheet.url)
