"""Derived planning sheets, separate from actual attendance and financial inputs."""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3

import utils

MONTHLY = 'план посещаемости на месяц'
WEEKLY = 'план посещаемости на неделю'
GENERAL = 'Общий план тренировок'
DETAILS = 'Подробности тренировки'
TITLES = (MONTHLY, WEEKLY, GENERAL, DETAILS)
STAGES = ('month', 'week', 'main')
NAMES = {'month': 'месяц', 'week': 'неделя', 'main': 'основной', None: '—'}
MARKS = {'yes': 'Y', 'no': 'N', 'pending': '—', 'unknown': 'нет истории', 'future': 'ещё не наступили'}
SLATE = {'red': .216, 'green': .255, 'blue': .318}
PALE = ({'red': .94, 'green': .97, 'blue': .96}, {'red': .94, 'green': .96, 'blue': 1})


class Formula(str):
    """Only trusted generated expressions may become spreadsheet formulas."""


def instant(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return utils.TZ.localize(dt) if dt.tzinfo is None else dt.astimezone(utils.TZ)


def _cell(value):
    if value in ('', None):
        return {}
    field = ('formulaValue' if isinstance(value, Formula) else
             'numberValue' if isinstance(value, (int, float)) else 'stringValue')
    return {'userEnteredValue': {field: value}}


def _column(number):
    out = ''
    while number:
        number, digit = divmod(number-1, 26)
        out = chr(65+digit)+out
    return out


def tier_formula(id_cell, limit):
    value = (f'INDEX(\'Справочник_клиентов\'!$I$2:$I${limit};'
             f'MATCH({id_cell}&"";INDEX(\'Справочник_клиентов\'!$B$2:$B${limit}&"";0);0))')
    return Formula(f'=IF({id_cell}="";"";IFERROR(IF({value}="";"";{value});""))')


def effective(stages):
    for kind in reversed(STAGES):
        answer = stages.get(kind)
        if answer and answer.get('status') in ('yes', 'no'):
            return answer['status'], kind
    return 'pending', None


def state_at(stages, changes, cutoff, enabled_at=None):
    """Recover an effective historical state only when evidence spans cutoff."""
    before = [c for c in changes if instant(c['changed_at']) <= cutoff]
    if before:
        after = [c for c in changes if instant(c['changed_at']) > cutoff]
        if after and before[-1]['new_status'] != after[0]['old_status']:
            # A legacy reset/cancellation may not have been journaled. Neither
            # endpoint proves when the missing transition happened.
            return 'unknown'
        return before[-1]['new_status']
    tracked = enabled_at is None or enabled_at <= cutoff
    if changes and tracked:
        # The complete effective ledger covers cutoff: the first later event
        # supplies the state immediately before it, including legacy main data.
        return changes[0]['old_status']
    known, kind = effective(stages)
    if kind and stages[kind].get('responded_at') and instant(stages[kind]['responded_at']) <= cutoff:
        return known
    if tracked and not any(a.get('status') in ('yes', 'no') for a in stages.values()):
        return 'pending'
    return 'unknown'


def baseline(stages, changes, cutoff, now, enabled_at=None):
    """Recover state at start-24h without inventing a legacy negative answer."""
    return 'future' if now < cutoff else state_at(stages, changes, cutoff, enabled_at)


def _session_label(session):
    start = instant(session['starts_at'])
    end = '–'+session['end_time'] if session.get('end_time') else ''
    day = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'][start.weekday()]
    return f'{day}, {start:%d.%m.%Y}\n{start:%H:%M}{end}' + (' · ОТМЕНЕНА' if session.get('cancelled') else '')


def _history(changes):
    lines = [f"{instant(c['changed_at']):%d.%m %H:%M} "
                     f"{NAMES.get(c.get('from_stage'), '—')}:{MARKS.get(c['old_status'], '—')} → "
                     f"{NAMES.get(c.get('to_stage'), '—')}:{MARKS.get(c['new_status'], '—')}"
                     for c in changes[-10:]]
    if len(changes) > 10:
        lines.insert(0, f'… ещё {len(changes)-10} ранних изменений в журнале бота')
    return '\n'.join(lines)


def project(snapshot, tier_limit=500):
    now = instant(snapshot['generated_at'])
    enabled = instant(snapshot['enabled_at']) if snapshot.get('enabled_at') else None
    people = sorted(snapshot.get('participants', []), key=lambda p: (p.get('full_name') or '', int(p['public_id'])))
    if len({str(p['public_id']) for p in people}) != len(people):
        raise ValueError('Повторный ID участника в планах')
    sessions = sorted(snapshot.get('sessions', []), key=lambda s: (instant(s['starts_at']), s['key']))
    if len({s['key'] for s in sessions}) != len(sessions):
        raise ValueError('Повторная тренировка в планах')
    answers = defaultdict(dict)
    for answer in snapshot.get('answers', []):
        if answer.get('cancelled'):
            continue
        if answer.get('kind') not in STAGES or answer.get('status') not in ('yes', 'no', 'pending'):
            raise ValueError('Неизвестный этап или ответ в плане')
        key = (int(answer['participant_id']), answer['key'])
        previous = answers[key].get(answer['kind'])
        if previous is None or (answer.get('responded_at') or '') >= (previous.get('responded_at') or ''):
            answers[key][answer['kind']] = answer
    journal = defaultdict(list)
    for change in snapshot.get('changes', []):
        if instant(change['changed_at']) <= now:
            journal[(int(change['participant_id']), change['key'])].append(change)
    for changes in journal.values():
        changes.sort(key=lambda c: instant(c['changed_at']))
    next_month = (now.replace(day=1)+timedelta(days=32)).replace(day=1)
    horizon = (next_month+timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    future = [s for s in sessions if now <= instant(s['starts_at']) < horizon]
    result = {}
    for title, stage in ((MONTHLY, 'month'), (WEEKLY, 'week')):
        rows = [['ID', 'ФИО', 'Телеграм', 'Tier']+[_session_label(s) for s in future],
                ['', 'План из бота. Y — да; N — нет; — нет ответа.', '', 'Менять в справочнике']]
        rows[1] += [['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'][instant(s['starts_at']).weekday()] for s in future]
        groups = []
        for i, session in enumerate(future, 4):
            start = instant(session['starts_at'])
            label = start.strftime('%Y-%m') if stage == 'month' else (start.date()-timedelta(days=start.weekday())).isoformat()
            if groups and groups[-1]['label'] == label:
                groups[-1]['end'] = i+1
            else:
                groups.append({'label': label, 'start': i, 'end': i+1})
            rows[1][i] += (' · '+start.strftime('%m.%Y') if stage == 'month' else ' · неделя '+label[8:]+'.'+label[5:7])
        for person in people:
            rownum = len(rows)+1
            row = [str(person['public_id']), person.get('full_name') or '',
                   '@'+person['username'] if person.get('username') else '', tier_formula(f'$A{rownum}', tier_limit)]
            for session in future:
                stage_answer = answers[(int(person['id']), session['key'])].get(stage, {})
                row.append('отменена' if session.get('cancelled') else MARKS[stage_answer.get('status', 'pending')])
            rows.append(row)
        result[title] = {'values': rows, 'kind': 'matrix', 'groups': groups,
                         'layout': [s['key'] for s in future]}
    overview = [['Тренировка', 'Ожидали за месяц', 'За неделю', 'Сейчас придут',
                 'Добавились в 24ч', 'Отказались в 24ч', 'Нет истории 24ч',
                 'Кто добавился', 'Кто отказался'],
                ['Изменения за сутки до старта', '', '', 'Последний ответ', 'Сравнение с началом суток',
                 'Сравнение с началом суток', 'Старые переходы неизвестны', 'ФИО добавившихся', 'ФИО отказавшихся']]
    details = [['Тренировка', 'ID', 'Участник', 'Tier', 'За месяц', 'За неделю',
                'На начало последних 24ч', 'Сейчас', 'Изменение за 24ч', 'История ответов', 'Телеграм'],
               ['Выберите тренировку фильтром ↓', '', 'Ответы меняются в боте', 'Из справочника',
                '', '', 'Ровно за сутки до старта', 'Последний ответ', 'До начала тренировки',
                'Последние 10 переходов. Полный журнал хранит бот.', '']]
    for session in sessions:
        cutoff = instant(session['starts_at'])-timedelta(hours=24)
        count = [0]*6
        added, refused = [], []
        for person in people:
            key = (int(person['id']), session['key'])
            stages, changes = answers[key], journal[key]
            current, _ = effective(stages)
            old = baseline(stages, changes, cutoff, now, enabled)
            end_status = (current if now <= instant(session['starts_at']) else
                          state_at(stages, changes, instant(session['starts_at']), enabled))
            month = stages.get('month', {}).get('status', 'pending')
            week = stages.get('week', {}).get('status', 'pending')
            change = ('нет истории' if old == 'unknown' or end_status == 'unknown' else
                      '24ч ещё не начались' if old == 'future' else
                      'добавился' if old != 'yes' and end_status == 'yes' else
                      'отказался' if old == 'yes' and end_status == 'no' else
                      'ответ очищен' if old == 'yes' and end_status == 'pending' else 'без изменений')
            if session.get('cancelled'):
                change = 'тренировка отменена'
            else:
                count[0] += month == 'yes'
                count[1] += week == 'yes'
                count[2] += current == 'yes'
                count[3] += change == 'добавился'
                count[4] += change == 'отказался'
                count[5] += change == 'нет истории'
                if change == 'добавился':
                    added.append(person.get('full_name') or str(person['public_id']))
                elif change == 'отказался':
                    refused.append(person.get('full_name') or str(person['public_id']))
            rownum = len(details)+1
            details.append([_session_label(session), str(person['public_id']), person.get('full_name') or '',
                            tier_formula(f'$B{rownum}', tier_limit), MARKS[month], MARKS[week], MARKS[old],
                            'отменена' if session.get('cancelled') else MARKS[current], change, _history(changes),
                            '@'+person['username'] if person.get('username') else ''])
        overview.append([_session_label(session)]+count+['\n'.join(added), '\n'.join(refused)])
    result[GENERAL] = {'values': overview, 'kind': 'summary', 'groups': [], 'layout': []}
    result[DETAILS] = {'values': details, 'kind': 'details', 'groups': [], 'layout': []}
    return result


def _state(book_id, sheet_id, value=None):
    import db
    with closing(sqlite3.connect(db.DB_PATH, timeout=30)) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS planning_sheet_state '
                     '(spreadsheet_id TEXT, sheet_id INTEGER, state_json TEXT NOT NULL, PRIMARY KEY(spreadsheet_id,sheet_id))')
        if value is not None:
            conn.execute('INSERT INTO planning_sheet_state VALUES (?,?,?) ON CONFLICT(spreadsheet_id,sheet_id) '
                         'DO UPDATE SET state_json=excluded.state_json', (str(book_id), sheet_id, json.dumps(value)))
            return value
        row = conn.execute('SELECT state_json FROM planning_sheet_state WHERE spreadsheet_id=? AND sheet_id=?',
                           (str(book_id), sheet_id)).fetchone()
        return json.loads(row[0]) if row else {}


def _at(values, r, c):
    return values[r][c] if r < len(values) and c < len(values[r]) else ''


def _rectangles(rectangles):
    """Keep the exact union after a partial cloud update, never its bounding box."""
    unique = set(map(tuple, rectangles))
    return [[r, c] for r, c in sorted(unique) if r and c and
            not any((rr, cc) != (r, c) and rr >= r and cc >= c for rr, cc in unique)]


def _diff(sheet_id, current, target, old_rows, old_cols, old_rectangles=None):
    rows, cols = max(old_rows, len(target)), max(old_cols, max(map(len, target)))
    owned = _rectangles((old_rectangles or [[old_rows, old_cols]]) + [[len(target), max(map(len, target))]])
    updates = []
    changed = 0
    for r in range(rows):
        first, cells = None, []
        for c in range(cols+1):
            value = _at(target, r, c)
            differs = (c < cols and any(r < rr and c < cc for rr, cc in owned)
                       and _at(current, r, c) != value)
            if differs:
                if first is None:
                    first = c
                cells.append(_cell(value))
                changed += 1
            elif first is not None:
                updates.append({'updateCells': {'start': {'sheetId': sheet_id, 'rowIndex': r, 'columnIndex': first},
                                                'rows': [{'values': cells}], 'fields': 'userEnteredValue'}})
                first, cells = None, []
    return updates, changed


def _style(sheet, spec):
    width = max(map(len, spec['values']))
    requests = [
        {'updateSheetProperties': {'properties': {'sheetId': sheet.id, 'gridProperties': {
            'frozenRowCount': 2, 'frozenColumnCount': 4 if spec['kind'] == 'matrix' else 1, 'hideGridlines': True}},
             'fields': 'gridProperties.frozenRowCount,gridProperties.frozenColumnCount,gridProperties.hideGridlines'}},
        {'repeatCell': {'range': {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 2,
                                  'startColumnIndex': 0, 'endColumnIndex': width},
                        'cell': {'userEnteredFormat': {'backgroundColor': SLATE, 'wrapStrategy': 'WRAP',
                            'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
                            'verticalAlignment': 'MIDDLE'}}, 'fields': 'userEnteredFormat'}},
        {'updateDimensionProperties': {'range': {'sheetId': sheet.id, 'dimension': 'ROWS', 'startIndex': 0, 'endIndex': 2},
                                        'properties': {'pixelSize': 48}, 'fields': 'pixelSize'}},
    ]
    widths = ([85, 260, 160, 85] + [145]*(width-4) if spec['kind'] == 'matrix' else
              [250, 150, 150, 150, 155, 155, 155, 260, 260] if spec['kind'] == 'summary' else
              [230, 75, 250, 75, 100, 110, 175, 105, 170, 440, 155])
    for i, pixels in enumerate(widths):
        requests.append({'updateDimensionProperties': {'range': {'sheetId': sheet.id, 'dimension': 'COLUMNS',
                                                                  'startIndex': i, 'endIndex': i+1},
                                                        'properties': {'pixelSize': pixels}, 'fields': 'pixelSize'}})
    if spec['kind'] == 'matrix' and width > 4:
        for mark, color in [('Y', {'red': .82, 'green': .94, 'blue': .85}), ('N', {'red': 1, 'green': .88, 'blue': .87})]:
            requests.append({'addConditionalFormatRule': {'index': 0, 'rule': {
                'ranges': [{'sheetId': sheet.id, 'startRowIndex': 2, 'startColumnIndex': 4,
                            'endColumnIndex': width}],
                'booleanRule': {'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': mark}]},
                                'format': {'backgroundColor': color}}}}})
    requests += _body_style(sheet, spec['kind'], 2, len(spec['values']), width)
    return requests


