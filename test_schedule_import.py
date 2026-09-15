"""Real SQLite transactions for preview/apply, using only temporary databases."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db
import events
import schedule_import as importer
import utils


def at(day, time='18:00'):
    return utils.TZ.localize(datetime.fromisoformat(f'{day}T{time}'))


def entry(day, time='18:00', end=None):
    return {'date': day, 'time': time, 'end_time': end}


class ScheduleImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name, 'schedule.sqlite3')
        self.clock = at('2026-09-15', '12:00')
        self.path_patch = patch.object(db, 'DB_PATH', str(self.path))
        self.time_patch = patch.object(utils, 'now', return_value=self.clock)
        self.path_patch.start()
        self.time_patch.start()
        await db.init_db()

    async def asyncTearDown(self):
        await db.close_db()
        db._conn = None
        self.time_patch.stop()
        self.path_patch.stop()
        self.folder.cleanup()

    async def weekly(self):
        return await events.save_slot(3, '18:00', starts_on='2026-09-01')

    async def rows(self):
        return [dict(row) for row in await db.list_schedule(active_only=False)]

    async def test_cancel_september_17_preserves_weekly_september_24(self):
        slot_id = await self.weekly()
        plan = await importer.build_plan([entry('2026-09-17', '19:00', '20:00'), entry('2026-09-24')])
        self.assertEqual([(x['slot_id'], x['date'], x['time']) for x in plan['missing']],
                         [(slot_id, '2026-09-17', '18:00')])
        self.assertEqual(len(plan['additions']), 1)
        self.assertEqual(len(plan['existing']), 1)
        result = await importer.apply_plan(json.loads(json.dumps(plan)), cancel_missing=True)
        self.assertEqual((result['added'], result['cancelled']), (1, 1))
        slot = await events.get_slot(slot_id)
        self.assertTrue(slot['is_active'])
        self.assertEqual(json.loads(slot['excluded_dates']), ['2026-09-17'])
        self.assertFalse(events.matches(slot, at('2026-09-17')))
        self.assertTrue(events.matches(slot, at('2026-09-24')))
        self.assertEqual(next(events.occurrences(slot, self.clock)).date().isoformat(), '2026-09-24')
        new_slot = next(s for s in await self.rows() if s['id'] != slot_id)
        self.assertEqual(new_slot['end_time'], '20:00')

    async def test_additive_mode_never_cancels_missing_occurrences(self):
        slot_id = await self.weekly()
        plan = await importer.build_plan([entry('2026-09-17', '19:00'), entry('2026-09-24')])
        self.assertTrue(plan['missing'])
        result = await importer.apply_plan(plan, cancel_missing=False)
        self.assertEqual(result['cancelled'], 0)
        self.assertTrue(events.matches(await events.get_slot(slot_id), at('2026-09-17')))
        self.assertEqual(len(await db.list_schedule()), 2)

    async def test_no_duplicate_for_weekly_once_or_repeated_import(self):
        await self.weekly()
        await events.save_slot(5, '18:00', training_date='2026-09-19')
        entries = [entry('2026-09-17'), entry('2026-09-19'), entry('2026-09-20'), entry('2026-09-20')]
        plan = await importer.build_plan(entries)
        self.assertEqual((len(plan['entries']), len(plan['additions']), len(plan['existing'])), (3, 1, 2))
        await importer.apply_plan(plan)
        with self.assertRaisesRegex(ValueError, 'предпросмотр'):
            await importer.apply_plan(plan)
        fresh = await importer.build_plan(entries)
        result = await importer.apply_plan(fresh)
        self.assertEqual((result['added'], result['cancelled']), (0, 0))
        self.assertEqual(len(await db.list_schedule()), 3)

    async def test_two_trainers_same_snapshot_only_one_can_apply(self):
        first = await importer.build_plan([entry('2026-09-17')])
        second = await importer.build_plan([entry('2026-09-17')])
        results = await asyncio.gather(importer.apply_plan(first), importer.apply_plan(second), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) and r['added'] == 1 for r in results), 1)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        self.assertEqual(len(await db.list_schedule()), 1)

    async def test_ui_edit_and_expired_start_make_preview_stale(self):
        slot_id = await self.weekly()
        plan = await importer.build_plan([entry('2026-09-17')])
        await events.save_slot(3, '17:00', slot_id=slot_id, starts_on='2026-09-01')
        with self.assertRaisesRegex(ValueError, 'предпросмотр'):
            await importer.apply_plan(plan)
        fresh = await importer.build_plan([entry('2026-09-17', '17:00')])
        with self.assertRaisesRegex(ValueError, 'предпросмотр'):
            await importer.apply_plan(fresh, now=at('2026-09-17', '17:00'))
        self.assertEqual((await events.get_slot(slot_id))['time'], '17:00')

    async def test_start_crossing_during_apply_rolls_back_whole_import(self):
        before = at('2026-09-17', '17:59')
        after = at('2026-09-17', '18:00')
        plan = await importer.build_plan([entry('2026-09-17')], now=before)
        with patch.object(utils, 'now', side_effect=[before, after]):
            with self.assertRaisesRegex(ValueError, 'предпросмотр'):
                await importer.apply_plan(plan)
        self.assertEqual(await self.rows(), [])

    async def test_transaction_rolls_back_additions_when_cancellation_fails(self):
        slot_id = await self.weekly()
        person = await db.register_participant(12345, 'person', 'Иванов Иван Иванович', '+79991112233')
        await events.response_for(person, slot_id, at('2026-09-17'))
        await db._c().execute("CREATE TRIGGER reject_cancel BEFORE UPDATE OF is_cancelled ON responses "
                              "WHEN NEW.is_cancelled=1 BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        await db._c().commit()
        plan = await importer.build_plan([entry('2026-09-17', '19:00'), entry('2026-09-24')])
        before = await self.rows()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'test rollback'):
            await importer.apply_plan(plan, cancel_missing=True)
        self.assertEqual(await self.rows(), before)
        response = await (await db._c().execute('SELECT is_cancelled FROM responses')).fetchone()
        self.assertEqual(response['is_cancelled'], 0)

    async def test_cancellation_updates_only_matching_future_delivery_and_answers(self):
        slot_id = await self.weekly()
        person = await db.register_participant(12345, 'person', 'Иванов Иван Иванович', '+79991112233')
        for day in ['2026-09-10', '2026-09-17', '2026-09-24']:
            response = await events.response_for(person, slot_id, at(day))
            await db._c().execute("UPDATE responses SET status='yes' WHERE id=?", (response['id'],))
            await events.queue_manual(slot_id, at(day), 777)
        await db._c().commit()
        plan = await importer.build_plan([entry('2026-09-17', '19:00'), entry('2026-09-24')])
        await importer.apply_plan(plan, cancel_missing=True)
        rows = await (await db._c().execute('SELECT starts_at,status,is_cancelled FROM responses ORDER BY starts_at')).fetchall()
        self.assertEqual([(r['status'], r['is_cancelled']) for r in rows], [('yes', 0), ('yes', 1), ('yes', 0)])
        manual = await (await db._c().execute('SELECT status FROM manual_polls ORDER BY starts_at')).fetchall()
        self.assertEqual([r['status'] for r in manual], ['pending', 'cancelled', 'pending'])
        _, summary = await events.summary()
        self.assertEqual(summary[0]['marks']['2026-09-10'], 'Y')
        self.assertEqual(summary[0]['marks']['2026-09-17'], '')

    async def test_weekly_end_time_overrides_only_selected_date_and_omission_preserves_it(self):
        slot_id = await self.weekly()
        await db._c().execute("UPDATE schedule SET end_time='19:00' WHERE id=?", (slot_id,))
        await db._c().commit()
        plan = await importer.build_plan([entry('2026-09-17', end='20:00'), entry('2026-09-24')])
        self.assertTrue(plan['existing'][0]['end_time_changed'])
        self.assertEqual(plan['existing'][0]['old_end_time'], '19:00')
        self.assertFalse(plan['existing'][1]['end_time_changed'])
        result = await importer.apply_plan(plan)
        self.assertEqual(result['end_times_updated'], 1)
        slot = await events.get_slot(slot_id)
        self.assertEqual(slot['end_time'], '19:00')
        self.assertEqual(events.end_time(slot, at('2026-09-17')), '20:00')
        self.assertEqual(events.end_time(slot, at('2026-09-24')), '19:00')
        plan = await importer.build_plan([entry('2026-09-17')])
        await importer.apply_plan(plan)
        self.assertEqual(events.end_time(await events.get_slot(slot_id), at('2026-09-17')), '20:00')

    async def test_once_end_update_and_existing_duplicate_slots_are_not_deleted(self):
        weekly = await self.weekly()
        once = await events.save_slot(3, '18:00', training_date='2026-09-17')
        plan = await importer.build_plan([entry('2026-09-17', end='20:00')])
        self.assertEqual(plan['existing'][0]['slot_ids'], [weekly, once])
        result = await importer.apply_plan(plan, cancel_missing=True)
        self.assertEqual((result['added'], result['cancelled'], result['end_times_updated']), (0, 0, 2))
        self.assertEqual((await events.get_slot(once))['end_time'], '20:00')
        self.assertEqual(len(await db.list_schedule()), 2)

    async def test_empty_past_only_and_outside_range_do_not_change_schedule(self):
        await self.weekly()
        before = await self.rows()
        for entries in ([], [entry('2026-09-10')]):
            plan = await importer.build_plan(entries)
            self.assertFalse(plan['additions'])
            self.assertFalse(plan['missing'])
            await importer.apply_plan(plan, cancel_missing=True)
            self.assertEqual(await self.rows(), before)
        plan = await importer.build_plan([entry('2026-09-19')])
        self.assertEqual((plan['range_start'], plan['range_end']), ('2026-09-19', '2026-09-19'))
        self.assertFalse(plan['missing'])

    async def test_today_past_only_does_not_cancel_later_today(self):
        slot_id = await events.save_slot(1, '18:00', starts_on='2026-09-01')
        plan = await importer.build_plan([entry('2026-09-15', '11:00')])
        self.assertEqual(len(plan['past']), 1)
        self.assertFalse(plan['missing'])
        result = await importer.apply_plan(plan, cancel_missing=True)
        self.assertEqual(result['cancelled'], 0)
        self.assertTrue(events.matches(await events.get_slot(slot_id), at('2026-09-15')))

    async def test_save_and_delete_keep_exception_fields_and_long_pause_has_next_date(self):
        slot_id = await self.weekly()
        exclusions = ['2026-09-17', '2026-09-24', '2026-10-01']
        overrides = {'2026-10-08': '20:00'}
        await db._c().execute('UPDATE schedule SET excluded_dates=?,end_times=? WHERE id=?',
                              (json.dumps(exclusions), json.dumps(overrides), slot_id))
        await db._c().commit()
        await events.save_slot(3, '18:00', slot_id=slot_id, starts_on='2026-09-01')
        slot = await events.get_slot(slot_id)
        self.assertEqual(json.loads(slot['excluded_dates']), exclusions)
        self.assertEqual(next(events.occurrences(slot, self.clock)), at('2026-10-08'))
        await events.delete_slot(slot_id)
        stored = (await self.rows())[0]
        self.assertEqual(json.loads(stored['end_times']), overrides)
        self.assertEqual(json.loads(stored['excluded_dates']), exclusions)
        self.assertFalse(events.matches(stored, at('2026-10-08')))

    async def test_conflicting_duplicate_ends_and_tampered_entries_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'разные окончания'):
            await importer.build_plan([entry('2026-09-17', end='19:00'), entry('2026-09-17', end='20:00')])
        plan = await importer.build_plan([entry('2026-09-17')])
        plan['entries'][0]['time'] = '19:00'
        with self.assertRaisesRegex(ValueError, 'предпросмотр'):
            await importer.apply_plan(plan)
        self.assertEqual(await self.rows(), [])

    async def test_moving_start_discards_only_incompatible_ends_keeps_exclusions(self):
        slot_id = await self.weekly()
        await db._c().execute('UPDATE schedule SET end_time=?,end_times=?,excluded_dates=? WHERE id=?',
                              ('21:30', json.dumps({'2026-09-17': '21:00', '2026-09-24': '23:30'}),
                               json.dumps(['2026-10-01']), slot_id))
        await db._c().commit()
        await events.save_slot(3, '22:00', slot_id=slot_id, starts_on='2026-09-01')
        slot = await events.get_slot(slot_id)
        self.assertIsNone(slot['end_time'])
        self.assertEqual(json.loads(slot['end_times']), {'2026-09-24': '23:30'})
        self.assertEqual(json.loads(slot['excluded_dates']), ['2026-10-01'])

    async def test_existing_database_migration_adds_fields_without_changing_schedule(self):
        await db.close_db()
        db._conn = None
        old_path = Path(self.folder.name, 'old.sqlite3')
        with sqlite3.connect(old_path) as conn:
            conn.execute('CREATE TABLE schedule (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                         'weekday INTEGER NOT NULL,time TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1)')
            conn.execute("INSERT INTO schedule (weekday,time) VALUES (3,'18:00')")
        conn.close()
        with patch.object(db, 'DB_PATH', str(old_path)):
            await db.init_db()
        slot = await events.get_slot(1)
        self.assertEqual((slot['weekday'], slot['time'], slot['is_active']), (3, '18:00', 1))
        self.assertIsNone(slot['end_time'])
        self.assertEqual(slot['excluded_dates'], '[]')
        self.assertEqual(slot['end_times'], '{}')

    async def test_366_day_span_allowed_but_larger_span_rejected(self):
        from datetime import timedelta
        first = self.clock.date()
        plan = await importer.build_plan([entry(first.isoformat()), entry((first+timedelta(days=366)).isoformat())])
        self.assertEqual(len(plan['entries']), 2)
        with self.assertRaisesRegex(ValueError, '366'):
            await importer.build_plan([entry(first.isoformat()), entry((first+timedelta(days=367)).isoformat())])

    async def test_scheduler_and_answer_handler_reject_excluded_occurrence(self):
        import scheduler
        from handlers_poll import process_answer
        slot_id = await self.weekly()
        person = await db.register_participant(12345, 'person', 'Иванов Иван Иванович', '+79991112233')
        await db.set_active(person, True)
        response = await events.response_for(person, slot_id, at('2026-09-17'))
        await db._c().execute('UPDATE responses SET message_id=99 WHERE id=?', (response['id'],))
        # Keep the response flag deliberately unset to verify that consumers
        # independently validate the excluded date, including races/stale polls.
        await db._c().execute('UPDATE schedule SET excluded_dates=? WHERE id=?',
                              (json.dumps(['2026-09-17']), slot_id))
        await db._c().commit()
        bot = SimpleNamespace(send_message=AsyncMock())
        message = SimpleNamespace(message_id=99, answer=AsyncMock(), edit_text=AsyncMock())
        callback = SimpleNamespace(id='test', answer=AsyncMock(), from_user=SimpleNamespace(id=12345),
                                   message=message, data=f"r:{response['id']}:yes")
        with patch.object(utils, 'now', return_value=at('2026-09-17', '17:00')):
            await scheduler.tick(bot)
            await process_answer(callback)
        bot.send_message.assert_not_called()
        message.answer.assert_awaited_once()
        self.assertIn('закрыт', message.answer.call_args.args[0])
        stored = await (await db._c().execute('SELECT status FROM responses WHERE id=?', (response['id'],))).fetchone()
        self.assertEqual(stored['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
