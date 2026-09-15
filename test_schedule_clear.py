"""Clear-schedule scenarios on isolated temporary SQLite files only."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import db
import events
import schedule_clear as clear
import utils


def at(day, time='18:00'):
    return utils.TZ.localize(datetime.fromisoformat(f'{day}T{time}'))


class ScheduleClearTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name, 'clear.sqlite3')
        self.clock = at('2026-09-15', '12:00')
        self.path_patch = patch.object(db, 'DB_PATH', str(self.path))
        self.clock_patch = patch.object(utils, 'now', return_value=self.clock)
        self.path_patch.start()
        self.clock_patch.start()
        await db.init_db()

    async def asyncTearDown(self):
        await db.close_db()
        db._conn = None
        self.clock_patch.stop()
        self.path_patch.stop()
        self.folder.cleanup()

    async def rows(self, table):
        return [dict(row) for row in await (await db._c().execute(f'SELECT * FROM {table} ORDER BY rowid')).fetchall()]

    async def weekly(self):
        return await events.save_slot(3, '18:00', starts_on='2026-09-01')

    async def test_preview_selects_all_active_weeklies_and_only_future_once(self):
        weekly = await self.weekly()
        distant = await events.save_slot(1, '20:00', starts_on='2028-01-01')
        future = await events.save_slot(5, '18:00', training_date='2026-09-19')
        past = await events.save_slot(4, '18:00', training_date='2026-09-11')
        inactive = await events.save_slot(0, '18:00', training_date='2026-09-21')
        await events.delete_slot(inactive)
        before = await self.rows('schedule')
        plan = await clear.build_clear_plan()
        self.assertEqual(plan['ids'], [weekly, distant, future])
        self.assertEqual((plan['recurring'], plan['one_off'], plan['total']), (2, 1, 3))
        self.assertEqual(await self.rows('schedule'), before)
        result = await clear.apply_clear_plan(json.loads(json.dumps(plan)))
        self.assertEqual(result, {'cleared': 3, 'cancelled_responses': 0})
        after = await self.rows('schedule')
        self.assertEqual(len(after), len(before))
        self.assertEqual([row['id'] for row in after if row['is_active']], [past])
        for old, new in zip(before, after):
            expected = dict(old, is_active=0) if old['id'] in plan['ids'] else old
            self.assertEqual(new, expected)

    async def test_history_and_identity_tables_preserved_future_polls_closed(self):
        weekly = await self.weekly()
        past_once = await events.save_slot(3, '18:00', training_date='2026-09-10')
        person = await db.register_participant(12345, 'person', 'Иванов Иван Иванович', '+79991112233')
        await db.upsert_legacy_identities([(8001, 'Петров Пётр', 'готово')])
        attendance_id = await db.get_or_create_attendance(person, '2026-09-10')
        await db.update_attendance_status(attendance_id, 'yes')
        await db._c().execute('CREATE TABLE attendance_sheet_sync (id INTEGER PRIMARY KEY,payload TEXT)')
        await db._c().execute("INSERT INTO attendance_sheet_sync VALUES (1,'manual Y override')")
        for day in ['2026-09-10', '2026-09-17', '2026-09-24']:
            response = await events.response_for(person, weekly, at(day))
            await db._c().execute("UPDATE responses SET status='yes' WHERE id=?", (response['id'],))
            await events.queue_manual(weekly, at(day), 777)
        await db._c().execute("UPDATE manual_polls SET status='done' WHERE substr(starts_at,1,10)='2026-09-24'")
        await db._c().commit()
        tables = ['participants', 'attendance', 'legacy_identities', 'attendance_sheet_sync']
        preserved = {table: await self.rows(table) for table in tables}
        past_before = (await self.rows('responses'))[0]
        result = await clear.apply_clear_plan(await clear.build_clear_plan())
        self.assertEqual(result, {'cleared': 1, 'cancelled_responses': 2})
        for table in tables:
            self.assertEqual(await self.rows(table), preserved[table])
        responses = await self.rows('responses')
        self.assertEqual(responses[0], past_before)
        self.assertEqual([r['is_cancelled'] for r in responses], [0, 1, 1])
        self.assertEqual([r['status'] for r in responses], ['yes', 'yes', 'yes'])
        self.assertEqual([r['status'] for r in await self.rows('manual_polls')], ['pending', 'cancelled', 'done'])
        self.assertTrue((await events.get_slot(past_once))['is_active'])
        _, summary = await events.summary()
        self.assertEqual(summary[0]['marks']['2026-09-10'], 'Y')
        self.assertEqual(summary[0]['marks']['2026-09-17'], '')

    async def test_other_trainer_new_slot_invalidates_confirmation_without_clearing(self):
        weekly = await self.weekly()
        plan = await clear.build_clear_plan()
        second = await events.save_slot(5, '19:00', training_date='2026-09-19')
        with self.assertRaisesRegex(ValueError, 'заново'):
            await clear.apply_clear_plan(plan)
        self.assertEqual([r['id'] for r in await db.list_schedule()], [weekly, second])

    async def test_two_confirmations_and_retry_do_not_clear_twice(self):
        await self.weekly()
        plan = await clear.build_clear_plan()
        results = await asyncio.gather(clear.apply_clear_plan(plan), clear.apply_clear_plan(plan),
                                       return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) and r['cleared'] == 1 for r in results), 1)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        with self.assertRaises(ValueError):
            await clear.apply_clear_plan(plan)
        self.assertEqual(len(await self.rows('schedule')), 1)
        self.assertEqual(await db.list_schedule(), [])

    async def test_empty_future_schedule_is_a_repeatable_noop(self):
        await events.save_slot(3, '18:00', training_date='2026-09-10')
        before = await self.rows('schedule')
        plan = await clear.build_clear_plan()
        self.assertEqual((plan['ids'], plan['total']), ([], 0))
        for _ in range(2):
            self.assertEqual(await clear.apply_clear_plan(plan), {'cleared': 0, 'cancelled_responses': 0})
        self.assertEqual(await self.rows('schedule'), before)

    async def test_response_failure_rolls_back_soft_deactivation_and_all_updates(self):
        weekly = await self.weekly()
        person = await db.register_participant(12345, 'person', 'Иванов Иван Иванович', '+79991112233')
        await events.response_for(person, weekly, at('2026-09-17'))
        await events.queue_manual(weekly, at('2026-09-17'), 777)
        await db._c().execute("CREATE TRIGGER fail_clear BEFORE UPDATE OF is_cancelled ON responses "
                              "WHEN NEW.is_cancelled=1 BEGIN SELECT RAISE(ABORT,'rollback clear'); END")
        await db._c().commit()
        before = {table: await self.rows(table) for table in ['schedule', 'responses', 'manual_polls']}
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'rollback clear'):
            await clear.apply_clear_plan(await clear.build_clear_plan())
        for table, rows in before.items():
            self.assertEqual(await self.rows(table), rows)

    async def test_once_or_weekly_start_crossing_requires_new_preview(self):
        await self.weekly()
        await events.save_slot(3, '18:00', training_date='2026-09-17')
        before_time, after_time = at('2026-09-17', '17:59'), at('2026-09-17', '18:00')
        plan = await clear.build_clear_plan(now=before_time)
        with self.assertRaisesRegex(ValueError, 'началась'):
            await clear.apply_clear_plan(plan, now=after_time)
        with patch.object(utils, 'now', side_effect=[before_time, after_time]):
            with self.assertRaisesRegex(ValueError, 'началась'):
                await clear.apply_clear_plan(plan)
        self.assertEqual(len(await db.list_schedule()), 2)

    async def test_tampered_target_ids_cannot_clear_unconfirmed_slots(self):
        await self.weekly()
        plan = await clear.build_clear_plan()
        plan['ids'].append(999)
        with self.assertRaises(ValueError):
            await clear.apply_clear_plan(plan)
        self.assertEqual(len(await db.list_schedule()), 1)


if __name__ == '__main__':
    unittest.main()
