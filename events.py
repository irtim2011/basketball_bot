"""Durable per-session delivery/response records; legacy history is retained."""
from datetime import datetime, timedelta
import db
import utils

def matches(slot, start):
    if not slot or slot['time'] != start.strftime('%H:%M'):
        return False
    if slot['starts_on'] and start.date().isoformat() < slot['starts_on']:
        return False
    return (slot['training_date'] == start.date().isoformat() if slot['training_date']
            else slot['weekday'] == start.weekday())

async def queue_manual(slot_id, start, trainer_id):
    await db._c().execute(
        "INSERT INTO manual_polls(schedule_id,starts_at,trainer_id,requested_at) VALUES (?,?,?,?) "
        "ON CONFLICT(schedule_id,starts_at) DO UPDATE SET status='pending', trainer_id=excluded.trainer_id, requested_at=excluded.requested_at",
        (slot_id,start.isoformat(),trainer_id,utils.now().isoformat()))
    await db._c().commit()

async def save_slot(weekday, time_str, training_date=None, slot_id=None, starts_on=None):
    if weekday not in range(7) or utils.parse_time_str(time_str) != time_str:
        raise ValueError("Некорректное время тренировки")
    if training_date:
        datetime.fromisoformat(training_date)
    conn = db._c()
    duplicate = await (await conn.execute(
        "SELECT id FROM schedule WHERE is_active=1 AND weekday=? AND time=? "
        "AND training_date IS ? AND id != ?", (weekday, time_str, training_date, slot_id or -1)
    )).fetchone()
    if duplicate:
        raise ValueError("Такая тренировка уже есть")
    if slot_id:
        cur = await conn.execute(
            "UPDATE schedule SET weekday=?, time=?, training_date=?, starts_on=? WHERE id=? AND is_active=1",
            (weekday, time_str, training_date, starts_on, slot_id))
        if not cur.rowcount:
            raise ValueError("Тренировка уже удалена")
    else:
        cur = await conn.execute(
            "INSERT INTO schedule (weekday,time,training_date,starts_on,is_active) VALUES (?,?,?,?,1)",
            (weekday, time_str, training_date, starts_on))
        slot_id = cur.lastrowid
    await conn.commit()
    return slot_id

async def get_slot(slot_id):
    return await (await db._c().execute(
        "SELECT * FROM schedule WHERE id=? AND is_active=1", (slot_id,))).fetchone()

async def delete_slot(slot_id):
    await db._c().execute("UPDATE schedule SET is_active=0 WHERE id=?", (slot_id,))
    await db._c().commit()

def occurrences(slot, now, days=14):
    if slot["training_date"]:
        dates = [datetime.fromisoformat(slot["training_date"]).date()]
    else:
        dates = [now.date() + timedelta(days=i) for i in range(days + 1)
                 if (now.date() + timedelta(days=i)).weekday() == slot["weekday"]]
    for day in dates:
        if slot['starts_on'] and day.isoformat() < slot['starts_on']:
            continue
        naive = datetime.fromisoformat(f"{day.isoformat()}T{slot['time']}")
        try:
            start = utils.TZ.localize(naive, is_dst=None)
        except Exception:
            continue  # Nonexistent or ambiguous DST occurrence.
        if start > now:
            yield start

async def response_for(participant_id, slot_id, start):
    conn = db._c()
    await conn.execute(
        "INSERT OR IGNORE INTO responses(participant_id,schedule_id,starts_at) VALUES (?,?,?)",
        (participant_id, slot_id, start.isoformat()))
    await conn.commit()
    return await (await conn.execute(
        "SELECT * FROM responses WHERE participant_id=? AND schedule_id=? AND starts_at=?",
        (participant_id, slot_id, start.isoformat()))).fetchone()

async def summary(limit_dates=None):
    records = await (await db._c().execute(
        "SELECT participant_id, training_date AS day, status FROM attendance "
        "UNION ALL SELECT participant_id, substr(starts_at,1,10), status FROM responses"
    )).fetchall()
    first = datetime(2026, 8, 1).date()
    last = max([utils.today(), first] + [datetime.fromisoformat(r['day']).date() for r in records])
    dates = [(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)]
    if limit_dates:
        dates = dates[-limit_dates:]
    marks = {}
    for record in records:
        key = (record['participant_id'], record['day'])
        if record['status'] == 'yes':
            marks[key] = 'Y'
        elif record['status'] == 'no' and marks.get(key) != 'Y':
            marks[key] = 'N'
    rows = [{"internal_id": p['id'], "participant_id": str(p['public_id']), "telegram_id": p['telegram_id'],
             "full_name": p['full_name'] or '', "username": p['username'] or '',
             "phone": p['phone'] or '',
             "marks": {d: marks.get((p['id'], d), '') for d in dates}}
            for p in await db.list_participants() if p['is_registered']]
    return dates, rows
