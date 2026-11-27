#!/usr/bin/env python3
"""Write a fictional ServiceNow-style export to test the Analysis page (Experts / Officers panes): python3 scripts/make-ooh-example.py [out.xlsx] [seed]  (another seed = another sample)
Needs openpyxl (app/.venv). People are fictional; the file is git-ignored (*.xlsx)."""
import datetime as dt
import sys

from _fake import Fake
from openpyxl import Workbook
from openpyxl.styles import Font

rnd = Fake(int(sys.argv[2]) if len(sys.argv) > 2 else 2026)
# the demo users of `python -m timelog.demo` (Bob and Erin are Experts), so the Experts / Non-experts tabs both have data
PEOPLE = {"Alice Normal": 1.0, "Bob Expert": 1.6, "Carol Normal": .7, "Dave Other": 1.3, "Erin Expert": 2.0}
TASKS = ["Patch storage array", "Restart batch cluster", "Database failover", "Firewall rule change", "Certificate renewal",
         "Hypervisor maintenance", "Backup restore test", "Network switch upgrade", "Disk space incident", "VPN outage"]
# Luxembourg public holidays 2026 (Easter 5 Apr)
HOL = {dt.date(2026, *md) for md in [(1, 1), (4, 6), (5, 1), (5, 9), (5, 14), (5, 25), (6, 23), (8, 15), (11, 1), (12, 25), (12, 26)]}
wb = Workbook()
ws = wb.active
ws.title = "Page 1"
ws.append(["Number", "Short description", "Assigned to", "Planned start date", "Planned end date", "State"])
for c in ws[1]:
    c.font = Font(bold=True)
n = 0
d = dt.date(2026, 1, 1)
while d <= dt.date(2026, 11, 25):
    off = d.weekday() >= 5 or d in HOL
    for p, w in PEOPLE.items():
        if rnd.random() < 0.045 * w * (2.2 if off else 1) * (1.6 if d.month in (3, 9) else 1):
            if off:
                s = dt.datetime.combine(d, dt.time(rnd.choice([6, 8, 10, 13, 16, 20]), rnd.choice([0, 30])))
            else:
                s = dt.datetime.combine(d, dt.time(rnd.choice([6, 7, 8, 12, 17, 19, 21, 22, 23]), rnd.choice([0, 30])))
            e = s + dt.timedelta(minutes=rnd.choice([30, 60, 90, 120, 180, 240, 360]))
            n += 1
            ws.append([f"CHG{n:07d}", rnd.choice(TASKS), p, s.strftime("%Y-%m-%d %H:%M:%S"),
                       e.strftime("%Y-%m-%d %H:%M:%S"), "Closed"])
    d += dt.timedelta(days=1)
# edge cases the report handles: overlap, past midnight, multi-day, bad rows
ws.append(["CHG9000001", "Overlapping work A", "Bob Expert", "2026-06-13 10:00:00", "2026-06-13 13:00:00", "Closed"])
ws.append(["CHG9000002", "Overlapping work B (merged)", "Bob Expert", "2026-06-13 12:00:00", "2026-06-13 15:00:00", "Closed"])
ws.append(["CHG9000003", "Overnight migration", "Alice Normal", "2026-07-03 22:00:00", "2026-07-04 03:00:00", "Closed"])
ws.append(["CHG9000004", "Planned over a long weekend", "Erin Expert", "2026-05-08 20:00:00", "2026-05-10 06:00:00", "Closed"])
ws.append(["CHG9000005", "Missing end date", "Dave Other", "2026-04-02 10:00:00", "", "Closed"])
ws.append(["CHG9000006", "No assignee", "", "2026-04-03 10:00:00", "2026-04-03 11:00:00", "Closed"])
# name variants that still match a Timelog user: "Last, First", lower case, and the username
ws.append(["CHG9000008", "Variant: Last, First", "Normal, Alice", "2026-08-15 09:30:00", "2026-08-15 12:30:00", "Closed"])
ws.append(["CHG9000009", "Variant: lower case", "bob expert", "2026-08-16 10:00:00", "2026-08-16 13:00:00", "Closed"])
ws.append(["CHG9000010", "Variant: username", "erin", "2026-08-22 08:00:00", "2026-08-22 10:00:00", "Closed"])
ws.append(["CHG9000007", "Contractor not in Timelog", "External Contractor", "2026-06-20 10:00:00", "2026-06-20 12:00:00", "Closed"])
for col, w in zip("ABCDEF", (14, 34, 18, 21, 21, 10)):
    ws.column_dimensions[col].width = w
out = sys.argv[1] if len(sys.argv) > 1 else "samples/analysis-example.xlsx"
wb.save(out)
print(f"{out}: {n + 6} rows")
