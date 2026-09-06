"""Readable client-level reports. The only editable tariff source is Тарифы!C."""
import calendar
from copy import deepcopy
from datetime import date, timedelta

import gspread

START = date(2026, 8, 1)
DAYS = 153
FIRST = 20
GREEN = {'red': .05, 'green': .40, 'blue': .19}
WHITE = {'red': 1, 'green': 1, 'blue': 1}
PALE = [{'red': .91, 'green': .96, 'blue': .94},
        {'red': .92, 'green': .95, 'blue': 1}]
RED = {'red': 1, 'green': .83, 'blue': .82}
NUMBER = {'type': 'NUMBER', 'pattern': '#,##0;[Red]-#,##0;–'}
MONEY = {'type': 'NUMBER', 'pattern': '#,##0 "₽";[Red]-#,##0 "₽";–'}


def col(n):
    out = ''
    while n:
        n, r = divmod(n-1, 26)
        out = chr(65+r)+out
    return out


def months():
    for m, name in zip(range(8, 13), ['Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']):
        yield name+' 2026', (date(2026, m, 1)-START).days, calendar.monthrange(2026, m)[1]


def cell(v):
    if v in ('', None):
        return {}
    return {'userEnteredValue': {('formulaValue' if isinstance(v, str) and v.startswith('=')
                                 else 'numberValue' if isinstance(v, (int, float)) else 'stringValue'): v}}


def write(sid, row, column, values):
    return {'updateCells': {'start': {'sheetId': sid, 'rowIndex': row, 'columnIndex': column},
                           'rows': [{'values': [cell(v) for v in r]} for r in values],
                           'fields': 'userEnteredValue'}}


def rg(sid, r0, r1, c0, c1):
    return dict(sheetId=sid, startRowIndex=r0, endRowIndex=r1,
                startColumnIndex=c0, endColumnIndex=c1)


def fmt(area, **formatting):
    return {'repeatCell': {'range': area, 'cell': {'userEnteredFormat': formatting},
                          'fields': ','.join('userEnteredFormat.'+key for key in formatting)}}


def header(area, color=GREEN):
    return fmt(area, backgroundColor=color, textFormat={'bold': True, 'foregroundColor': WHITE},
               horizontalAlignment='CENTER', verticalAlignment='MIDDLE', wrapStrategy='WRAP')


def dim(sid, dimension, start, end, **props):
    return {'updateDimensionProperties': {'range': {'sheetId': sid, 'dimension': dimension,
            'startIndex': start, 'endIndex': end}, 'properties': props, 'fields': ','.join(props)}}


def rule(area, formula, color=RED):
    return {'addConditionalFormatRule': {'index': 0, 'rule': {'ranges': [area],
            'booleanRule': {'condition': {'type': 'CUSTOM_FORMULA',
                'values': [{'userEnteredValue': formula}]}, 'format': {'backgroundColor': color}}}}}


def reset(book, title, rows, cols):
    # Preserve the sheet ID: downloaded and cloud internal links stay valid.
    from finance_sheet import _reset_view
    return _reset_view(book, title, rows, cols)


def existing_view(book, title, rows, cols):
    try:
        sheet = book.worksheet(title)
        if sheet.row_count < rows or sheet.col_count < cols:
            sheet.resize(rows=max(rows, sheet.row_count), cols=max(cols, sheet.col_count))
        return sheet, False
    except gspread.WorksheetNotFound:
        return book.add_worksheet(title=title, rows=rows, cols=cols), True


def write_calculations(sheet, values):
    # Skip layout blanks so notes outside calculated cells and all formats survive.
    requests = []
    for r, row in enumerate(values):
        first = 0
        while first < len(row):
            if row[first] == '':
                first += 1
                continue
            end = first+1
            while end < len(row) and row[end] != '':
                end += 1
            requests.append(write(sheet.id, r, first, [row[first:end]]))
            first = end
    for start in range(0, len(requests), 400):
        sheet.spreadsheet.batch_update({'requests': requests[start:start+400]})


def lookup(id_cell, limit):
    # Tariff IDs are numeric, registration IDs may arrive as text.
    # INDEX retains the source-cell reference for the empty check. A plain
    # VLOOKUP converts a genuinely empty tariff to 0 when Excel recalculates.
    tariff = (f'INDEX(\'Тарифы\'!$C$2:$C${limit};'
              f'MATCH({id_cell}&"";INDEX(\'Тарифы\'!$A$2:$A${limit}&"";0);0))')
    return f'=IF({id_cell}="";"";IFERROR(IF({tariff}="";"";{tariff});""))'


