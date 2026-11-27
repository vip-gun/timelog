"""Analysis (out-of-hours interventions): upload a ServiceNow export, see hours per person and day type + trend.
An analysis can be **saved** as a dataset (rows in `ooh_interventions`), which can then be combined, edited and deleted.
Without saving, the file is analysed in memory and never stored. Everything here is for managers and administrators."""
import datetime as dt
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import ooh_report as ooh
from ..access import can_view_team
from ..calendar_utils import holiday_set
from ..db import get_db
from ..models import OohDataset, OohIntervention, User
from ..security import current_user, flash, verify_csrf
from ..web import redirect, render
from ..workflow import audit
from ..history_import import _find, user_index

router = APIRouter(prefix="/analysis")
GROUPS = {"experts": ("Experts", True), "officers": ("Officers", False)}
PAGE_SIZE = 100
MAX_NAME = 128


def _allowed(db: Session, me: User) -> None:
    if not can_view_team(db, me):
        raise HTTPException(status_code=404, detail="Not found")


def _can_edit(me: User, ds: OohDataset) -> bool:
    return me.is_admin or ds.created_by_id == me.id


def _dataset(db: Session, me: User, ds_id: int, edit: bool = False) -> OohDataset:
    _allowed(db, me)
    ds = db.get(OohDataset, ds_id)
    if ds is None or (edit and not _can_edit(me, ds)):
        raise HTTPException(status_code=404, detail="Not found")
    return ds


def _dataset_rows(db: Session):
    """Saved datasets with their size and period, newest first."""
    rows = db.execute(
        select(OohDataset, func.count(OohIntervention.id), func.count(func.distinct(OohIntervention.person)),
               func.min(OohIntervention.start), func.max(OohIntervention.end))
        .outerjoin(OohIntervention, OohIntervention.dataset_id == OohDataset.id)
        .group_by(OohDataset.id).order_by(OohDataset.created_at.desc(), OohDataset.id.desc()))
    return [{"ds": d, "rows": n, "people": p, "first": a, "last": b} for d, n, p, a, b in rows]


def _split_all(db: Session, parsed: ooh.Parsed):
    """Split the interventions into Experts and Officers, matching "Assigned to" to Timelog users by name. **Experts** are
    the Expert-flagged users plus managers (anyone with direct reports) and Management accounts, who also do
    out-of-hours changes; everybody else is an Officer; the Timelog name replaces the spelling of the file, so "bob expert", "Expert, Bob" and "bob" are one
    person. Returns ({group: Parsed}, names that match no single user)."""
    idx, known, unmatched = user_index(db), {}, []
    manager_ids = set(db.scalars(select(User.manager_id).where(User.manager_id.is_not(None)).distinct()))
    keep = {key: [] for key in GROUPS}
    for item in parsed.items:
        if item.person not in known:
            user, _ = _find(idx, item.person)
            known[item.person] = user
            if user is None:
                unmatched.append(item.person)
        user = known[item.person]
        if user is not None:
            key = "experts" if (user.is_expert or user.is_management or user.id in manager_ids) else "officers"
            keep[key].append(ooh.Intervention(item.row, user.full_name, item.description, item.start, item.end, item.number))
    parts = {k: ooh.Parsed(items=v, skipped=parsed.skipped if k == "experts" else [], rows=parsed.rows)
             for k, v in keep.items()}
    return parts, sorted(set(unmatched), key=str.lower)


def _panes(db: Session, parsed: ooh.Parsed, biz_start: int, biz_end: int):
    """One result pane per population, from a single file or selection: no second upload to switch between them."""
    parts, unmatched = _split_all(db, parsed)
    panes = []
    for key, (label, _) in GROUPS.items():
        ctx = None
        if parts[key].items:
            report = _build(db, parts[key], biz_start, biz_end)
            ctx = _report_ctx(report) if report.grand_total > 0 else None
        panes.append({"key": key, "label": label, "ctx": ctx})
    return (panes if any(p["ctx"] for p in panes) else None), unmatched


def _page(request: Request, db: Session, me: User, **extra):
    ctx = {"biz_start": 9, "biz_end": 18, "error": None, "panes": None, "datasets": _dataset_rows(db), "me": me,
           "unmatched": [], "can_edit": lambda ds: _can_edit(me, ds)}
    ctx.update(extra)
    return render(request, "ooh.html", ctx)


def _report_ctx(report: ooh.Report) -> dict:
    return ooh.chart_context(report)


