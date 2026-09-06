"""Durable ID/date reconciliation between the editable attendance and bot data.

Manual corrections (including a cleared cell) win over subsequent bot answers.
The SQLite journal survives release replacement and tracks uncertain cloud writes.
"""
from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sqlite3
import threading

_thread_lock = threading.RLock()
_local = threading.local()
FIELDS = ('fio', 'username', 'phone')


def _database_path():
    import db
    return str(Path(db.DB_PATH).resolve())


@contextmanager
def workbook_lock():
    """Serialize cloud sync/export across threads and Linux service/CLI processes."""
    with _thread_lock:
        if getattr(_local, 'depth', 0):
            _local.depth += 1
            try:
                yield
            finally:
                _local.depth -= 1
            return
        path = Path(_database_path() + '.sheet.lock')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+b') as handle:
            try:
                import fcntl
            except ImportError:  # Desktop tests use the same in-process lock.
                fcntl = None
            if fcntl:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _local.depth = 1
            try:
                yield
            finally:
                _local.depth = 0
                if fcntl:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _state(book_id, value=None):
    with closing(sqlite3.connect(_database_path(), timeout=30)) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS attendance_sheet_sync '
                     '(spreadsheet_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)')
        if value is not None:
            conn.execute('INSERT INTO attendance_sheet_sync VALUES (?,?) '
                         'ON CONFLICT(spreadsheet_id) DO UPDATE SET state_json=excluded.state_json',
                         (book_id, json.dumps(value, ensure_ascii=False)))
            return value
        row = conn.execute('SELECT state_json FROM attendance_sheet_sync WHERE spreadsheet_id=?',
                           (book_id,)).fetchone()
        return json.loads(row[0]) if row else {}


def _text(value):
    return '' if value is None else str(value).strip()


def parse_grid(grid, title):
    from google_sheet import BASE_HEADERS, FLAG_HEADER, _four_digits, _parse_date_label
    if not grid or not any(grid[0]):
        return {'dates': [], 'order': [], 'records': {}}
    header = [_text(v) for v in grid[0]]
    if header[:5] != BASE_HEADERS or len(header) < 7 or header[5] != FLAG_HEADER:
        raise ValueError(f'{title}: не меняйте заголовки ID, Telegram ID, ФИО и дат')
    indexed = []
    for column, label in enumerate(header[6:], 6):
        if not label:
            continue
        parsed = _parse_date_label(label)
        if not parsed:
            raise ValueError(f'{title}: некорректная дата {label}')
        indexed.append((column, parsed.isoformat()))
    dates = [day for _, day in indexed]
    if len(dates) != len(set(dates)) or not dates:
        raise ValueError(f'{title}: отсутствуют или дублируются даты')
    records, order, telegrams = {}, [], set()
    for row_no, row in enumerate(grid[2:], 3):
        if not any(_text(v) for v in row):
            continue
        padded = list(row) + [''] * max(0, len(header) - len(row))
        code = _four_digits(padded[0])
        if not code or code in records:
            raise ValueError(f'{title}: некорректный или повторный ID в строке {row_no}')
        telegram = _text(padded[1])
        if telegram and (not telegram.isdecimal() or telegram in telegrams):
            raise ValueError(f'{title}: некорректный или повторный Telegram ID в строке {row_no}')
        if telegram:
            telegrams.add(telegram)
        values = dict(zip(FIELDS, [_text(padded[i]) for i in (2, 3, 4)]))
        for column, day in indexed:
            value = _text(padded[column]).upper()
            if value not in ('', 'Y', 'N'):
                raise ValueError(f'{title}: ID {code}, {day}: допустимы Y, N или пустая ячейка')
            if value:
                values[day] = value
        records[code] = {'telegram_id': telegram, 'values': values}
        order.append(code)
    return {'dates': sorted(dates), 'order': order, 'records': records}


def to_grid(snapshot):
    from google_sheet import BASE_HEADERS, FLAG_HEADER, _active_formula, _column_name
    days = snapshot['dates']
    labels = [date.fromisoformat(day).strftime('%d.%m.%Y') for day in days]
    weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    grid = [BASE_HEADERS + [FLAG_HEADER] + labels,
            ['', '', '', '', 'День недели', 'За последние 30 дней'] +
            [weekdays[date.fromisoformat(day).weekday()] for day in days]]
    last = _column_name(len(grid[0]))
    for row_number, code in enumerate(snapshot['order'], 3):
        record = snapshot['records'][code]
        values = record['values']
        grid.append([code, record['telegram_id']] + [values.get(k, '') for k in FIELDS] +
                    [_active_formula(row_number, last)] + [values.get(day, '') for day in days])
    return grid


