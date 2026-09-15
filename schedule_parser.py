"""Parse forwarded Russian training schedules without I/O or date guessing.

``entries`` are chronological and unique by date/start. They are a preview
only: a caller must refuse import while ``errors`` is nonempty, including
when the 100-session limit is exceeded. Past dates are retained unchanged.
"""
from datetime import date
import re


_WEEKDAYS = {
    'пн': 0, 'понедельник': 0,
    'вт': 1, 'вторник': 1,
    'ср': 2, 'среда': 2,
    'чт': 3, 'четверг': 3,
    'пт': 4, 'пятница': 4,
    'сб': 5, 'суббота': 5,
    'вс': 6, 'воскресенье': 6,
}
_WEEKDAY_NAMES = ('понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье')
_MONTH = (r'(?:январ[ьяе]|феврал[ьяе]|март(?:а|е)?|апрел[ьяе]|ма[йяе]|'
          r'июн[ьяе]|июл[ьяе]|август(?:а|е)?|сентябр[ьяе]|октябр[ьяе]|'
          r'ноябр[ьяе]|декабр[ьяе])')
_MONTH_YEAR = _MONTH + r'(?:\s+[0-9]{4}(?:\s*(?:г\.?|года))?)?'
_HEADER = re.compile(
    r'^(?:расписание(?:\s+тренировок)?(?:\s+(?:(?:на|в)\s+)?' + _MONTH_YEAR + r')?'
    r'|тренировки\s+(?:(?:на|в)\s+)?' + _MONTH_YEAR + r'|'
    + _MONTH_YEAR + r')\s*[:!]?$'
)
_DATE = re.compile(r'^([0-9]{1,2})\.([0-9]{1,2})(?:\.([0-9]{4}))?')
_DAY_WORD = re.compile(r'^([а-яё]+)\.?\s*,?\s*', re.IGNORECASE)
_TIMES = re.compile(r'^([0-9]{1,2}:[0-9]{2})(?:\s*-\s*([0-9]{1,2}:[0-9]{2}))?\s*\.?$')
_DASHES = str.maketrans({dash: '-' for dash in '‐‑‒–—−'})
_HEADER_DECORATION = re.compile(r'^[🏀📅📆🗓\ufe0f\s]+')
_BULLET = re.compile(r'^(?:[•*]\s*|-\s+)')


def _time(value):
    hour, minute = map(int, value.split(':'))
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def parse_schedule(text: str, today: date) -> dict:
    """Return entries, line-specific errors, warnings and exact duplicate count.

    Dates without a year use ``today.year``, including around New Year. A month
    heading never overrides that rule. End times must be later on the same day.
    """
    entries, errors, warnings = {}, [], []
    duplicate_count = 0
    assumed_year = False
    heading_years = set()

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip('\ufeff').strip().translate(_DASHES)
        if not line:
            continue
        heading = _HEADER_DECORATION.sub('', line).casefold()
        if _HEADER.fullmatch(heading):
            heading_years.update(int(value) for value in re.findall(r'\b[0-9]{4}\b', heading))
            continue
        line = _BULLET.sub('', line)
        match = _DATE.match(line)
        if not match:
            errors.append(f'Строка {number}: не распознана. Нужны дата и время, например «14.09, Пн 19:30–21:30».')
            continue
        day, month, explicit_year = match.groups()
        year = int(explicit_year) if explicit_year else today.year
        assumed_year = assumed_year or explicit_year is None
        try:
            training_date = date(year, int(month), int(day))
        except ValueError:
            errors.append(f'Строка {number}: даты {int(day):02d}.{int(month):02d}.{year:04d} не существует.')
            continue

        tail = line[match.end():].strip()
        if tail.startswith(','):
            tail = tail[1:].strip()
        weekday = _DAY_WORD.match(tail)
        if weekday:
            word = weekday.group(1).casefold()
            if word not in _WEEKDAYS:
                shown = weekday.group(1)[:99] + '…' if len(word) > 100 else weekday.group(1)
                errors.append(f'Строка {number}: неизвестный день недели «{shown}». Используйте Пн, Вт, Ср, Чт, Пт, Сб или Вс.')
                continue
            if _WEEKDAYS[word] != training_date.weekday():
                errors.append(f'Строка {number}: {training_date:%d.%m.%Y} — {_WEEKDAY_NAMES[training_date.weekday()]}, а не «{weekday.group(1)}».')
                continue
            tail = tail[weekday.end():]
        times = _TIMES.fullmatch(tail)
        if not times:
            errors.append(f'Строка {number}: неверный формат времени или лишний текст. Нужны ЧЧ:ММ либо ЧЧ:ММ–ЧЧ:ММ.')
            continue
        raw_start, raw_end = times.groups()
        start = _time(raw_start)
        end = _time(raw_end) if raw_end else None
        if start is None or (raw_end and end is None):
            errors.append(f'Строка {number}: время вне диапазона 00:00–23:59.')
            continue
        if end is not None and end <= start:
            errors.append(f'Строка {number}: конец должен быть позже начала в тот же день. Ночные тренировки с переходом через полночь не поддерживаются.')
            continue

        key = (training_date.isoformat(), start)
        if key in entries:
            if entries[key]['end_time'] != end:
                errors.append(f'Строка {number}: у тренировки {training_date:%d.%m.%Y} в {start} разные варианты времени окончания.')
            else:
                duplicate_count += 1
            continue
        entries[key] = {'date': key[0], 'time': start, 'end_time': end}

    if assumed_year:
        warnings.append(f'В датах без года выбран {today.year} год по текущей дате. Проверьте год перед сохранением.')
        if any(year != today.year for year in heading_years):
            warnings.append('Год в заголовке отличается от выбранного. Чтобы использовать его, укажите год прямо в датах: ДД.ММ.ГГГГ.')
    if duplicate_count:
        warnings.append(f'Повторяющихся тренировок объединено: {duplicate_count}.')
    ordered = [entries[key] for key in sorted(entries)]
    if len(ordered) > 100:
        errors.append(f'Найдено {len(ordered)} уникальных тренировок; можно импортировать не более 100 за один раз.')
    if ordered:
        first, last = date.fromisoformat(ordered[0]['date']), date.fromisoformat(ordered[-1]['date'])
        if (last - first).days > 366:
            errors.append('Между первой и последней тренировкой больше 366 дней. Разделите расписание на несколько импортов.')
    elif not errors:
        errors.append('Не найдено тренировок. Пришлите строки с датой и временем.')
    return {'entries': ordered, 'errors': errors, 'warnings': warnings,
            'duplicate_count': duplicate_count}
