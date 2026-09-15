"""Confirmed soft clearing of future schedules; attendance history is untouched."""
import asyncio
import hashlib
import json

import events
from schedule_import import _connection, _now, _snapshot, _start

STALE = 'Расписание изменилось или тренировка уже началась. Откройте очистку расписания заново.'


def _plan(slots, now):
    ids, next_starts = [], []
    recurring = one_off = 0
    for slot in slots:
        if not slot['is_active']:
            continue
        if slot['training_date']:
            start = _start({'date': slot['training_date'], 'time': slot['time']})
            if start <= now:
                continue
            one_off += 1
        else:
            recurring += 1
            start = next(events.occurrences(slot, now), None)
        ids.append(slot['id'])
        next_starts.append([slot['id'], start.isoformat() if start is not None else None])
    # Include the next occurrence so a weekly session starting while the
    # confirmation is open also requires a fresh preview.
    payload = json.dumps({'schedule': slots, 'ids': ids, 'next_starts': next_starts},
                         sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return {'ids': ids, 'fingerprint': hashlib.sha256(payload.encode('utf-8')).hexdigest(),
            'recurring': recurring, 'one_off': one_off, 'total': len(ids)}


def _build(now):
    conn = _connection()
    try:
        return _plan(_snapshot(conn), _now(now))
    finally:
        conn.close()


async def build_clear_plan(now=None):
    return await asyncio.to_thread(_build, now)


def _apply(plan, supplied_now):
    conn = _connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        now = _now(supplied_now)
        snapshot = _snapshot(conn)
        current = _plan(snapshot, now)
        if (not isinstance(plan, dict) or current['fingerprint'] != plan.get('fingerprint')
                or current['ids'] != plan.get('ids')):
            raise ValueError(STALE)
        result = {'cleared': 0, 'cancelled_responses': 0}
        for slot_id in current['ids']:
            changed = conn.execute('UPDATE schedule SET is_active=0 WHERE id=? AND is_active=1',
                                   (slot_id,)).rowcount
            if changed != 1:
                raise ValueError(STALE)
            result['cleared'] += changed
            result['cancelled_responses'] += conn.execute(
                'UPDATE responses SET is_cancelled=1 WHERE schedule_id=? AND is_cancelled=0 '
                'AND datetime(starts_at)>datetime(?)', (slot_id, now.isoformat())).rowcount
            conn.execute("UPDATE manual_polls SET status='cancelled' WHERE schedule_id=? "
                         "AND status='pending' AND datetime(starts_at)>datetime(?)",
                         (slot_id, now.isoformat()))
        if _plan(snapshot, _now(supplied_now))['fingerprint'] != current['fingerprint']:
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


async def apply_clear_plan(plan, now=None):
    return await asyncio.to_thread(_apply, plan, now)
