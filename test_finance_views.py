import unittest
from unittest.mock import MagicMock, patch
import finance_sheet as finance
import google_sheet

class FinanceViewsTests(unittest.TestCase):
    def test_nominal_dates_totals_and_id_based_tariffs(self):
        book, sheet = MagicMock(), MagicMock(id=12)
        with patch.object(finance, '_reset_view', return_value=sheet):
            finance._set_nominal_finance(book)
        values=sheet.update.call_args.kwargs['values']
        self.assertEqual(len(values),154)
        self.assertEqual(values[0][:2],['Дата','День'])
        self.assertIn('Аналитика_тех',values[1][2])
        self.assertEqual(values[-1][0],'=DATE(2026;8;1)+152')
        self.assertEqual(values[1][4],'=D2*600')
        self.assertEqual(values[7][7],'ИТОГО ПО СЕКЦИИ')
        self.assertEqual(values[5][8],'=SUM(C124:C154)')

    def test_actual_only_five_months_and_total(self):
        book, sheet=MagicMock(),MagicMock(id=13)
        with patch.object(finance,'_reset_view',return_value=sheet):finance._set_monthly_profit(book)
        values=sheet.update.call_args.kwargs['values']
        self.assertEqual(len(values),8)
        self.assertEqual(values[-1][0],'ИТОГО')
        self.assertEqual(values[1][1].count('SUMPRODUCT'),31)
        self.assertEqual(values[2][1].count('SUMPRODUCT'),30)
        self.assertEqual(values[1][3],'=C2*600')

    def test_copy_resets_exact_used_grid_before_paste(self):
        book=MagicMock()
        with patch('finance_views.mirror') as mirror:
            finance.restore_attendance_copy(book)
        mirror.assert_called_once_with(book)

    def test_reset_removes_old_contents_and_groups(self):
        book, sheet=MagicMock(),MagicMock(id=2)
        book.worksheet.return_value=sheet
        book.fetch_sheet_metadata.return_value={'sheets':[{'properties':{'sheetId':2},
            'columnGroups':[{'range':{'sheetId':2,'dimension':'COLUMNS','startIndex':2,'endIndex':8}}]}]}
        finance._reset_view(book,'Посещения',81,159)
        requests=book.batch_update.call_args.args[0]['requests']
        self.assertIn('deleteDimensionGroup',requests[0])
        clearing=next(r['updateCells'] for r in requests if 'updateCells' in r)
        self.assertIn('userEnteredValue',clearing['fields'])
        size=next(r['updateSheetProperties']['properties']['gridProperties'] for r in requests if 'updateSheetProperties' in r)
        self.assertEqual((size['rowCount'],size['columnCount']),(81,159))

    def test_successful_sync_reconciles_before_adding_roster(self):
        worksheet=MagicMock(row_count=200,col_count=159)
        calls=[]
        with patch.object(google_sheet,'_worksheet',return_value=worksheet), \
                patch('attendance_sync.workbook_lock'), \
                patch('attendance_sync.reconcile_book',side_effect=lambda *args: calls.append('reconcile') or [(1,8001)]) as reconcile, \
                patch('finance_views.sync_roster',side_effect=lambda *args: calls.append('roster')) as refresh:
            self.assertEqual(google_sheet._sync_blocking([],[]),[(1,8001)])
        reconcile.assert_called_once_with(worksheet.spreadsheet,[],[])
        refresh.assert_called_once_with(worksheet.spreadsheet)
        self.assertEqual(calls,['reconcile','roster'])

class ExportAuditTests(unittest.TestCase):
    def test_detects_external_and_missing_sheet_references(self):
        import tempfile
        from pathlib import Path
        from openpyxl import Workbook
        import audit_xlsx
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'audit.xlsx'
            book=Workbook();book.remove(book.active)
            for title in sorted(audit_xlsx.REQUIRED):book.create_sheet(title)
            sheet=book['Фактическая прибыль']
            sheet['A1']="='Тарифы'!C2"
            book.save(path)
            self.assertTrue(audit_xlsx.audit(path)['passed'])
            sheet['A2']="='Missing'!A1"
            sheet['A3']="='[book.xlsx]Тарифы'!A1"
            book.save(path)
            result=audit_xlsx.audit(path)
            self.assertFalse(result['passed'])
            self.assertTrue(any('missing sheet Missing' in error for error in result['errors']))
            self.assertTrue(any('external or broken' in error for error in result['errors']))
            book.close()
