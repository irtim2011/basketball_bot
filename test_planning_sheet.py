"""Planning projections never invent attendance, history, or trainer-owned Tier."""
import copy
import re
import unittest
from unittest.mock import patch

import finance_roster
import planning_sheet as view


def snapshot():
    return {'generated_at': '2026-09-17T18:00:00+03:00', 'enabled_at': '2026-09-15T00:00:00+03:00',
            'participants': [{'id': 1, 'public_id': 1001, 'full_name': 'Иванов Иван Иванович', 'username': 'ivan'}],
            'sessions': [{'key': '2026-09-17T16:00:00+00:00', 'starts_at': '2026-09-17T19:00:00+03:00',
                          'end_time': '21:00', 'schedule_id': 1, 'cancelled': False}],
            'answers': [], 'changes': []}


def answer(data, kind, status, stamp='2026-09-16T12:00:00+03:00', **extra):
    data['answers'].append(dict(participant_id=1, key=data['sessions'][0]['key'],
                               kind=kind, status=status, responded_at=stamp, **extra))


def change(data, old, new, stamp, from_stage='main', to_stage='main'):
    data['changes'].append(dict(participant_id=1, key=data['sessions'][0]['key'],
                               from_stage=from_stage, to_stage=to_stage,
                               old_status=old, new_status=new, changed_at=stamp))


def overview(data):
    return view.project(data)[view.GENERAL]['values'][2]


