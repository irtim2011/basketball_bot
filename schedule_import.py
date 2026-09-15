"""Preview and atomically apply dated schedules without deleting weekly series."""
import asyncio
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

import db
import events
import utils

STALE = 'Расписание изменилось или тренировка уже началась. Обновите предпросмотр импорта.'


def _now(value=None):
    current = value if value is not None else utils.now()
    if current.tzinfo is None:
        current = utils.TZ.localize(current, is_dst=None)
    return current.astimezone(utils.TZ)


def _start(entry):
    naive = datetime.fromisoformat(f"{entry['date']}T{entry['time']}")
    try:
        return utils.TZ.localize(naive, is_dst=None)
    except Exception as exc:
        raise ValueError('Дата и время тренировки неоднозначны в выбранном часовом поясе') from exc


def _normalize(entries):
    if not isinstance(entries, list):
        raise ValueError('Список тренировок не распознан')
    selected = {}
    for raw in entries:
        try:
            if not isinstance(raw, dict) or not isinstance(raw.get('date'), str) or not isinstance(raw.get('time'), str):
                raise ValueError()
            day = date.fromisoformat(raw['date']).isoformat()
            time = raw['time']
            ending = raw.get('end_time')
            if ending is not None and not isinstance(ending, str):
                raise ValueError()
            ending = ending or None
            if day != raw['date'] or utils.parse_time_str(time) != time:
                raise ValueError()
            if ending is not None and (utils.parse_time_str(ending) != ending or ending <= time):
                raise ValueError()
            entry = {'date': day, 'time': time, 'end_time': ending}
            _start(entry)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('Проверьте дату, начало и окончание тренировки') from exc
        key = (day, time)
        if key in selected and selected[key]['end_time'] != ending:
            raise ValueError(f'У тренировки {day} {time} указаны разные окончания')
        selected[key] = entry
    ordered = [selected[key] for key in sorted(selected)]
    if ordered and (date.fromisoformat(ordered[-1]['date']) - date.fromisoformat(ordered[0]['date'])).days > 366:
        raise ValueError('Между крайними датами одного импорта должно быть не больше 366 дней')
    return ordered


def _connection():
    path = Path(db.DB_PATH).resolve()
    if not path.is_file():
        raise RuntimeError('База бота не найдена; сначала запустите инициализацию')
    uri = 'file:' + quote(path.as_posix(), safe='/:') + '?mode=rw'
    conn = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _snapshot(conn):
    return [dict(row) for row in conn.execute('SELECT * FROM schedule ORDER BY id')]


