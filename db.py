"""
Async data layer built on aiosqlite.

Tables
------
participants: the roster. `is_active` = currently receives weekly polls
              (this is what the trainer toggles). `is_registered` = the
              person has completed the /start flow (ФИО + телефон + telegram).
schedule:     weekly recurring training slots (weekday + time).
attendance:   one row per (participant, training_date). status is
              'pending' | 'yes' | 'no'. This is the "technical" table with
              extra bookkeeping columns (poll_message_id, timestamps) that
              never appear in the trainer-facing summary table.
"""

import asyncio
import aiosqlite
from datetime import datetime
from typing import Optional

from config import DB_PATH
import utils

_conn: Optional[aiosqlite.Connection] = None
_participant_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id INTEGER UNIQUE CHECK(public_id BETWEEN 1000 AND 9999),
    telegram_id INTEGER UNIQUE,
    username TEXT,              -- without '@', lowercase
    full_name TEXT,
    phone TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_registered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    registered_at TEXT
);

CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL,   -- 0=Monday .. 6=Sunday
    time TEXT NOT NULL,         -- 'HH:MM', training start time
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    training_date TEXT NOT NULL,     -- 'YYYY-MM-DD'
    status TEXT NOT NULL DEFAULT 'pending',
    poll_message_id INTEGER,
    poll_sent_at TEXT,
    responded_at TEXT,
    UNIQUE(participant_id, training_date)
);

CREATE TABLE IF NOT EXISTS legacy_identities (
    public_id INTEGER PRIMARY KEY CHECK(public_id BETWEEN 1000 AND 9999),
    canonical_name TEXT NOT NULL,
    match_tokens TEXT NOT NULL,
    quality TEXT NOT NULL DEFAULT 'готово',
    updated_at TEXT NOT NULL
);
"""


async def init_db():
    global _conn
    _conn = await aiosqlite.connect(DB_PATH)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(SCHEMA)
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute("PRAGMA busy_timeout=5000")
    participant_columns = {r[1] for r in await (await _conn.execute("PRAGMA table_info(participants)")).fetchall()}
    if "public_id" not in participant_columns:
        await _conn.execute("ALTER TABLE participants ADD COLUMN public_id INTEGER")
    await _conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS participants_public_id_uq ON participants(public_id)"
    )
    for row in await (await _conn.execute(
        "SELECT id FROM participants WHERE public_id IS NULL ORDER BY id"
    )).fetchall():
        preferred = 1000 + row["id"] if row["id"] <= 8999 else None
        await _assign_public_id(row["id"], preferred)

    columns = {r[1] for r in await (await _conn.execute("PRAGMA table_info(schedule)")).fetchall()}
    if "training_date" not in columns:
        await _conn.execute("ALTER TABLE schedule ADD COLUMN training_date TEXT")
    if "starts_on" not in columns:
        await _conn.execute("ALTER TABLE schedule ADD COLUMN starts_on TEXT")
    await _conn.executescript("""
        CREATE TABLE IF NOT EXISTS manual_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER NOT NULL,
            starts_at TEXT NOT NULL,
            trainer_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            UNIQUE(schedule_id, starts_at)
        );
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id INTEGER NOT NULL REFERENCES participants(id),
            schedule_id INTEGER NOT NULL,
            starts_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','yes','no')),
            message_id INTEGER,
            responded_at TEXT,
            UNIQUE(participant_id, schedule_id, starts_at)
        );
    """)
    await _conn.commit()


async def close_db():
    if _conn:
        await _conn.close()


def _c() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("DB is not initialized - call init_db() first")
    return _conn


# ------------------------------------------------------------------ #
# Participants
# ------------------------------------------------------------------ #

async def get_participant_by_telegram_id(telegram_id: int) -> Optional[aiosqlite.Row]:
    cur = await _c().execute(
        "SELECT * FROM participants WHERE telegram_id = ?", (telegram_id,)
    )
    return await cur.fetchone()


async def get_participant_by_username(username: str) -> Optional[aiosqlite.Row]:
    cur = await _c().execute(
        "SELECT * FROM participants WHERE username = ? AND telegram_id IS NULL",
        (username,),
    )
    return await cur.fetchone()


async def get_participant(participant_id: int) -> Optional[aiosqlite.Row]:
    cur = await _c().execute(
        "SELECT * FROM participants WHERE id = ?", (participant_id,)
    )
    return await cur.fetchone()


async def get_participant_by_public_id(public_id: int) -> Optional[aiosqlite.Row]:
    return await (await _c().execute(
        "SELECT * FROM participants WHERE public_id=?", (public_id,)
    )).fetchone()


async def upsert_legacy_identities(identities):
    now = utils.now().isoformat()
    for public_id, canonical_name, quality in identities:
        tokens = "|".join(utils.fio_match_tokens(canonical_name))
        if tokens:
            await _c().execute(
                "INSERT INTO legacy_identities(public_id,canonical_name,match_tokens,quality,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(public_id) DO UPDATE SET "
                "canonical_name=excluded.canonical_name,match_tokens=excluded.match_tokens,"
                "quality=excluded.quality,updated_at=excluded.updated_at",
                (int(public_id), canonical_name, tokens, quality, now),
            )
    await _c().commit()


async def find_legacy_identity(full_name: str, participant_id=None):
    rows = await (await _c().execute(
        "SELECT l.*, p.id AS claimed_by FROM legacy_identities l "
        "LEFT JOIN participants p ON p.public_id=l.public_id WHERE l.quality='готово'"
    )).fetchall()
    match = utils.unique_legacy_match(full_name, rows)
    if match is not None and match['claimed_by'] in (None, participant_id):
        return match
    return None


async def _assign_public_id(participant_id: int, preferred: int | None = None) -> int:
    current = await (await _c().execute(
        "SELECT public_id FROM participants WHERE id=?", (participant_id,)
    )).fetchone()
    if not current:
        raise ValueError("Участник не найден")
    if current["public_id"] is not None:
        return current["public_id"]
    candidate = None
    if preferred is not None and 1000 <= preferred <= 9999:
        taken = await (await _c().execute(
            "SELECT 1 FROM participants WHERE public_id=?", (preferred,)
        )).fetchone()
        if not taken:
            candidate = preferred
    if candidate is None:
        row = await (await _c().execute("""
            WITH RECURSIVE codes(value) AS (
                SELECT 1001 UNION ALL SELECT value + 1 FROM codes WHERE value < 9999
            )
            SELECT value FROM codes
            WHERE NOT EXISTS (SELECT 1 FROM participants WHERE public_id=value)
              AND NOT EXISTS (SELECT 1 FROM legacy_identities WHERE public_id=value)
            ORDER BY value LIMIT 1
        """)).fetchone()
        if not row:
            raise RuntimeError("Закончились свободные четырёхзначные ID участников")
        candidate = row["value"]
    await _c().execute(
        "UPDATE participants SET public_id=? WHERE id=?", (candidate, participant_id)
    )
    return candidate


async def adopt_public_id(participant_id: int, public_id: int) -> bool:
    """Use an existing four-digit Sheet ID after Telegram ID reconciliation."""
    if not 1000 <= public_id <= 9999:
        return False
    async with _participant_lock:
        conflict = await (await _c().execute(
            "SELECT id FROM participants WHERE public_id=? AND id<>?", (public_id, participant_id)
        )).fetchone()
        if conflict:
            return False
        cur = await _c().execute(
            "UPDATE participants SET public_id=? WHERE id=?", (public_id, participant_id)
        )
        await _c().commit()
        return bool(cur.rowcount)


async def create_stub_participant(username: str, full_name: str | None) -> int:
    """Trainer pre-adds someone by @username before they've ever opened the bot."""
    now = utils.now().isoformat()
    async with _participant_lock:
        cur = await _c().execute(
            "INSERT INTO participants (telegram_id, username, full_name, phone, "
            "is_active, is_registered, created_at) VALUES (NULL, ?, ?, NULL, 1, 0, ?)",
            (username, full_name, now),
        )
        await _assign_public_id(cur.lastrowid)
        await _c().commit()
        return cur.lastrowid


