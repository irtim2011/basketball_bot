"""Independent live-data reconciliation, without messaging Telegram users."""
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

from finance_sheet import _client
from config import GOOGLE_SHEET_ID
import audit_xlsx
import google_sheet


def attendance_snapshot(raw):
    """Key by permanent ID and actual date, so a trainer may reorder rows."""
    days = [datetime.strptime(str(v), '%d.%m.%Y').date() for v in raw[0][6:]]
    people = {}
    for row in raw[2:]:
        if not row or row[0] in ('', None):
            continue
        pid = str(int(row[0]))
        assert pid not in people, ('duplicate attendance ID', pid)
        marks = {day: str(row[i+6]).strip().upper() if i+6 < len(row) else ''
                 for i, day in enumerate(days)}
        assert set(marks.values()) <= {'', 'Y', 'N'}, ('invalid attendance', pid)
        people[pid] = {'row': row, 'marks': marks}
    return people


def month_outcome(marks, tariff, purchased, today, month):
    """Numeric oracle: past/today Y incur rent even when income is unknown."""
    count = sum(mark == 'Y' for day, mark in marks.items()
                if day.year == 2026 and day.month == month and day <= today)
    price = 0 if tariff in ('', None) else float(tariff)
    rent = count * 600
    return count, count * price, purchased * price, rent, purchased * price - rent


