"""Review/confirmation screens must show every consequential schedule change."""
from datetime import date
from types import SimpleNamespace
import unittest
import sqlite3
from unittest.mock import AsyncMock, patch
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.context import FSMContext
import handlers_import as screen
from schedule_parser import parse_schedule


SAMPLE = '''РАСПИСАНИЕ ТРЕНИРОВОК В СЕНТЯБРЕ
14.09, Пн19:30 - 21:30
15.09, Вт19:30 - 21:30
16.09, Ср19:30 - 21:30
19.09, Сб20:00 -22:00

21.09, Пн19:30 - 21:30
22.09, Вт19:30 - 21:30
23.09, Ср19:30 - 21:30
24.09, Чт19:30 - 21:30
25.09, Пт19:30 - 21:30
26.09, Сб20:00 -22:00
27.09, Вс20:00 -22:00

28.09, Пн19:30 - 21:30
29.09, Вт19:30 - 21:30
30.09, Ср19:30 - 21:30'''


class ImportScreens(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
        self.msg = SimpleNamespace(answer=AsyncMock(return_value=SimpleNamespace(message_id=40)))
        self.cb = SimpleNamespace(data='', from_user=SimpleNamespace(id=1), message=self.msg, answer=AsyncMock(), id='cb')
        parsed = parse_schedule(SAMPLE, date(2026, 9, 15))
        self.assertEqual(parsed['errors'], [])
        self.assertEqual(len(parsed['entries']), 14)
        self.plan = {'entries': parsed['entries'], 'additions': parsed['entries'][1:],
                     'existing': [], 'missing': [{'date':'2026-09-17','time':'19:30','end_time':None,'slot_id':5}],
                     'past': parsed['entries'][:1], 'range_start':'2026-09-14','range_end':'2026-09-30', 'fingerprint':'x'}
        await self.state.update_data(import_plan=self.plan, import_warnings=parsed['warnings'])

    async def asyncTearDown(self):
        await self.storage.close()

    async def test_all_review_pages_fit_telegram_and_missing_dates_are_explicit(self):
        seen = []
        for page in range(2):
            await screen.show_review(self.msg, self.state, page)
            text = self.msg.answer.await_args.args[0]
            self.assertLess(len(text), 4096)
            seen.append(text)
            self.assertEqual(await self.state.get_state(), screen.ScheduleImport.review.state)
        self.assertIn('Нет в сообщении: 17.09.2026', '\n'.join(seen))
        self.assertIn('Прошло, пропускаю: 14.09.2026', '\n'.join(seen))

    async def test_add_mode_never_passes_cancel_flag_and_duplicate_save_loses_draft(self):
        self.cb.data = 'import:choose:add'
        await screen.choose_mode(self.cb, self.state)
        self.assertFalse((await self.state.get_data())['import_cancel_missing'])
        with patch.object(screen, 'apply_plan', new_callable=AsyncMock) as apply, \
             patch.object(screen.google_sheet, 'queue'):
            apply.return_value = {'added':13,'existing':0,'cancelled':0,'end_times_updated':0}
            await screen.save_import(self.cb, self.state)
        apply.assert_awaited_once_with(self.plan, False)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

    async def test_cancel_mode_names_date_before_separate_confirmation(self):
        self.cb.data = 'import:choose:replace'
        with patch.object(screen, 'apply_plan', new_callable=AsyncMock) as apply:
            await screen.choose_mode(self.cb, self.state)
        apply.assert_not_awaited()
        self.assertTrue((await self.state.get_data())['import_cancel_missing'])
        text = self.msg.answer.await_args.args[0]
        self.assertIn('17.09.2026', text)
        self.assertIn('Другие даты еженедельных серий сохранятся', text)
        self.assertEqual(await self.state.get_state(), screen.ScheduleImport.confirm.state)

    async def test_stale_preview_is_rebuilt_and_never_automatically_reconfirmed(self):
        await self.state.update_data(import_cancel_missing=True)
        fresh = dict(self.plan, fingerprint='new')
        with patch.object(screen, 'apply_plan', new_callable=AsyncMock, side_effect=ValueError('Расписание изменилось')), \
             patch.object(screen, 'build_plan', new_callable=AsyncMock, return_value=fresh), \
             patch.object(screen.google_sheet, 'queue') as queue:
            await screen.save_import(self.cb, self.state)
        queue.assert_not_called()
        self.assertEqual(await self.state.get_state(), screen.ScheduleImport.review.state)
        self.assertEqual((await self.state.get_data())['import_plan']['fingerprint'], 'new')

    async def test_parser_error_does_not_build_or_write_plan(self):
        await self.state.set_state(screen.ScheduleImport.text)
        self.msg.text = '15.09 Вт19:30\nнепонятная строка'
        with patch.object(screen, 'build_plan', new_callable=AsyncMock) as build:
            await screen.receive_text(self.msg, self.state)
        build.assert_not_awaited()
        self.assertIn('Расписание ещё не изменено', self.msg.answer.await_args.args[0])

    async def test_database_busy_keeps_draft_and_requires_fresh_read(self):
        await self.state.update_data(import_cancel_missing=True)
        with patch.object(screen, 'apply_plan', new_callable=AsyncMock, side_effect=sqlite3.OperationalError('database is locked')), \
             patch.object(screen.log, 'exception'), patch.object(screen.google_sheet, 'queue') as queue:
            await screen.save_import(self.cb, self.state)
        queue.assert_not_called()
        self.assertEqual((await self.state.get_data())['import_plan'], self.plan)
        self.assertEqual(await self.state.get_state(), screen.ScheduleImport.review.state)
        self.assertIn('Проверить состояние', self.msg.answer.await_args.args[0])
