"""Durable month/week forecasts, separate from attendance and its finances."""
import asyncio
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
from urllib.parse import quote

import db
import events
import utils

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS planning_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS planned_polls (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 participant_id INTEGER NOT NULL REFERENCES participants(id),
 kind TEXT NOT NULL CHECK(kind IN ('month','week')),
 period_start TEXT NOT NULL, period_end TEXT NOT NULL, due_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
 generation INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT, lease_until TEXT, sent_at TEXT, created_at TEXT NOT NULL,
 UNIQUE(participant_id,kind,period_start)
);
CREATE TABLE IF NOT EXISTS planned_answers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, poll_id INTEGER NOT NULL REFERENCES planned_polls(id),
 occurrence_key TEXT NOT NULL, starts_at TEXT NOT NULL, end_time TEXT, schedule_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','yes','no')),
 responded_at TEXT, is_cancelled INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 1,
 UNIQUE(poll_id,occurrence_key)
);
CREATE TABLE IF NOT EXISTS planning_callback_receipts (
 callback_id TEXT PRIMARY KEY, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answer_change_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, participant_id INTEGER NOT NULL,
 occurrence_key TEXT NOT NULL, log_type TEXT NOT NULL CHECK(log_type IN ('stage','effective')),
 from_stage TEXT, to_stage TEXT, old_status TEXT NOT NULL, new_status TEXT NOT NULL,
 changed_at TEXT NOT NULL, callback_id TEXT NOT NULL,
 UNIQUE(callback_id,log_type)
);
CREATE INDEX IF NOT EXISTS planned_polls_due ON planned_polls(status,next_attempt_at);
CREATE INDEX IF NOT EXISTS planned_answers_key ON planned_answers(occurrence_key);
CREATE INDEX IF NOT EXISTS answer_changes_key ON answer_change_log(occurrence_key,changed_at,id);
"""


def _now(value=None):
    value = value or utils.now()
    if value.tzinfo is None:
        value = utils.TZ.localize(value, is_dst=None)
    return value.astimezone(utils.TZ)


def occurrence_key(start):
    return _now(start).astimezone(timezone.utc).isoformat()


def _connection():
    path = Path(db.DB_PATH).resolve()
    conn = sqlite3.connect('file:' + quote(path.as_posix(), safe='/:') + '?mode=rw',
                           uri=True, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


@contextmanager
def _transaction():
    conn = _connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init(now):
    now = _now(now)
    conn = _connection()
    try:
        conn.executescript(SCHEMA)
        conn.execute('BEGIN IMMEDIATE')
        inserted = conn.execute("INSERT OR IGNORE INTO planning_meta VALUES ('enabled_at',?)", (occurrence_key(now),)).rowcount
        if inserted:
            # Observe existing main answers at enable time, never invent their
            # earlier history. This anchors baselines AFTER tracking was enabled.
            seen = set()
            rows = conn.execute("SELECT * FROM responses WHERE is_cancelled=0 AND status IN ('yes','no') "
                                'AND datetime(starts_at)>datetime(?) ORDER BY responded_at DESC,id DESC',
                                (now.isoformat(),)).fetchall()
            for row in rows:
                key = occurrence_key(datetime.fromisoformat(row['starts_at']))
                identity = (row['participant_id'], key)
                if identity in seen:
                    continue
                seen.add(identity)
                conn.execute('INSERT INTO answer_change_log(participant_id,occurrence_key,log_type,from_stage,to_stage,old_status,new_status,changed_at,callback_id) '
                             "VALUES (?,?,'effective',NULL,'main','pending',?,?,?)",
                             (row['participant_id'], key, row['status'], now.isoformat(),
                              f"system:initial:{row['participant_id']}:{key}"))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


async def init_schema(now=None):
    await asyncio.to_thread(_init, now)


def _slots(conn):
    return [dict(row) for row in conn.execute('SELECT * FROM schedule ORDER BY id')]


def _sessions(slots, first, last):
    baseline = utils.TZ.localize(datetime.combine(first, time.min)) - timedelta(microseconds=1)
    result = {}
    for slot in slots:
        if not slot['is_active']:
            continue
        for start in events.occurrences(slot, baseline, (last-first).days+1):
            if not first <= start.date() <= last:
                continue
            key = occurrence_key(start)
            result.setdefault(key, {'key': key, 'starts_at': start.isoformat(),
                                    'end_time': events.end_time(slot, start),
                                    'schedule_id': slot['id'], 'cancelled': False})
    return result


def _next_month(day):
    return date(day.year + (day.month == 12), 1 if day.month == 12 else day.month+1, 1)


def _periods(now):
    month = now.date().replace(day=1)
    monday = now.date() - timedelta(days=now.weekday())
    for kind, first, last in (
        ('month', month, _next_month(month)-timedelta(days=1)),
        ('month', _next_month(month), _next_month(_next_month(month))-timedelta(days=1)),
        ('week', monday, monday+timedelta(days=6)),
        ('week', monday+timedelta(days=7), monday+timedelta(days=13)),
    ):
        due = utils.TZ.localize(datetime.combine(first-timedelta(days=1), time(12)))
        yield kind, first, last, due


def _refresh_poll(conn, poll, now, slots=None, invalidate=True):
    if invalidate:
        cancel_invalid_future(conn, now)
    sessions = _sessions(slots or _slots(conn), date.fromisoformat(poll['period_start']),
                         date.fromisoformat(poll['period_end']))
    existing = {row['occurrence_key']: dict(row) for row in conn.execute(
        'SELECT * FROM planned_answers WHERE poll_id=?', (poll['id'],))}
    for key, item in sessions.items():
        if datetime.fromisoformat(item['starts_at']) <= now:
            continue  # Never add a new retrospective question.
        old = existing.get(key)
        if not old:
            conn.execute('INSERT INTO planned_answers(poll_id,occurrence_key,starts_at,end_time,schedule_id) '
                         'VALUES (?,?,?,?,?)',
                         (poll['id'], key, item['starts_at'], item['end_time'], item['schedule_id']))
        else:
            before = _effective(conn, poll['participant_id'], key) if old['is_cancelled'] else None
            conn.execute('UPDATE planned_answers SET end_time=?,schedule_id=?,is_cancelled=0, '
                         "status=CASE WHEN is_cancelled=1 THEN 'pending' ELSE status END, "
                         'responded_at=CASE WHEN is_cancelled=1 THEN NULL ELSE responded_at END, '
                         'generation=generation+CASE WHEN is_cancelled=1 THEN 1 ELSE 0 END WHERE id=?',
                         (item['end_time'], item['schedule_id'], old['id']))
            if before is not None:
                _log_system_effective(conn, poll['participant_id'], key, before, now, 'restore')


def _prepare(now):
    now = _now(now)
    with _transaction() as conn:
        enabled = datetime.fromisoformat(conn.execute(
            "SELECT value FROM planning_meta WHERE key='enabled_at'").fetchone()[0])
        slots = _slots(conn)
        people = list(conn.execute('SELECT * FROM participants WHERE is_active=1 AND is_registered=1 AND telegram_id IS NOT NULL'))
        for kind, first, last, due in _periods(now):
            if not enabled <= due <= now:
                continue
            if kind == 'week' and now >= due + timedelta(days=7):
                continue  # On Sunday send the next week, not two weeks at once.
            sessions = _sessions(slots, first, last)
            if not any(datetime.fromisoformat(item['starts_at']) > now for item in sessions.values()):
                continue
            for person in people:
                conn.execute('INSERT OR IGNORE INTO planned_polls(participant_id,kind,period_start,period_end,due_at,created_at) '
                             'VALUES (?,?,?,?,?,?)', (person['id'], kind, first.isoformat(), last.isoformat(),
                                                     due.isoformat(), now.isoformat()))
                conn.execute("UPDATE planned_polls SET status='pending',next_attempt_at=NULL,lease_until=NULL "
                             "WHERE participant_id=? AND kind=? AND period_start=? AND status='cancelled' AND message_id IS NULL",
                             (person['id'], kind, first.isoformat()))
        # Poll rows are durable; completed deliveries are never resent on schedule changes.
        cancel_invalid_future(conn, now)
        for poll in conn.execute("SELECT * FROM planned_polls WHERE status IN ('pending','sending')").fetchall():
            _refresh_poll(conn, poll, now, slots, invalidate=False)


def cancel_invalid_future(conn, now=None):
    """For schedule mutation transactions; safe before the planning migration exists."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='planned_answers'").fetchone():
        return 0
    now = _now(now)
    slots = _slots(conn)
    by_day = {}
    changed = 0
    cancelled, affected = [], {}
    rows = conn.execute('SELECT pa.*,pp.participant_id FROM planned_answers pa '
                        'JOIN planned_polls pp ON pp.id=pa.poll_id WHERE pa.is_cancelled=0 '
                        'AND datetime(pa.starts_at)>datetime(?)', (now.isoformat(),)).fetchall()
    for answer in rows:
        day = datetime.fromisoformat(answer['starts_at']).date()
        if day not in by_day:
            by_day[day] = _sessions(slots, day, day)
        if answer['occurrence_key'] not in by_day[day]:
            cancelled.append(answer['id'])
            identity = (answer['participant_id'], answer['occurrence_key'])
            affected.setdefault(identity, _effective(conn, *identity))
    # Parent schedule transactions cancel main responses before this hook.
    # Include them even when no monthly/weekly poll existed for this person.
    for answer in conn.execute('SELECT participant_id,starts_at FROM responses WHERE is_cancelled=1 '
                               'AND datetime(starts_at)>datetime(?)', (now.isoformat(),)).fetchall():
        start = datetime.fromisoformat(answer['starts_at'])
        if start.date() not in by_day:
            by_day[start.date()] = _sessions(slots, start.date(), start.date())
        key = occurrence_key(start)
        if key not in by_day[start.date()]:
            identity = (answer['participant_id'], key)
            affected.setdefault(identity, _effective(conn, *identity))
    for answer_id in cancelled:
        changed += conn.execute('UPDATE planned_answers SET is_cancelled=1,generation=generation+1 WHERE id=?',
                                (answer_id,)).rowcount
    for (participant_id, key), before in affected.items():
        _log_system_effective(conn, participant_id, key, before, now, 'cancel')
    return changed


