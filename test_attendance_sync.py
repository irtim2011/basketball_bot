import asyncio
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import attendance_sync as sync

DAYS = ['2026-08-01', '2026-09-06']


def grid(mark='Y', second='', code='1001', telegram='77', name='Васильев Дмитрий'):
    return [['ID участника', 'Telegram ID', 'ФИО', 'Телеграм', 'Телефон', 'flag_active',
             '01.08.2026', '06.09.2026'],
            ['', '', '', '', 'День недели', 'За последние 30 дней', 'Сб', 'Вс'],
            [code, telegram, name, '@dima', '+79991234567', '', mark, second]]


def bot(mark='Y', second='', code='1001', telegram=77, name='Васильев Дмитрий'):
    return [{'internal_id': 1, 'participant_id': code, 'telegram_id': telegram,
             'full_name': name, 'username': 'dima', 'phone': '+79991234567',
             'marks': dict(zip(DAYS, (mark, second)))}]


def success(journal):
    journal = deepcopy(journal)
    journal['snapshot'] = journal.pop('pending')['target']
    return json.loads(json.dumps(journal))


class ReconciliationTests(unittest.TestCase):
    def start(self, source=None, public=None, rows=None):
        source = source or grid()
        target, journal, _ = sync.reconcile_grids(source, public or source, DAYS, rows or bot(), {})
        return target, success(journal)

    def test_manual_y_to_n_and_blank_survive_bot_change_and_restart(self):
        for manual in ('N', ''):
            with self.subTest(manual=manual):
                original, state = self.start()
                edited = deepcopy(original)
                edited[2][6] = manual
                target, journal, _ = sync.reconcile_grids(original, edited, DAYS, bot(), state)
                self.assertEqual(target[2][6], manual)
                state = success(journal)
                target, journal, _ = sync.reconcile_grids(target, target, DAYS, bot('N', 'Y'), state)
                self.assertEqual(target[2][6:], [manual, 'Y'])
                target, _, _ = sync.reconcile_grids(target, target, DAYS, bot('Y', 'Y'), success(journal))
                self.assertEqual(target[2][6:], [manual, 'Y'])

    def test_bootstrap_does_not_replay_old_bot_values_into_cleared_cloud_cell(self):
        target, journal, _ = sync.reconcile_grids(grid('Y'), grid(''), DAYS, bot('Y'), {})
        self.assertEqual(target[2][6], '')
        self.assertEqual(journal['overrides']['1001'][DAYS[0]], '')
        again, _, _ = sync.reconcile_grids(target, target, DAYS, bot('N'), success(journal))
        self.assertEqual(again[2][6], '')

    def test_bootstrap_equal_cloud_correction_is_not_lost_to_old_database(self):
        target, journal, _ = sync.reconcile_grids(grid('N'), grid('N'), DAYS, bot('Y'), {})
        self.assertEqual(target[2][6], 'N')
        target, _, _ = sync.reconcile_grids(target, target, DAYS, bot(''), success(journal))
        self.assertEqual(target[2][6], 'N')

    def test_new_changed_bot_answer_and_cancelled_answer_are_applied(self):
        target, state = self.start()
        target, journal, _ = sync.reconcile_grids(target, target, DAYS, bot('Y', 'Y'), state)
        self.assertEqual(target[2][7], 'Y')
        target, _, _ = sync.reconcile_grids(target, target, DAYS, bot('Y', ''), success(journal))
        self.assertEqual(target[2][7], '')

    def test_manual_name_phone_and_username_override_later_registration(self):
        original, state = self.start()
        edited = deepcopy(original)
        edited[2][2:5] = ['Васильев Дмитрий Андреевич', '@correct', '+78888888888']
        target, journal, _ = sync.reconcile_grids(original, edited, DAYS, bot(), state)
        changed = bot(name='Васильев Дима')
        changed[0].update(phone='+71111111111', username='new')
        target, _, _ = sync.reconcile_grids(target, target, DAYS, changed, success(journal))
        self.assertEqual(target[2][2:5], edited[2][2:5])

    def test_sorting_rows_keeps_marks_with_stable_ids(self):
        original = grid() + [grid('N', code='1002', telegram='88', name='Петров Иван')[2]]
        target, state = self.start(original)
        edited = deepcopy(target)
        edited[2:] = reversed(edited[2:])
        edited[2][7] = 'Y'
        target, _, _ = sync.reconcile_grids(target, edited, DAYS, bot(), state)
        self.assertEqual([r[0] for r in target[2:]], ['1002', '1001'])
        self.assertEqual(target[2][6:], ['N', 'Y'])
        self.assertEqual(target[3][6:], ['Y', ''])

    def test_new_registered_participant_is_added_once(self):
        original, state = self.start()
        rows = bot() + [{**bot('N', 'Y', code='1002', telegram=88, name='Петров Иван')[0], 'internal_id': 2}]
        target, journal, _ = sync.reconcile_grids(original, original, DAYS, rows, state)
        self.assertEqual(len(target), 4)
        self.assertEqual(target[3][0], '1002')
        self.assertEqual(target[3][6:], ['N', 'Y'])
        again, _, _ = sync.reconcile_grids(target, target, DAYS, rows, success(journal))
        self.assertEqual(target, again)

    def test_legacy_adoption_keeps_corrected_cells_and_one_row(self):
        original = grid('N', code='8001')
        target, journal, adoptions = sync.reconcile_grids(original, original, DAYS, bot('Y'), {})
        self.assertEqual(adoptions, [(1, 8001)])
        self.assertEqual(len(target), 3)
        self.assertEqual(target[2][0], '8001')
        self.assertEqual(target[2][6], 'N')
        self.assertIn('8001', journal['bot_snapshot'])

    def test_duplicate_changed_or_deleted_identity_and_date_fail_closed(self):
        original, state = self.start()
        bad_grids = []
        duplicate = deepcopy(original)
        duplicate.append(duplicate[2][:])
        bad_grids.append(duplicate)
        for column, value in ((0, '9999'), (1, '1234'), (6, 'not attendance')):
            changed = deepcopy(original)
            changed[2][column] = value
            bad_grids.append(changed)
        bad_grids.append(original[:2])
        missing_date = deepcopy(original)
        missing_date[0][6] = ''
        bad_grids.append(missing_date)
        added_date = deepcopy(original)
        added_date[0].append('31.07.2026')
        bad_grids.append(added_date)
        for bad in bad_grids:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sync.reconcile_grids(original, bad, DAYS, bot(), state)

    def test_bot_can_extend_calendar_without_allowing_manual_header_changes(self):
        original, state = self.start()
        dates = DAYS + ['2027-01-01']
        rows = bot()
        rows[0]['marks']['2027-01-01'] = 'Y'
        target, journal, _ = sync.reconcile_grids(original, original, dates, rows, state)
        self.assertEqual(target[0][-1], '01.01.2027')
        self.assertEqual(target[2][-1], 'Y')
        again, _, _ = sync.reconcile_grids(target, target, dates, rows, success(journal))
        self.assertEqual(target, again)

    def test_mirror_only_keeps_bot_baseline_and_captures_edits(self):
        original, state = self.start()
        edited = deepcopy(original)
        edited[2][7] = 'N'
        target, journal, _ = sync.reconcile_grids(original, edited, None, None, state)
        self.assertEqual(journal['bot_snapshot'], state['bot_snapshot'])
        target, _, _ = sync.reconcile_grids(target, target, DAYS, bot('Y', 'Y'), success(journal))
        self.assertEqual(target[2][7], 'N')

    def test_partial_write_recovery_does_not_treat_old_cloud_value_as_manual(self):
        original, state = self.start()
        target, pending, _ = sync.reconcile_grids(original, original, DAYS, bot('Y', 'Y'), state)
        # Source write succeeded; public write did not. Retry keeps intended bot Y.
        recovered, journal, _ = sync.reconcile_grids(target, original, DAYS, bot('Y', 'Y'), pending)
        self.assertEqual(recovered[2][7], 'Y')
        self.assertNotIn(DAYS[1], journal['overrides'].get('1001', {}))
        final, _, _ = sync.reconcile_grids(recovered, recovered, DAYS, bot('Y', 'N'), success(journal))
        self.assertEqual(final[2][7], 'N')

    def test_new_manual_correction_during_recovery_wins(self):
        original, state = self.start()
        target, pending, _ = sync.reconcile_grids(original, original, DAYS, bot('Y', 'Y'), state)
        edited = deepcopy(original)
        edited[2][7] = 'N'  # Neither the pre-write blank nor intended Y.
        recovered, journal, _ = sync.reconcile_grids(target, edited, DAYS, bot('Y', 'Y'), pending)
        self.assertEqual(recovered[2][7], 'N')
        self.assertEqual(journal['overrides']['1001'][DAYS[1]], 'N')


