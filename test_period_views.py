import copy
import unittest
import period_views
import planning_sheet as view
from test_planning_sheet import snapshot, answer

class Calendars(unittest.TestCase):
    def test_day_counts_and_portable_formulas(self):
        result=period_views.build(snapshot(),220)
        self.assertEqual(len(result[view.WEEKLY]['values'][2])-4,7)
        self.assertEqual(len(result[view.MONTHLY]['values'][2])-4,31)
        self.assertTrue(all('DAY(EOMONTH(' in f for f in result[view.MONTHLY]['values'][2][4:]))
        self.assertIn('2028-02',period_views.build(dict(snapshot(),generated_at='2027-12-01T10:00:00+03:00'),220)[view.MONTHLY]['options'])
        self.assertIn('2026-09-28',result[view.WEEKLY]['options'])
        self.assertTrue(all('IMPORTRANGE' not in str(row) for row in result[view.MONTHLY]['values']))

    def test_cancelled_and_legacy_sessions_are_not_schedule(self):
        data=snapshot()
        for key,extra in [('cancel',{'cancelled':True}),('legacy',{'in_schedule':False})]:
            data['sessions'].append(dict(data['sessions'][0],key=key,starts_at='2026-09-18T19:00:00+03:00',**extra))
        raw=period_views.build(data,220)[period_views.DATA]['values']
        self.assertFalse(any('2026-09-18' in row[0] for row in raw))

    def test_two_sessions_same_day_keep_separate_answers_and_time(self):
        data=snapshot();answer(data,'month','yes')
        data['sessions'].append(dict(data['sessions'][0],key='second',starts_at='2026-09-17T21:00:00+03:00'))
        data['answers'].append(dict(participant_id=1,key='second',kind='month',status='no',responded_at=None))
        raw=dict(period_views.build(data,220)[period_views.DATA]['values'][1:])
        self.assertEqual(raw['1001|2026-09-17|month'],'19:00 Y\n21:00 N')
        self.assertEqual(raw['1001|2026-09-17|week'],'19:00 —\n21:00 —')
