import unittest
from unittest.mock import MagicMock
import finance_views as views


class ReadableFinanceTests(unittest.TestCase):
    def test_every_day_and_month_is_covered_once(self):
        offsets = [d for _, first, count in views.months() for d in range(first, first+count)]
        self.assertEqual(offsets, list(range(153)))
        nominal = views.nominal_values(200, 200)
        self.assertEqual(nominal[-1][0], '=IF(\'Посещения\'!A200="";"";\'Посещения\'!A200)')
        self.assertIn('DATE(2026;8;1)+152', nominal[12][-1])
        for r in range(19, len(nominal)):
            self.assertEqual(len(nominal[r]), 159)
        actual = views.actual_values(200, 200, 500)
        for formula in [v for row in actual for v in row if isinstance(v, str) and v.startswith('=')]:
            self.assertNotIn('SUMPRODUCT', formula)
        # All five client-level rent counts stop at today using real date cells.
        for m in range(5):
            self.assertIn('TODAY()', actual[19][8+5*m])
            self.assertIn('COUNTIFS', actual[19][8+5*m])
        # Duplicated purchase rows aggregate by stable ID, not row number or name.
        self.assertIn("SUMIF('Покупки тарифов'!$B$3:$B$500;$A20", actual[19][6])
        self.assertEqual(actual[19][7], '=IF($A20="";"";IF(G20=0;0;IF(LEN($C20)=0;"нет тарифа";G20*$C20)))')

    def test_mirror_delegates_without_destructive_copy(self):
        from unittest.mock import patch
        book = MagicMock()
        with patch('attendance_sync.reconcile_book', return_value=[]) as reconcile:
            views.mirror(book)
        reconcile.assert_called_once_with(book, None, None)
        book.batch_update.assert_not_called()