class FakeSheet:
    def __init__(self, title, sid, values):
        self.title, self.id, self.values = title, sid, deepcopy(values)
        self.row_count, self.col_count = 200, 401

    def get_all_values(self, **kwargs):
        return deepcopy(self.values)


class FakeBook:
    id = 'test-workbook'

    def __init__(self):
        self.sheets = {'Посещения_bot': FakeSheet('Посещения_bot', 1, grid()),
                       'Посещения': FakeSheet('Посещения', 2, grid())}
        self.requests = []
        self.fail_after = None

    def worksheet(self, name):
        return self.sheets[name]

    def batch_update(self, body):
        for i, request in enumerate(body['requests']):
            if self.fail_after is not None and i >= self.fail_after:
                self.fail_after = None
                raise OSError('transport failure after partial application')
            self.requests.append(request)
            if 'updateSheetProperties' in request:
                continue
            update = request['updateCells']
            sheet = next(s for s in self.sheets.values() if s.id == update['start']['sheetId'])
            row, column = update['start']['rowIndex'], update['start']['columnIndex']
            while len(sheet.values) <= row:
                sheet.values.append([])
            for offset, cell in enumerate(update['rows'][0]['values']):
                while len(sheet.values[row]) <= column + offset:
                    sheet.values[row].append('')
                values = cell.get('userEnteredValue', {})
                sheet.values[row][column + offset] = next(iter(values.values()), '')


class DurableJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'bot.db')
        self.database = patch('db.DB_PATH', self.path)
        self.database.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.database.stop)

    def test_durable_restart_selective_writes_and_idempotence(self):
        book = FakeBook()
        sync.reconcile_book(book, DAYS, bot())
        book.sheets['Посещения'].values[2][6] = ''
        sync.reconcile_book(book, DAYS, bot())
        persisted = sync._state(book.id)
        self.assertEqual(persisted['overrides']['1001'][DAYS[0]], '')
        self.assertNotIn('pending', persisted)
        book.requests.clear()
        sync.reconcile_book(book, DAYS, bot('N'))
        self.assertEqual(book.sheets['Посещения_bot'].values[2][6], '')
        self.assertEqual(book.requests, [])
        # There are no copy/reset/clear requests or changes to formatting.
        book.sheets['Посещения'].values[2][7] = 'N'
        sync.reconcile_book(book, DAYS, bot('N'))
        self.assertTrue(book.requests)
        self.assertTrue(all(r['updateCells']['fields'] == 'userEnteredValue' for r in book.requests))

    def test_journal_saved_before_failure_and_retry_recovers(self):
        book = FakeBook()
        sync.reconcile_book(book, DAYS, bot())
        book.fail_after = 1
        with self.assertRaises(OSError):
            sync.reconcile_book(book, DAYS, bot('Y', 'Y'))
        self.assertIn('pending', sync._state(book.id))
        sync.reconcile_book(book, DAYS, bot('Y', 'Y'))
        self.assertNotIn('pending', sync._state(book.id))
        self.assertEqual(book.sheets['Посещения_bot'].values, book.sheets['Посещения'].values)
        self.assertEqual(book.sheets['Посещения'].values[2][7], 'Y')

    def test_lock_is_reentrant(self):
        with sync.workbook_lock():
            with sync.workbook_lock():
                self.assertEqual(sync._local.depth, 2)


class SyncOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_sync_reads_database_after_first_sync_completes(self):
        import google_sheet
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocking(dates, rows):
            calls.append(rows)
            if len(calls) == 1:
                entered.set()
                if not release.wait(2):
                    raise TimeoutError('test sync did not finish')
            return []

        with patch.object(google_sheet, '_sync_lock', asyncio.Lock()), \
             patch('events.summary', new_callable=AsyncMock, side_effect=[(DAYS, ['old']), (DAYS, ['new'])]) as summary, \
             patch.object(google_sheet, '_sync_blocking', side_effect=blocking):
            first = asyncio.create_task(google_sheet.sync_now())
            second = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                second = asyncio.create_task(google_sheet.sync_now())
                await asyncio.sleep(0)
                self.assertEqual(summary.await_count, 1)
            finally:
                release.set()
                await asyncio.gather(first, *([second] if second else []))
        self.assertEqual(calls, [['old'], ['new']])


if __name__ == '__main__':
    unittest.main()