def _body_style(sheet, kind, first, last, width):
    if first >= last:
        return []
    requests = [{'repeatCell': {'range': {'sheetId': sheet.id, 'startRowIndex': first, 'endRowIndex': last,
                                         'startColumnIndex': 0, 'endColumnIndex': width},
                               'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP', 'verticalAlignment': 'TOP'}},
                               'fields': 'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment'}}]
    if kind in ('summary', 'details'):
        requests.append({'updateDimensionProperties': {'range': {'sheetId': sheet.id, 'dimension': 'ROWS',
                                                                 'startIndex': first, 'endIndex': last},
                                                        'properties': {'pixelSize': 42}, 'fields': 'pixelSize'}})
    return requests


def _new_columns_style(sheet, first, last):
    """Extend the matrix look without touching the trainer's existing formats."""
    requests = [
        {'repeatCell': {'range': {'sheetId': sheet.id, 'startRowIndex': 0, 'endRowIndex': 2,
                                  'startColumnIndex': first, 'endColumnIndex': last},
                        'cell': {'userEnteredFormat': {'backgroundColor': SLATE, 'wrapStrategy': 'WRAP',
                            'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
                            'verticalAlignment': 'MIDDLE'}}, 'fields': 'userEnteredFormat'}},
        {'updateDimensionProperties': {'range': {'sheetId': sheet.id, 'dimension': 'COLUMNS',
                                                 'startIndex': first, 'endIndex': last},
                                        'properties': {'pixelSize': 145}, 'fields': 'pixelSize'}},
    ]
    for mark, color in [('Y', {'red': .82, 'green': .94, 'blue': .85}), ('N', {'red': 1, 'green': .88, 'blue': .87})]:
        requests.append({'addConditionalFormatRule': {'index': 0, 'rule': {
            'ranges': [{'sheetId': sheet.id, 'startRowIndex': 2, 'startColumnIndex': first, 'endColumnIndex': last}],
            'booleanRule': {'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': mark}]},
                            'format': {'backgroundColor': color}}}}})
    return requests


