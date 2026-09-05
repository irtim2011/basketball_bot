import re
from datetime import datetime, date
import pytz

from config import TIMEZONE, WEEKDAY_ALIASES

TZ = pytz.timezone(TIMEZONE)

_GIVEN_NAMES = {
    "александр", "александра", "алексей", "андрей", "антон", "аркадий", "арсений",
    "артем", "артемий", "вадим", "валерий", "ваня", "василий", "вика", "виктория",
    "виталий", "владимир", "владислав", "вугар", "глеб", "георгий", "григорий",
    "гриша", "давид", "даниил", "данила", "диана", "дима", "дмитрий", "егор",
    "екатерина", "захар", "иван", "илья", "кирилл", "клим", "коля", "лера", "валерия",
    "макар", "мартин", "мария", "маша", "матвей", "михаил", "миша", "ника",
    "николай", "ольга", "павел", "паша", "рома", "роман", "самсон", "саша",
    "семен", "семён", "сердар", "степа", "степан", "тереза", "тимофей", "федор",
    "фёдор", "френки", "элиза", "юлия", "ярик", "ярослав",
}
_FORMAL_GIVEN = {
    "ваня": "Иван", "вика": "Виктория", "гриша": "Григорий", "дима": "Дмитрий",
    "коля": "Николай", "лера": "Валерия", "маша": "Мария", "миша": "Михаил",
    "паша": "Павел", "рома": "Роман", "степа": "Степан", "ярик": "Ярослав",
}


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def normalize_username(raw: str | None) -> str | None:
    """Strips leading @ and lower-cases a telegram username. Returns None for empty input."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("@"):
        raw = raw[1:]
    raw = raw.strip()
    return raw.lower() or None


def normalize_phone(raw: str) -> str:
    """Keeps a leading + (if present) and digits only."""
    raw = raw.strip()
    plus = "+" if raw.startswith("+") else ""
    digits = re.sub(r"\D", "", raw)
    return f"{plus}{digits}"


def _name_part(value: str) -> str:
    special = {"мкговен": "МкГовен"}
    if value.lower() in special:
        return special[value.lower()]
    return "-".join(piece[:1].upper() + piece[1:].lower() for piece in value.split("-"))


def clean_person_name(raw: str) -> str:
    """Normalize whitespace and capitalization without guessing a person's identity."""
    parts = [_name_part(part) for part in raw.strip().split()]
    for index in range(1, len(parts)):
        if parts[index].lower() in {"из", "де", "фон", "ван"}:
            parts[index] = parts[index].lower()
    return " ".join(parts)


def registration_fio(raw: str) -> str | None:
    """Accept a strict surname/name/patronymic value for new registrations."""
    cleaned = clean_person_name(raw)
    parts = cleaned.split()
    if len(parts) != 3 or any(not re.fullmatch(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*", p) for p in parts):
        return None
    return cleaned


def legacy_canonical_fio(raw: str) -> tuple[str, str]:
    """Convert clear legacy 'given surname' entries to 'surname given'."""
    cleaned = clean_person_name(raw)
    parts = cleaned.split()
    if len(parts) >= 3 and parts[1].lower() == "из":
        return cleaned, "⚠️ ФИО требует уточнения"
    if len(parts) != 2:
        return cleaned, "готово" if len(parts) == 3 else "⚠️ ФИО требует уточнения"
    first, second = parts
    first_key, second_key = first.lower().replace("ё", "е"), second.lower().replace("ё", "е")
    known = {name.replace("ё", "е") for name in _GIVEN_NAMES}
    if first_key in known and second_key not in known:
        formal = _FORMAL_GIVEN.get(first.lower(), first)
        if first.lower() == "саша":
            formal = "Александра" if second.lower().endswith("а") else "Александр"
        return f"{second} {formal}", "готово"
    if second_key in known and first_key not in known:
        formal = _FORMAL_GIVEN.get(second.lower(), second)
        return f"{first} {formal}", "готово"
    return cleaned, "⚠️ проверьте порядок ФИО"


def fio_match_tokens(raw: str) -> tuple[str, ...]:
    words = re.findall(r"[а-яё0-9]+", raw.lower().replace("ё", "е"))
    ignored = {"фио", "требует", "уточнения", "instagram", "инстаграма", "из"}
    return tuple(sorted(word for word in words if word not in ignored))


def parse_weekday(raw: str) -> int | None:
    key = raw.strip().lower()
    return WEEKDAY_ALIASES.get(key)


def parse_time_str(raw: str) -> str | None:
    """Validates 'HH:MM' and returns it normalized, or None if invalid."""
    raw = raw.strip()
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    return f"{hh:02d}:{mm:02d}"


def format_date_header(iso_date: str) -> str:
    """'2026-09-01' -> '01.09'"""
    d = date.fromisoformat(iso_date)
    return d.strftime("%d.%m")


def one_name_typo(left: str, right: str) -> bool:
    """One insertion, deletion, substitution or adjacent transposition."""
    if left == right or min(len(left), len(right)) < 3 or abs(len(left)-len(right)) > 1:
        return False
    if len(left) == len(right):
        differences = [i for i, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
        return len(differences) == 1 or (len(differences) == 2 and
            differences[1] == differences[0]+1 and
            left[differences[0]] == right[differences[1]] and
            left[differences[1]] == right[differences[0]])
    short, long = sorted((left, right), key=len)
    return any(long[:i] + long[i+1:] == short for i in range(len(long)))


def unique_legacy_match(full_name, candidates):
    """Candidates are mappings with canonical_name; ambiguity never selects an ID."""
    wanted = fio_match_tokens(full_name)
    if len(wanted) < 2:
        return None
    exact = [row for row in candidates if len(fio_match_tokens(row['canonical_name'])) >= 2
             and set(fio_match_tokens(row['canonical_name'])).issubset(set(wanted))]
    if exact:
        return exact[0] if len(exact) == 1 else None
    parts = clean_person_name(full_name).lower().replace('ё','е').split()
    matches = []
    for row in candidates:
        old = row['canonical_name'].lower().replace('ё','е').split()
        if len(old) not in (2, 3) or len(parts) not in (2, 3):
            continue
        if len(old) == 3 and (len(parts) != 3 or old[2] != parts[2]):
            continue
        if (old[0] == parts[0] and one_name_typo(old[1], parts[1])) or (
                old[1] == parts[1] and one_name_typo(old[0], parts[0])):
            matches.append(row)
    return matches[0] if len(matches) == 1 else None