def _build(db: Session, parsed: ooh.Parsed, biz_start: int, biz_end: int) -> ooh.Report:
    span = ooh.holiday_range(parsed)
    holidays = holiday_set(db, span[0], span[1]) if span else set()
    return ooh.build_report(parsed, holidays, biz_start, biz_end)


def _parsed_from(rows) -> ooh.Parsed:
    items = [ooh.Intervention(r.id, r.person, r.description, r.start, r.end, r.number) for r in rows]
    return ooh.Parsed(items=items, rows=len(items))


@router.get("")
def form(request: Request, me: User = Depends(current_user), db: Session = Depends(get_db)):
    _allowed(db, me)
    return _page(request, db, me)


@router.post("", dependencies=[Depends(verify_csrf)])
async def analyse(request: Request, file: UploadFile = File(...), biz_start: int = Form(9), biz_end: int = Form(18),
                  save: str = Form(""), name: str = Form(""), me: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    _allowed(db, me)
    try:
        parsed = ooh.parse_file(await file.read(ooh.MAX_BYTES + 1), file.filename or "")
        panes, unmatched = _panes(db, parsed, biz_start, biz_end)
    except ooh.OohError as exc:
        return _page(request, db, me, error=str(exc), biz_start=biz_start, biz_end=biz_end)
    saved = None
    if save:
        label = (name.strip() or (file.filename or "Export").rsplit(".", 1)[0])[:MAX_NAME]
        saved = OohDataset(name=label, source_filename=(file.filename or "")[:256], created_by_id=me.id)
        saved.interventions = [OohIntervention(number=i.number, person=i.person[:128], description=i.description, start=i.start, end=i.end)
                               for i in parsed.items]
        db.add(saved)
        db.flush()
        audit(db, me, "ooh_save", "ooh_dataset", saved.id, name=label, rows=len(parsed.items), file=file.filename)
        db.commit()
        flash(request, f"Saved as dataset '{label}' ({len(parsed.items)} interventions).", "success")
    if panes is None:
        return _page(request, db, me, unmatched=unmatched, biz_start=biz_start, biz_end=biz_end,
                     error="No out-of-hours time found for Experts or Officers in this file "
                           "(people are matched to Timelog users by name).")
    return _page(request, db, me, filename=file.filename, biz_start=biz_start, biz_end=biz_end, saved=saved,
                 unmatched=unmatched, panes=panes)


@router.get("/view")
def view(request: Request, ids: List[int] = Query(default=[]), biz_start: int = 9, biz_end: int = 18,
         me: User = Depends(current_user), db: Session = Depends(get_db)):
    """Visualise one or several saved datasets together (identical intervals of one person are merged)."""
    _allowed(db, me)
    sets = [db.get(OohDataset, i) for i in dict.fromkeys(ids)]
    sets = [d for d in sets if d is not None]
    if not sets:
        flash(request, "Select at least one saved dataset.", "error")
        return redirect("/analysis")
    rows = list(db.scalars(select(OohIntervention).where(OohIntervention.dataset_id.in_([d.id for d in sets]))))
    try:
        panes, unmatched = _panes(db, _parsed_from(rows), biz_start, biz_end)
    except ooh.OohError as exc:
        return _page(request, db, me, error=str(exc))
    if panes is None:
        return _page(request, db, me, unmatched=unmatched, error="No out-of-hours time for Experts or Officers in the selected data.")
    return _page(request, db, me, filename=", ".join(d.name for d in sets), biz_start=biz_start, biz_end=biz_end,
                 unmatched=unmatched, panes=panes)


# ------------------------------------------------------------------ one dataset: rename, edit rows, delete
@router.get("/datasets/{ds_id}")
def dataset(request: Request, ds_id: int, page: int = 1, person: str = "", me: User = Depends(current_user),
            db: Session = Depends(get_db)):
    ds = _dataset(db, me, ds_id)
    q = select(OohIntervention).where(OohIntervention.dataset_id == ds.id)
    if person:
        q = q.where(OohIntervention.person == person)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 1), pages)
    rows = list(db.scalars(q.order_by(OohIntervention.start, OohIntervention.id).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)))
    people = list(db.scalars(select(OohIntervention.person).where(OohIntervention.dataset_id == ds.id)
                             .distinct().order_by(OohIntervention.person)))
    return render(request, "ooh_dataset.html", {"ds": ds, "rows": rows, "page": page, "pages": pages, "total": total,
                                                "person": person, "people": people, "editable": _can_edit(me, ds)})


