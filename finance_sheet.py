"""Idempotent setup for trainer-owned purchase and finance sheets."""
from datetime import date

import gspread

from config import GOOGLE_CREDENTIALS_FILE, GOOGLE_SHEET_ID


GREEN = {"red": 0.05, "green": 0.40, "blue": 0.19}
RED = {"red": 0.72, "green": 0.19, "blue": 0.16}
PALE_RED = {"red": 1.0, "green": 0.80, "blue": 0.80}
PALE_YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.72}
PALE_GRAY = {"red": 0.94, "green": 0.94, "blue": 0.94}
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
WARNING = "⚠️ ЕСТЬ ПОСЕЩЕНИЕ — ЗАПОЛНИТЕ ТАРИФ"


def _client():
    return gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)


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
    book.batch_update({"requests": requests})


def _set_analytics(book):
    tech = _sheet(book, "Аналитика_тех", 200, 8)
    tech.batch_clear(["A1:H200"])
    tech.update(values=[["ID", "ФИО", "Посещений последние 30 дней",
                         "Посещений предыдущие 30 дней", "Тариф", "flag_active",
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
            f'=IF(A{row}="";"";IFNA(VLOOKUP(A{row};\'Тарифы\'!$A$2:$C$199;3;FALSE);""))',
            f'=IF(A{row}="";"";IF(C{row}>0;"active";"inactive"))',
            f'=IF(A{row}="";"";IF(AND(D{row}>=2;C{row}=0);"⚠️ неожиданно перестал ходить";""))',
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
         "Клиенты, которые неожиданно перестали ходить"],
        ["ID", "ФИО", "Посещений за месяц", "Тариф, ₽", "Посещения × тариф, ₽", "",
         "ID", "ФИО", "Предыдущие 30 дней", "Последние 30 дней", "Статус"],
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
        "Эксперимент: минимум 2 посещения в предыдущие 30 дней и 0 в последние 30 дней."
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


def setup():
    book = _client().open_by_key(GOOGLE_SHEET_ID)
    _set_tariff_alert(book)
    _set_purchases(book)
    _set_actual_profit(book)
    _set_analytics(book)
    return book


if __name__ == "__main__":
    spreadsheet = setup()
    print(spreadsheet.url)
