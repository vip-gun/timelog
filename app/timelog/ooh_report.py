"""Out-of-hours intervention report: parse a ServiceNow export and split each intervention into hours per day type.

Input columns (header names, case-insensitive): Short description, Assigned to, Planned start date, Planned end date
(dates as ``YYYY-MM-DD hh:mm:ss`` text or real Excel datetimes). Nothing is stored: the file is analysed in memory.

Counting rules (same day types as CLAUDE.md §1.1, holiday wins over weekend):
- business day (W): only the hours **outside** ``biz_start``-``biz_end`` count;
- Saturday, Sunday, legal holiday: every hour counts.
Overlapping interventions of one person are merged first, so the same hour is never counted twice.
"""
import csv
import datetime as dt
import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .models import HOLIDAY, SATURDAY, SUNDAY, WEEKDAY

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 50000
TYPES = (WEEKDAY, SATURDAY, SUNDAY, HOLIDAY)
TYPE_LABELS = {WEEKDAY: "Business day", SATURDAY: "Saturday", SUNDAY: "Sunday", HOLIDAY: "Public holiday"}
REQUIRED = {
    "description": ("short description", "short_description"),
    "person": ("assigned to", "assigned_to"),
    "start": ("planned start date", "planned_start_date"),
    "end": ("planned end date", "planned_end_date"),
}
OPTIONAL = {"number": ("number", "change request number", "change request", "change number")}   # ServiceNow change request
LONG_SPAN_HOURS = 24
MAX_ITEMS_SHOWN = 500


class OohError(Exception):
    pass


@dataclass
class Intervention:
    row: int
    person: str
    description: str
    start: dt.datetime
    end: dt.datetime
    number: str = ""          # ServiceNow change request number (optional column "Number")


@dataclass
class Skipped:
    row: int
    reason: str


@dataclass
class Parsed:
    items: List[Intervention] = field(default_factory=list)
    skipped: List[Skipped] = field(default_factory=list)
    rows: int = 0


@dataclass
class Report:
    parsed: Parsed
    biz_start: int
    biz_end: int
    people: List[str]                                   # sorted by total hours, descending
    totals: Dict[str, Dict[str, float]]                 # person -> type -> hours
    monthly: Dict[Tuple[str, str], Dict[str, float]]    # (person, "YYYY-MM") -> type -> hours
    months: List[str]                                   # every month from first to last, gaps included
    tickets: Dict[str, int]                             # person -> interventions with out-of-hours time
    detail: List[dict]                                  # one row per intervention
    overlaps: int
    long_spans: int
    holidays_used: Set[dt.date]

    def person_total(self, p: str) -> float:
        return sum(self.totals[p].values())

    @property
    def grand(self) -> Dict[str, float]:
        return {t: sum(self.totals[p][t] for p in self.people) for t in TYPES}

    @property
    def grand_total(self) -> float:
        return sum(self.grand.values())


# ---------------------------------------------------------------- reading
def _norm(h) -> str:
    return " ".join(str(h or "").strip().lower().replace("_", " ").split())


def _map_header(header: List) -> Dict[str, int]:
    names = {_norm(h): i for i, h in enumerate(header)}
    out = {}
    for key, aliases in REQUIRED.items():
        for a in aliases:
            if _norm(a) in names:
                out[key] = names[_norm(a)]
                break
    for key, aliases in OPTIONAL.items():
        for a in aliases:
            if _norm(a) in names:
                out[key] = names[_norm(a)]
                break
    missing = [REQUIRED[k][0].title() for k in REQUIRED if k not in out]
    if missing:
        raise OohError("Missing column(s): " + ", ".join(missing) + ". Expected Short description, Assigned to, "
                       "Planned start date, Planned end date in the first row.")
    return out


