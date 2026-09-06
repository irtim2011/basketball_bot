"""Independent live-data reconciliation, without sending messages to Telegram users."""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

from finance_sheet import _client
from config import GOOGLE_SHEET_ID
import audit_xlsx
import google_sheet


def verify(folder):
    book=_client().open_by_key(GOOGLE_SHEET_ID)
    checks={}
    src=book.worksheet('Посещения_bot')
    dst=book.worksheet('Посещения')
    a=src.get('A1:FC200',value_render_option='FORMULA')
    b=dst.get('A1:FC200',value_render_option='FORMULA')
    assert a==b, 'Attendance formulas/values differ'
    assert (src.row_count,src.col_count)==(dst.row_count,dst.col_count)
    checks['identical_attendance_values_formulas']=True
    metadata=book.fetch_sheet_metadata(params={'includeGridData':'true',
        'fields':'sheets(properties,conditionalFormats,data(startRow,startColumn,columnMetadata(pixelSize,hiddenByUser),rowMetadata(pixelSize,hiddenByUser)))'})
    get=lambda sid: next(s for s in metadata['sheets'] if s['properties']['sheetId']==sid)
    x,y=get(src.id),get(dst.id)
    assert x['properties']['gridProperties']==y['properties']['gridProperties'], 'Grid/freeze mismatch'
    for key in ['columnMetadata','rowMetadata']:
        one=x.get('data',[{}])[0].get(key,[])
        two=y.get('data',[{}])[0].get(key,[])
        default=100 if key=='columnMetadata' else 21
        norm=lambda z:[(v.get('pixelSize',default),v.get('hiddenByUser',False)) for v in z]
        size=src.col_count if key=='columnMetadata' else src.row_count
        assert norm(one)+[(default,False)]*(size-len(one))==norm(two)+[(default,False)]*(size-len(two)), key
    normrules=lambda z: json.dumps(z,ensure_ascii=False,sort_keys=True).replace(str(src.id),'SHEET').replace(str(dst.id),'SHEET')
    assert normrules(x.get('conditionalFormats',[]))==normrules(y.get('conditionalFormats',[]))
    checks['identical_dimensions_freezes_conditional_formats']=True
    raw=src.get('A1:FC200',value_render_option='UNFORMATTED_VALUE')
    people={str(row[0]):row for row in raw[2:] if row and row[0]}
    prices={str(int(row[0])):row[2] if len(row)>2 else '' for row in book.worksheet('Тарифы').get('A2:C200',value_render_option='UNFORMATTED_VALUE') if row and row[0]}
    purchases=book.worksheet('Покупки тарифов').get('A3:FD500',value_render_option='UNFORMATTED_VALUE')
    bought=defaultdict(float)
    for row in purchases:
        if len(row)<2 or not row[1]: continue
        for i,value in enumerate(row[7:160]):
            if value not in ('',None):
                month=(datetime(2026,8,1)+timedelta(days=i)).month
                bought[str(row[1]),month]+=float(value)
    nominal=book.worksheet('Номинальная доходность').get('A20:FC217',value_render_option='UNFORMATTED_VALUE')
    actual=book.worksheet('Фактическая прибыль').get('A20:AE217',value_render_option='UNFORMATTED_VALUE')
    nominal_by={str(r[0]):r for r in nominal if r and r[0]}
    actual_by={str(r[0]):r for r in actual if r and r[0]}
    monthly=defaultdict(lambda:[0,0,0])
    for pid,row in people.items():
        tariff=prices.get(pid,'')
        for m in range(8,13):
            indices=[i for i in range(153) if (datetime(2026,8,1)+timedelta(days=i)).month==m]
            count=sum(1 for i in indices if i+6<len(row) and row[i+6]=='Y')
            n_income=count*float(tariff) if tariff not in ('',None) else 0
            a_income=bought[pid,m]*float(tariff) if tariff not in ('',None) else 0
            n_row=nominal_by[pid]
            assert sum(float(n_row[i+6]) for i in indices if i+6<len(n_row) and isinstance(n_row[i+6],(int,float)))==n_income,(pid,m,'nominal')
            start=6+5*(m-8)
            actual_row=actual_by[pid]
            assert float(actual_row[start])==bought[pid,m],(pid,m,'bought')
            if tariff not in ('',None) or bought[pid,m]==0:
                assert float(actual_row[start+1])==a_income,(pid,m,'cash')
            assert float(actual_row[start+2])==count,(pid,m,'visits')
            assert float(actual_row[start+3])==count*600,(pid,m,'rent')
            monthly[m][0]+=count
            monthly[m][1]+=n_income
            monthly[m][2]+=a_income
    checks['client_months_reconciled']=len(people)*5
    ns=book.worksheet('Номинальная доходность').get('A5:E9',value_render_option='UNFORMATTED_VALUE')
    ac=book.worksheet('Фактическая прибыль').get('A5:F9',value_render_option='UNFORMATTED_VALUE')
    for idx,m in enumerate(range(8,13)):
        visits,nom,cash=monthly[m]
        assert ns[idx][1:]==[visits,nom,visits*600,nom-visits*600],('nominal total',m,ns[idx])
        assert ac[idx][2:]==[visits,cash,visits*600,cash-visits*600],('actual total',m,ac[idx])
    checks['monthly_totals']=dict(monthly)
    run=book.worksheet('RUN').get('A7:G205',value_render_option='UNFORMATTED_VALUE')
    today=datetime.now(ZoneInfo('Europe/Moscow')).date()
    expected=set()
    for pid,row in people.items():
        visits=sum(1 for i in range(153) if today-timedelta(days=29)<=(datetime(2026,8,1)+timedelta(days=i)).date()<=today and i+6<len(row) and row[i+6]=='Y')
        if visits:
            expected.add(pid)
            rr=next(r for r in run if r and str(r[0])==pid)
            assert rr[3]==visits
            assert (rr[4] if len(rr)>4 else '')==prices.get(pid,''),(pid,'RUN price')
            assert str(rr[6]).startswith('Тарифы!C')
    assert {str(r[0]) for r in run if r and r[0]}==expected
    checks['RUN_last_30_days_clients']=len(expected)
    out=Path(folder)/'verified-2.4.6.xlsx'
    shutil.move(google_sheet.export_workbook_xlsx(),out)
    audit=audit_xlsx.audit(out)
    assert audit['passed'],audit['errors'][:10]
    checks['xlsx_audit']=audit
    checks['xlsx_path']=str(out)
    Path(folder,'verification.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
    print(json.dumps(checks,ensure_ascii=False,indent=2))


if __name__=='__main__': verify(sys.argv[1])