def source(id_cell, limit):
    return f'=IF({id_cell}="";"";IFERROR("Тарифы!C"&(MATCH({id_cell}&"";INDEX(\'Тарифы\'!$A$2:$A${limit}&"";0);0)+1);"ID отсутствует в Тарифы"))'


def identity(row, bot_row, limit):
    return [f'=IF(\'Посещения\'!A{bot_row}="";"";\'Посещения\'!A{bot_row})',
            f'=IF($A{row}="";"";\'Посещения\'!C{bot_row})', lookup(f'$A{row}', limit),
            source(f'$A{row}', limit)]


def mirror(book):
    """Reconcile both attendance tabs, retaining trainer corrections and formatting."""
    from attendance_sync import reconcile_book
    return reconcile_book(book, None, None)


def sync_roster(book):
    from finance_roster import sync_roster as append_people, refresh_input_formulas, capacities, view_capacity_state
    added = append_people(book)
    source = book.worksheet('Посещения')
    capacity = max(200, source.row_count)
    nominal = book.worksheet('Номинальная доходность')
    if (capacity > nominal.row_count - FIRST + 3 or
            view_capacity_state(book.id) != capacities(book)):
        setup_views(book, reconcile=False)
    elif added:
        refresh_input_formulas(book, capacity)
        purchase_totals(book, book.worksheet('Тарифы').row_count)
    return added


def purchase_totals(book, limit):
    """Five visible monthly count columns; never rewrite user-entered purchases."""
    sheet = book.worksheet('Покупки тарифов')
    directory = book.worksheet('Справочник_клиентов')
    from finance_roster import purchase_identity_formulas
    n = sheet.row_count
    sheet.resize(rows=n, cols=max(sheet.col_count, 165))
    values = [[name+' · куплено' for name, _, _ in months()], ['Сумма тренировок']*5]
    for r in range(3, n+1):
        values.append([f'=IF($B{r}="";"";SUM({col(8+offset)}{r}:{col(7+offset+count)}{r}))'
                       for _, offset, count in months()])
    requests = [write(sheet.id, 0, 160, values), header(rg(sheet.id, 0, 2, 160, 165)),
                dim(sheet.id, 'COLUMNS', 160, 165, pixelSize=155),
                write(sheet.id, 2, 1, [purchase_identity_formulas(r, directory.row_count)
                                       for r in range(3, n+1)]),
                write(sheet.id, 2, 5, [[lookup(f'$B{r}', limit)] for r in range(3, n+1)]),
                write(sheet.id, 2, 6, [[f'=IF($B{r}="";"";IFERROR(INDEX(\'Справочник_клиентов\'!$H$2:$H${directory.row_count};MATCH($B{r}&"";INDEX(\'Справочник_клиентов\'!$B$2:$B${directory.row_count}&"";0);0));"ID не найден"))'] for r in range(3, n+1)]),
                {'setDataValidation': {'range': rg(sheet.id, 1, n, 3, 7)}},
                {'setDataValidation': {'range': rg(sheet.id, 1, 2, 0, 1)}},
                {'setDataValidation': {'range': rg(sheet.id, 2, n, 0, 1), 'rule': {
                    'condition': {'type': 'ONE_OF_RANGE', 'values': [{'userEnteredValue':
                        f"='Справочник_клиентов'!$A$2:$A${directory.row_count}"}]},
                    'strict': True, 'showCustomUi': True}}},
                rule(rg(sheet.id, 2, n, 5, 6), '=AND($B3<>"";$F3="")')]
    book.batch_update({'requests': requests})
    book.batch_update({'requests': [write(directory.id, 1, 6,
        [[lookup(f'$B{r}', limit)] for r in range(2, directory.row_count+1)])]})
    return n