class ProjectionTests(unittest.TestCase):
    def test_precedence_and_pending_is_not_no(self):
        data = snapshot()
        answer(data, 'month', 'yes')
        answer(data, 'week', 'no')
        answer(data, 'main', 'pending', None)
        projected = view.project(data)
        detail = projected[view.DETAILS]['values'][2]
        self.assertEqual(detail[4:8], ['Y', 'N', 'N', 'N'])
        self.assertEqual(projected[view.MONTHLY]['values'][2][4], 'Y')
        self.assertEqual(projected[view.WEEKLY]['values'][2][4], 'N')
        answer(data, 'main', 'yes', '2026-09-17T16:00:00+03:00')
        self.assertEqual(view.project(data)[view.DETAILS]['values'][2][7], 'Y')
        self.assertEqual(view.effective({'month': {'status': 'yes'}, 'week': {'status': 'pending'}}), ('yes', 'month'))

    def test_yes_no_yes_keeps_history_but_net_composition_is_unchanged(self):
        data = snapshot()
        answer(data, 'main', 'yes', '2026-09-17T17:00:00+03:00')
        change(data, 'pending', 'yes', '2026-09-16T12:00:00+03:00', None)
        change(data, 'yes', 'no', '2026-09-17T12:00:00+03:00')
        change(data, 'no', 'yes', '2026-09-17T17:00:00+03:00')
        result = view.project(data)
        self.assertEqual(result[view.GENERAL]['values'][2][3:7], [1, 0, 0, 0])
        self.assertEqual(result[view.DETAILS]['values'][2][6:9], ['Y', 'Y', 'без изменений'])
        self.assertEqual(len(result[view.DETAILS]['values'][2][9].splitlines()), 3)

    def test_first_response_with_complete_journal_counts_named_addition(self):
        data = snapshot()
        answer(data, 'main', 'yes', '2026-09-17T17:00:00+03:00')
        change(data, 'pending', 'yes', '2026-09-17T17:00:00+03:00', None)
        row = overview(data)
        self.assertEqual(row[3:7], [1, 1, 0, 0])
        self.assertEqual(row[7], 'Иванов Иван Иванович')

    def test_unknown_legacy_and_deploy_after_cutoff_do_not_invent_addition(self):
        data = snapshot()
        answer(data, 'main', 'yes', '2026-09-17T17:00:00+03:00')
        self.assertEqual(overview(data)[3:7], [1, 0, 0, 1])
        change(data, 'pending', 'yes', '2026-09-17T17:00:00+03:00', None)
        data['enabled_at'] = '2026-09-17T00:00:00+03:00'
        self.assertEqual(overview(data)[3:7], [1, 0, 0, 1])
        data['answers'][0]['responded_at'] = '2026-09-16T12:00:00+03:00'
        data['changes'] = []
        self.assertEqual(view.project(data)[view.DETAILS]['values'][2][6], 'Y')

    def test_old_lower_stage_does_not_override_ledger_baseline(self):
        data = snapshot()
        answer(data, 'month', 'yes', '2026-09-16T12:00:00+03:00')
        answer(data, 'main', 'yes', '2026-09-17T17:00:00+03:00')
        change(data, 'no', 'yes', '2026-09-17T17:00:00+03:00')
        self.assertEqual(view.project(data)[view.DETAILS]['values'][2][6], 'N')
        self.assertEqual(overview(data)[4], 1)

    def test_unlogged_legacy_reset_is_unknown_not_a_fabricated_refusal(self):
        data = snapshot()
        answer(data, 'week', 'no', '2026-09-17T17:00:00+03:00')
        change(data, 'pending', 'yes', '2026-09-16T12:00:00+03:00', None, 'week')
        # A previous version cancelled/reset the poll without recording Y→pending.
        change(data, 'pending', 'no', '2026-09-17T17:00:00+03:00', None, 'week')
        self.assertEqual(overview(data)[3:7], [0, 0, 0, 1])
        self.assertEqual(view.project(data)[view.DETAILS]['values'][2][6], 'нет истории')

    def test_before_final_day_no_speculative_changes(self):
        data = snapshot()
        data['generated_at'] = '2026-09-16T18:00:00+03:00'
        answer(data, 'month', 'yes')
        self.assertEqual(overview(data)[3:7], [1, 0, 0, 0])
        self.assertEqual(view.project(data)[view.DETAILS]['values'][2][8], '24ч ещё не начались')

    def test_changes_after_training_start_are_history_not_final_day_delta(self):
        data = snapshot()
        data['generated_at'] = '2026-09-17T22:00:00+03:00'
        answer(data, 'main', 'no', '2026-09-17T21:00:00+03:00')
        change(data, 'pending', 'yes', '2026-09-16T12:00:00+03:00', None)
        change(data, 'yes', 'no', '2026-09-17T21:00:00+03:00')
        self.assertEqual(overview(data)[3:7], [0, 0, 0, 0])
        detail = view.project(data)[view.DETAILS]['values'][2]
        self.assertEqual(detail[6:9], ['Y', 'N', 'без изменений'])
        self.assertIn('17.09 21:00', detail[9])

    def test_cancelled_answers_and_sessions_never_count(self):
        data = snapshot()
        answer(data, 'main', 'yes', cancelled=True)
        self.assertEqual(overview(data)[3], 0)
        answer(data, 'month', 'yes')
        data['sessions'][0]['cancelled'] = True
        self.assertEqual(overview(data)[1:7], [0]*6)
        result = view.project(data)
        self.assertEqual(result[view.MONTHLY]['values'][2][4], 'отменена')

    def test_matrix_separates_same_day_and_caps_at_next_month(self):
        data = snapshot()
        for key, start in [('later', '2026-09-17T20:00:00+03:00'),
                           ('last', '2026-10-31T23:00:00+03:00'),
                           ('outside', '2026-11-01T00:00:00+03:00'),
                           ('past', '2026-09-17T10:00:00+03:00')]:
            data['sessions'].append(dict(key=key, starts_at=start, end_time=None, schedule_id=2, cancelled=False))
        matrix = view.project(data)[view.MONTHLY]
        self.assertEqual(matrix['layout'], [data['sessions'][0]['key'], 'later', 'last'])
        self.assertEqual(len(matrix['values'][0]), 7)
        self.assertIn('Чт, 17.09.2026\n19:00–21:00', matrix['values'][0][4])
        self.assertEqual([(g['start'], g['end']) for g in matrix['groups']], [(4, 6), (6, 7)])

    def test_tier_joins_permanent_id_preserves_blank_and_formula_like_names_are_literal(self):
        data = snapshot()
        data['participants'][0]['full_name'] = '=IMPORTXML("bad")'
        row = view.project(data, 850)[view.MONTHLY]['values'][2]
        self.assertIn("'Справочник_клиентов'!$I$2:$I$850", row[3])
        self.assertIn('MATCH($A3&"";', row[3])
        self.assertIn('$B$2:$B$850&""', row[3])
        self.assertIn('="";"";', row[3])
        self.assertIn('stringValue', view._cell(row[1])['userEnteredValue'])
        self.assertIn('formulaValue', view._cell(row[3])['userEnteredValue'])


