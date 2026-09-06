"""Regressions for stable participant identity and cancelled future sessions."""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import db
import events
import utils
from handlers_poll import process_answer


class BotAuditTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / 'audit.db')
        self.now = utils.TZ.localize(datetime(2026, 9, 7, 12))
        self.clock = patch('utils.now', return_value=self.now)
        self.mock_now = self.clock.start()
        await db.init_db()
        self.pid = await db.register_participant(77, None, 'Иванов Иван Иванович', '1234567')

    async def asyncTearDown(self):
        await db.close_db()
        db._conn = None
        db.DB_PATH = self.old_path
        self.clock.stop()
        self.tmp.cleanup()

    async def answered(self, sid, start, message_id=123):
        row = await events.response_for(self.pid, sid, start)
        await db._c().execute(
            "UPDATE responses SET status='yes', message_id=? WHERE id=?", (message_id, row['id']))
        await db._c().commit()
        return row['id']

    async def test_profile_correction_never_reassigns_registered_public_id(self):
        old = (await db.get_participant(self.pid))['public_id']
        await db.upsert_legacy_identities([(8006, 'Петров Пётр', 'готово')])
        for preferred in (None, 8006):
            await db.register_participant(77, 'new_username', 'Петров Пётр Петрович', '7654321',
                                          existing_id=self.pid, preferred_public_id=preferred)
            person = await db.get_participant(self.pid)
            self.assertEqual(person['public_id'], old)
            self.assertEqual(person['full_name'], 'Петров Пётр Петрович')
        self.assertIsNone(await db.get_participant_by_public_id(8006))

    async def test_invited_unregistered_person_can_still_adopt_legacy_identity(self):
        await db.upsert_legacy_identities([(8006, 'Петров Пётр', 'готово')])
        stub = await db.create_stub_participant('new_user', None)
        await db.register_participant(88, 'new_user', 'Петров Пётр Петрович', '7654321', existing_id=stub)
        self.assertEqual((await db.get_participant(stub))['public_id'], 8006)

    async def test_delete_cancels_future_answer_but_preserves_completed_history(self):
        sid = await events.save_slot(0, '18:00', starts_on='2026-08-01')
        future = self.now + timedelta(hours=6)
        past = future - timedelta(days=7)
        await self.answered(sid, past)
        await self.answered(sid, future)
        await events.delete_slot(sid)
        self.mock_now.return_value = self.now + timedelta(days=2)
        await db.close_db()
        await db.init_db()
        _, rows = await events.summary()
        self.assertEqual(rows[0]['marks'][past.date().isoformat()], 'Y')
        self.assertEqual(rows[0]['marks'][future.date().isoformat()], '')

    async def test_rescheduling_back_needs_new_answer_and_rejects_old_message(self):
        sid = await events.save_slot(0, '18:00', '2026-09-07')
        start = self.now + timedelta(hours=6)
        rid = await self.answered(sid, start)
        await events.save_slot(0, '19:00', '2026-09-07', sid)
        _, rows = await events.summary()
        self.assertEqual(rows[0]['marks']['2026-09-07'], '')
        await events.save_slot(0, '18:00', '2026-09-07', sid)
        response = await events.response_for(self.pid, sid, start)
        self.assertEqual(response['status'], 'pending')
        self.assertIsNone(response['message_id'])
        await db._c().execute('UPDATE responses SET message_id=456 WHERE id=?', (rid,))
        await db._c().commit()
        cb = SimpleNamespace(id='audit', data=f'r:{rid}:yes', answer=AsyncMock(),
                             from_user=SimpleNamespace(id=77),
                             message=SimpleNamespace(message_id=123, answer=AsyncMock(), edit_text=AsyncMock()))
        with patch('handlers_poll.google_sheet.queue'):
            await process_answer(cb)
            response = await events.response_for(self.pid, sid, start)
            self.assertEqual(response['status'], 'pending')
            cb.message.message_id = 456
            await process_answer(cb)
        response = await events.response_for(self.pid, sid, start)
        self.assertEqual(response['status'], 'yes')

    async def test_upgrade_reconciles_existing_cancelled_future_sessions(self):
        sid = await events.save_slot(0, '18:00', '2026-09-07')
        await self.answered(sid, self.now + timedelta(hours=6))
        # Simulate the old release deleting a slot without cancellation tracking.
        await db._c().execute('UPDATE schedule SET is_active=0 WHERE id=?', (sid,))
        await db._c().execute('ALTER TABLE responses DROP COLUMN is_cancelled')
        await db._c().commit()
        await db.close_db()
        await db.init_db()
        row = await (await db._c().execute('SELECT is_cancelled FROM responses')).fetchone()
        self.assertEqual(row['is_cancelled'], 1)