def nominal_values(capacity, limit):
    end = FIRST + capacity - 3
    v = [['']*159 for _ in range(end)]
    v[0][0] = 'Номинальная доходность · по посещениям'
    v[1][0] = 'Доход = посещение × тариф. Аренда = посещение × 600 ₽. Тариф меняйте на листе «Тарифы».'
    v[3][:5] = ['Месяц', 'Посещений', 'Доход, ₽', 'Аренда, ₽', 'Прибыль, ₽']
    for r, (name, offset, count) in enumerate(months(), 4):
        a, b = col(7+offset), col(6+offset+count)
        v[r][:5] = [name, f'=SUM({a}16:{b}16)', f'=SUM({a}15:{b}15)',
                     f'=SUM({a}17:{b}17)', f'=C{r+1}-D{r+1}']
    v[9][:5] = ['ВСЯ СЕКЦИЯ']+[f'=SUM({c}5:{c}9)' for c in 'BCDE']
    v[10][0] = f'=IF(COUNTIF(G20:FC{end};"нет тарифа")=0;"Тарифы посетивших заполнены";"⚠ Есть посещения без тарифа — доход пока неполный")'
    v[12][:6] = ['ID', 'ФИО', 'Тариф, ₽', 'Источник тарифа', 'Посещений всего', 'Доход всего, ₽']
    for offset in range(DAYS):
        c = col(7+offset)
        v[12][offset+6] = '=DATE(2026;8;1)+'+str(offset)
        v[13][offset+6] = f'=TEXT({c}13;"ddd")'
        v[14][offset+6] = f'=SUM({c}{FIRST}:{c}{end})'
        v[15][offset+6] = f'=IF({c}$13>TODAY();0;COUNTIF(\'Посещения\'!{c}$3:{c}${capacity};"Y"))'
        v[16][offset+6] = f'={c}16*600'
        v[17][offset+6] = f'={c}15-{c}17'
    for r, label in zip(range(14, 18), ['ДОХОД', 'ПОСЕЩЕНИЙ', 'АРЕНДА', 'ПРИБЫЛЬ']):
        v[r][1] = label
        v[r][5] = f'=SUM(G{r+1}:FC{r+1})'
    for r, bot in enumerate(range(3, capacity+1), FIRST):
        v[r-1][:6] = identity(r, bot, limit)+[
            f'=IF($A{r}="";"";COUNTIFS($G$13:$FC$13;"<="&TODAY();\'Посещения\'!G{bot}:FC{bot};"Y"))',
            f'=IF($A{r}="";"";SUM(G{r}:FC{r}))']
        for offset in range(DAYS):
            c = col(7+offset)
            v[r-1][offset+6] = f'=IF($A{r}="";"";IF(AND({c}$13<=TODAY();\'Посещения\'!{c}{bot}="Y");IF(LEN($C{r})=0;"нет тарифа";$C{r});0))'
    return v


def actual_values(capacity, limit, purchase_rows):
    end = FIRST + capacity - 3
    v = [['']*31 for _ in range(end)]
    v[0][0] = 'Фактическая прибыль · по покупкам'
    v[1][0] = 'По каждому клиенту: куплено × тариф − посещения × 600 ₽. Все итоги — суммы строк ниже.'
    v[3][:6] = ['Месяц', 'Куплено', 'Посещений', 'Доход, ₽', 'Аренда, ₽', 'Прибыль, ₽']
    for m, (name, _, _) in enumerate(months()):
        c = 7+5*m
        v[4+m][:6] = [name]+[f'=SUM({col(c+i)}{FIRST}:{col(c+i)}{end})' for i in [0, 2, 1, 3]]+[f'=D{5+m}-E{5+m}']
        v[12][c-1] = name
        v[13][c-1:c+4] = ['Куплено', 'Доход, ₽', 'Посещений', 'Аренда, ₽', 'Прибыль, ₽']
        for i in range(5):
            v[14][c-1+i] = f'=SUM({col(c+i)}{FIRST}:{col(c+i)}{end})'
    v[9][:6] = ['ВСЯ СЕКЦИЯ']+[f'=SUM({c}5:{c}9)' for c in 'BCDEF']
    v[10][0] = f'=IF(COUNTIF(G20:AE{end};"нет тарифа")=0;"Тарифы покупателей заполнены";"⚠ Есть покупки без тарифа — доход пока неполный")'
    v[12][:6] = ['ID', 'ФИО', 'Тариф, ₽', 'Источник тарифа', 'Куплено всего', 'Прибыль всего, ₽']
    v[14][1] = 'ВСЯ СЕКЦИЯ'
    for r, bot in enumerate(range(3, capacity+1), FIRST):
        v[r-1][:6] = identity(r, bot, limit)+[
            f'=IF($A{r}="";"";SUM(G{r};L{r};Q{r};V{r};AA{r}))',
            f'=IF($A{r}="";"";SUM(K{r};P{r};U{r};Z{r};AE{r}))']
        for m, (_, offset, count) in enumerate(months()):
            base = 7+5*m
            bought, income, visits, rent = [col(base+i)+str(r) for i in range(4)]
            v[r-1][base-1:base+4] = [
                f'=IF($A{r}="";"";SUMIF(\'Покупки тарифов\'!$B$3:$B${purchase_rows};$A{r};\'Покупки тарифов\'!${col(161+m)}$3:${col(161+m)}${purchase_rows}))',
                f'=IF($A{r}="";"";IF({bought}=0;0;IF(LEN($C{r})=0;"нет тарифа";{bought}*$C{r})))',
                f'=IF($A{r}="";"";COUNTIFS(\'Номинальная доходность\'!{col(7+offset)}$13:{col(6+offset+count)}$13;"<="&TODAY();\'Посещения\'!{col(7+offset)}{bot}:{col(6+offset+count)}{bot};"Y"))',
                f'=IF($A{r}="";"";{visits}*600)',
                f'=IF($A{r}="";"";N({income})-{rent})']
    return v