class FakeSheet:
    def __init__(self, title, rows, cols, values, sheet_id=7):
        self.title, self.id, self.row_count, self.col_count = title, sheet_id, rows, cols
        self.values = copy.deepcopy(values)

    def resize(self, rows=None, cols=None):
        self.row_count, self.col_count = rows or self.row_count, cols or self.col_count

    def get(self, area, **kwargs):
        def coord(value):
            letters, row = re.match(r'([A-Z]+)(\d+)', value).groups()
            col = 0
            for c in letters:
                col = col*26+ord(c)-64
            return int(row)-1, col-1
        first, _, last = area.partition(':')
        r1, c1 = coord(first)
        r2, c2 = coord(last or first)
        rows = [[view._at(self.values, r, c) for c in range(c1, c2+1)] for r in range(r1, r2+1)]
        for row in rows:
            while row and row[-1] == '':
                row.pop()
        while rows and not rows[-1]:
            rows.pop()
        return rows


class FakeBook:
    id = 'test-book'

    def __init__(self, directory):
        self.sheets = [directory]
        self.requests = []

    def worksheet(self, name):
        return next(s for s in self.sheets if s.title == name)

    def worksheets(self):
        return self.sheets

    def add_worksheet(self, title, rows, cols):
        sheet = FakeSheet(title, rows, cols, [], len(self.sheets)+20)
        self.sheets.append(sheet)
        return sheet

    def fetch_sheet_metadata(self):
        return {'sheets': [{'properties': {'sheetId': s.id}} for s in self.sheets]}

    def batch_update(self, body):
        self.requests.extend(body['requests'])
        for request in body['requests']:
            if 'updateCells' not in request:
                continue
            part = request['updateCells']
            start = part['start']
            sheet = next(s for s in self.sheets if s.id == start['sheetId'])
            for roff, row in enumerate(part['rows']):
                ri = start.get('rowIndex', 0)+roff
                while len(sheet.values) <= ri:
                    sheet.values.append([])
                for coff, cell in enumerate(row['values']):
                    ci = start.get('columnIndex', 0)+coff
                    while len(sheet.values[ri]) <= ci:
                        sheet.values[ri].append('')
                    sheet.values[ri][ci] = next(iter(cell.get('userEnteredValue', {}).values()), '')


