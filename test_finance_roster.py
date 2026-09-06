"""Roster synchronization must retain trainer inputs and use permanent IDs."""
import copy
import re
import unittest

import finance_roster as roster


class FinanceRosterTests(unittest.TestCase):
    def test_append_only_preserves_tariff_changes_and_same_name_distinct_people(self):
        attendance = [[], [], ['1001', '11', 'Иванов Иван Иванович', '@one', '+711'],
                      [1002, '22', 'Иванов Иван Иванович', '@two', '+722']]
        tariffs = [[1001, 'Имя изменено вручную', 9999, 'generated', 'Комментарий тренера']]
        directory = [['Имя изменено · ID 1001 · @old', '1001', 'Имя изменено']]
        originals = copy.deepcopy((attendance, tariffs, directory))
        new_tariffs, new_people = roster.missing_people(attendance, tariffs, directory)
        self.assertEqual(new_tariffs, [[1002, 'Иванов Иван Иванович', '', '', 'Новый участник: заполните тариф']])
        self.assertEqual(len(new_people), 1)
        self.assertEqual(new_people[0][1:6], [1002, 'Иванов Иван Иванович', '22', '@two', '+722'])
        self.assertEqual((attendance, tariffs, directory), originals)

    def test_zero_tariff_and_empty_tariff_are_existing_inputs_not_reseeded(self):
        attendance = [[], [], ['1001', '', 'Клиент один'], ['1002', '', 'Клиент два']]
        tariffs = [['1001', 'Клиент один', 0], [1002, 'Клиент два', '']]
        directory = [['Первый · ID 1001 · без Telegram', 1001],
                     ['Второй · ID 1002 · без Telegram', '1002']]
        self.assertEqual(roster.missing_people(attendance, tariffs, directory), ([], []))

    def test_id_types_match_and_duplicate_ids_fail(self):
        self.assertEqual(roster.keyed_rows([['1001'], [1002], [], [' 8001 ']], 0, 2),
                         {1001: 2, 1002: 3, 8001: 5})
        with self.assertRaisesRegex(ValueError, 'Дублируется ID 1001'):
            roster.keyed_rows([['1001'], [1001]], 0, 2)
        for invalid in ('10000', '999', '1e3', 'abc', '1001.5'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                roster.keyed_rows([[invalid]], 0, 2)

    def test_appending_avoids_partial_manual_rows_and_comments(self):
        # A trainer may have begun entering a tariff or note before assigning ID.
        rows = [[1001, 'ФИО', 2000], [], ['', '', 3500, '', 'Еще заполняется'], [], []]
        self.assertEqual(roster._last_row(rows, 5), 4)

    def test_purchase_id_offset_survives_name_and_username_changes(self):
        formula = roster.purchase_identity_formulas(3, 220)[0]
        pattern = r'MID\(A3;FIND\("([^\"]+)";A3\)\+(\d+);(\d+)\)'
        match = re.search(pattern, formula)
        self.assertIsNotNone(match)
        delimiter, offset, count = match.group(1), int(match.group(2)), int(match.group(3))
        for text in ['Васильев Дмитрий · ID 1001 · @Dima',
                     'Новое ФИО · ID 1001 · @newname', 'Юлия · ID 8001 · без Telegram']:
            # FIND/MID use one-based positions in both Excel and Google Sheets.
            first_excel = text.index(delimiter) + 1 + offset
            extracted = int(text[first_excel-1:first_excel-1+count])
            self.assertEqual(extracted, 8001 if '8001' in text else 1001)
        for metadata_formula in roster.purchase_identity_formulas(3, 220)[1:]:
            self.assertIn('MATCH($B3&"";', metadata_formula)
            self.assertIn("'Справочник_клиентов'!$B$2:$B$220&\"\"", metadata_formula)

    def test_tariff_alert_joins_ids_as_text_and_uses_real_attendance_row(self):
        formula = roster.tariff_alert_formulas(78, 250)
        self.assertIn('MATCH(A78&"";', formula)
        self.assertIn("'Посещения'!$A$3:$A$250&\"\"", formula)
        self.assertIn("'Посещения'!$G$3:$FC$250", formula)
        self.assertIn('COUNTIFS', formula)
        self.assertIn('"<="&TODAY()', formula)
        self.assertIn('NOT(ISNUMBER(C78))', formula)
        self.assertIn('C78<0', formula)


if __name__ == '__main__':
    unittest.main()