def report_style(sheet, rows, columns, nominal, used):
    sid = sheet.id
    requests = [fmt(rg(sid, 0, rows, 0, columns), textFormat={'fontFamily': 'Arial', 'fontSize': 10},
                    verticalAlignment='MIDDLE'),
        {'mergeCells': {'range': rg(sid, 0, 1, 0, 6), 'mergeType': 'MERGE_ALL'}},
        {'mergeCells': {'range': rg(sid, 1, 2, 0, 6), 'mergeType': 'MERGE_ALL'}},
        {'mergeCells': {'range': rg(sid, 10, 11, 0, 6), 'mergeType': 'MERGE_ALL'}},
        header(rg(sid, 0, 1, 0, 6)), fmt(rg(sid, 1, 2, 0, 6), wrapStrategy='WRAP'),
        fmt(rg(sid, 10, 11, 0, 6), wrapStrategy='WRAP', textFormat={'bold': True}),
        header(rg(sid, 3, 4, 0, 5 if nominal else 6)),
        header(rg(sid, 9, 10, 0, 5 if nominal else 6)),
        header(rg(sid, 12, 14, 0, 6)),
        dim(sid, 'ROWS', 0, 1, pixelSize=36), dim(sid, 'ROWS', 1, 2, pixelSize=40),
        dim(sid, 'ROWS', 3, 4, pixelSize=36), dim(sid, 'ROWS', 10, 11, pixelSize=32),
        dim(sid, 'ROWS', 12, 14, pixelSize=34),
        {'updateSheetProperties': {'properties': {'sheetId': sid,
            'gridProperties': {'frozenRowCount': 14, 'frozenColumnCount': 0, 'hideGridlines': True}},
            'fields': 'gridProperties(frozenRowCount,frozenColumnCount,hideGridlines)'}},
        fmt(rg(sid, FIRST-1, rows, 2, 3), numberFormat=MONEY),
        fmt(rg(sid, FIRST-1, rows, 4, 5), numberFormat=NUMBER),
        fmt(rg(sid, FIRST-1, rows, 5, 6), numberFormat=MONEY),
        rule(rg(sid, FIRST-1, rows, 2, 4), f'=AND($A{FIRST}<>"";$C{FIRST}="")')]
    for i, width in enumerate((105, 270, 120, 150, 140, 155)):
        requests.append(dim(sid, 'COLUMNS', i, i+1, pixelSize=width))
    # Leave the prefilled blank rows visible: a newly registered client appears
    # automatically there without another finance-setup run.
    if nominal:
        for m, (_, offset, count) in enumerate(months()):
            requests.extend([fmt(rg(sid, 12, rows, 6+offset, 6+offset+count), backgroundColor=PALE[m%2]),
                             header(rg(sid, 12, 14, 6+offset, 6+offset+count))])
        requests.extend([dim(sid, 'COLUMNS', 6, 159, pixelSize=105),
            fmt(rg(sid, 12, 13, 6, 159), numberFormat={'type':'DATE','pattern':'dd.mm.yyyy'}),
            fmt(rg(sid, 14, rows, 6, 159), numberFormat=MONEY),
            fmt(rg(sid, 15, 16, 6, 159), numberFormat=NUMBER),
            header(rg(sid, 17, 18, 1, 159)),
            rule(rg(sid, FIRST-1, rows, 6, 159), '=G20="нет тарифа"'),
            fmt(rg(sid, 4, 9, 2, 5), numberFormat=MONEY)])
    else:
        for m in range(5):
            c = 6+5*m
            requests.extend([fmt(rg(sid, 12, rows, c, c+5), backgroundColor=PALE[m%2]),
                header(rg(sid, 12, 14, c, c+5)),
                {'mergeCells': {'range': rg(sid, 12, 13, c, c+5), 'mergeType': 'MERGE_ALL'}},
                dim(sid, 'COLUMNS', c, c+5, pixelSize=120),
                fmt(rg(sid, 14, rows, c, c+5), numberFormat=NUMBER),
                rule(rg(sid, FIRST-1, rows, c+4, c+5), f'=AND(ISNUMBER({col(c+5)}20);{col(c+5)}20<0)'),
                rule(rg(sid, FIRST-1, rows, c, c+5), f'={col(c+2)}20="нет тарифа"')])
        requests.append(fmt(rg(sid, 4, 10, 3, 6), numberFormat=MONEY))
    return requests


