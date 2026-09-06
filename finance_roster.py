"""Append new identities without rebuilding trainer-owned tariff/purchase inputs."""
import re


def view_capacity_state(book_id, value=None):
    """Formula bounds are deployment-independent and grow with editable sheets."""
    from contextlib import closing
    import json
    import sqlite3
    import db
    with closing(sqlite3.connect(db.DB_PATH, timeout=30)) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS finance_view_capacity '
                     '(spreadsheet_id TEXT PRIMARY KEY, capacity_json TEXT NOT NULL)')
        if value is not None:
            conn.execute('INSERT INTO finance_view_capacity VALUES (?,?) '
                         'ON CONFLICT(spreadsheet_id) DO UPDATE SET capacity_json=excluded.capacity_json',
                         (str(book_id), json.dumps(value)))
            return value
        found = conn.execute('SELECT capacity_json FROM finance_view_capacity WHERE spreadsheet_id=?',
                             (str(book_id),)).fetchone()
        return json.loads(found[0]) if found else None


def capacities(book):
    return {title: book.worksheet(title).row_count for title in (
        'Посещения', 'Тарифы', 'Покупки тарифов', 'Справочник_клиентов')}


def keyed_rows(rows, key_column, first_row):
    result = {}
    for number, row in enumerate(rows, first_row):
        value = row[key_column] if len(row) > key_column else ''
        if value in ('', None):
            continue
        text = str(value).strip()
        if not re.fullmatch(r'\d{4}', text):
            raise ValueError(f'Некорректный ID в строке {number}: {text}')
        key = int(text)
        if key in result:
            raise ValueError(f'Дублируется ID {key}')
        result[key] = number
    return result


def missing_people(attendance, tariff_rows, directory_rows):
    """Match by ID only; never merge same names or replace existing input rows."""
    roster = keyed_rows(attendance[2:], 0, 3)
    tariffs = keyed_rows(tariff_rows, 0, 2)
    directory = keyed_rows(directory_rows, 1, 2)
    tariff_new, directory_new = [], []
    for pid, row_number in roster.items():
        row = attendance[row_number-1] + ['']*6
        if pid not in tariffs:
            tariff_new.append([pid, row[2], '', '', 'Новый участник: заполните тариф'])
        if pid not in directory:
            directory_new.append([
                f'{row[2]} · ID {pid} · {row[3] or "без Telegram"}', pid,
                row[2], row[1], row[3], row[4], '',
                'проверьте ФИО' if len(str(row[2]).split()) < 3 else 'готово'])
    return tariff_new, directory_new


def _last_row(rows, width):
    return max((i+2 for i, row in enumerate(rows) if any(v not in ('', None) for v in row[:width])), default=1)


def sync_roster(book):
    from finance_views import lookup
    attendance = book.worksheet('Посещения').get_all_values(pad_values=False)
    tariff = book.worksheet('Тарифы')
    directory = book.worksheet('Справочник_клиентов')
    tariffs = tariff.get(f'A2:E{tariff.row_count}', value_render_option='UNFORMATTED_VALUE')
    people = directory.get(f'A2:H{directory.row_count}', value_render_option='UNFORMATTED_VALUE')
    new_tariffs, new_people = missing_people(attendance, tariffs, people)
    added = False
    for sheet, previous, width, additions in [(tariff, tariffs, 5, new_tariffs),
                                             (directory, people, 8, new_people)]:
        if not additions:
            continue
        first = _last_row(previous, width)+1
        last = first+len(additions)-1
        if last > sheet.row_count:
            sheet.resize(rows=max(last+50, sheet.row_count*2))
        sheet.update(values=additions, range_name=f'A{first}:{chr(64+width)}{last}', raw=True)
        if sheet.id == directory.id:
            sheet.update(values=[[lookup(f'B{r}', tariff.row_count)] for r in range(first, last+1)],
                         range_name=f'G{first}:G{last}', raw=False)
        added = True
    return added


def purchase_identity_formulas(row, directory_limit):
    """The embedded four-digit ID is immutable even when FIO/username changes."""
    key = f'$B{row}'
    formulas = [f'=IF(A{row}="";"";IFERROR(VALUE(MID(A{row};FIND(" · ID ";A{row})+6;4));"ID не найден"))']
    for c in ('D', 'E', 'F'):
        area = f"'Справочник_клиентов'!${c}$2:${c}${directory_limit}"
        value = f'INDEX({area};MATCH({key}&"";INDEX(\'Справочник_клиентов\'!$B$2:$B${directory_limit}&"";0);0))'
        formulas.append(f'=IF({key}="";"";IFERROR(IF({value}="";"";{value});"ID не найден"))')
    return formulas


def tariff_alert_formulas(row, capacity):
    # Order of tariff rows and attendance rows is deliberately independent.
    visits = (f'IFERROR(COUNTIFS(\'Номинальная доходность\'!$G$13:$FC$13;"<="&TODAY();'
              f'INDEX(\'Посещения\'!$G$3:$FC${capacity};'
              f'MATCH(A{row}&"";INDEX(\'Посещения\'!$A$3:$A${capacity}&"";0);0);0);"Y");0)')
    invalid = f'OR(C{row}="";NOT(ISNUMBER(C{row}));C{row}<0)'
    return (f'=IF(A{row}="";"";IF({invalid};IF({visits}>0;'
            '"⚠ Есть посещения — заполните тариф";"тариф не заполнен");"готово"))')


def refresh_input_formulas(book, capacity):
    """Refresh generated helper columns only. Inputs A/C/E and purchases stay intact."""
    from finance_views import write, rule, rg
    tariff = book.worksheet('Тарифы')
    end = tariff.row_count
    book.batch_update({'requests': [
        write(tariff.id, 1, 3, [[tariff_alert_formulas(r, capacity)] for r in range(2, end+1)]),
        write(tariff.id, 1, 6, [[f'=COUNTIF(D2:D{end};"⚠*")',
              '=IF(G2=0;"Тарифы посетивших заполнены";"Заполните красные строки — доход пока неполный")']])
    ]})
    formula = '=LEFT($D2;1)="⚠"'
    from finance_sheet import _has_conditional_formula
    if not _has_conditional_formula(book, tariff.id, formula):
        book.batch_update({'requests': [rule(rg(tariff.id, 1, end, 0, 5), formula)]})