async def add_participant_by_id(telegram_id: int):
    if not 0 < telegram_id < 2**52:
        raise ValueError('Некорректный Telegram ID')
    async with _participant_lock:
        await _c().execute(
            "INSERT INTO participants (telegram_id,is_active,is_registered,created_at) VALUES (?,1,0,?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET is_active=1", (telegram_id, utils.now().isoformat()))
        row = await (await _c().execute(
            "SELECT id FROM participants WHERE telegram_id=?", (telegram_id,)
        )).fetchone()
        await _assign_public_id(row["id"])
        await _c().commit()


async def register_participant(
    telegram_id: int, username: str | None, full_name: str, phone: str,
    existing_id: int | None = None, preferred_public_id: int | None = None,
) -> int:
    now = utils.now().isoformat()
    async with _participant_lock:
        if preferred_public_id is None:
            legacy = await find_legacy_identity(full_name, existing_id)
            if legacy is not None:
                preferred_public_id = legacy['public_id']
        if existing_id is not None:
            if preferred_public_id is not None:
                conflict = await (await _c().execute(
                    "SELECT id FROM participants WHERE public_id=? AND id<>?",
                    (preferred_public_id, existing_id),
                )).fetchone()
                if not conflict:
                    await _c().execute(
                        "UPDATE participants SET public_id=? WHERE id=?",
                        (preferred_public_id, existing_id),
                    )
            await _c().execute(
                "UPDATE participants SET telegram_id=?, username=COALESCE(?, username), "
                "full_name=?, phone=?, is_registered=1, registered_at=? WHERE id=?",
                (telegram_id, username, full_name, phone, now, existing_id),
            )
            await _assign_public_id(existing_id)
            await _c().commit()
            return existing_id

        cur = await _c().execute(
            "INSERT INTO participants (telegram_id, username, full_name, phone, "
            "is_active, is_registered, created_at, registered_at) "
            "VALUES (?, ?, ?, ?, 0, 1, ?, ?)",
            (telegram_id, username, full_name, phone, now, now),
        )
        await _assign_public_id(cur.lastrowid, preferred_public_id)
        await _c().commit()
        return cur.lastrowid