def reports(book, capacity, limit, purchase_rows, used):
    for title, values, nominal in [('Номинальная доходность', nominal_values(capacity, limit), True),
                                   ('Фактическая прибыль', actual_values(capacity, limit, purchase_rows), False)]:
        sheet, created = existing_view(book, title, len(values), len(values[0]))
        write_calculations(sheet, values)
        if created:
            book.batch_update({'requests': report_style(sheet, len(values), len(values[0]), nominal, used)})


def run(book, capacity, limit):
    """Last 30 days consistently, plus transparent direct tariff lookup."""
    tech, _ = existing_view(book, 'Аналитика_тех', capacity, 10)
    t = [['ID', 'ФИО', 'Последние 30 дней', '30–59 дней назад', 'Тариф', 'flag_active',
          'Перестали ходить', 'Телеграм', 'Номер active', 'Номер паузы']]
    for r, bot in enumerate(range(3, capacity+1), 2):
        date_values = 'IFERROR(DATEVALUE(\'Посещения\'!$G$1:$FC$1);0)'
        counts = lambda a, b: f'=IF(A{r}="";"";SUMPRODUCT(N({date_values}>=TODAY()-{a});N({date_values}<=TODAY()-{b});N(\'Посещения\'!G{bot}:FC{bot}="Y")))'
        t.append([f'=IF(\'Посещения\'!A{bot}="";"";\'Посещения\'!A{bot})',
            f'=IF(A{r}="";"";\'Посещения\'!C{bot})', counts(29, 0), counts(59, 30), lookup(f'A{r}', limit),
            f'=IF(A{r}="";"";IF(C{r}>0;"active";""))',
            f'=IF(A{r}="";"";IF(AND(D{r}>=2;C{r}=0);"перестал(а) ходить";""))',
            f'=IF(A{r}="";"";\'Посещения\'!D{bot})',
            f'=IF(F{r}="active";COUNTIF(F$2:F{r};"active");"")',
            f'=IF(G{r}<>"";COUNTIF(G$2:G{r};"перестал(а) ходить");"")'])
    book.batch_update({'requests': [write(tech.id, 0, 0, t),
        {'updateSheetProperties': {'properties': {'sheetId': tech.id, 'hidden': True}, 'fields': 'hidden'}}]})
    try:
        sheet = book.worksheet('RUN')
    except gspread.WorksheetNotFound:
        sheet = book.worksheet('Аналитика клиентов')
        sheet.update_title('RUN')
    sheet, created = existing_view(book, 'RUN', capacity+5, 13)
    v = [['']*13 for _ in range(capacity+5)]
    v[0][0], v[0][8] = 'RUN · активные за последние 30 дней', 'Клиенты перестали ходить'
    v[1][0] = '=TEXT(TODAY()-29;"dd.mm.yyyy")&" — "&TEXT(TODAY();"dd.mm.yyyy")'
    v[1][8] = 'Раньше: ≥2 посещений 30–59 дней назад. Сейчас: 0 за последние 30 дней.'
    v[2][0] = 'Тариф берётся напрямую из «Тарифы», столбец C, по ID. Адрес исходной ячейки — справа.'
    v[5][:7] = ['ID', 'ФИО', 'Телеграм', 'Посещений за 30 дней', 'Тариф, ₽', 'Доход за 30 дней, ₽', 'Источник тарифа']
    v[5][8:] = ['ID', 'ФИО', '30–59 дней назад', 'Последние 30 дней', 'Статус']
    for r in range(7, capacity+6):
        for dest, rank in [('A', 'I'), ('I', 'J')]:
            v[r-1][0 if dest=='A' else 8] = f'=IFERROR(INDEX(\'Аналитика_тех\'!$A$2:$A${capacity};MATCH(ROW()-6;\'Аналитика_тех\'!${rank}$2:${rank}${capacity};0));"")'
        def tech_lookup(key, n):
            return f'=IF({key}{r}="";"";VLOOKUP({key}{r};\'Аналитика_тех\'!$A$2:$H${capacity};{n};FALSE))'
        v[r-1][1:7] = [tech_lookup('A', 2), tech_lookup('A', 8), tech_lookup('A', 3), lookup(f'A{r}', limit),
            f'=IF(A{r}="";"";IF(E{r}="";"нет тарифа";D{r}*E{r}))', source(f'A{r}', limit)]
        v[r-1][9:] = [tech_lookup('I', 2), tech_lookup('I', 4), tech_lookup('I', 3), tech_lookup('I', 7)]
    requests = [write(sheet.id, 0, 0, v), header(rg(sheet.id, 0, 1, 0, 7)),
        header(rg(sheet.id, 0, 1, 8, 13)), header(rg(sheet.id, 5, 6, 0, 7)), header(rg(sheet.id, 5, 6, 8, 13)),
        rule(rg(sheet.id, 6, capacity+5, 4, 7), '=AND($A7<>"";$E7="")'),
        fmt(rg(sheet.id, 6, capacity+5, 4, 6), numberFormat=MONEY),
        dim(sheet.id, 'ROWS', 0, 1, pixelSize=36), dim(sheet.id, 'ROWS', 1, 3, pixelSize=32),
        dim(sheet.id, 'ROWS', 5, 6, pixelSize=45),
        {'updateSheetProperties': {'properties': {'sheetId': sheet.id,
            'gridProperties': {'frozenRowCount': 6, 'hideGridlines': True}},
            'fields': 'gridProperties(frozenRowCount,hideGridlines)'}}]
    for r, a, b in [(0,0,7),(1,0,7),(2,0,7),(0,8,13),(1,8,13)]:
        requests += [{'mergeCells': {'range': rg(sheet.id,r,r+1,a,b), 'mergeType':'MERGE_ALL'}},
                     fmt(rg(sheet.id,r,r+1,a,b), wrapStrategy='WRAP')]
    for i, width in enumerate((75,260,145,120,110,150,145,25,75,250,130,130,185)):
        requests.append(dim(sheet.id, 'COLUMNS', i, i+1, pixelSize=width))
    if created:
        book.batch_update({'requests': requests})
    else:
        write_calculations(sheet, v)