def _check_identity(current, previous, title, alternative=None):
    """Names are editable; identity keys and existing date headers are not."""
    alternative = alternative or previous
    if not set(previous['dates']).issubset(current['dates']):
        raise ValueError(f'{title}: удалена дата; восстановите заголовки')
    added_dates = set(current['dates']) - set(previous['dates']) - set(alternative['dates'])
    if added_dates:
        raise ValueError(f'{title}: даты добавляет бот; восстановите исходные заголовки')
    for code, record in previous['records'].items():
        if code not in current['records']:
            raise ValueError(f'{title}: удалён или изменён ID {code}; исправьте ключ')
        allowed = {record['telegram_id']}
        if code in alternative['records']:
            allowed.add(alternative['records'][code]['telegram_id'])
        if current['records'][code]['telegram_id'] not in allowed:
            raise ValueError(f'{title}: изменён Telegram ID клиента {code}')
    unknown = set(current['records']) - set(previous['records']) - set(alternative['records'])
    if unknown:
        raise ValueError(f'{title}: неизвестные ID {", ".join(sorted(unknown))}; добавьте человека через бота')


def _put(record, key, value):
    if value or key in FIELDS:
        record['values'][key] = value
    else:
        record['values'].pop(key, None)


def reconcile_grids(source_grid, public_grid, dates, bot_rows, state):
    """Pure reconciliation plan; state is a previously durable journal record."""
    from google_sheet import merge_grid
    source = parse_grid(source_grid, 'Посещения_bot')
    public = parse_grid(public_grid, 'Посещения')
    if not public['records'] and not state:
        public = deepcopy(source)
    pending = state.get('pending')
    previous = pending['target'] if pending else state.get('snapshot')
    overrides = deepcopy(state.get('overrides', {}))
    if previous:
        for title, current, kind in [('Посещения_bot', source, 'source'), ('Посещения', public, 'public')]:
            before = pending['before'][kind] if pending else previous
            _check_identity(current, before, title, previous)
        target = deepcopy(previous)
        # Row sorting is safe: reconcile by ID and retain the visible row order.
        target['order'] = public['order'] + [c for c in target['order'] if c not in public['records']]
    else:
        if set(source['records']) != set(public['records']):
            raise ValueError('Посещения: список ID отличается от исходного; восстановите ключи перед синхронизацией')
        if set(source['dates']) != set(public['dates']):
            raise ValueError('Посещения: даты отличаются от исходных; восстановите заголовки')
        target = deepcopy(source)
        target['order'] = public['order'] + [c for c in source['order'] if c not in public['records']]
        for code, record in public['records'].items():
            if code not in source['records']:
                raise ValueError(f'Посещения: неизвестный ID {code}; восстановите исходный ключ')
            if record['telegram_id'] != source['records'][code]['telegram_id']:
                raise ValueError(f'Посещения: изменён Telegram ID клиента {code}')
            target['records'][code] = deepcopy(record)
    target['dates'] = sorted(set(target['dates']) | set(source['dates']) | set(public['dates']) | set(dates or []))

    # When a write response was lost, both BEFORE and TARGET are legitimate
    # observations. Neither is evidence of a new manual edit after that attempt.
    for kind, current in [('source', source), ('public', public)]:
        for code, record in current['records'].items():
            baseline = previous or source
            old_record = baseline['records'].get(code, {'values': {}})
            for key in (*FIELDS, *target['dates']):
                value = record['values'].get(key, '')
                old = old_record['values'].get(key, '')
                is_manual = value != old
                if pending:
                    before_record = pending['before'][kind]['records'].get(code)
                    if before_record is not None:
                        is_manual = is_manual and value != before_record['values'].get(key, '')
                if is_manual:
                    overrides.setdefault(code, {})[key] = value

    # Reuse the established unique legacy/Telegram matching, including ID adoption.
    candidate_grid, adoptions = merge_grid(to_grid(target), dates or [], bot_rows or [])
    candidate = parse_grid(candidate_grid, 'База бота')
    known_codes = set(target['records'])
    for code in candidate['order']:
        if code not in target['records']:
            target['records'][code] = deepcopy(candidate['records'][code])
            target['order'].append(code)
        else:
            # Telegram ownership can only be assigned by registered bot data.
            target['records'][code]['telegram_id'] = candidate['records'][code]['telegram_id']

    bot_snapshot = deepcopy(state.get('bot_snapshot'))
    if bot_rows is not None:
        bot_snapshot = {}
        adopted = dict(adoptions)
        by_telegram = {v['telegram_id']: k for k, v in candidate['records'].items() if v['telegram_id']}
        old_bot = state.get('bot_snapshot')
        for participant in bot_rows:
            code = str(adopted.get(participant.get('internal_id'), participant['participant_id']))
            telegram = _text(participant.get('telegram_id'))
            code = by_telegram.get(telegram, code)
            values = {'fio': participant.get('full_name') or '',
                      'username': '@' + participant['username'] if participant.get('username') else '',
                      'phone': participant.get('phone') or ''}
            values.update({day: mark for day, mark in participant.get('marks', {}).items() if mark})
            bot_snapshot[code] = values
            old_values = (old_bot or {}).get(code, {})
            keys = set(values) | set(old_values) | set(target['dates'])
            for key in keys:
                value = values.get(key, '')
                if old_bot is None and code in known_codes:
                    cloud = target['records'][code]['values'].get(key, '')
                    # First observation records old DB history; it never replays
                    # it over an already corrected workbook, including blanks.
                    if value != cloud:
                        overrides.setdefault(code, {}).setdefault(key, cloud)
                elif value != old_values.get(key, '') or code not in known_codes:
                    if key not in overrides.get(code, {}):
                        _put(target['records'][code], key, value)
    for code, corrections in overrides.items():
        if code in target['records']:
            for key, value in corrections.items():
                _put(target['records'][code], key, value)
    plan = {'version': 1, 'snapshot': state.get('snapshot'), 'bot_snapshot': bot_snapshot,
            'overrides': overrides,
            'pending': {'target': target, 'before': {'source': source, 'public': public}}}
    return to_grid(target), plan, adoptions