def _as_datetime(v) -> Optional[dt.datetime]:
    if isinstance(v, dt.datetime):
        return v.replace(microsecond=0, tzinfo=None)
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _rows_from_file(data: bytes, filename: str) -> List[List]:
    if len(data) > MAX_BYTES:
        raise OohError(f"File too large (max {MAX_BYTES // (1024 * 1024)} MB).")
    if not data:
        raise OohError("The file is empty.")
    if data[:2] == b"PK":                       # .xlsx is a zip container
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
            raise OohError("The file is not a readable .xlsx workbook.") from exc
        try:
            ws = wb.worksheets[0]
            rows = []
            for r in ws.iter_rows(values_only=True):
                rows.append(list(r))
                if len(rows) > MAX_ROWS + 1:
                    raise OohError(f"Too many rows (max {MAX_ROWS}).")
            return rows
        finally:
            wb.close()
    if filename.lower().endswith(".xls"):
        raise OohError("Old .xls files are not supported: save the export as .xlsx or .csv.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if len(rows) > MAX_ROWS + 1:
        raise OohError(f"Too many rows (max {MAX_ROWS}).")
    return rows


def parse_file(data: bytes, filename: str = "") -> Parsed:
    rows = _rows_from_file(data, filename)
    header_at = next((i for i, r in enumerate(rows) if any(_norm(c) for c in r)), None)
    if header_at is None:
        raise OohError("The file has no data.")
    cols = _map_header(rows[header_at])
    parsed = Parsed()
    for n, r in enumerate(rows[header_at + 1:], start=header_at + 2):
        if not any(str(c or "").strip() for c in r):
            continue
        parsed.rows += 1

        def cell(key):
            i = cols.get(key)
            return r[i] if i is not None and i < len(r) else None

        person = str(cell("person") or "").strip()
        start, end = _as_datetime(cell("start")), _as_datetime(cell("end"))
        if not person:
            parsed.skipped.append(Skipped(n, "No assignee"))
        elif start is None or end is None:
            parsed.skipped.append(Skipped(n, "Start or end date missing or not YYYY-MM-DD hh:mm:ss"))
        elif end <= start:
            parsed.skipped.append(Skipped(n, "End is not after start"))
        else:
            parsed.items.append(Intervention(n, person, str(cell("description") or "").strip()[:200], start, end,
                                             str(cell("number") or "").strip()[:64]))
    if not parsed.items and not parsed.skipped:
        raise OohError("The file has a header but no interventions.")
    return parsed


# ---------------------------------------------------------------- calculation
def _classify(date: dt.date, holidays: Set[dt.date]) -> str:
    if date in holidays:
        return HOLIDAY
    return {5: SATURDAY, 6: SUNDAY}.get(date.weekday(), WEEKDAY)


def _merge(spans: List[Tuple[dt.datetime, dt.datetime, int]]):
    """Merge overlapping/touching spans; returns merged [(start, end, [row ids])]."""
    out: List[list] = []
    for s, e, row in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
            out[-1][2].append(row)
        else:
            out.append([s, e, [row]])
    return out


def _split_day(start: dt.datetime, end: dt.datetime, holidays: Set[dt.date],
               biz_start: int, biz_end: int):
    """Yield (date, type, counted_hours) for each calendar day touched by [start, end)."""
    day = start.date()
    while dt.datetime.combine(day, dt.time()) < end:
        d0 = dt.datetime.combine(day, dt.time())
        d1 = d0 + dt.timedelta(days=1)
        s, e = max(start, d0), min(end, d1)
        t = _classify(day, holidays)
        secs = (e - s).total_seconds()
        if t == WEEKDAY:
            b0, b1 = d0 + dt.timedelta(hours=biz_start), d0 + dt.timedelta(hours=biz_end)
            inside = max(0.0, (min(e, b1) - max(s, b0)).total_seconds())
            secs -= inside
        yield day, t, secs / 3600
        day += dt.timedelta(days=1)


def holiday_range(parsed: Parsed) -> Optional[Tuple[dt.date, dt.date]]:
    if not parsed.items:
        return None
    return (min(i.start for i in parsed.items).date(), max(i.end for i in parsed.items).date())


def build_report(parsed: Parsed, holidays: Set[dt.date], biz_start: int = 9, biz_end: int = 18) -> Report:
    if not (0 <= biz_start < biz_end <= 24):
        raise OohError("Business hours must satisfy 0 <= start < end <= 24.")
    totals: Dict[str, Dict[str, float]] = defaultdict(lambda: {t: 0.0 for t in TYPES})
    monthly: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(lambda: {t: 0.0 for t in TYPES})
    tickets: Dict[str, int] = defaultdict(int)
    by_person: Dict[str, List] = defaultdict(list)
    for i in parsed.items:
        by_person[i.person].append((i.start, i.end, i.row))

    overlaps = 0
    row_hours: Dict[int, Dict[str, float]] = {i.row: {t: 0.0 for t in TYPES} for i in parsed.items}
    used: Set[dt.date] = set()
    for person, spans in by_person.items():
        _ = totals[person]
        for s, e, rows in _merge(spans):
            overlaps += len(rows) - 1
            share = rows[0]                     # attribute merged time to the earliest row of the group
            for day, t, h in _split_day(s, e, holidays, biz_start, biz_end):
                if h <= 0:
                    continue
                totals[person][t] += h
                monthly[(person, day.strftime("%Y-%m"))][t] += h
                row_hours[share][t] += h
                if t == HOLIDAY:
                    used.add(day)
    for i in parsed.items:
        if sum(row_hours[i.row].values()) > 0:
            tickets[i.person] += 1

    people = sorted((p for p in totals if sum(totals[p].values()) > 0), key=lambda p: (-sum(totals[p].values()), p.lower()))
    months: List[str] = []
    if monthly:
        keys = sorted({m for _, m in monthly})
        y, m = map(int, keys[0].split("-"))
        ye, me = map(int, keys[-1].split("-"))
        while (y, m) <= (ye, me):
            months.append(f"{y:04d}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    detail = [{"row": i.row, "number": i.number, "person": i.person, "description": i.description, "start": i.start, "end": i.end,
               "hours": row_hours[i.row], "total": sum(row_hours[i.row].values())}
              for i in sorted(parsed.items, key=lambda x: x.start) if sum(row_hours[i.row].values()) > 0]
    long_spans = sum(1 for i in parsed.items if (i.end - i.start).total_seconds() / 3600 > LONG_SPAN_HOURS)
    return Report(parsed, biz_start, biz_end, people, dict(totals), dict(monthly), months, dict(tickets), detail,
                  overlaps, long_spans, used)