class PersistenceTests(unittest.TestCase):
    def directory(self, header='', tiers=None):
        tiers = tiers or ['', '']
        return FakeSheet('Справочник_клиентов', 20, 9,
                         [['']*8+[header], ['one', 1001]+['']*6+[tiers[0]], ['two', '1002']+['']*6+[tiers[1]]])

    def test_tier_initializes_once_and_preserves_manual_blank_and_b(self):
        book = FakeBook(self.directory())
        self.assertTrue(finance_roster.ensure_tier(book))
        self.assertEqual([r[8] for r in book.sheets[0].values], ['Tier', 'A', 'A'])
        book.sheets[0].values[1][8] = 'B'
        book.sheets[0].values[2][8] = ''
        writes = len(book.requests)
        self.assertFalse(finance_roster.ensure_tier(book))
        self.assertEqual(len(book.requests), writes)
        self.assertEqual([r[8] for r in book.sheets[0].values], ['Tier', 'B', ''])
        _, new = finance_roster.missing_people([[], [], [1003, '', 'Новый Клиент']], [], [])
        self.assertEqual(new[0][8], 'A')

    def test_tier_refuses_unknown_header_before_any_write(self):
        book = FakeBook(self.directory('Заметки'))
        with self.assertRaisesRegex(ValueError, 'заголовок не Tier'):
            finance_roster.ensure_tier(book)
        self.assertEqual(book.requests, [])

    def test_diff_clears_only_union_of_old_and_new_managed_rectangles(self):
        current = [['old']*5 for _ in range(10)]
        current[7] += ['', '', 'trainer note']  # outside old10x5 and new3x8
        target = [['new']*8 for _ in range(3)]
        updates, count = view._diff(7, current, target, 10, 5)
        book = FakeBook(FakeSheet('x', 20, 10, current))
        book.batch_update({'requests': updates})
        self.assertEqual(book.sheets[0].values[7][7], 'trainer note')
        self.assertEqual(book.sheets[0].values[7][:5], ['']*5)
        self.assertEqual(book.sheets[0].values[:3], target)
        self.assertEqual(count, 59)
        retry, _ = view._diff(7, book.sheets[0].values, target, 10, 8, [[10, 5], [3, 8]])
        self.assertEqual(retry, [])

    def test_sync_second_pass_preserves_notes_formats_and_manual_tier(self):
        book = FakeBook(self.directory('Tier', ['B', '']))
        states = {}
        def state(book_id, sheet_id, value=None):
            if value is not None:
                states[sheet_id] = copy.deepcopy(value)
            return states.get(sheet_id, {})
        with patch.object(view, '_state', side_effect=state):
            result = view.sync_views(book, snapshot())
            self.assertGreater(result['changed_cells'], 0)
            details = book.worksheet(view.DETAILS)
            details.values[2] += ['ручная заметка справа']
            book.requests = []
            second = view.sync_views(book, snapshot())
        self.assertEqual(second['changed_cells'], 0)
        self.assertEqual(book.requests, [])
        self.assertEqual(details.values[2][11], 'ручная заметка справа')
        self.assertEqual(book.sheets[0].values[1][8], 'B')
        self.assertEqual(book.sheets[0].values[2][8], '')

    def test_growth_formats_only_new_columns_and_extends_conditional_rules(self):
        book = FakeBook(self.directory('Tier', ['B', '']))
        states = {}
        def state(book_id, sheet_id, value=None):
            if value is not None:
                states[sheet_id] = copy.deepcopy(value)
            return states.get(sheet_id, {})
        data = snapshot()
        with patch.object(view, '_state', side_effect=state):
            view.sync_views(book, data)
            matrix = book.worksheet(view.MONTHLY)
            book.requests = []
            data['sessions'].append(dict(key='second', starts_at='2026-09-18T19:00:00+03:00',
                                         end_time=None, schedule_id=2, cancelled=False))
            view.sync_views(book, data)
        headers = [r['repeatCell']['range'] for r in book.requests if 'repeatCell' in r
                   and r['repeatCell']['range']['sheetId'] == matrix.id
                   and r['repeatCell']['range']['startRowIndex'] == 0]
        self.assertEqual([(r['startColumnIndex'], r['endColumnIndex']) for r in headers], [(5, 6)])
        rules = [r['addConditionalFormatRule']['rule'] for r in book.requests if 'addConditionalFormatRule' in r
                 and r['addConditionalFormatRule']['rule']['ranges'][0]['sheetId'] == matrix.id]
        self.assertEqual(len(rules), 2)
        self.assertEqual([r['ranges'][0]['startColumnIndex'] for r in rules], [5, 5])
        self.assertEqual([r['ranges'][0]['endColumnIndex'] for r in rules], [6, 6])

    def test_detail_filter_is_open_ended_and_not_replaced_when_rows_grow(self):
        book = FakeBook(self.directory('Tier', ['B', '']))
        states = {}
        def state(book_id, sheet_id, value=None):
            if value is not None:
                states[sheet_id] = copy.deepcopy(value)
            return states.get(sheet_id, {})
        data = snapshot()
        with patch.object(view, '_state', side_effect=state):
            view.sync_views(book, data)
            filters = [r['setBasicFilter']['filter'] for r in book.requests if 'setBasicFilter' in r]
            self.assertEqual(len(filters), 1)
            self.assertNotIn('endRowIndex', filters[0]['range'])
            # Simulate the trainer selecting a date and sorting by participant.
            manual = dict(filters[0], criteria={'0': {'hiddenValues': ['another date']}},
                          sortSpecs=[{'dimensionIndex': 2, 'sortOrder': 'ASCENDING'}])
            original = copy.deepcopy(manual)
            book.requests = []
            data['participants'].append(dict(id=2, public_id=1002, full_name='Петров Петр Петрович', username='petr'))
            view.sync_views(book, data)
        self.assertFalse(any('setBasicFilter' in r or 'clearBasicFilter' in r for r in book.requests))
        self.assertEqual(manual, original)
        details = book.worksheet(view.DETAILS)
        row_formats = [r['repeatCell']['range'] for r in book.requests if 'repeatCell' in r
                       and r['repeatCell']['range']['sheetId'] == details.id]
        self.assertEqual([(r['startRowIndex'], r['endRowIndex']) for r in row_formats], [(3, 4)])


if __name__ == '__main__':
    unittest.main()