def _read(sheet):
    return sheet.get_all_values(pad_values=False, value_render_option='FORMULA')


def _cell(grid, row, column):
    return grid[row][column] if row < len(grid) and column < len(grid[row]) else ''


def _changes(sheet, current, target):
    """Only cell values change. Existing formatting, notes and validation survive."""
    requests = []
    rows = max(sheet.row_count, len(target))
    cols = max(sheet.col_count, len(target[0]))
    if rows != sheet.row_count or cols != sheet.col_count:
        requests.append({'updateSheetProperties': {'properties': {'sheetId': sheet.id,
            'gridProperties': {'rowCount': rows, 'columnCount': cols}},
            'fields': 'gridProperties.rowCount,gridProperties.columnCount'}})
    for r, row in enumerate(target):
        start, cells = None, []
        for c in range(len(row) + 1):
            changed = c < len(row) and _cell(current, r, c) != row[c]
            if changed:
                if start is None:
                    start = c
                value = row[c]
                # Only the managed flag column is a formula; names and phones
                # remain literal text even when they begin with '=', '+' etc.
                field = 'formulaValue' if c == 5 and r >= 2 else 'stringValue'
                cells.append({'userEnteredValue': {field: str(value)}} if value != '' else {})
            elif start is not None:
                requests.append({'updateCells': {'start': {'sheetId': sheet.id,
                    'rowIndex': r, 'columnIndex': start}, 'rows': [{'values': cells}],
                    'fields': 'userEnteredValue'}})
                start, cells = None, []
    return requests


def reconcile_book(book, dates=None, rows=None):
    """Reconcile manual edits; None bot inputs mean preserve the last bot snapshot."""
    from config import GOOGLE_SHEET_NAME
    with workbook_lock():
        source = book.worksheet(GOOGLE_SHEET_NAME)
        try:
            public = book.worksheet('Посещения')
        except Exception as exc:
            import gspread
            if not isinstance(exc, gspread.WorksheetNotFound):
                raise
            public = book.add_worksheet(title='Посещения', rows=source.row_count, cols=source.col_count)
        source_grid, public_grid = _read(source), _read(public)
        state = _state(str(book.id))
        target, journal, adoptions = reconcile_grids(source_grid, public_grid, dates, rows, state)
        requests = _changes(source, source_grid, target) + _changes(public, public_grid, target)
        # Persist corrections and uncertainty before the first possible cloud write.
        _state(str(book.id), journal)
        if requests:
            # Do not overwrite a trainer edit made while this plan was prepared.
            if _read(source) != source_grid or _read(public) != public_grid:
                raise RuntimeError('Посещения изменились во время синхронизации; повтор будет выполнен автоматически')
            for offset in range(0, len(requests), 400):
                book.batch_update({'requests': requests[offset:offset + 400]})
        journal['snapshot'] = journal.pop('pending')['target']
        _state(str(book.id), journal)
        return adoptions
