import unittest
from unittest.mock import MagicMock
import finance_views as views


class ReadableFinanceTests(unittest.TestCase):
    def test_every_day_and_month_is_covered_once(self):
        offsets = [d for _, first, count in views.months() for d in range(first, first+count)]
        self.assertEqual(offsets, list(range(153)))
        nominal = views.nominal_values(200, 200)
        self.assertEqual(nominal[-1][0], '=IF(\'Посещения_bot\'!A200="";"";\'Посещения_bot\'!A200)')
        self.assertIn('DATE(2026;8;1)+152', nominal[12][-1])
        for r in range(19, len(nominal)):
            self.assertEqual(len(nominal[r]), 159)
        actual = views.actual_values(200, 200, 500)
        for formula in [v for row in actual for v in row if isinstance(v, str) and v.startswith('=')]:
            self.assertNotIn('SUMPRODUCT', formula)
            self.assertLess(len(formula), 190)
        # Duplicated purchase rows aggregate by stable ID, not row number or name.
        self.assertIn("SUMIF('Покупки тарифов'!$B$3:$B$500;$A20", actual[19][6])
        self.assertEqual(actual[19][7], '=IF($A20="";"";IF(G20=0;0;IF($C20="";"нет тарифа";G20*$C20)))')

    def test_exact_copy_keeps_grid_and_dimensions_and_formulas(self):
        book = MagicMock()
        source, target = MagicMock(id=1,row_count=200,col_count=401), MagicMock(id=2)
        book.worksheet.side_effect = [source, target]
        book.fetch_sheet_metadata.return_value = {'sheets': [
            {'properties': {'sheetId':1, 'gridProperties': {'rowCount':200,'columnCount':401,
             'frozenRowCount':2,'frozenColumnCount':6}},
             'data': [{'columnMetadata':[{'pixelSize':85},{'pixelSize':125},{'pixelSize':260}],
                       'rowMetadata':[{'pixelSize':45}]}]},
            {'properties': {'sheetId':2}, 'conditionalFormats':[{},{}]}]}
        views.mirror(book)
        requests=book.batch_update.call_args.args[0]['requests']
        paste=next(r['copyPaste'] for r in requests if 'copyPaste' in r)
        self.assertEqual(paste['pasteType'], 'PASTE_NORMAL')
        self.assertEqual(paste['destination']['endColumnIndex'], 401)
        grid=next(r['updateSheetProperties']['properties']['gridProperties'] for r in requests if 'updateSheetProperties' in r)
        self.assertEqual(grid['frozenColumnCount'],6)
        widths=[r['updateDimensionProperties']['properties'].get('pixelSize') for r in requests if 'updateDimensionProperties' in r]
        self.assertIn(260,widths)
        self.assertIn(45,widths)