def month_label(key: str) -> str:
    y, m = key.split("-")
    return ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][int(m)] + " " + y[2:]


# ---------------------------------------------------------------- chart geometry (rendered as inline SVG)
def nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    mag = 10 ** (len(str(int(v))) - 1) if v >= 1 else 1
    for step in (1, 2, 2.5, 5, 10):
        if v <= step * mag:
            return float(step * mag)
    return float(10 * mag)


def _r(v: float, digits: int):
    return int(round(v)) if digits == 0 else round(v, digits)


def hbars(report: Report, width: int = 900, label_w: int = 190, row_h: int = 30, digits: int = 1) -> dict:
    """Horizontal stacked bars, one row per person."""
    area = width - label_w - 70
    top = nice_max(max((report.person_total(p) for p in report.people), default=0))
    rows = []
    for n, p in enumerate(report.people):
        x, segs = float(label_w), []
        for t in TYPES:
            h = report.totals[p][t]
            if h <= 0:
                continue
            w = h / top * area
            segs.append({"x": round(x, 1), "w": round(max(w, 1.0), 1), "t": t, "h": _r(h, digits)})
            x += w
        rows.append({"y": n * row_h + 4, "name": p, "segs": segs, "end": round(x + 6, 1),
                     "total": _r(report.person_total(p), digits)})
    h = len(rows) * row_h + 8
    xticks = [{"x": round(label_w + f * area, 1), "text": f"{top * f:g}"} for f in (0, .25, .5, .75, 1)]
    return {"w": width, "h": h + 44, "label_w": label_w, "rows": rows, "bar_h": row_h - 10, "axis_y": h, "xticks": xticks,
            "xt": {"x": round(label_w + area / 2, 1), "y": h + 38}, "yt": {"x": 12, "y": round(h / 2, 1)}}


