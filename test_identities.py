import tempfile
from pathlib import Path
from datetime import date, timedelta
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch
import db
import utils
import google_sheet
import finance_sheet

class NameTests(TestCase):
    def test_registration(self):
        self.assertEqual(utils.registration_fio('  иванов  иван иванович '), 'Иванов Иван Иванович')
        for value in ('Иван Иванов', 'Test Full Name', 'Иванов Иван 123', 'Иванов Иван Иванович Четвёртый'):
            self.assertIsNone(utils.registration_fio(value))

    def test_unique_and_ambiguous_names(self):
        rows = [{'canonical_name': 'Иванов Иван', 'public_id': 8006}]
        for value in ('Иванов Иван Иванович', 'Иван Иванов Иванович', 'Иванво Иван Иванович',
                      'Ивнов Иван Иванович', 'Иванов Ивван Иванович', 'Иванов Ивн Иванович'):
            self.assertIs(utils.unique_legacy_match(value, rows), rows[0], value)
        for value in ('Петров Пётр Петрович', 'Ивнов Ивн Иванович'):
            self.assertIsNone(utils.unique_legacy_match(value, rows))
        rows.append({'canonical_name': 'Иванов Ивана', 'public_id': 8010})
        self.assertIsNone(utils.unique_legacy_match('Иванов Иваны Иванович', rows))
        rows.append({'canonical_name': 'Иванов Иван', 'public_id': 8013})
        self.assertIsNone(utils.unique_legacy_match('Иванов Иван Иванович', rows))

    def test_sheet_adoption_keeps_historical_marks(self):
        grid = [google_sheet.BASE_HEADERS + ['flag_active','01.08.2026'], ['']*7,
                ['8006','','Иванов Иван','','','','Y']]
        rows=[{'internal_id':1,'participant_id':1001,'telegram_id':77,
               'full_name':'Иванво Иван Иванович','marks':{}}]
        merged, adoptions=google_sheet.merge_grid(grid,['2026-08-01'],rows)
        self.assertEqual(adoptions,[(1,8006)])
        self.assertEqual(len(merged),3)
        self.assertEqual(merged[2][0],'8006')
        self.assertEqual(merged[2][-1],'Y')

    def test_different_telegram_cannot_overwrite_existing_id(self):
        grid = [google_sheet.BASE_HEADERS + ['flag_active','01.08.2026'], ['']*7,
                ['8006','99','Иванов Иван','','','','Y']]
        with self.assertRaises(ValueError):
            google_sheet.merge_grid(grid, [], [{'internal_id':1,'participant_id':8006,
                'telegram_id':77,'full_name':'Петров Пётр Петрович','marks':{}}])

    def test_month_end_expense_and_week_groups(self):
        for nominal in (False, True):
            book, sheet = MagicMock(), MagicMock(id=12)
            with patch.object(finance_sheet, '_sheet', return_value=sheet), patch.object(finance_sheet,'_delete_column_groups'):
                finance_sheet._set_weekly_finance(book, 'test', nominal)
            values=sheet.update.call_args.kwargs['values']
            days=0; month_ends=0
            for col, label in enumerate(values[0]):
                if len(label) != 10 or label[2] != '.': continue
                from datetime import datetime
                day=datetime.strptime(label,'%d.%m.%Y').date(); days+=1
                expense=values[3][col]
                if nominal: self.assertTrue(expense.endswith('3*600'))
                elif (day+timedelta(days=1)).month != day.month:
                    self.assertIn('COUNTIF',expense);self.assertTrue(expense.endswith('*600'));month_ends+=1
                else: self.assertEqual(expense,'=0')
            self.assertEqual(days,153)
            if not nominal:self.assertEqual(month_ends,5)
            groups=[req for req in book.batch_update.call_args.args[0]['requests'] if 'addDimensionGroup' in req]
            self.assertEqual(len(groups),23)

class IdentityTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory();self.old=db.DB_PATH
        db.DB_PATH=str(Path(self.folder.name)/'db.sqlite');await db.init_db()
    async def asyncTearDown(self):
        await db.close_db();db._conn=None;db.DB_PATH=self.old;self.folder.cleanup()
    async def test_adopts_old_id_once_and_preserves_ambiguity(self):
        await db.upsert_legacy_identities([(8006,'Иванов Иван','готово')])
        pid=await db.register_participant(77,None,'Ивнов Иван Иванович','1234567')
        self.assertEqual((await db.get_participant(pid))['public_id'],8006)
        self.assertIsNone(await db.find_legacy_identity('Иванов Иван Иванович'))
        pid2=await db.register_participant(78,None,'Иванов Иван Иванович','1234568')
        self.assertNotEqual((await db.get_participant(pid2))['public_id'],8006)
        await db.upsert_legacy_identities([(8010,'Петров Пётр','готово'),(8013,'Петров Пётр','готово')])
        self.assertIsNone(await db.find_legacy_identity('Петров Пётр Петрович'))
    async def test_reserves_legacy_id_for_future_registration(self):
        await db.upsert_legacy_identities([(1001,'Иванов Иван','готово')])
        pid=await db.register_participant(77,None,'Петров Пётр Петрович','1234567')
        self.assertEqual((await db.get_participant(pid))['public_id'],1002)