def _plan(entries, slots, now):
    entries = _normalize(entries)
    low = entries[0]['date'] if entries else None
    high = entries[-1]['date'] if entries else None
    future = [entry for entry in entries if _start(entry) > now]
    past = [entry for entry in entries if _start(entry) <= now]
    existing_by_key = {}
    if low is not None and future:
        first, last = date.fromisoformat(low), date.fromisoformat(high)
        for slot in slots:
            if not slot['is_active']:
                continue
            days = ([date.fromisoformat(slot['training_date'])] if slot['training_date'] else
                    [first+timedelta(days=i) for i in range((last-first).days+1)
                     if (first+timedelta(days=i)).weekday() == slot['weekday']])
            for day in days:
                if not first <= day <= last:
                    continue
                item = {'date': day.isoformat(), 'time': slot['time']}
                start = _start(item)
                if start <= now or not events.matches(slot, start):
                    continue
                existing_by_key.setdefault((item['date'], item['time']), []).append(slot)
    additions, existing, missing = [], [], []
    requested = {(entry['date'], entry['time']) for entry in future}
    for entry in future:
        matched = existing_by_key.get((entry['date'], entry['time']), [])
        if not matched:
            additions.append(dict(entry))
            continue
        old_ends = {str(slot['id']): events.end_time(slot, _start(entry)) for slot in matched}
        existing.append(dict(entry, slot_id=matched[0]['id'], slot_ids=[s['id'] for s in matched],
                             old_end_time=old_ends[str(matched[0]['id'])], old_end_times=old_ends,
                             end_time_changed=entry['end_time'] is not None and
                             any(old != entry['end_time'] for old in old_ends.values())))
    for key, matched in sorted(existing_by_key.items()):
        if key in requested:
            continue
        for slot in matched:
            entry = {'date': key[0], 'time': key[1], 'slot_id': slot['id'],
                     'recurring': not bool(slot['training_date'])}
            entry['end_time'] = events.end_time(slot, _start(entry))
            missing.append(entry)
    result = {'entries': entries, 'additions': additions, 'existing': existing, 'missing': missing,
              'past': past, 'range_start': low, 'range_end': high}
    payload = json.dumps({'schedule': slots, 'plan': result}, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'))
    result['fingerprint'] = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    result['created_at'] = now.isoformat()
    return result


def _build(entries, now):
    conn = _connection()
    try:
        return _plan(entries, _snapshot(conn), _now(now))
    finally:
        conn.close()


async def build_plan(entries, now=None):
    return await asyncio.to_thread(_build, entries, now)


def _apply(plan, cancel_missing, now):
    conn = _connection()
    try:
        # The dedicated connection is essential: unrelated async handlers may
        # commit the shared db connection while this import awaits execution.
        conn.execute('BEGIN IMMEDIATE')
        supplied_now = now
        now = _now(supplied_now)
        snapshot = _snapshot(conn)
        current = _plan(plan.get('entries'), snapshot, now)
        if current['fingerprint'] != plan.get('fingerprint'):
            raise ValueError(STALE)
        by_id = {slot['id']: dict(slot) for slot in snapshot}
        result = {'added': 0, 'existing': len(current['existing']), 'cancelled': 0,
                  'end_times_updated': 0, 'skipped_past': len(current['past']),
                  'range_start': current['range_start'], 'range_end': current['range_end']}
        for entry in current['additions']:
            conn.execute('INSERT INTO schedule (weekday,time,training_date,starts_on,is_active,end_time) '
                         'VALUES (?,?,?,?,1,?)',
                         (date.fromisoformat(entry['date']).weekday(), entry['time'], entry['date'],
                          None, entry['end_time']))
            result['added'] += 1
        for entry in current['existing']:
            if entry['end_time'] is None:
                continue  # An omitted end never erases an existing duration.
            for slot_id in entry['slot_ids']:
                slot = by_id[slot_id]
                if events.end_time(slot, _start(entry)) == entry['end_time']:
                    continue
                if slot['training_date']:
                    conn.execute('UPDATE schedule SET end_time=? WHERE id=?', (entry['end_time'], slot_id))
                    slot['end_time'] = entry['end_time']
                else:
                    overrides = events._slot_json(slot, 'end_times', dict).copy()
                    if entry['end_time'] == slot['end_time']:
                        overrides.pop(entry['date'], None)
                    else:
                        overrides[entry['date']] = entry['end_time']
                    slot['end_times'] = json.dumps(overrides, sort_keys=True)
                    conn.execute('UPDATE schedule SET end_times=? WHERE id=?', (slot['end_times'], slot_id))
                result['end_times_updated'] += 1
        if cancel_missing:
            for entry in current['missing']:
                slot_id = entry['slot_id']
                slot = by_id[slot_id]
                if slot['training_date']:
                    conn.execute('UPDATE schedule SET is_active=0 WHERE id=?', (slot_id,))
                else:
                    excluded = set(events._slot_json(slot, 'excluded_dates', list))
                    excluded.add(entry['date'])
                    slot['excluded_dates'] = json.dumps(sorted(excluded))
                    conn.execute('UPDATE schedule SET excluded_dates=? WHERE id=?', (slot['excluded_dates'], slot_id))
                conn.execute('UPDATE responses SET is_cancelled=1 WHERE schedule_id=? '
                             'AND substr(starts_at,1,10)=? AND datetime(starts_at)>datetime(?)',
                             (slot_id, entry['date'], now.isoformat()))
                conn.execute("UPDATE manual_polls SET status='cancelled' WHERE schedule_id=? "
                             'AND substr(starts_at,1,10)=? AND datetime(starts_at)>datetime(?)',
                             (slot_id, entry['date'], now.isoformat()))
                result['cancelled'] += 1
        # A preview must also remain future if lock contention or a large batch
        # carried processing across the start time. Explicit test clocks stay fixed.
        if _plan(current['entries'], snapshot, _now(supplied_now))['fingerprint'] != current['fingerprint']:
            raise ValueError(STALE)
        from planning import cancel_invalid_future
        cancel_invalid_future(conn, now)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


async def apply_plan(plan, cancel_missing=False, now=None):
    return await asyncio.to_thread(_apply, plan, bool(cancel_missing), now)