def sync_views(book, snapshot):
    """Run under the existing workbook lock; write only owned derived rectangles."""
    from finance_roster import ensure_tier
    ensure_tier(book)
    directory = book.worksheet('Справочник_клиентов')
    projection = project(snapshot, directory.row_count)
    sheets = {sheet.title: sheet for sheet in book.worksheets()}
    total = 0
    for title, spec in projection.items():
        values = spec['values']
        rows, cols = len(values), max(map(len, values))
        created = title not in sheets
        sheet = book.add_worksheet(title=title, rows=max(rows, 20), cols=max(cols, 4)) if created else sheets[title]
        previous = _state(book.id, sheet.id)
        old_rows, old_cols = previous.get('rows', 0), previous.get('cols', 0)
        rectangles = previous.get('rectangles', [[old_rows, old_cols]])
        if sheet.row_count < max(rows, old_rows) or sheet.col_count < max(cols, old_cols):
            sheet.resize(rows=max(sheet.row_count, rows, old_rows), cols=max(sheet.col_count, cols, old_cols))
        area = f'A1:{_column(max(cols, old_cols))}{max(rows, old_rows)}'
        current = sheet.get(area, value_render_option='FORMULA')
        updates, changed = _diff(sheet.id, current, values, old_rows, old_cols, rectangles)
        signature = hashlib.sha256(json.dumps(spec['layout']).encode()).hexdigest()
        pending = {'rows': max(rows, old_rows), 'cols': max(cols, old_cols),
                   'rectangles': _rectangles(rectangles+[[rows, cols]]), 'layout': previous.get('layout')}
        _state(book.id, sheet.id, pending)
        if created:
            updates += _style(sheet, spec)
        else:
            if spec['kind'] == 'matrix' and cols > old_cols:
                updates += _new_columns_style(sheet, max(4, old_cols), cols)
            if rows > old_rows:
                updates += _body_style(sheet, spec['kind'], max(2, old_rows), rows, cols)
        if spec['kind'] == 'details' and created:
            updates.append({'setBasicFilter': {'filter': {'range': {'sheetId': sheet.id,
                'startRowIndex': 0, 'startColumnIndex': 0, 'endColumnIndex': cols}}}})
        if spec['kind'] == 'matrix' and previous.get('layout') != signature:
            metadata = book.fetch_sheet_metadata()
            info = next(s for s in metadata['sheets'] if s['properties']['sheetId'] == sheet.id)
            for group in reversed(info.get('columnGroups', [])):
                if (group['range'].get('startIndex', 0) >= 4 and
                        group['range'].get('endIndex', 0) <= max(cols, old_cols)):
                    updates.append({'deleteDimensionGroup': {'range': group['range']}})
            for i, group in enumerate(spec['groups']):
                if group['end']-group['start'] > 1:
                    updates.append({'addDimensionGroup': {'range': {'sheetId': sheet.id, 'dimension': 'COLUMNS',
                                            'startIndex': group['start'], 'endIndex': group['end']}}})
                first_new = group['start'] if created else max(group['start'], old_cols)
                if rows > 2 and first_new < group['end']:
                    updates.append({'repeatCell': {'range': {'sheetId': sheet.id, 'startRowIndex': 2, 'endRowIndex': rows,
                                                 'startColumnIndex': first_new, 'endColumnIndex': group['end']},
                        'cell': {'userEnteredFormat': {'backgroundColor': PALE[i % 2]}},
                        'fields': 'userEnteredFormat.backgroundColor'}})
        # Re-read before changing values so a concurrent edit is never silently
        # overwritten by an old snapshot. Derived-cell changes will re-render on retry.
        if updates and sheet.get(area, value_render_option='FORMULA') != current:
            raise RuntimeError(f'Лист «{title}» изменился во время обновления; повторите синхронизацию')
        for first in range(0, len(updates), 400):
            book.batch_update({'requests': updates[first:first+400]})
        _state(book.id, sheet.id, {'rows': rows, 'cols': cols, 'layout': signature})
        total += changed
    return {'sheets': list(projection), 'changed_cells': total}
