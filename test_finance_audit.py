"""Regression cases for the financial review; live verification checks sheet values."""
import unittest
from datetime import date, timedelta

import finance_views
from verify_readable_finance import attendance_snapshot, month_outcome


class FinancialAuditTests(unittest.TestCase):
    def test_future_yes_does_not_charge_rent_or_nominal_income(self):
        today = date(2026, 9, 6)
        marks = {today-timedelta(days=1): 'Y', today: 'Y',
                 today+timedelta(days=1): 'Y', date(2026, 8, 31): 'Y',
                 date(2026, 9, 1): 'N'}
        self.assertEqual(month_outcome(marks, 2000, 4, today, 9),
                         (2, 4000, 8000, 1200, 6800))
        # Advancing the date automatically includes that next day's attendance.
        self.assertEqual(month_outcome(marks, 2000, 4, today+timedelta(days=1), 9),
                         (3, 6000, 8000, 1800, 6200))

    def test_missing_and_free_tariffs_both_keep_all_rent(self):
        today = date(2026, 9, 6)
        marks = {date(2026, 9, 1): 'Y', today: 'Y'}
        for tariff in ('', None, 0):
            with self.subTest(tariff=tariff):
                self.assertEqual(month_outcome(marks, tariff, 4, today, 9),
                                 (2, 0, 0, 1200, -1200))
        self.assertEqual(month_outcome(marks, '', 0, today, 9),
                         (2, 0, 0, 1200, -1200))

    def test_snapshot_joins_by_id_and_date_not_position(self):
        headers = ['ID', 'Telegram', 'ФИО', '', '', '', '05.09.2026', '06.09.2026']
        raw = [headers, [], ['1001', '123', 'New name', '', '', '', 'Y', 'N'],
               [8001, '', 'Legacy name', '', '', '', '', 'Y']]
        reordered = [headers[:6]+headers[6:][::-1], [],
                     raw[3][:6]+raw[3][6:][::-1], raw[2][:6]+raw[2][6:][::-1]]
        first, second = attendance_snapshot(raw), attendance_snapshot(reordered)
        self.assertEqual(set(first), set(second))
        for pid in first:
            self.assertEqual(first[pid]['marks'], second[pid]['marks'])
        with self.assertRaisesRegex(AssertionError, 'duplicate attendance ID'):
            attendance_snapshot(raw+[raw[2]])

    def test_financial_formula_guards_match_the_oracle_policy(self):
        nominal = finance_views.nominal_values(4, 20)
        actual = finance_views.actual_values(4, 20, 50)
        for offset in range(153):
            self.assertIn('<=TODAY()', nominal[19][6+offset])
            self.assertIn('>TODAY()', nominal[15][6+offset])
            self.assertIn("'Посещения'!", nominal[19][6+offset])
        for m in range(5):
            self.assertEqual(actual[4+m][5], f'=D{5+m}-E{5+m}')
            self.assertIn('COUNTIFS', actual[19][8+5*m])
            self.assertIn('"<="&TODAY()', actual[19][8+5*m])
            self.assertIn('N(', actual[19][10+5*m])
            self.assertNotIn('нет тарифа', actual[19][10+5*m])
            self.assertIn('нет тарифа', actual[19][7+5*m])


if __name__ == '__main__':
    unittest.main()
