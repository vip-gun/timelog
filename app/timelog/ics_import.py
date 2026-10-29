"""Minimal iCalendar (RFC 5545) reader for importing public holidays (no external dependency).

Supported: all-day events (DTSTART;VALUE=DATE), multi-day events (exclusive DTEND or DURATION:PnD),
timed events (their start date is used), folded lines and escaped text. Recurring events (RRULE)
and cancelled events are skipped and reported - public holiday feeds list each date explicitly.
"""
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_BYTES = 1024 * 1024
MAX_DAYS_PER_EVENT = 31
YEAR_RANGE = (1990, 2100)


class IcsError(ValueError):
    pass


@dataclass
class IcsResult:
    holidays: Dict[dt.date, str] = field(default_factory=dict)  # date -> name (first occurrence wins)
    skipped_recurring: int = 0
    skipped_other: int = 0

    @property
    def years(self) -> List[int]:
        return sorted({d.year for d in self.holidays})


def _unfold(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    value = re.sub(r"\\[nN]", " ", value)
    return value.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _parse_line(line: str) -> Tuple[str, Dict[str, str], str]:
    if ":" not in line:
        return "", {}, ""
    head, value = line.split(":", 1)
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"').upper()
    return parts[0].upper(), params, value.strip()


def _parse_date(value: str) -> Optional[dt.date]:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", value)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _event_days(props: Dict[str, Tuple[Dict[str, str], str]]) -> Optional[List[dt.date]]:
    if "DTSTART" not in props:
        return None
    start_params, start_value = props["DTSTART"]
    start = _parse_date(start_value)
    if start is None:
        return None
    all_day = start_params.get("VALUE") == "DATE" or len(start_value) == 8
    if not all_day:
        return [start]  # timed event: count its start date
    days = 1
    if "DTEND" in props:
        end = _parse_date(props["DTEND"][1])
        if end is not None and end > start:
            days = (end - start).days  # DTEND is exclusive for all-day events
    elif "DURATION" in props:
        m = re.match(r"^P(\d+)D$", props["DURATION"][1].upper())
        if m:
            days = max(1, int(m.group(1)))
    if days > MAX_DAYS_PER_EVENT:
        return None
    return [start + dt.timedelta(days=i) for i in range(days)]


def parse_ics(data: bytes) -> IcsResult:
    if len(data) > MAX_BYTES:
        raise IcsError("File too large (max 1 MB).")
    text = data.decode("utf-8-sig", errors="replace")
    if "BEGIN:VCALENDAR" not in text.upper():
        raise IcsError("Not an iCalendar (.ics) file.")
    result = IcsResult()
    props: Optional[Dict[str, Tuple[Dict[str, str], str]]] = None
    for line in _unfold(text):
        name, params, value = _parse_line(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            props = {}
        elif name == "END" and value.upper() == "VEVENT" and props is not None:
            _add_event(result, props)
            props = None
        elif props is not None and name and name not in props:
            props[name] = (params, value)
    if not result.holidays and not result.skipped_recurring and not result.skipped_other:
        raise IcsError("No event found in the file.")
    return result


def _add_event(result: IcsResult, props: Dict[str, Tuple[Dict[str, str], str]]) -> None:
    if "RRULE" in props:
        result.skipped_recurring += 1
        return
    if props.get("STATUS", ({}, ""))[1].upper() == "CANCELLED":
        result.skipped_other += 1
        return
    days = _event_days(props)
    if not days or not (YEAR_RANGE[0] <= days[0].year <= YEAR_RANGE[1]):
        result.skipped_other += 1
        return
    name = _unescape(props.get("SUMMARY", ({}, ""))[1])[:128] or "Public holiday"
    for d in days:
        result.holidays.setdefault(d, name)
