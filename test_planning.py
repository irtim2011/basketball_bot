import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import db
import events
import planning
import utils
from handlers_planning import render_card


class PlanningTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / 'planning.db')
        self.enabled = utils.TZ.localize(datetime(2026, 9, 20, 11, 59))
        self.clock = patch('utils.now', return_value=self.enabled)
        self.mock_now = self.clock.start()
        await db.init_db()
        await planning.init_schema(self.enabled)
        self.pid = await db.register_participant(77, 'test_user', 'Иванов Иван Иванович', '1234567')
        await db.set_active(self.pid, True)
        self.messages = []

        async def send(chat_id, text, **kwargs):
            result = SimpleNamespace(message_id=100+len(self.messages))
            self.messages.append((chat_id, text, kwargs, result.message_id))
            return result
        self.bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))

    async def asyncTearDown(self):
        await db.close_db()
        db._conn = None
        db.DB_PATH = self.old_path
        self.clock.stop()
        self.tmp.cleanup()

    def when(self, day, hour=12, minute=0):
        return utils.TZ.localize(datetime(2026, 9, day, hour, minute))

    async def slot(self, day='2026-09-24', clock='19:30', recurring=False):
        value = datetime.fromisoformat(day)
        return await events.save_slot(value.weekday(), clock, None if recurring else day, starts_on=day)

    async def rows(self, query, values=()):
        return [dict(row) for row in await (await db._c().execute(query, values)).fetchall()]

    async def poll_and_answer(self, kind='week'):
        poll = (await self.rows('SELECT * FROM planned_polls WHERE kind=? ORDER BY id DESC', (kind,)))[0]
        answer = (await self.rows('SELECT * FROM planned_answers WHERE poll_id=? ORDER BY starts_at', (poll['id'],)))[0]
        return poll, answer

    async def main_response(self, sid, start, message_id=900):
        response = await events.response_for(self.pid, sid, start)
        await db._c().execute('UPDATE responses SET message_id=? WHERE id=?', (message_id, response['id']))
        await db._c().commit()
        return response['id']

    async def test_weekly_due_and_restart_idempotency(self):
        await self.slot()
        self.assertEqual(await planning.deliver_due(self.bot, self.enabled), {'sent': 0, 'failed': 0})
        result = await planning.deliver_due(self.bot, self.when(20))
        self.assertEqual(result['sent'], 1)
        poll, answer = await self.poll_and_answer()
        self.assertEqual((poll['period_start'], poll['period_end']), ('2026-09-21', '2026-09-27'))
        self.assertEqual(answer['occurrence_key'], '2026-09-24T16:30:00+00:00')
        self.assertIn('План на неделю', self.messages[0][1])
        await db.close_db()
        await db.init_db()
        await planning.init_schema(self.when(21))
        await planning.deliver_due(self.bot, self.when(21))
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual((await planning.snapshot(self.when(21)))['enabled_at'], planning.occurrence_key(self.enabled))

    async def test_early_poll_only_selected_person_and_session_then_normal_period(self):
        start=utils.TZ.localize(datetime(2026,10,1,2))
        sid=await self.slot('2026-10-01','02:00')
        await self.slot('2026-10-02','19:30')
        second=await db.register_participant(88,'second','Петров Петр Петрович','7654321')
        await db.set_active(second,True)
        self.assertEqual(await planning.queue_early([self.pid],sid,start,self.enabled),2)
        self.assertEqual(await planning.queue_early([self.pid],sid,start,self.enabled),0)
        await planning.deliver_due(self.bot,self.enabled)
        self.assertEqual([m[0] for m in self.messages],[77,77])
        polls=await self.rows('SELECT * FROM planned_polls')
        for poll in polls:
            rows=await self.rows('SELECT * FROM planned_answers WHERE poll_id=?',(poll['id'],))
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['schedule_id'],sid)
        await planning.deliver_due(self.bot,self.when(30))
        month=await self.rows("SELECT * FROM planned_polls WHERE participant_id=? AND kind='month'",(self.pid,))
        self.assertIsNone(month[0]['early_scope'])
        rows=await self.rows('SELECT * FROM planned_answers WHERE poll_id=?',(month[0]['id'],))
        self.assertEqual(len(rows),2)
        self.assertTrue(any(m[0]==88 for m in self.messages))

    async def test_first_enable_never_sends_preexisting_due_campaigns(self):
        await self.slot()
        later = self.when(20, 12, 1)
        await db._c().execute("UPDATE planning_meta SET value=? WHERE key='enabled_at'", (later.isoformat(),))
        await db._c().commit()
        await planning.deliver_due(self.bot, later)
        self.assertEqual(self.messages, [])
        self.assertEqual(await self.rows('SELECT * FROM planned_polls'), [])

    async def test_monthly_last_day_noon_for_next_month(self):
        await self.slot('2026-10-08')
        await planning.deliver_due(self.bot, self.when(30, 11, 59))
        self.assertEqual(self.messages, [])
        await planning.deliver_due(self.bot, self.when(30))
        poll, answer = await self.poll_and_answer('month')
        self.assertEqual(poll['period_start'], '2026-10-01')
        self.assertEqual(poll['period_end'], '2026-10-31')
        self.assertEqual(answer['starts_at'][:10], '2026-10-08')
        self.assertEqual(len(self.messages), 1)

    async def test_once_weekly_same_occurrence_and_excluded_date(self):
        await self.slot(recurring=True)
        await self.slot()
        other = await self.slot('2026-09-25')
        await db._c().execute('UPDATE schedule SET excluded_dates=? WHERE id=?', ('["2026-09-25"]', other))
        await db._c().commit()
        await planning.deliver_due(self.bot, self.when(20))
        self.assertEqual(len(await self.rows('SELECT * FROM planned_answers')), 1)

    async def test_six_items_pagination_refresh_without_delivery_wave(self):
        for day in range(21, 28):
            await self.slot(f'2026-09-{day}')
        await planning.deliver_due(self.bot, self.when(20))
        poll, _ = await self.poll_and_answer()
        card = await planning.get_card(poll['id'], 77, poll['message_id'], 0, self.when(20))
        self.assertEqual((card['pages'], len(card['entries'])), (2, 6))
        text, markup = render_card(card)
        self.assertLess(len(text), 4096)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertTrue(all(len(value.encode('utf-8')) <= 64 for value in callbacks))
        self.assertEqual(sum(value.startswith('pa:') for value in callbacks), 12)
        card2 = await planning.get_card(poll['id'], 77, poll['message_id'], 1, self.when(20))
        self.assertEqual(len(card2['entries']), 1)
        await self.slot('2026-09-25', '21:00')
        await planning.deliver_due(self.bot, self.when(20, 12, 1))
        self.assertEqual(len(self.messages), 1)
        updated = await planning.get_card(poll['id'], 77, poll['message_id'], 1, self.when(20))
        self.assertEqual(updated['total'], 8)

    async def test_forecast_isolated_from_attendance_and_callback_ownership(self):
        await self.slot()
        await planning.deliver_due(self.bot, self.when(20))
        poll, answer = await self.poll_and_answer()
        args = [answer['id'], answer['generation'], 77, poll['message_id'], 'yes', 'plan-1']
        for owner, message in ((99, poll['message_id']), (77, 999)):
            invalid = args.copy(); invalid[2:4] = [owner, message]
            self.assertIsNone(await planning.answer_planned(*invalid, now=self.when(20)))
        result = await planning.answer_planned(*args, now=self.when(20))
        self.assertTrue(result['changed'])
        _, rows = await events.summary()
        self.assertEqual(rows[0]['marks']['2026-09-24'], '')
        self.assertEqual(await self.rows('SELECT * FROM responses'), [])
        self.assertEqual(await self.rows('SELECT * FROM attendance'), [])
        snapshot = await planning.snapshot(self.when(20))
        self.assertEqual(snapshot['changes'][0]['old_status'], 'pending')
        self.assertEqual(snapshot['changes'][0]['new_status'], 'yes')
        json.dumps(snapshot)

    async def test_cancel_restore_rejects_old_generation_and_start_deadline(self):
        sid = await self.slot()
        await planning.deliver_due(self.bot, self.when(20))
        poll, answer = await self.poll_and_answer()
        self.mock_now.return_value = self.when(20)
        await events.delete_slot(sid)
        await planning.invalidate_future(self.when(20))
        invalid = await planning.answer_planned(answer['id'], answer['generation'], 77,
                                               poll['message_id'], 'yes', 'old', self.when(20))
        self.assertIsNone(invalid)
        await self.slot()  # A new once slot at the same instant has the same stable key.
        card = await planning.get_card(poll['id'], 77, poll['message_id'], now=self.when(20))
        fresh = card['entries'][0]
        self.assertGreater(fresh['generation'], answer['generation'])
        self.assertIsNone(await planning.answer_planned(answer['id'], answer['generation'], 77,
                                                       poll['message_id'], 'yes', 'stale', self.when(20)))
        at_start = utils.TZ.localize(datetime(2026, 9, 24, 19, 30))
        self.assertIsNone(await planning.answer_planned(fresh['id'], fresh['generation'], 77,
                                                       poll['message_id'], 'yes', 'late', at_start))

    async def test_priority_main_over_week_and_late_month_changes(self):
        sid = await self.slot('2026-10-08')
        await planning.deliver_due(self.bot, self.when(30))
        month, monthly = await self.poll_and_answer('month')
        await planning.answer_planned(monthly['id'], monthly['generation'], 77, month['message_id'], 'yes', 'month-yes', self.when(30))
        sunday = utils.TZ.localize(datetime(2026, 10, 4, 12))
        await planning.deliver_due(self.bot, sunday)
        week, weekly = await self.poll_and_answer('week')
        await planning.answer_planned(weekly['id'], weekly['generation'], 77, week['message_id'], 'no', 'week-no', sunday)
        start = utils.TZ.localize(datetime(2026, 10, 8, 19, 30))
        rid = await self.main_response(sid, start)
        await planning.record_main_answer(rid, 77, 900, 'yes', 'main-yes', start-timedelta(hours=23))
        await planning.answer_planned(monthly['id'], monthly['generation'], 77, month['message_id'], 'no',
                                       'month-late-no', start-timedelta(hours=22))
        changes = (await planning.snapshot(start-timedelta(hours=21)))['changes']
        self.assertEqual([(c['old_status'], c['new_status']) for c in changes], [('pending', 'yes'), ('yes', 'no'), ('no', 'yes')])
        self.assertEqual([(c['from_stage'], c['to_stage']) for c in changes], [(None, 'month'), ('month', 'week'), ('week', 'main')])
        self.assertEqual(len(await self.rows("SELECT * FROM answer_change_log WHERE log_type='stage'")), 4)

    async def test_main_history_retries_and_after_start(self):
        sid = await self.slot()
        start = utils.TZ.localize(datetime(2026, 9, 24, 19, 30))
        rid = await self.main_response(sid, start)
        self.assertIsNone(await planning.record_main_answer(rid, 99, 900, 'yes', 'foreign', self.when(23)))
        self.assertIsNone(await planning.record_main_answer(rid, 77, 899, 'yes', 'old-msg', self.when(23)))
        await planning.record_main_answer(rid, 77, 900, 'no', 'first', self.when(23))
        await planning.record_main_answer(rid, 77, 900, 'yes', 'second', start)
        repeat = await planning.record_main_answer(rid, 77, 900, 'no', 'first', start+timedelta(minutes=1))
        self.assertFalse(repeat['changed'])
        self.assertEqual(repeat['status'], 'yes')
        self.mock_now.return_value = start+timedelta(minutes=2)
        await events.delete_slot(sid)  # Past attendance survives a later schedule clear.
        result = await planning.record_main_answer(rid, 77, 900, 'no', 'third', start+timedelta(minutes=3))
        self.assertTrue(result['changed'])
        changes = (await planning.snapshot(start+timedelta(minutes=4)))['changes']
        self.assertEqual([(c['old_status'], c['new_status']) for c in changes], [('pending', 'no'), ('no', 'yes'), ('yes', 'no')])

    async def test_retry_is_durable_and_does_not_resend_success(self):
        await self.slot()
        deliver = self.bot.send_message.side_effect
        self.bot.send_message.side_effect = RuntimeError('offline')
        with self.assertLogs('planning', level='WARNING'):
            await planning.deliver_due(self.bot, self.when(20))
        self.bot.send_message.side_effect = deliver
        await db.close_db(); await db.init_db()
        await planning.deliver_due(self.bot, self.when(20)+timedelta(seconds=29))
        self.assertEqual(len(self.messages), 0)
        await planning.deliver_due(self.bot, self.when(20)+timedelta(seconds=30))
        await planning.deliver_due(self.bot, self.when(20)+timedelta(seconds=31))
        self.assertEqual(len(self.messages), 1)

    async def test_no_both_old_and_next_week_campaigns_on_sunday(self):
        await self.slot('2026-09-27')
        await self.slot('2026-09-28')
        await planning.deliver_due(self.bot, self.when(27))
        polls = await self.rows("SELECT * FROM planned_polls WHERE kind='week'")
        self.assertEqual(len(polls), 1)
        self.assertEqual(polls[0]['period_start'], '2026-09-28')

    async def test_abandoned_delivery_lease_is_recovered_after_restart(self):
        await self.slot()
        due = self.when(20)
        planning._prepare(due)
        claimed = planning._claim(due)
        self.assertIsNotNone(claimed)
        await db.close_db(); await db.init_db()
        await planning.deliver_due(self.bot, due+timedelta(minutes=2))
        self.assertEqual(len(self.messages), 0)
        await planning.deliver_due(self.bot, due+timedelta(minutes=3))
        self.assertEqual(len(self.messages), 1)

    async def test_snapshot_next_month_horizon_and_cancelled_history(self):
        await self.slot('2026-10-08')
        await self.slot('2026-11-05')
        snapshot = await planning.snapshot(self.when(20))
        self.assertEqual([item['starts_at'][:10] for item in snapshot['sessions']], ['2026-10-08','2026-11-05'])
        sid = await self.slot()
        await planning.deliver_due(self.bot, self.when(20))
        self.mock_now.return_value = self.when(23)
        await events.delete_slot(sid)
        await planning.invalidate_future(self.when(23))
        snapshot = await planning.snapshot(self.when(25))
        cancelled = [item for item in snapshot['sessions'] if item['starts_at'].startswith('2026-09-24')]
        self.assertTrue(cancelled[0]['cancelled'])

    async def test_restored_previous_month_session_prefers_valid_main_over_cancelled_plan(self):
        old_sid = await self.slot()
        await planning.deliver_due(self.bot, self.when(20))
        self.mock_now.return_value = self.when(21)
        await events.delete_slot(old_sid)
        await planning.invalidate_future(self.when(21))
        new_sid = await self.slot()
        start = utils.TZ.localize(datetime(2026, 9, 24, 19, 30))
        rid = await self.main_response(new_sid, start)
        await planning.record_main_answer(rid, 77, 900, 'yes', 'restored-main', self.when(23))
        snapshot = await planning.snapshot(utils.TZ.localize(datetime(2026, 10, 1, 12)))
        key = planning.occurrence_key(start)
        session = next(item for item in snapshot['sessions'] if item['key'] == key)
        self.assertFalse(session['cancelled'])
        self.assertEqual(session['schedule_id'], new_sid)
        plan = next(item for item in snapshot['answers'] if item['key'] == key and item['kind'] == 'week')
        main = next(item for item in snapshot['answers'] if item['key'] == key and item['kind'] == 'main')
        self.assertTrue(plan['cancelled'])
        self.assertEqual((main['status'], main['cancelled']), ('yes', False))

    async def test_unsent_cancelled_poll_revives_after_participant_reenabled(self):
        await self.slot()
        due = self.when(20)
        planning._prepare(due)
        await db.set_active(self.pid, False)
        self.assertIsNone(planning._claim(due))
        poll = (await self.rows('SELECT * FROM planned_polls'))[0]
        self.assertEqual((poll['status'], poll['message_id']), ('cancelled', None))
        await db.set_active(self.pid, True)
        await planning.deliver_due(self.bot, due+timedelta(minutes=1))
        polls = await self.rows('SELECT * FROM planned_polls')
        self.assertEqual(len(polls), 1)
        self.assertEqual(polls[0]['id'], poll['id'])
        self.assertEqual(polls[0]['status'], 'sent')
        self.assertEqual(len(self.messages), 1)

    async def test_cancel_restore_records_pending_for_correct_24_hour_baseline(self):
        sid = await self.slot()
        await planning.deliver_due(self.bot, self.when(20))
        poll, answer = await self.poll_and_answer()
        await planning.answer_planned(answer['id'], answer['generation'], 77, poll['message_id'],
                                       'yes', 'original-yes', self.when(20))
        self.mock_now.return_value = self.when(21)
        await events.delete_slot(sid)
        await planning.invalidate_future(self.when(21))
        await self.slot()
        card = await planning.get_card(poll['id'], 77, poll['message_id'], now=self.when(21))
        restored = card['entries'][0]
        self.assertEqual(restored['status'], 'pending')
        await planning.answer_planned(restored['id'], restored['generation'], 77, poll['message_id'],
                                       'no', 'restored-no', self.when(23, 20))
        changes = (await planning.snapshot(self.when(23, 21)))['changes']
        self.assertEqual([(c['old_status'], c['new_status']) for c in changes],
                         [('pending', 'yes'), ('yes', 'pending'), ('pending', 'no')])
        cutoff = self.when(23, 19, 30)
        before_cutoff = [c for c in changes if datetime.fromisoformat(c['changed_at']) <= cutoff]
        self.assertEqual(before_cutoff[-1]['new_status'], 'pending')
        # Repeated background validation must not append duplicate system events.
        await planning.invalidate_future(self.when(23, 21))
        self.assertEqual(len((await planning.snapshot(self.when(23, 21)))['changes']), 3)

    async def test_main_only_cancellation_records_reset_after_parent_marks_cancelled(self):
        sid = await self.slot()
        start = utils.TZ.localize(datetime(2026, 9, 24, 19, 30))
        rid = await self.main_response(sid, start)
        await planning.record_main_answer(rid, 77, 900, 'yes', 'main-before-cancel', self.when(20))
        self.mock_now.return_value = self.when(21)
        await events.delete_slot(sid)
        await planning.invalidate_future(self.when(21))
        await planning.invalidate_future(self.when(21))
        changes = (await planning.snapshot(self.when(21)))['changes']
        self.assertEqual([(c['old_status'], c['new_status']) for c in changes],
                         [('pending', 'yes'), ('yes', 'pending')])
        self.assertEqual((changes[1]['from_stage'], changes[1]['to_stage']), ('main', None))

    async def test_enable_observes_legacy_main_answer_without_inventing_earlier_history(self):
        sid = await self.slot()
        start = utils.TZ.localize(datetime(2026, 9, 24, 19, 30))
        rid = await self.main_response(sid, start)
        # Simulate the first migration from a release with no planning journal.
        await db._c().execute("DELETE FROM planning_meta WHERE key='enabled_at'")
        await db._c().execute("UPDATE responses SET status='yes',responded_at=? WHERE id=?",
                              (self.when(19).isoformat(), rid))
        await db._c().commit()
        await planning.init_schema(self.enabled)
        await planning.init_schema(self.when(21))
        changes = (await planning.snapshot(self.when(20)))['changes']
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['new_status'], 'yes')
        self.assertEqual(changes[0]['changed_at'], self.enabled.isoformat())
        self.mock_now.return_value = self.when(21)
        await events.delete_slot(sid)
        changes = (await planning.snapshot(self.when(21)))['changes']
        self.assertEqual([(c['old_status'], c['new_status']) for c in changes],
                         [('pending', 'yes'), ('yes', 'pending')])
