from datetime import date, timedelta
from unittest import TestCase

from schedule_parser import parse_schedule


class ScheduleParserTests(TestCase):
    def parse(self, text, today=date(2026, 9, 15)):
        return parse_schedule(text, today)

    def test_complete_fourteen_session_forward(self):
        text = '''РАСПИСАНИЕ ТРЕНИРОВОК В СЕНТЯБРЕ

01.09, Вт19:30 - 21:30
02.09, Среда 19:30–21:30
05.09, Сб20:00 -22:00
07.09, Пн19:30 - 21:30
08.09, Вт19:30 - 21:30
12.09, Сб20:00 -22:00
14.09, Пн19:30 - 21:30
15.09, Вт19:30 - 21:30
19.09, Сб20:00 -22:00
21.09, Пн19:30 - 21:30
22.09, Вт19:30 - 21:30
26.09, Сб20:00 -22:00
28.09, Пн19:30 - 21:30
29.09, Вт19:30 - 21:30'''.replace('\n', '\r\n')
        result = self.parse(text)
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['entries']), 14)
        self.assertEqual(result['entries'][6], {'date': '2026-09-14', 'time': '19:30', 'end_time': '21:30'})
        self.assertEqual(result['entries'][-1]['date'], '2026-09-29')
        self.assertEqual(result['duplicate_count'], 0)
        self.assertEqual(len(result['warnings']), 1)
        self.assertIn('2026', result['warnings'][0])
        self.assertEqual(result['entries'][0]['date'], '2026-09-01')  # Past dates stay.

    def test_explicit_year_full_weekday_and_start_only(self):
        result = self.parse('📅 Расписание тренировок на сентябрь 2026:\n• 15.09.2026, вторник 9:05\n14.09.2026 ПОНЕДЕЛЬНИК, 19:30 — 21:30')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['entries'][1], {'date': '2026-09-15', 'time': '09:05', 'end_time': None})

    def test_duplicate_collapsing_and_conflicting_end_times(self):
        result = self.parse('15.09 Вт19:30-21:30\n15.09.2026 19:30–21:30\n15.09 19:30-22:00\n15.09 19:30')
        self.assertEqual(len(result['entries']), 1)
        self.assertEqual(result['duplicate_count'], 1)
        self.assertEqual(len(result['errors']), 2)
        self.assertIn('Строка 3', result['errors'][0])
        self.assertIn('разные варианты', result['errors'][0])

    def test_same_date_different_start_is_not_duplicate(self):
        result = self.parse('15.09 20:00-22:00\n15.09 09:00-11:00')
        self.assertFalse(result['errors'])
        self.assertEqual([row['time'] for row in result['entries']], ['09:00', '20:00'])
        self.assertEqual(result['duplicate_count'], 0)

    def test_invalid_date_day_time_and_trailing_text_are_errors(self):
        cases = {
            '31.09 19:30': 'не существует',
            '29.02.2026 19:30': 'не существует',
            '15.09 Пн19:30': 'вторник',
            '15.09 Втт19:30': 'неизвестный день',
            '15.09 24:00': 'вне диапазона',
            '15.09 19:60': 'вне диапазона',
            '15.09 19.30': 'формат времени',
            '15.09 19:30, зал 2': 'лишний текст',
            '15.09.26 19:30': 'формат времени',
            '15.09 19:30-21:30-22:00': 'формат времени',
        }
        for text, error in cases.items():
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual(result['entries'], [])
                self.assertEqual(len(result['errors']), 1)
                self.assertIn(error, result['errors'][0])

    def test_overnight_and_zero_duration_are_rejected(self):
        for text in ('15.09 23:00-01:00', '15.09 19:30-19:30'):
            result = self.parse(text)
            self.assertEqual(result['entries'], [])
            self.assertIn('Ночные', result['errors'][0])

    def test_no_silent_unknown_lines_or_empty_import(self):
        result = self.parse('Расписание:\n\n14.09 19:30\nМесто уточним позже\nРасписание 14.09 в 19:30')
        self.assertEqual(len(result['entries']), 1)
        self.assertEqual(len(result['errors']), 2)
        self.assertIn('Строка 4', result['errors'][0])
        for text in ('', '\r\n \t ', 'РАСПИСАНИЕ ТРЕНИРОВОК В СЕНТЯБРЕ'):
            self.assertIn('Не найдено', self.parse(text)['errors'][0])

    def test_year_boundary_never_rolls_implicit_dates_to_next_year(self):
        result = self.parse('31.12 19:30\n01.01 19:30', today=date(2026, 12, 31))
        self.assertEqual([row['date'] for row in result['entries']], ['2026-01-01', '2026-12-31'])
        self.assertIn('2026', result['warnings'][0])
        result = self.parse('31.12.2026 19:30\n01.01.2027 19:30', today=date(2026, 12, 31))
        self.assertFalse(result['errors'])
        self.assertFalse(result['warnings'])
        self.assertEqual(result['entries'][1]['date'], '2027-01-01')

    def test_heading_year_does_not_silently_override_date_year(self):
        result = self.parse('Расписание тренировок на январь 2027\n01.01 19:30')
        self.assertEqual(result['entries'][0]['date'], '2026-01-01')
        self.assertEqual(len(result['warnings']), 2)
        self.assertIn('заголовке', result['warnings'][1])

    def test_leap_year_and_all_seven_weekdays(self):
        result = self.parse('29.02.2028 Вт 19:30')
        self.assertFalse(result['errors'])
        names = ['Пн.', 'Вторник', 'Ср', 'Четверг', 'Пятница', 'Суббота', 'Вс.']
        lines = [f'{date(2026, 9, 14) + timedelta(days=i):%d.%m.%Y}, {name} 19:30' for i, name in enumerate(names)]
        result = self.parse('\n'.join(lines))
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['entries']), 7)

    def test_limit_counts_unique_sessions(self):
        lines = [f'{date(2026, 9, 1) + timedelta(days=i):%d.%m.%Y} 19:30' for i in range(100)]
        result = self.parse('\n'.join(lines + [lines[0]] * 2))
        self.assertEqual(len(result['entries']), 100)
        self.assertEqual(result['duplicate_count'], 2)
        self.assertFalse(result['errors'])
        result = self.parse('\n'.join(lines + ['31.12.2026 19:30']))
        self.assertEqual(len(result['entries']), 101)
        self.assertIn('не более 100', result['errors'][0])

    def test_long_bad_text_does_not_expand_telegram_diagnostic(self):
        result = self.parse('15.09 ' + 'А' * 10000 + ' 19:30')
        self.assertEqual(len(result['errors']), 1)
        self.assertLess(len(result['errors'][0]), 200)
        self.assertTrue(all(len(warning) <= 200 for warning in result['warnings']))

    def test_span_checks_366_day_boundary(self):
        self.assertFalse(self.parse('01.01.2026 19:30\n02.01.2027 19:30')['errors'])
        result = self.parse('01.01.2026 19:30\n03.01.2027 19:30')
        self.assertIn('366', result['errors'][0])