async def set_active(participant_id: int, is_active: bool):
    await _c().execute(
        "UPDATE participants SET is_active=? WHERE id=?", (int(is_active), participant_id)
    )
    await _c().commit()


async def list_participants(active_only: bool = False):
    if active_only:
        cur = await _c().execute(
            "SELECT * FROM participants WHERE is_active=1 AND is_registered=1 "
            "ORDER BY full_name"
        )
    else:
        cur = await _c().execute("SELECT * FROM participants ORDER BY full_name")
    return await cur.fetchall()


async def get_active_registered_participants():
    cur = await _c().execute(
        "SELECT * FROM participants WHERE is_active=1 AND is_registered=1 "
        "AND telegram_id IS NOT NULL"
    )
    return await cur.fetchall()


# ------------------------------------------------------------------ #
# Schedule
# ------------------------------------------------------------------ #

async def add_schedule(weekday: int, time_str: str) -> int:
    cur = await _c().execute(
        "INSERT INTO schedule (weekday, time, is_active) VALUES (?, ?, 1)",
        (weekday, time_str),
    )
    await _c().commit()
    return cur.lastrowid


async def remove_schedule(schedule_id: int):
    await _c().execute("DELETE FROM schedule WHERE id=?", (schedule_id,))
    await _c().commit()


async def list_schedule(active_only: bool = True):
    if active_only:
        cur = await _c().execute(
            "SELECT * FROM schedule WHERE is_active=1 ORDER BY weekday, time"
        )
    else:
        cur = await _c().execute("SELECT * FROM schedule ORDER BY weekday, time")
    return await cur.fetchall()


# ------------------------------------------------------------------ #
# Attendance
# ------------------------------------------------------------------ #

async def get_or_create_attendance(participant_id: int, training_date: str) -> int:
    cur = await _c().execute(
        "SELECT id FROM attendance WHERE participant_id=? AND training_date=?",
        (participant_id, training_date),
    )
    row = await cur.fetchone()
    if row:
        return row["id"]
    now = utils.now().isoformat()
    cur = await _c().execute(
        "INSERT INTO attendance (participant_id, training_date, status, poll_sent_at) "
        "VALUES (?, ?, 'pending', ?)",
        (participant_id, training_date, now),
    )
    await _c().commit()
    return cur.lastrowid


async def set_poll_message(attendance_id: int, message_id: int):
    await _c().execute(
        "UPDATE attendance SET poll_message_id=? WHERE id=?", (message_id, attendance_id)
    )
    await _c().commit()


async def get_attendance(attendance_id: int) -> Optional[aiosqlite.Row]:
    cur = await _c().execute("SELECT * FROM attendance WHERE id=?", (attendance_id,))
    return await cur.fetchone()


async def update_attendance_status(attendance_id: int, status: str):
    now = utils.now().isoformat()
    await _c().execute(
        "UPDATE attendance SET status=?, responded_at=? WHERE id=?",
        (status, now, attendance_id),
    )
    await _c().commit()


async def get_summary_table(limit_dates: int = 12):
    """
    Returns (dates, rows) where:
      dates = sorted list of ISO date strings (most recent `limit_dates`)
      rows  = list of dicts: {full_name, username, phone, marks: {date: 'Y'/''}}
    Only includes participants who are registered (active or not, so history
    isn't lost when someone is removed from the mailing list).
    """
    cur = await _c().execute(
        "SELECT DISTINCT training_date FROM attendance ORDER BY training_date DESC LIMIT ?",
        (limit_dates,),
    )
    date_rows = await cur.fetchall()
    dates = sorted(r["training_date"] for r in date_rows)

    participants = await list_participants(active_only=False)
    participants = [p for p in participants if p["is_registered"]]

    rows = []
    for p in participants:
        cur = await _c().execute(
            "SELECT training_date, status FROM attendance WHERE participant_id=?",
            (p["id"],),
        )
        att_rows = await cur.fetchall()
        marks = {r["training_date"]: r["status"] for r in att_rows}
        rows.append({
            "full_name": p["full_name"] or "",
            "username": p["username"] or "",
            "phone": p["phone"] or "",
            "marks": {d: ("Y" if marks.get(d) == "yes" else "") for d in dates},
        })
    return dates, rows
