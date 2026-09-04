import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import texts
import utils
from configure import parse_admins, updated_text, save
import test_bot

class PresentationTests(unittest.TestCase):
    def test_clear_russian_date_and_weekly_first_occurrence(self):
        start=utils.TZ.localize(datetime(2026,9,4,4,15))
        slot={'training_date':'2026-09-04','weekday':4,'time':'04:15','starts_on':'2026-09-04'}
        result=texts.schedule_text(slot,start)
        self.assertIn('Пятница, 4 сентября 2026',result)
        self.assertIn('Начало в 04:15 по Москве',result)
        self.assertIn('Разовая',result)
        self.assertIsNone(texts.next_start(slot,start+timedelta(seconds=1)))
        slot.update(training_date=None,starts_on='2026-11-06')
        self.assertEqual(texts.next_start(slot,start).date().isoformat(),'2026-11-06')

    def test_basketball_copy_and_three_hours(self):
        start=utils.TZ.localize(datetime(2026,9,10,14,30))
        self.assertIn('🏀',texts.poll_text(start))
        self.assertIn('Ты в составе',texts.poll_text(start,'yes'))
        self.assertIn('не придёшь',texts.poll_text(start,'no'))
        with patch('texts.POLL_OFFSET_MINUTES',180):
            self.assertEqual(texts.reminder_label(),'за 3 часа')

    def test_two_admins_and_atomic_settings_keep_database_path(self):
        self.assertEqual(parse_admins('440415724, 54545234'),'440415724,54545234')
        for value in ['440415724','1,1','1,-2','1,2,3']:
            with self.assertRaises(ValueError): parse_admins(value)
        original='# keep\nBOT_TOKEN=old\nDB_PATH=/safe/data.db\nPOLL_OFFSET_MINUTES=60\n'
        changed=updated_text(original,{'BOT_TOKEN':'new','POLL_OFFSET_MINUTES':'180','TRAINER_IDS':'1,2'})
        self.assertIn('DB_PATH=/safe/data.db',changed)
        self.assertEqual(changed.count('BOT_TOKEN='),1)
        self.assertNotIn('=60',changed)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bot.env'
            save(path,changed)
            self.assertEqual(path.read_text(),changed)

class ReminderTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_bot.DataTests.asyncSetUp
    asyncTearDown = test_bot.DataTests.asyncTearDown
    slot = test_bot.DataTests.slot
    async def test_exact_three_hour_boundary(self):
        from scheduler import tick
        await self.slot('21:00')
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=12)))
        with patch('scheduler.POLL_OFFSET_MINUTES',180):
            self.mock_now.return_value=self.now-timedelta(seconds=1)
            await tick(bot)
            bot.send_message.assert_not_awaited()
            self.mock_now.return_value=self.now
            await tick(bot)
            bot.send_message.assert_awaited_once()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_database_gets_four_digit_public_ids(self):
        import db
        old_path = db.DB_PATH
        folder = tempfile.TemporaryDirectory()
        path = Path(folder.name) / 'old.db'
        connection = sqlite3.connect(path)
        connection.executescript('''
            CREATE TABLE participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE, username TEXT, full_name TEXT, phone TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_registered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, registered_at TEXT
            );
            INSERT INTO participants
                (telegram_id,username,full_name,phone,is_active,is_registered,created_at)
            VALUES (77,'old','Old User','1234567',1,1,'2026-01-01');
        ''')
        connection.commit()
        connection.close()
        db.DB_PATH = str(path)
        try:
            await db.init_db()
            participant = await db.get_participant_by_telegram_id(77)
            self.assertEqual(participant['public_id'], 1001)
        finally:
            await db.close_db()
            db._conn = None
            db.DB_PATH = old_path
            folder.cleanup()