def _invalidate(now):
    with _transaction() as conn:
        return cancel_invalid_future(conn, now)


async def invalidate_future(now=None):
    return await asyncio.to_thread(_invalidate, now)


def _card(conn, poll, now, page=0):
    answers = [dict(row) for row in conn.execute(
        'SELECT * FROM planned_answers WHERE poll_id=? ORDER BY starts_at,id', (poll['id'],))]
    pages = max(1, (len(answers)+5)//6)
    page = max(0, min(int(page), pages-1))
    for item in answers:
        item['key'] = item.pop('occurrence_key')
        item['can_answer'] = not item['is_cancelled'] and datetime.fromisoformat(item['starts_at']) > now
    return dict(id=poll['id'], kind=poll['kind'], period_start=poll['period_start'], period_end=poll['period_end'],
                participant_id=poll['participant_id'], generation=poll['generation'], page=page, pages=pages,
                entries=answers[page*6:(page+1)*6], total=len(answers))


def _claim(now):
    now = _now(now)
    with _transaction() as conn:
        polls = conn.execute("SELECT pp.*, p.telegram_id, p.is_active, p.is_registered FROM planned_polls pp "
                             "JOIN participants p ON p.id=pp.participant_id WHERE pp.status IN ('pending','sending') "
                             'AND (pp.next_attempt_at IS NULL OR datetime(pp.next_attempt_at)<=datetime(?)) '
                             'AND (pp.lease_until IS NULL OR datetime(pp.lease_until)<=datetime(?)) '
                             'ORDER BY pp.due_at,pp.kind,pp.id', (now.isoformat(), now.isoformat())).fetchall()
        for poll in polls:
            _refresh_poll(conn, poll, now)
            future = conn.execute('SELECT 1 FROM planned_answers WHERE poll_id=? AND is_cancelled=0 '
                                  'AND datetime(starts_at)>datetime(?) LIMIT 1', (poll['id'], now.isoformat())).fetchone()
            if not future or not poll['is_active'] or not poll['is_registered']:
                conn.execute("UPDATE planned_polls SET status='cancelled',lease_until=NULL WHERE id=?", (poll['id'],))
                continue
            conn.execute("UPDATE planned_polls SET status='sending',attempts=attempts+1,lease_until=? WHERE id=?",
                         ((now+timedelta(minutes=3)).isoformat(), poll['id']))
            return {'poll': dict(poll), 'card': _card(conn, poll, now)}
    return None


def _delivered(poll_id, message_id, now):
    with _transaction() as conn:
        conn.execute("UPDATE planned_polls SET status='sent',message_id=?,sent_at=?,lease_until=NULL,next_attempt_at=NULL WHERE id=?",
                     (message_id, _now(now).isoformat(), poll_id))


def _retry(poll_id, now, delay, forbidden=False):
    with _transaction() as conn:
        conn.execute('UPDATE planned_polls SET status=?,lease_until=NULL,next_attempt_at=? WHERE id=?',
                     ('cancelled' if forbidden else 'pending', (_now(now)+timedelta(seconds=delay)).isoformat(), poll_id))
        if forbidden:
            conn.execute('UPDATE participants SET is_active=0 WHERE id=(SELECT participant_id FROM planned_polls WHERE id=?)', (poll_id,))


async def deliver_due(bot, now=None):
    """Called under scheduler.delivery_lock. A bounded batch preserves main-poll priority."""
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    from handlers_planning import render_card
    await asyncio.to_thread(_prepare, now)
    sent = failed = 0
    for _ in range(10):
        item = await asyncio.to_thread(_claim, now)
        if not item:
            break
        poll = item['poll']
        text, markup = render_card(item['card'])
        try:
            message = await bot.send_message(poll['telegram_id'], text, reply_markup=markup, request_timeout=20)
            await asyncio.to_thread(_delivered, poll['id'], message.message_id, now)
            sent += 1
        except TelegramForbiddenError:
            await asyncio.to_thread(_retry, poll['id'], now, 0, True)
            failed += 1
        except TelegramRetryAfter as exc:
            await asyncio.to_thread(_retry, poll['id'], now, max(1, exc.retry_after))
            failed += 1
            break
        except Exception as exc:
            await asyncio.to_thread(_retry, poll['id'], now, 30)
            log.warning('Planning delivery %s failed: %s', poll['id'], type(exc).__name__)
            failed += 1
        await asyncio.sleep(0.05)
    return {'sent': sent, 'failed': failed}


def _owned_poll(conn, poll_id, telegram_id, message_id):
    return conn.execute("SELECT pp.* FROM planned_polls pp JOIN participants p ON p.id=pp.participant_id "
                        "WHERE pp.id=? AND p.telegram_id=? AND pp.message_id=? AND pp.status='sent'",
                        (poll_id, telegram_id, message_id)).fetchone()


def _get_card(poll_id, telegram_id, message_id, page, now):
    now = _now(now)
    with _transaction() as conn:
        poll = _owned_poll(conn, poll_id, telegram_id, message_id)
        if not poll:
            return None
        _refresh_poll(conn, poll, now)
        return _card(conn, poll, now, page)


async def get_card(poll_id, telegram_id, message_id, page=0, now=None):
    return await asyncio.to_thread(_get_card, poll_id, telegram_id, message_id, page, now)


def _effective(conn, participant_id, key):
    main = conn.execute("SELECT status FROM responses WHERE participant_id=? AND datetime(starts_at)=datetime(?) "
                        "AND is_cancelled=0 AND status IN ('yes','no') ORDER BY responded_at DESC,id DESC LIMIT 1",
                        (participant_id, key)).fetchone()
    if main:
        return 'main', main['status']
    row = conn.execute("SELECT pp.kind,pa.status FROM planned_answers pa JOIN planned_polls pp ON pp.id=pa.poll_id "
                       "WHERE pp.participant_id=? AND pa.occurrence_key=? AND pa.is_cancelled=0 AND pa.status IN ('yes','no') "
                       "ORDER BY CASE pp.kind WHEN 'week' THEN 0 ELSE 1 END,pa.responded_at DESC,pa.id DESC LIMIT 1",
                       (participant_id, key)).fetchone()
    return (row['kind'], row['status']) if row else (None, 'pending')


def _log_answer(conn, participant_id, key, kind, old_status, new_status, before, callback_id, now):
    if old_status != new_status:
        conn.execute('INSERT INTO answer_change_log(participant_id,occurrence_key,log_type,from_stage,to_stage,old_status,new_status,changed_at,callback_id) '
                     "VALUES (?,?,'stage',?,?,?,?,?,?)", (participant_id, key, kind, kind, old_status, new_status, now.isoformat(), callback_id))
    after = _effective(conn, participant_id, key)
    if before != after:
        conn.execute('INSERT INTO answer_change_log(participant_id,occurrence_key,log_type,from_stage,to_stage,old_status,new_status,changed_at,callback_id) '
                     "VALUES (?,?,'effective',?,?,?,?,?,?)", (participant_id, key, before[0], after[0], before[1], after[1], now.isoformat(), callback_id))


def _log_system_effective(conn, participant_id, key, before, now, reason):
    """Record a reset as pending, so later 24-hour baselines do not reuse old Y/N."""
    latest = conn.execute("SELECT * FROM answer_change_log WHERE participant_id=? AND occurrence_key=? "
                          "AND log_type='effective' ORDER BY id DESC LIMIT 1", (participant_id, key)).fetchone()
    if latest:
        # Main cancellation may already have happened in the parent transaction;
        # its last published effective value is then only available in the ledger.
        before = (latest['to_stage'], latest['new_status'])
    after = _effective(conn, participant_id, key)
    if before == after:
        return
    callback_id = f"system:{reason}:{participant_id}:{key}:{latest['id'] if latest else 0}"
    conn.execute('INSERT INTO answer_change_log(participant_id,occurrence_key,log_type,from_stage,to_stage,old_status,new_status,changed_at,callback_id) '
                 "VALUES (?,?,'effective',?,?,?,?,?,?)", (participant_id, key, before[0], after[0], before[1], after[1], now.isoformat(), callback_id))


def _record_main(response_id, telegram_id, message_id, answer, callback_id, now):
    if answer not in ('yes', 'no'):
        return None
    now = _now(now)
    with _transaction() as conn:
        row = conn.execute('SELECT r.* FROM responses r JOIN participants p ON p.id=r.participant_id '
                           'WHERE r.id=? AND p.telegram_id=? AND r.message_id=? AND r.is_cancelled=0',
                           (response_id, telegram_id, message_id)).fetchone()
        if not row:
            return None
        slot = conn.execute('SELECT * FROM schedule WHERE id=?', (row['schedule_id'],)).fetchone()
        start = datetime.fromisoformat(row['starts_at'])
        if start > now and not events.matches(slot, start):
            return None
        if conn.execute('SELECT 1 FROM planning_callback_receipts WHERE callback_id=?', (callback_id,)).fetchone():
            return dict(row, changed=False)
        key = occurrence_key(start)
        before = _effective(conn, row['participant_id'], key)
        changed = row['status'] != answer
        if changed:
            conn.execute('UPDATE responses SET status=?,responded_at=? WHERE id=?', (answer, now.isoformat(), response_id))
            _log_answer(conn, row['participant_id'], key, 'main', row['status'], answer, before, callback_id, now)
        conn.execute('INSERT INTO planning_callback_receipts VALUES (?,?)', (callback_id, now.isoformat()))
        current = conn.execute('SELECT * FROM responses WHERE id=?', (response_id,)).fetchone()
        return dict(current, changed=changed)


async def record_main_answer(response_id, telegram_id, message_id, answer, callback_id, now=None):
    """Update the main answer and its stage/effective history atomically, even after start."""
    return await asyncio.to_thread(_record_main, response_id, telegram_id, message_id, answer, callback_id, now)


def _answer_planned(answer_id, generation, telegram_id, message_id, answer, callback_id, now):
    if answer not in ('yes', 'no'):
        return None
    now = _now(now)
    with _transaction() as conn:
        row = conn.execute('SELECT * FROM planned_answers WHERE id=?', (answer_id,)).fetchone()
        if not row:
            return None
        poll = _owned_poll(conn, row['poll_id'], telegram_id, message_id)
        if not poll:
            return None
        _refresh_poll(conn, poll, now)
        row = conn.execute('SELECT * FROM planned_answers WHERE id=?', (answer_id,)).fetchone()
        if row['is_cancelled'] or row['generation'] != generation or datetime.fromisoformat(row['starts_at']) <= now:
            return None
        if conn.execute('SELECT 1 FROM planning_callback_receipts WHERE callback_id=?', (callback_id,)).fetchone():
            return {'poll_id': poll['id'], 'changed': False}
        before = _effective(conn, poll['participant_id'], row['occurrence_key'])
        changed = row['status'] != answer
        if changed:
            conn.execute('UPDATE planned_answers SET status=?,responded_at=? WHERE id=?', (answer, now.isoformat(), answer_id))
            _log_answer(conn, poll['participant_id'], row['occurrence_key'], poll['kind'], row['status'], answer, before, callback_id, now)
        conn.execute('INSERT INTO planning_callback_receipts VALUES (?,?)', (callback_id, now.isoformat()))
        return {'poll_id': poll['id'], 'changed': changed}


async def answer_planned(answer_id, generation, telegram_id, message_id, answer, callback_id, now=None):
    return await asyncio.to_thread(_answer_planned, answer_id, generation, telegram_id, message_id, answer, callback_id, now)


def _snapshot(now):
    now = _now(now)
    conn = _connection()
    try:
        conn.execute('BEGIN')  # Consistent read; no use of the shared async connection.
        enabled = conn.execute("SELECT value FROM planning_meta WHERE key='enabled_at'").fetchone()[0]
        people = [dict(row) for row in conn.execute('SELECT * FROM participants ORDER BY id')]
        slots = _slots(conn)
        by_slot = {slot['id']: slot for slot in slots}
        first = now.date().replace(day=1)
        end = _next_month(_next_month(first))-timedelta(days=1)
        recent = now-timedelta(days=30)
        sessions = _sessions(slots, first, end)
        selected_answers = {}
        planned = conn.execute('SELECT pa.*,pp.participant_id,pp.kind,pp.period_start FROM planned_answers pa '
                               'JOIN planned_polls pp ON pp.id=pa.poll_id WHERE datetime(pa.starts_at)>=datetime(?)',
                               (recent.isoformat(),)).fetchall()
        main = conn.execute('SELECT * FROM responses WHERE datetime(starts_at)>=datetime(?)', (recent.isoformat(),)).fetchall()
        active_by_day = {}
        for row, kind in [(row, row['kind']) for row in planned] + [(row, 'main') for row in main]:
            start = datetime.fromisoformat(row['starts_at'])
            key = occurrence_key(start)
            slot = by_slot.get(row['schedule_id'])
            cancelled = bool(row['is_cancelled'])
            if start.date() not in active_by_day:
                active_by_day[start.date()] = _sessions(slots, start.date(), start.date())
            active_session = active_by_day[start.date()].get(key)
            if start > now and not active_session:
                cancelled = True
            session = active_session or {'key': key, 'starts_at': start.isoformat(),
                                         'end_time': row['end_time'] if kind != 'main' else (events.end_time(slot, start) if slot else None),
                                         'schedule_id': row['schedule_id'], 'cancelled': cancelled}
            # A cancelled old forecast must not hide a restored occurrence's
            # valid main answer, including after that date leaves this month.
            previous = sessions.get(key)
            if previous is None or (previous['cancelled'] and not session['cancelled']):
                sessions[key] = session
            answer = {'participant_id': row['participant_id'], 'key': key, 'kind': kind,
                      'status': row['status'], 'responded_at': row['responded_at'],
                      'period_start': row['period_start'] if kind != 'main' else None,
                      'cancelled': cancelled}
            identity = (row['participant_id'], key, kind)
            rank = (not cancelled, row['status'] in ('yes', 'no'), row['responded_at'] or '', row['id'])
            if identity not in selected_answers or rank > selected_answers[identity][0]:
                selected_answers[identity] = (rank, answer)
        changes = [dict(id=row['id'], participant_id=row['participant_id'], key=row['occurrence_key'],
                        from_stage=row['from_stage'], to_stage=row['to_stage'], old_status=row['old_status'],
                        new_status=row['new_status'], changed_at=row['changed_at'])
                   for row in conn.execute("SELECT * FROM answer_change_log WHERE log_type='effective' "
                                           'AND datetime(occurrence_key)>=datetime(?) ORDER BY changed_at,id', (recent.isoformat(),))]
        return {'participants': people, 'sessions': [sessions[key] for key in sorted(sessions)],
                'answers': [selected_answers[key][1] for key in sorted(selected_answers)],
                'changes': changes, 'generated_at': now.isoformat(), 'enabled_at': enabled}
    finally:
        conn.rollback()
        conn.close()


async def snapshot(now=None):
    return await asyncio.to_thread(_snapshot, now)