def _back(ds_id: int, request: Request, **params):
    return redirect(f"/analysis/datasets/{ds_id}", **params)


@router.post("/datasets/{ds_id}/rename", dependencies=[Depends(verify_csrf)])
def rename(request: Request, ds_id: int, name: str = Form(""), me: User = Depends(current_user),
           db: Session = Depends(get_db)):
    ds = _dataset(db, me, ds_id, edit=True)
    name = name.strip()[:MAX_NAME]
    if not name:
        flash(request, "The name cannot be empty.", "error")
    else:
        audit(db, me, "ooh_rename", "ooh_dataset", ds.id, old=ds.name, new=name)
        ds.name = name
        db.commit()
        flash(request, "Dataset renamed.", "success")
    return _back(ds_id, request)


def _parse_row(person: str, description: str, start: str, end: str, number: str = ""):
    person, description, number = person.strip(), description.strip(), number.strip()[:64]
    if not person or len(person) > 128:
        raise ValueError("Person is required (max 128 characters).")
    s, e = ooh._as_datetime(start.replace("T", " ")), ooh._as_datetime(end.replace("T", " "))
    if s is None or e is None:
        raise ValueError("Start and end must look like YYYY-MM-DD hh:mm:ss.")
    if e <= s:
        raise ValueError("The end must be after the start.")
    return person, description[:200], s, e, number


@router.post("/datasets/{ds_id}/rows", dependencies=[Depends(verify_csrf)])
def add_row(request: Request, ds_id: int, person: str = Form(""), description: str = Form(""), start: str = Form(""),
            end: str = Form(""), number: str = Form(""), me: User = Depends(current_user), db: Session = Depends(get_db)):
    ds = _dataset(db, me, ds_id, edit=True)
    try:
        p, d, s, e, n = _parse_row(person, description, start, end, number)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return _back(ds_id, request)
    row = OohIntervention(dataset_id=ds.id, number=n, person=p, description=d, start=s, end=e)
    db.add(row)
    db.flush()
    audit(db, me, "ooh_row_add", "ooh_dataset", ds.id, row=row.id, person=p)
    db.commit()
    flash(request, "Intervention added.", "success")
    return _back(ds_id, request)


@router.post("/datasets/{ds_id}/rows/{row_id}", dependencies=[Depends(verify_csrf)])
def update_row(request: Request, ds_id: int, row_id: int, person: str = Form(""), description: str = Form(""),
               start: str = Form(""), end: str = Form(""), number: str = Form(""), me: User = Depends(current_user),
               db: Session = Depends(get_db)):
    ds = _dataset(db, me, ds_id, edit=True)
    row = db.get(OohIntervention, row_id)
    if row is None or row.dataset_id != ds.id:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        row.person, row.description, row.start, row.end, row.number = _parse_row(person, description, start, end, number)
    except ValueError as exc:
        db.rollback()
        flash(request, f"Row {row_id}: {exc}", "error")
        return _back(ds_id, request)
    audit(db, me, "ooh_row_update", "ooh_dataset", ds.id, row=row.id)
    db.commit()
    flash(request, f"Row {row_id} saved.", "success")
    return _back(ds_id, request)


@router.post("/datasets/{ds_id}/rows/{row_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_row(request: Request, ds_id: int, row_id: int, me: User = Depends(current_user),
               db: Session = Depends(get_db)):
    ds = _dataset(db, me, ds_id, edit=True)
    row = db.get(OohIntervention, row_id)
    if row is None or row.dataset_id != ds.id:
        raise HTTPException(status_code=404, detail="Not found")
    audit(db, me, "ooh_row_delete", "ooh_dataset", ds.id, row=row.id, person=row.person)
    db.delete(row)
    db.commit()
    flash(request, "Intervention deleted.", "success")
    return _back(ds_id, request)


@router.post("/datasets/{ds_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_dataset(request: Request, ds_id: int, confirm: str = Form(""), me: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    ds = _dataset(db, me, ds_id, edit=True)
    if confirm != "DELETE":
        flash(request, "Type DELETE to confirm.", "error")
        return _back(ds_id, request)
    name = ds.name
    audit(db, me, "ooh_delete", "ooh_dataset", ds.id, name=name, rows=len(ds.interventions))
    db.delete(ds)
    db.commit()
    flash(request, f"Dataset '{name}' deleted.", "success")
    return redirect("/analysis")