def setup_views(book, reconcile=True):
    from attendance_sync import workbook_lock
    with workbook_lock():
        return _setup_views_locked(book, reconcile)


def _setup_views_locked(book, reconcile):
    if reconcile:
        mirror(book)
    from finance_roster import sync_roster as append_people, refresh_input_formulas
    append_people(book)
    source_sheet = book.worksheet('Посещения')
    capacity = max(200, source_sheet.row_count)
    if source_sheet.row_count < capacity:
        source_sheet.resize(rows=capacity)
    tariffs = book.worksheet('Тарифы')
    ids = tariffs.get('A2:A'+str(tariffs.row_count), value_render_option='UNFORMATTED_VALUE')
    from finance_roster import keyed_rows
    keyed_rows(ids, 0, 2)
    # Normalize ID type, preserving all tariff inputs and notes.
    if ids:
        tariffs.update(values=[[int(r[0])] if r and r[0] not in ('',None) else [''] for r in ids],
                       range_name='A2:A'+str(len(ids)+1), raw=True)
    used = len(source_sheet.get('A3:A'+str(capacity)))
    refresh_input_formulas(book, capacity)
    purchase_rows = purchase_totals(book, tariffs.row_count)
    run(book, capacity, tariffs.row_count)
    reports(book, capacity, tariffs.row_count, purchase_rows, used+2)
    from finance_roster import capacities, view_capacity_state
    view_capacity_state(book.id, capacities(book))


if __name__ == '__main__':
    from finance_sheet import _client
    from config import GOOGLE_SHEET_ID
    setup_views(_client().open_by_key(GOOGLE_SHEET_ID))
    print('Updated attendance reconciliation, RUN and client-level financial reports.')
