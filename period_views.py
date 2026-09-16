"""Calendar selectors use ordinary workbook formulas and survive XLSX export."""
from collections import defaultdict
from datetime import date, timedelta
import planning_sheet as view

DATA = 'Планы_данные'


def build(snapshot, tier_limit):
    now = view.instant(snapshot['generated_at'])
    people = sorted(snapshot['participants'], key=lambda p: (p.get('full_name') or '', int(p['public_id'])))
    by_day = defaultdict(list)
    for session in snapshot['sessions']:
        if not session.get('cancelled') and session.get('in_schedule', True):
            by_day[view.instant(session['starts_at']).date().isoformat()].append(session)
    answers = {}
    for a in snapshot['answers']:
        if not a.get('cancelled'):
            key = (int(a['participant_id']), a['key'], a['kind'])
            if key not in answers or (a.get('responded_at') or '') >= (answers[key].get('responded_at') or ''):
                answers[key] = a
    raw = [['Ключ', 'Значение']]
    for day, sessions in sorted(by_day.items()):
        sessions.sort(key=lambda s: view.instant(s['starts_at']))
        times = [view.instant(s['starts_at']).strftime('%H:%M') for s in sessions]
        raw.append(['schedule|'+day, '\n'.join(times)])
        for p in people:
            for stage in ('month', 'week'):
                marks = [view.MARKS[answers.get((int(p['id']), s['key'], stage), {}).get('status', 'pending')] for s in sessions]
                value = marks[0] if len(marks) == 1 else '\n'.join(t+' '+m for t,m in zip(times,marks))
                raw.append([f"{p['public_id']}|{day}|{stage}", value])
    limit = max(2, len(raw))
    lookup = lambda key, fallback: f'IFERROR(INDEX(\'{DATA}\'!$B$2:$B${limit};MATCH({key};\'{DATA}\'!$A$2:$A${limit};0));"{fallback}")'
    result = {DATA: {'values': raw, 'kind': 'raw', 'groups': [], 'layout': []}}
    for title, stage, count in ((view.MONTHLY,'month',31), (view.WEEKLY,'week',7)):
        default = now.strftime('%Y-%m') if stage == 'month' else (now.date()-timedelta(days=now.weekday())).isoformat()
        start = 'DATE(VALUE(LEFT($B$1;4));VALUE(MID($B$1;6;2));'+('1)' if stage=='month' else 'VALUE(RIGHT($B$1;2)))')
        rows = [['Месяц ↓' if stage=='month' else 'Неделя с ↓', default, '', ''],
                ['','Выберите период в B1. Y — придёт; N — нет; — нет ответа.','',''],
                ['ID','ФИО','Телеграм','Tier'],
                ['','Ответы на опрос «'+('месяц' if stage=='month' else 'неделя')+'»','','Время →']]
        for n in range(count):
            col = view._column(n+5)
            formula = f'={start}+{n}'
            if stage == 'month':
                formula = f'=IF({n}<DAY(EOMONTH({start};0));{start}+{n};"")'
            rows[2].append(view.Formula(formula))
            rows[3].append(view.Formula(f'=IF({col}$3="";"";'+lookup(f'"schedule|"&TEXT({col}$3;"yyyy-mm-dd")','нет тренировки')+')'))
        for p in people:
            r = len(rows)+1
            row = [str(p['public_id']),p.get('full_name') or '', '@'+p['username'] if p.get('username') else '',view.tier_formula(f'$A{r}',tier_limit)]
            for n in range(count):
                col = view._column(n+5)
                key = f'$A{r}&"|"&TEXT({col}$3;"yyyy-mm-dd")&"|{stage}"'
                row.append(view.Formula(f'=IF({col}$3="";"";'+lookup(key,'')+')'))
            rows.append(row)
        first = min([date(2026,8,1), now.date().replace(day=1)] + [date.fromisoformat(d).replace(day=1) for d in by_day])
        last = max([date(now.year+1,12,31)] + [date.fromisoformat(d) for d in by_day])
        options=[]
        day=first if stage=='month' else first-timedelta(days=first.weekday())
        while day<=last:
            options.append(day.strftime('%Y-%m') if stage=='month' else day.isoformat())
            day = ((day.replace(day=28)+timedelta(days=4)).replace(day=1) if stage=='month' else day+timedelta(days=7))
        result[title]={'values':rows,'kind':'calendar','groups':[], 'layout':['calendar-v1'], 'options':options}
    return result


def style(sheet, spec):
    width=max(map(len,spec['values']))
    requests=view._style(sheet,dict(spec,kind='matrix'))
    requests += [{'updateSheetProperties':{'properties':{'sheetId':sheet.id,'gridProperties':{'frozenRowCount':4}},'fields':'gridProperties.frozenRowCount'}},
        {'mergeCells':{'range':{'sheetId':sheet.id,'startRowIndex':1,'endRowIndex':2,'startColumnIndex':1,'endColumnIndex':4},'mergeType':'MERGE_ALL'}},
        {'updateDimensionProperties':{'range':{'sheetId':sheet.id,'dimension':'ROWS','startIndex':2,'endIndex':4},'properties':{'pixelSize':42},'fields':'pixelSize'}},
        {'repeatCell':{'range':{'sheetId':sheet.id,'startRowIndex':2,'endRowIndex':4,'endColumnIndex':width},'cell':{'userEnteredFormat':{'backgroundColor':view.SLATE,'textFormat':{'bold':True,'foregroundColor':{'red':1,'green':1,'blue':1}},'wrapStrategy':'WRAP'}},'fields':'userEnteredFormat'}},
        {'repeatCell':{'range':{'sheetId':sheet.id,'startRowIndex':2,'endRowIndex':3,'startColumnIndex':4,'endColumnIndex':width},'cell':{'userEnteredFormat':{'numberFormat':{'type':'DATE','pattern':'ddd dd.MM'}}},'fields':'userEnteredFormat.numberFormat'}},
        {'repeatCell':{'range':{'sheetId':sheet.id,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':1,'endColumnIndex':2},'cell':{'userEnteredFormat':{'backgroundColor':{'red':1,'green':.94,'blue':.72},'textFormat':{'bold':True,'foregroundColor':{'red':0,'green':0,'blue':0}},'numberFormat':{'type':'TEXT'}}},'fields':'userEnteredFormat'}},
        {'updateDimensionProperties':{'range':{'sheetId':sheet.id,'dimension':'COLUMNS','startIndex':4,'endIndex':width},'properties':{'pixelSize':105},'fields':'pixelSize'}},
        {'updateDimensionProperties':{'range':{'sheetId':sheet.id,'dimension':'COLUMNS','startIndex':3,'endIndex':4},'properties':{'pixelSize':85},'fields':'pixelSize'}}]
    return requests