def columns(months: List[str], data: Dict[str, Dict[str, float]], ymax: float, width: int, height: int,
            left: int = 56, bottom: int = 40, top: int = 8, labels: bool = True, digits: int = 1) -> dict:
    """Stacked columns, one per month; `data[month][type]` hours. Missing month = zero-height column."""
    area_w, area_h = width - left - 6, height - bottom - top
    slot = area_w / max(len(months), 1)
    bw = min(slot * 0.7, 46)
    bars, xlabels = [], []
    every = max(1, -(-len(months) // 12)) if labels else len(months)
    for i, m in enumerate(months):
        x = left + i * slot + (slot - bw) / 2
        y = top + area_h
        tot = 0.0
        for t in TYPES:
            h = data.get(m, {}).get(t, 0.0)
            if h <= 0:
                continue
            ph = h / ymax * area_h
            y -= ph
            tot += h
            bars.append({"x": round(x, 1), "y": round(y, 1), "w": round(bw, 1), "h": round(max(ph, 0.8), 1),
                         "t": t, "title": f"{month_label(m)} · {TYPE_LABELS[t]}: {h:.{digits}f}"})
        if labels and (i % every == 0 or i == len(months) - 1 and every == 1):
            xlabels.append({"x": round(x + bw / 2, 1), "text": month_label(m)})
        elif not labels and i in (0, len(months) - 1):
            xlabels.append({"x": round(x + bw / 2, 1), "text": month_label(m)})
    ticks = [{"y": round(top + area_h - f * area_h, 1), "text": f"{ymax * f:g}"} for f in (0, .5, 1)]
    return {"w": width, "h": height, "left": left, "top": top, "base": top + area_h, "right": width - 6,
            "bars": bars, "xlabels": xlabels, "ticks": ticks,
            "xt": {"x": round(left + area_w / 2, 1), "y": height - 4}, "yt": {"x": 12, "y": round(top + area_h / 2, 1)}}


def team_columns(report: Report, digits: int = 1) -> dict:
    data = {m: {t: sum(report.monthly.get((p, m), {}).get(t, 0.0) for p in report.people) for t in TYPES}
            for m in report.months}
    ymax = nice_max(max((sum(v.values()) for v in data.values()), default=0))
    return columns(report.months, data, ymax, 900, 300, digits=digits)


def person_columns(report: Report, digits: int = 1) -> list:
    """Small multiples sharing one vertical scale, so people can be compared at a glance."""
    ymax = nice_max(max((sum(report.monthly.get((p, m), {}).values()) for p in report.people for m in report.months),
                        default=0))
    out = []
    for p in report.people:
        data = {m: report.monthly.get((p, m), {}) for m in report.months}
        out.append({"name": p, "total": _r(report.person_total(p), digits),
                    "chart": columns(report.months, data, ymax, 340, 150, left=46, bottom=38, labels=False, digits=digits)})
    return out


def heatmap(report: Report, digits: int = 1) -> list:
    peak = max((sum(report.monthly.get((p, m), {}).values()) for p in report.people for m in report.months),
               default=0) or 1
    rows = []
    for p in report.people:
        cells = []
        for m in report.months:
            h = sum(report.monthly.get((p, m), {}).values())
            cells.append({"h": h, "level": 0 if h <= 0 else min(5, 1 + int(h / peak * 4.999))})
        rows.append({"name": p, "cells": cells, "total": _r(report.person_total(p), digits)})
    return rows


def chart_context(report: Report, digits: int = 1) -> dict:
    """Everything the chart blocks of `_chart_blocks.html` need."""
    return dict(report=report, hbars=hbars(report, digits=digits), team_chart=team_columns(report, digits),
                people_charts=person_columns(report, digits), heat=heatmap(report, digits), types=TYPES,
                type_labels=TYPE_LABELS, mlabel=month_label)


def synthetic_report(people_totals: Dict[str, Dict[str, float]], monthly: Dict[Tuple[str, str], Dict[str, float]],
                     months: List[str], tickets: Dict[str, int], detail: Optional[List[dict]] = None) -> Report:
    """A Report for data that is not parsed from a file (logged sheets): same charts, no intervals."""
    totals = {p: {t: v.get(t, 0.0) for t in TYPES} for p, v in people_totals.items()}
    people = sorted((p for p in totals if sum(totals[p].values()) > 0), key=lambda p: (-sum(totals[p].values()), p.lower()))
    return Report(Parsed(), 0, 0, people, totals, monthly, months, tickets, detail or [], 0, 0, set())
