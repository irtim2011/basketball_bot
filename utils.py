import re
from datetime import datetime, date
import pytz

from config import TIMEZONE, WEEKDAY_ALIASES

TZ = pytz.timezone(TIMEZONE)


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
