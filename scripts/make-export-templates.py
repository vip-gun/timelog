#!/usr/bin/env python3
"""One-off: derive the two Finance export layouts from the reference workbooks kept in the git-ignored templates/ folder.

    python3 scripts/make-export-templates.py            (needs openpyxl; run from the repo root)

Writes app/timelog/export_templates/{oncall,overtime}_layout.xlsx: one prototype month tab per workbook (named MONTH)
with ALL cell values of the data area, personal names, company address, external links and document properties removed.
Only formats, column widths, merged cells, conditional formats, validations and the fixed headings stay. The exporter
(timelog/export_finance.py) copies these styles; it never reads the reference workbooks.
"""
import sys
from pathlib import Path

import openpyxl
from openpyxl.cell.cell import MergedCell

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "templates"
OUT = ROOT / "app" / "timelog" / "export_templates"


def scrub(wb):
    wb.properties.creator = "Timelog"
    wb.properties.lastModifiedBy = "Timelog"
    wb.properties.title = wb.properties.subject = wb.properties.description = wb.properties.keywords = None
    wb._external_links = []
    for name in list(wb.defined_names.keys()):
        if "[" in (wb.defined_names[name].attr_text or ""):   # points into another workbook
            del wb.defined_names[name]


def clear(ws, min_row, max_row, min_col, max_col):
    for row in ws.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col):
        for cell in row:
            if not isinstance(cell, MergedCell):
                cell.value = None


def oncall():
    wb = openpyxl.load_workbook(SRC / "Y26M01_IT_OnCall.xlsx")
    for name in wb.sheetnames:
        if name not in ("OCT", "Support"):
            del wb[name]
    ws = wb["OCT"]
    ws.title = "MONTH"
    clear(ws, 3, 5, 4, 40)          # dates / weekday / type headers (rewritten by the exporter)
    clear(ws, 6, 130, 1, 40)        # employee blocks, pricing block, check row
    ws["B4"].value = "PRIME REQUEST SHEET"
    sup = wb["Support"]
    clear(sup, 27, 41, 2, 4)        # holiday list (rewritten from the holiday table)
    scrub(wb)
    wb.save(OUT / "oncall_layout.xlsx")


def overtime():
    wb = openpyxl.load_workbook(SRC / "Y26M01_IT_Overtime.xlsx")
    for name in wb.sheetnames:
        if name not in ("READ FIRST", "OCT 2026"):
            del wb[name]
    ws = wb["OCT 2026"]
    ws.title = "MONTH"
    clear(ws, 2, 2, 4, 34)          # stale dates
    clear(ws, 3, 3, 4, 34)          # weekday headings
    clear(ws, 4, 60, 1, 52)         # employees, formulas, footer note
    rf = wb["READ FIRST"]
    rf["B3"].value = "Doe John"
    rf["A8"].value = "Send the file to your manager for approval, with Payroll in copy"
    rf["B6"].value = "PAID"
    clear(rf, 11, 23, 3, 4)         # holiday list (rewritten from the holiday table)
    scrub(wb)
    wb.save(OUT / "overtime_layout.xlsx")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    if not SRC.is_dir():
        sys.exit("templates/ folder with the reference workbooks not found")
    oncall()
    overtime()
    print("written to", OUT)