def verify(folder):
    book = _client().open_by_key(GOOGLE_SHEET_ID)
    checks = {}
    today = datetime.now(ZoneInfo('Europe/Moscow')).date()
    src, dst = book.worksheet('Посещения_bot'), book.worksheet('Посещения')
    source_raw = src.get(f'A1:FC{src.row_count}', value_render_option='UNFORMATTED_VALUE')
    public_raw = dst.get(f'A1:FC{dst.row_count}', value_render_option='UNFORMATTED_VALUE')
    internal, people = attendance_snapshot(source_raw), attendance_snapshot(public_raw)
    assert set(internal) == set(people), 'Attendance IDs differ after synchronization'
    for pid, person in people.items():
        assert internal[pid]['marks'] == person['marks'], (pid, 'attendance differs after sync')
        assert str(internal[pid]['row'][1]) == str(person['row'][1]), (pid, 'Telegram owner differs')
    checks['attendance_matches_by_id_and_date'] = True

    tariff_sheet = book.worksheet('Тарифы')
    prices = {}
    for row in tariff_sheet.get(f'A2:C{tariff_sheet.row_count}', value_render_option='UNFORMATTED_VALUE'):
        if not row or row[0] in ('', None):
            continue
        pid = str(int(row[0]))
        assert pid not in prices, ('duplicate tariff ID', pid)
        prices[pid] = row[2] if len(row) > 2 else ''
    purchase_sheet = book.worksheet('Покупки тарифов')
    purchases = purchase_sheet.get(f'A3:FD{purchase_sheet.row_count}', value_render_option='UNFORMATTED_VALUE')
    bought = defaultdict(float)
    for row in purchases:
        has_purchases = any(v not in ('', None, 0) for v in row[7:160])
        if len(row) < 2 or row[1] in ('', None):
            assert not has_purchases, ('purchase has no resolved ID', row[:2])
            continue
        pid = str(int(row[1]))
        assert pid in people, ('purchase ID missing from attendance', pid)
        for i, value in enumerate(row[7:160]):
            if value not in ('', None):
                month = (date(2026, 8, 1) + timedelta(days=i)).month
                bought[pid, month] += float(value)

    nominal_sheet, actual_sheet = book.worksheet('Номинальная доходность'), book.worksheet('Фактическая прибыль')
    nominal = nominal_sheet.get(f'A20:FC{nominal_sheet.row_count}', value_render_option='UNFORMATTED_VALUE')
    actual = actual_sheet.get(f'A20:AE{actual_sheet.row_count}', value_render_option='UNFORMATTED_VALUE')
    nominal_by = {str(int(r[0])): r for r in nominal if r and r[0] not in ('', None)}
    actual_by = {str(int(r[0])): r for r in actual if r and r[0] not in ('', None)}
    assert set(nominal_by) == set(people), 'Nominal report misses or adds clients'
    assert set(actual_by) == set(people), 'Actual report misses or adds clients'
    assert len(nominal_by) == sum(bool(r and r[0] not in ('', None)) for r in nominal), 'Duplicate nominal IDs'
    assert len(actual_by) == sum(bool(r and r[0] not in ('', None)) for r in actual), 'Duplicate actual IDs'
    monthly = defaultdict(lambda: [0, 0, 0])
    for pid, person in people.items():
        tariff = prices.get(pid, '')
        n_row, a_row = nominal_by[pid], actual_by[pid]
        assert n_row[2] == tariff, (pid, 'nominal tariff')
        assert a_row[2] == tariff, (pid, 'actual tariff')
        for m in range(8, 13):
            count, n_income, a_income, rent, profit = month_outcome(person['marks'], tariff, bought[pid, m], today, m)
            indices = [i for i in range(153) if (date(2026, 8, 1) + timedelta(days=i)).month == m]
            for i in indices:
                day = date(2026, 8, 1) + timedelta(days=i)
                marked = person['marks'].get(day) == 'Y' and day <= today
                expected = ('нет тарифа' if tariff in ('', None) else tariff) if marked else 0
                assert n_row[i+6] == expected, (pid, day.isoformat(), 'nominal daily', n_row[i+6], expected)
            assert sum(float(n_row[i+6]) for i in indices if isinstance(n_row[i+6], (int, float))) == n_income, (pid, m, 'nominal')
            start = 6 + 5 * (m-8)
            assert float(a_row[start]) == bought[pid, m], (pid, m, 'bought')
            expected_cash = 'нет тарифа' if tariff in ('', None) and bought[pid, m] else a_income
            assert a_row[start+1] == expected_cash, (pid, m, 'cash')
            assert a_row[start+2:start+5] == [count, rent, profit], (pid, m, 'visits/rent/profit')
            monthly[m][0] += count
            monthly[m][1] += n_income
            monthly[m][2] += a_income
    checks['client_months_reconciled'] = len(people) * 5
    ns = nominal_sheet.get('A5:E9', value_render_option='UNFORMATTED_VALUE')
    ac = actual_sheet.get('A5:F9', value_render_option='UNFORMATTED_VALUE')
    for idx, m in enumerate(range(8, 13)):
        visits, nom, cash = monthly[m]
        assert ns[idx][1:] == [visits, nom, visits*600, nom-visits*600], ('nominal total', m, ns[idx])
        assert ac[idx][2:] == [visits, cash, visits*600, cash-visits*600], ('actual total', m, ac[idx])
        assert ac[idx][5] == ac[idx][3] - ac[idx][4], ('actual accounting identity', m)
    checks['monthly_totals'] = dict(monthly)

    run_sheet = book.worksheet('RUN')
    run = run_sheet.get(f'A7:G{run_sheet.row_count}', value_render_option='UNFORMATTED_VALUE')
    run_by = {str(int(r[0])): r for r in run if r and r[0] not in ('', None)}
    expected = set()
    for pid, person in people.items():
        visits = sum(mark == 'Y' for day, mark in person['marks'].items()
                     if today-timedelta(days=29) <= day <= today)
        if visits:
            expected.add(pid)
            rr = run_by[pid]
            assert rr[3] == visits, (pid, 'RUN visits')
            assert (rr[4] if len(rr) > 4 else '') == prices.get(pid, ''), (pid, 'RUN price')
            assert str(rr[6]).startswith('Тарифы!C'), (pid, 'RUN tariff source')
    assert set(run_by) == expected, 'RUN active client set'
    checks['RUN_last_30_days_clients'] = len(expected)
    out = Path(folder) / 'verified-2.4.7.xlsx'
    shutil.move(google_sheet.export_workbook_xlsx(), out)
    audit = audit_xlsx.audit(out)
    assert audit['passed'], audit['errors'][:10]
    checks['xlsx_audit'] = audit
    checks['xlsx_path'] = str(out)
    Path(folder, 'verification.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2))
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    verify(sys.argv[1])
