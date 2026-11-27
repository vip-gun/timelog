# Timelog — On-call & overtime tracking

Web application to declare **on-call duty** and **overtime**, have them validated by the line manager, and export an
aggregated monthly view for Finance/Payroll. It runs in Docker containers on-premise, without internet access.

This file is the single source of truth for business rules and design decisions.
Update it whenever a rule or decision changes (and keep README.md, the User Guide and Admin › Documentation in step).

---

## 1. Reference data

### 1.1 Day types and on-call rates
- Day type: `H` if the date is in the legal-holiday table, else `Sat` / `Sun`, else `W`. **Holiday takes precedence**:
  each day has exactly one type.
- Default grade `STD` (seeded): **W = 45, Sat = 70, Sun = 70, H = 90** (EUR per on-call day), editable per grade.
- Expert with an intervention on a day: the **daily rate is doubled**, whatever the duration.
- Non-experts: interventions are not paid through on-call; they are declared as overtime.
- One holiday table, admin-maintained (manual entry or `.ics` import), seeded with the Luxembourg holidays including
  24/12 ("Xmas eve").

### 1.2 Overtime pay codes and time in lieu
- Compensation per entry: **PAID** (default) or **TIL** (time in lieu, no payment).
- Payroll codes (hours, additive premiums — Finance applies the employee's hourly rate):

| Code | Meaning | Hours counted |
|---|---|---|
| HL100 | Normal (base 100%) | all paid OT hours |
| HL101 | Normal 50% premium | all paid OT hours |
| HL111 | Night 30% premium | paid night hours |
| HL130 | Sunday 70% premium | paid Sunday hours |
| HL121 | Legal holiday 200% premium | paid legal-holiday hours |

A weekend day that is also a holiday counts in HL121 only (never HL130).

- TIL credit factors (admin-editable, table `overtime_rules`):

| Day type | Day factor | Night factor |
|---|---|---|
| Weekday (W) | 1.5 | 1.8 |
| Saturday | 1.5 | 1.8 |
| Sunday | 2.2 | 2.5 |
| Legal holiday | 3.5 | 3.8 |

---

## 2. Business rules

- **Employee types**: normal and **Expert** (`users.is_expert`). Both can do on-call.
  **Only normal employees can file overtime** (enforced server-side, 403 for experts).
- **Management role** (`users.is_management`, Admin › Users form, tag "Management"): **approval only**. No on-call, no overtime, no submission: the On-call/Overtime nav items and dashboard tiles are hidden, `/oncall`, `/overtime`, `POST /submit` redirect home with a notice, and `workflow.get_or_create_sheet` refuses (so no sheet can ever exist for them). `teams.oncall_enabled` is false for them. They approve through the normal hierarchy (set them as line manager of the people concerned; skip-level rules unchanged), have Approvals and Statistics, and are excluded from `export.scope_users` (nothing to report). Not mapped to an LDAP group: set by hand. No "no line manager" banner for them. The User Guide shows only the approval parts.
- **User lifecycle** (Admin › Users, `routes/admin.py`): the **Team** field suggests the existing teams (`<datalist>`, free text still
  allowed for a new team). **Archive** (`users.is_archived`, `POST /admin/users/<id>/archive|unarchive`): disables sign-in
  (`is_active` false), hides the user from the list (an "Archived (N)" view shows them) and from the manager/team suggestions, and keeps
  every sheet, payroll record and audit entry; *Restore* re-activates. **Delete** (`POST /admin/users/<id>/delete`) removes the account
  and its unapproved sheets, signatures and notices, unlinks direct reports (no manager) and clears the actor of old audit rows; it is
  **refused** for yourself, the last active admin, a user with **approved sheets** or who recorded a payroll run (archive instead).
  Both are audited (`user_archive`, `user_unarchive`, `user_delete`). Archiving a manager leaves their reports pointing at them: the
  admin reassigns them (the flash says how many).
- **Submissions cleanup** (Admin › Submissions, `GET /admin/sheets`, `POST /admin/sheets/delete`, admin only): lists every sheet (filter user / year / kind / status, max 500) and **permanently deletes** the ticked ones whatever their status, with lines, signatures and payroll-run item (a run left without items is removed so its period reopens). Typed `DELETE` confirmation; audited `sheets_delete`. Meant for test instances; approved/sent sheets are accounting evidence, so back up first.
- **Edit-user form** (`admin/user_edit.html`): a label | field | note table in three groups (Identity, Organisation, Roles & access), styled by `.form-table`.
- **Teams** (Admin › Teams, `routes/admin.py`, `teams.py`, table `teams`, `templates/admin/teams.html`): membership stays the free-text
  `users.team` (LDAP `department`); the `teams` table only holds per-team switches. **`teams.oncall_enabled` (default on)**: a team with
  it off does **overtime only**. A team without a row, and a user without a team, always have on-call (`teams.oncall_enabled`). Effects:
  no On-call nav item/dashboard tiles/column, `/oncall` redirects to the dashboard (with a notice) unless that month already has an
  on-call sheet (then read-only with a banner), `POST /oncall/...` is refused, `workflow.month_submittable` skips on-call drafts. Existing
  sheets stay visible and exported. **Experts only declare on-call**, so a team with Experts cannot be switched off and an Expert cannot be
  put in such a team (`teams.expert_conflict`, enforced in the user form and the bulk edit). Teams page: create, rename (moves every member;
  LDAP members get `team_locked`), delete when it has no member at all, on-call switch. Audited (`team_create|update|delete`).
- **Preparing LDAP users in advance** (Admin › Users › Prepare LDAP users, `GET|POST /admin/users/ldap`, admin only): create
  `auth_source=ldap` accounts before the first sign-in from a list of usernames (up to 200). Optional directory lookup
  (`ldap_auth.lookup_users`, service account only: names, e-mail, team, manager, group flags; usernames not found are not
  created; a user outside `LDAP_GROUP_REQUIRED` is created with a warning) via `sync_user(login=False)`; optional team / manager /
  Expert / grade applied to all. A team or manager set here gets `team_locked` / `manager_locked`, otherwise the directory values apply.
  At first login the existing record is matched by `ldap_dn` or username (`_upsert`) and completed as usual; LDAP group flags still
  override Admin/Expert/Procurement. Existing usernames (local or LDAP) are skipped. The users list tags them "Not signed in yet".
  Audited (`user_preprovision`).
- **Bulk user edit** (Admin › Users, `POST /admin/users/bulk`): tick users (select-all in `app.js`), fill only the fields to change: team (or
  clear), line manager (or none), Expert yes/no, **Management yes/no** (an Expert cannot be Management), grade, and **Reload from the directory**
  (LDAP users only: clears `team_locked`/`manager_locked`, looks them up with the service account via `lookup_users` and applies the directory values
  through `sync_user(login=False)` before the other fields; local accounts and users not found are skipped and listed; a directory outage aborts the batch). Users that would break a rule (hierarchy cycle, Expert in an overtime-only team) are
  skipped and listed; the rest are applied (one commit per user). "Lock" (default on) sets `manager_locked` / `team_locked` for LDAP users so
  the next login does not overwrite the change (`users.team_locked`, honoured by `ldap_auth._upsert`). Audited (`users_bulk_update`).
- **Grades**: on-call daily rates are configurable **per grade** (`grades` table). Each user has a grade (falls back to the
  default grade).
- **On-call amount per day** = `rate[grade][day_type] × (2 if expert and intervention else 1)`.
- **Rotation weeks**: cover is assigned per week. The start weekday and a handover time (default **Friday 18:00**,
  informational: days are booked whole) are editable in Admin › Settings (`app_settings` keys `oncall_week_start`,
  `oncall_handover`; the `ONCALL_WEEK_START` env value is the fallback; existing bookings are not changed). The on-call grid
  groups days by rotation week with a **week switch** (`wk_<start date>`) that books all 7 days. A week straddling two months
  books the other month's days on that month's sheet (`workflow.add_oncall_days`, keeps existing days); if that sheet is
  submitted/approved those days are skipped with a warning. The switch shows "on" when all 7 days are booked across both months;
  client JS never switches it on automatically for a spilling week (it would silently book the other month).
- **On-call conflicts**: two employees cannot be on call at the same time. For managers/admins (never for the employee), an
  on-call sheet is flagged where another employee has the same day on a **submitted or approved** sheet
  (`workflow.oncall_conflicts`; drafts do not count): red banner + highlighted rows (names, status) on the on-call sheet and a
  "N day(s) in conflict" tag on the Approvals list. Colleagues outside the viewer's scope are **not named** ("1 other employee
  outside your team"), to respect the visibility rule. Highlight only: approval is not blocked. The check is organisation-wide,
  not per team.
- **Where intervention details are recorded**:
  - *Experts*: on the on-call grid (Intervention checkbox → double rate, Incident #, Comment).
  - *Non-experts*: **only on the overtime sheet** (hours, ServiceNow #, description). Their on-call grid has no
    Incident/Comment/Intervention inputs (the server ignores such fields); the Intervention column shows read-only the overtime
    hours logged on each on-call day, linking to the overtime sheet.
  - Overtime entries are **automatically flagged as on-call interventions** when their date is a booked on-call day
    (`workflow.oncall_dates`, any sheet status, across months). Detected when the entry is added and **re-detected at
    submission** (`recompute_overtime`). The form shows a live "on-call day" hint (`data-oncall-dates`).
- **History import / backfill** (Admin › Import History, `GET|POST /admin/import`, `history_import.py`, `templates/admin/import.html`, admin only): upload the **on-call** and/or **overtime workbook(s)** (several files per kind, up to 40; the Payroll page gives one per month; **a workbook may also hold one sheet per month**, every sheet is read and its month comes from the dates in its header, not from its name) in the layout of the Finance exports (`export_finance.py`; tabs = work months; on-call: dates in row 3 D..AH, one row per employee, "- Incidents/calls" row = Expert intervention, rates from the pricing block; overtime: month in A2, dates in row 2, last name / first name / Paid-TIL, hours per day, night hours AN..AQ, TIL credit AR/AX; "(late)" tabs are skipped with a warning). Names are matched to users (first+last, last+first, display name, username; unknown or ambiguous = skipped and listed). Each tab/employee becomes a sheet **already approved and marked sent to Finance** (payroll period = work month, `payroll_runs` created or extended, decided_by = the admin, comment "Imported from a Finance workbook (history)", no notices/e-mails); existing sheets of that user/kind/month are never touched; Management users, Expert overtime and teams without on-call are skipped. On-call: day type from the holiday table, amount doubled for an Expert incident. **Overtime times are unknown**: each day is one entry from 00:00 of the length of the hours (description says "times unknown"); night hours per day type and the TIL credit are spread over that type's days so the sheet totals equal the workbook. A **dry run** (default on) shows the result without saving; one audit entry `history_import`. Closed periods stay closed, so the imported months can no longer receive new approvals.
- **Statistics charts** (`/team`, `stats_charts.py`, `templates/_chart_blocks.html`, shared with the Analysis page): a **vertical menu on the left with three sub-tabs** (`?group=overview|experts|officers`, default Overview; `non-experts` is accepted as an alias for officers): **Overview** (the four stat tiles, the "Work done" table and the late claims), then **Experts** and **Officers** with the charts. The month navigation and the months / sheets / team filters sit above the menu and apply to every tab. The chart tabs are built from what is **logged in Timelog** for the people in the viewer's scope (team filter applies): **Officers** tab: **overtime hours** per person by day type (bars + table, monthly evolution, per-person trend on a shared scale, person x month table), **Experts** tab: **on-call days with an intervention** (Experts tick Intervention on the on-call sheet; a non-Expert's intervention is an overtime entry, so it is in the Officers tab; on-call days without an intervention are not charted), in days. Window = last 6/12/24 months ending at the selected month; sheets = submitted + approved, or approved only (`months`, `status` query parameters); drafts and rejected sheets are left out; by work month of the sheet. Charts are inline SVG (no JS, no inline style); every chart has **labelled axes** (titles "Hours" or "Days" and "Month" on the column charts, "Hours"/"Days" with a 0-100 % scale and "Person" on the bar chart).
- **Analysis (out-of-hours report)** (`/analysis`, `/ooh` redirects with 301; `routes/ooh.py`, `ooh_report.py`, `templates/ooh.html`, nav "Analysis" after Statistics; managers and admins only, others 404): upload a **ServiceNow export** (`.xlsx` or `.csv`, ≤ 10 MB, ≤ 50 000 rows) with the columns *Short description, Assigned to, Planned start date, Planned end date* (`YYYY-MM-DD hh:mm:ss` text or real Excel datetimes; header names case-insensitive). **Not stored unless asked**: the file is analysed in memory; tick "Save this data as a dataset" to keep it (see below). Each intervention is split per calendar day and classified with the holiday table (holiday wins over weekend): **business day = only the hours outside the working window** (form fields, default 09:00-18:00), Saturday / Sunday / public holiday = every hour. Overlapping interventions of one person are merged (no double count); rows without assignee/valid dates/end > start are skipped and listed; interventions > 24 h are flagged. **One upload, two result panes, Experts and Officers** (Officers = non-Experts): the export holds both populations, so there is **no per-tab upload**; the results come back in two panes switched from a **vertical menu on the left** (JS-free, `:target`; first pane shown by default). "Assigned to" is matched to Timelog users by name (`history_import.user_index/_find`: first+last, last+first, display name, username); the `users.is_expert` flag picks the pane (the Timelog name replaces the spelling in the file, so variants merge), names matching no single user are listed in a banner and left out of both. The optional column **Number** (ServiceNow change request number; aliases "Change request number") is read as well, stored with saved datasets (`ooh_interventions.number`, editable on the dataset page) and shown with every item. **Clicking a chart opens the list of items behind it in a popup** (a `<button data-dialog>` around each chart opens a `<dialog class="chart-dialog">`, wired in `app.js`: Close button, Esc and a click on the backdrop close it; the bars and the monthly chart list all items of the pane, each per-person trend chart that person's items; first 500 shown). Statistics charts do the same with the logged overtime entries (ServiceNow # = `incident_ref`) or expert intervention days. A banner at the top states the **required columns** (Short description, Assigned to, Planned start date, Planned end date, `YYYY-MM-DD hh:mm:ss`). Saving a dataset stores **all** rows of the file; the split is applied when visualising (also for `/analysis/view?ids=`, which shows both panes). Output, all server-rendered inline SVG/tables (no JS, no inline style, dark-mode tokens `--t-W/SAT/SUN/H`, `--heat-*`): KPI tiles, stacked bars + table per person, monthly team columns, small multiples per person on one shared scale, person × month heat table, detail and skipped rows. Local time, no time-zone handling.
  **Saved datasets** (tables `ooh_datasets` + `ooh_interventions`, included in Admin › Backup automatically): saving stores the valid rows (not the skipped ones) with a name, source file name, creator and time. `/analysis` lists them; tick several and **Visualise selected** (`/analysis/view?ids=`) to combine them (identical or overlapping intervals of one person are merged, so re-saved exports do not double count). `/analysis/datasets/<id>` shows the rows (100 per page, person filter) and, **for the creator or an administrator only** (others are read-only; a non-owner POST gets 404), allows rename, edit/delete/add a row (start/end `YYYY-MM-DD hh:mm:ss`, end after start) and delete the dataset (typed `DELETE`). Viewing is for managers and administrators. Audited: `ooh_save`, `ooh_rename`, `ooh_row_add|update|delete`, `ooh_delete`.
- **Overtime grid** (`/overtime/<y>/<m>`, `POST /overtime/<y>/<m>/save`, `workflow.save_overtime_rows`): the page shows a compact table with **one row per day** (date, From, To, [Comp.], ServiceNow #, Description of Intervention) from the oldest declarable day to today; "+" (JS) adds an extra row for another session the same day, "✕" clears a row. Save applies all rows at once, all or nothing: blank row = nothing (or deletes its saved entry), changed row = replaced, unchanged = kept, unposted entries untouched. It edits the latest sheet when draft/rejected, opens a supplementary sheet after an approval, and is hidden (read-only tables) while the sheet is submitted. `POST /overtime/entries` (single entry) remains as a plain endpoint. The overtime label for the ticket field is "ServiceNow #" (field `incident_ref`; on-call still says Incident #).
- **Overtime entry** = date + start/end time in **15-minute steps** (`OVERTIME_STEP_MINUTES`, default 15, so the minimum
  entry is 15 min). The time fields are text inputs with a `<datalist>` of suggestions: the user may pick one or **type** it
  (`08:45`, `8.45`, `8h45`, `0845`, `8`; `calendar_utils.parse_hhmm` is tolerant, JS normalises to HH:MM on blur; `24:00` allowed
  for the end). The sheet shows hours by day type split into **day and night hours** with the factors (table in the overtime
  page, `calc.OvertimeTotals.night_by_type`). Hours and night hours
  are computed; night window `NIGHT_START`/`NIGHT_END` (default 22:00–06:00). An entry crossing midnight is **split
  automatically** into one entry per calendar day (refused if the second part falls in another month: the user enters it in the
  next month's sheet). Entries of one user may not overlap. No future dates. End `24:00`/`00:00` = end of day.
- **Weekend/holiday overtime** needs no pre-approval. The unused `overtime_entries.preapproved` column stays in the schema and in the
  signature digest so old signatures stay valid.
- **Back-dated overtime**: employees may log overtime for any day up to `OVERTIME_LOOKBACK_MONTHS` (default 12) back. The entry
  goes to the sheet of its own work month. If that month's sheet is draft/rejected → added to it; submitted → refused (withdraw
  first); **approved → a supplementary sheet** (`sheets.seq` = 2, 3…) is opened for that month and goes through the normal
  approval. Supplementary sheets exist for overtime only (an on-call month stays a single sheet; an admin can reopen it).
- **Snapshots**: rates/factors are snapshotted at submission (`day_type`, `rate`, `amount` on rows, grade code and expert flag on
  the sheet) so later rate changes never alter submitted/approved figures. In draft, values are recomputed on every save.
- **Public holidays import**: admins upload an `.ics` file (Admin › Legal holidays). Own RFC 5545 reader (`ics_import.py`, no
  dependency): all-day, multi-day (exclusive DTEND / DURATION PnD), timed events (start date), folded lines, escapes; RRULE and
  cancelled events skipped and reported; max 1 MB. Modes: *merge* (add/rename by date) or *replace* (for each year present in the
  file, dates absent from the file are deleted). Audited. Draft sheets pick the change up at next save; submitted/approved sheets
  keep their snapshot.
- **Time in lieu switch** (Admin › Settings, `til_enabled`, default on). When off: the PAID/TIL choice, TIL columns/tiles/credits,
  export TIL columns and the "TIL factors" admin tab are hidden (`features.til`), new TIL entries are refused server-side and a
  sheet still holding TIL entries cannot be submitted (re-enter them as paid). Existing TIL data stays visible and exported.
- **Amount visibility** (Admin › Settings, `show_amounts_to_employees`, default on): when off, employees do not receive money
  amounts or rates for their own sheets (stat tiles, grid column, totals, dashboard, the `data-rates` attribute used by JS).
  Admins always see amounts; managers see them for people below them (`app_settings.can_see_amounts`). Overtime hours / TIL are
  not money and stay visible.
- **Signature on submission** (Admin › Settings, `signature_required`, default off). When on, submitting requires a signature
  plus a ticked confirmation statement (otherwise refused server-side; JS also blocks the form). The signature is drawn on a
  canvas, **or uploaded as a picture** (PNG/JPEG/WebP/GIF ≤ 5 MB, fitted into the canvas client-side), and always posted as a PNG
  data URL validated server-side (PNG magic, ≤ 300 KB, ≤ 2000 px; no image library). "Remember this signature" stores the
  validated PNG in `saved_signatures` (one row per user); it is pre-loaded on later submissions, deletable ("Forget it",
  `POST /signature/forget`), and `/signature/saved.png` is visible to its owner only. Stored in `signatures` with the **SHA-256 of
  the sheet content** (canonical JSON; numbers normalised to 2 decimals because in-memory and DB-loaded Decimals differ in text),
  time, IP and user agent. History is kept (a resubmission after rejection adds a new signature); only the current submission's
  signature is shown (`Sheet.signature`). Owner, managers above and admins see the image (`/signatures/<id>.png`, same visibility
  as the sheet) with a ✓/⚠ "content unchanged since signature" check; the export has a "Signed (UTC)" column. This is a *simple
  electronic signature* (evidence of intent and integrity), not a qualified one.

## 3. Workflow

One **sheet** per (user, kind ∈ {oncall, overtime}, year, month, seq).

```
draft ──submit──▶ submitted ──approve──▶ approved
  ▲                 │   │
  └──withdraw───────┘   └──reject(comment)──▶ rejected ──(edit)──▶ submit again
approved ──reopen (admin only, audited, latest sheet of the month only)──▶ draft
```
- Editable only in `draft` / `rejected`.
- **One submission for the month**: on-call and overtime keep separate drafts/sheets (approved separately) but the employee has a
  **single submit process**: `POST /submit/<year>/<month>` (`workflow.submit_month`) sends every editable sheet of that month that
  has content (on-call sheet, overtime sheet and supplementary overtime sheets) together, with one comment and one signature (a
  `signatures` row per sheet, each with its own digest). All or nothing: if one sheet is refused (e.g. unjustified weekend
  overtime) nothing is submitted. The submit panel is on both pages; the on-call page's "Save & submit" first saves the grid.
  Withdraw / reject / approve stay per sheet.
- **Navigation** (`base.html`): Dashboard, On-call, Overtime, Approvals, Statistics (managers/admin; `/team`, `access.can_view_team`),
  Analysis (managers/admin), Payroll (procurement/admin only), **User Guide** (everyone, adapts to role and team: no on-call parts for overtime-only teams, a "For managers" section for managers/admins; last item of the nav, just before Admin; outlined pill, `.guide-link`), then **Admin on the right, just before the user block**.
- **Month sync**: the month last viewed on the on-call or overtime page is kept in the session (`remember_month`); the nav links
  `/oncall` and `/overtime` open that month (else the current one). A "Go to Current Month" button returns to today's month; the arrows and month title stay on the right and the button sits **just left of the left arrow** (out of the flow), so showing it never moves them.
- **Approver = the direct line manager, any manager above them in the chain (skip-level approval, `access.is_above`), or an admin.**
  Nobody approves their own sheet; peers and people outside the chain cannot. The Approvals list and the dashboard counter of a
  manager cover everyone below them (`subordinate_ids`); the audit entry carries `skip_level: true` when the approver is not the
  direct manager. Submission e-mails still go to the direct manager.
- **Payroll period** (`sheets.payroll_year/month`) is fixed at approval by `calc.payroll_period`: the **first open payroll month, not
  before the work month, whose cut-off date has not passed** (approved on or before the date = paid with that month). **The Finance
  cut-off is one explicit date per cut-off month, applying to the previous month's work** (work of month n is declared in n+1, so the July row is the date for June work; stored in `payroll_cutoffs` under the work/payroll month, the calendar page and `routes/payroll.py` shift by one month) (table `payroll_cutoffs`, `PayrollCutoff`), edited by **IT Procurement or admin** on
  `/payroll/cutoffs` (cut-off calendar, one year at a time, linked from Payroll and Admin › Settings; date between the 1st of the month
  and 150 days later; blank = delete; months already sent to Finance are locked; audited `payroll_cutoffs_update`). A month **without a
  date uses the default pattern**: day `payroll_cutoff_day` of the following month (default **5**, `PAYROLL_CUTOFF_DAY` env, editable in
  Admin › Settings as `payroll_cutoff_day`). Resolved by `app_settings.cutoff_rule` / `cutoff_schedule`. Changing a date affects only
  sheets approved afterwards. **A period already sent to Finance is closed** (`closed_periods`): the sheet moves on to the next period
  even if its cut-off has not passed. A sheet paid in a later period than its work month is a **late claim** (`Sheet.is_late`). The User
  Guide lists the upcoming dates.
- **IT Procurement role and payroll runs** (`users.is_procurement`, set in Admin › Users or by `LDAP_GROUP_PROCUREMENT`;
  `routes/payroll.py`, `templates/payroll.html`, nav "Payroll", dependency `payroll_user` = procurement or admin). Procurement
  exports the **whole organisation** (`export.scope_users`, `access.can_export`) but cannot open individual sheets (404). On
  `/payroll` (default period = oldest unsent period holding approved sheets, else the period an approval today of last month's work
  would hit; banner for older unsent periods) it sees what is included (late items listed first with approval dates), what is
  still awaiting approval, and the download status. **Control**: every whole-organisation download of the on-call / overtime report
  is audited with a digest of the sheet ids of the period (`workflow.period_digest`, `last_export`); `POST
  /payroll/<y>/<m>/sent` (`workflow.mark_sent`) requires a ticked confirmation, both reports downloaded by that user **and still
  matching the current data** (if something was approved since, download again), a non-empty period, and an unclosed period. It
  writes `payroll_runs` + `payroll_run_items` (one run per period, one item per sheet; `Sheet.sent_at`, "Sent to Finance" shown in
  the sheet meta), an audit entry `payroll_sent`, in-app notices to the employees (and e-mail), and notices to submitters still
  waiting ("paid with the following month at the earliest"). A sent sheet cannot be reopened.
- **Notification bell** (`bell.py`, `<details class="bell">` in `base.html`, left of the user name, works without JS; `app.js` closes it on outside click/Escape): lists unread notices (dismissable), requests (managers/admins: sheets waiting for approval, linking to Approvals), the user's rejected sheets with the comment, and sheets approved in the last 14 days. The red badge counts what needs attention (unread notices + approval request + rejected sheets); approvals are informational. The banners at the top of pages remain. The Admin nav button is dark red (`--burgundy` tokens).
- **Payroll deadline box** (`workflow.cutoff_banner`, `.deadline-box` in the `base.html` header, between the nav/Admin and the user block, hidden ≤ 1440 px): "Payroll Deadline: DD/MM/YYYY" = the Finance cut-off that applies today to last month's work (first open payroll period whose cut-off has not passed, via `expected_period` + `cutoff_rule`); always in a red shade (`--err-*`), solid red with white text when ≤ 3 days are left; the tooltip gives days left and, when last month's own cut-off has passed, "paid with <month>".
- **Notices** (`notifications` table, shown as banners at the top of every page until dismissed, `POST /notices/<id>/read`,
  owner only; e-mail too when SMTP is configured): late approval (`workflow.decide`: "paid with <month>, not <work month>"),
  late submission / logging for a period past its cut-off or closed (`workflow.timing_message`, `warn_payroll_timing`; on-time
  submissions only get a flash with the approval deadline), payroll sent, still-waiting after a run.
- **Finance exports** are per **payroll period** (all approved sheets with that payroll period: regular + late claims) and come in
  three downloads under **Payroll** (`routes/payroll.py`, IT Procurement or admin only, whole organisation; managers have no
  downloads, their Statistics page is view-only):
  - `/payroll/export/oncall` → `Y<yy>M<mm>_IT_OnCall.xlsx` and `/payroll/export/overtime` → `Y<yy>M<mm>_IT_Overtime.xlsx`
    (`export_finance.py`), in the **layout of the two reference workbooks**: the layouts are sanitized prototype tabs
    `app/timelog/export_templates/{oncall,overtime}_layout.xlsx` (formats, widths, merged cells, conditional formats, fixed headings;
    no names, no company address, no external links; generated once by `scripts/make-export-templates.py` from the git-ignored
    `templates/` folder; allowed through `.gitignore`/`.dockerignore`/pre-commit). The exporter copies the row styles and writes
    **values, not formulas** (on-call amounts are the snapshotted rates; one bucket per day, a weekend holiday counts as a holiday).
    One tab per work month: the payroll month first, earlier work months (late claims) as extra tabs named "<tab> (late)".
    *On-call*: team blocks, one row per employee with `1` on on-call days (+ "- Incidents/calls" row for Experts), pricing sheet
    below (pricing row = employee row + title row), hidden `Support` tab (holidays, default-grade prices). *Overtime*: `READ FIRST` +
    one row per employee **and compensation** (Paid / TIL), hours per day (holiday days headed `LH`), AI–AM totals by day type,
    AN–AQ night hours per day type, AR/AX TIL credit, AS–AW payroll codes (AU = night hours).
  - On-call export pricing block, as in the reference sheet: banner "PRICING SHEET (Automatic - no input please)  -  ON CALL PRIME request"
    (the parenthesis in red, centred across D:AH), the work month in AK (e.g. "June 26"), and the **"Addition for Payroll's Check"** box in
    AK:AM: employee name (AK) and amount (AL = AI of the row), a **team subtotal in AM** on the blank row closing each team (double rule),
    the team total also in AI of the pricing team header row, and the grand total in AL of the closing row. Values, not formulas.
  - `/payroll/export/control` → the detailed control workbook (`export.py`): Summary (status of every team member, counts late claims
    awaiting validation), On-Call, On-Call Grid, Overtime Payroll, detail tabs.
- Every transition is written to `audit_log`.
- Optional e-mail notifications (SMTP relay, disabled unless `SMTP_HOST` is set).

## 4. Security model

- Roles: **employee** (everyone), **Management** (`is_management`: approves only, declares nothing), **manager** (implicit: has direct reports), **IT Procurement** (`is_procurement`: whole-organisation
  payroll reports and the sent-to-Finance record, no access to individual sheets), **admin** (`is_admin`).
- Visibility (`access.py`, the single place for authz): a user sees their own sheets; a manager sees sheets of **all users below
  them in the hierarchy** (recursive, cycle-safe); admin sees all. **Colleagues never see each other.** Unauthorised access to
  another user's data returns **404** (no existence disclosure).
- Approve/reject: any manager above the employee in the hierarchy (direct or skip-level) or an admin.
- Reports/exports: IT Procurement and admin only, whole organisation (`export.scope_users`); a manager has the Statistics page
  (their subtree) but no downloads.
- Sessions: signed cookie (Starlette `SessionMiddleware`), `HttpOnly`, `SameSite=Lax`, `Secure` when `SESSION_COOKIE_SECURE=true`
  (default in compose). Lifetime `SESSION_MAX_AGE`.
- **CSRF**: per-session token, required on every POST (hidden `csrf_token` field).
- Local passwords hashed with **PBKDF2-SHA256, 600k iterations** (stdlib).
- Login throttling: in-memory per username+IP (5 failures / 15 min), per process only (AD lockout policy also applies to LDAP users).
- LDAP: user input escaped with `escape_filter_chars`; **empty passwords rejected** (no anonymous bind success).
- CSP `default-src 'self'` (+ `img-src data:`): no inline JS/CSS, no external resources. JS sets CSS custom properties through the
  CSSOM (allowed); templates contain no inline `style`/`<script>`.

## 5. Authentication

- **LDAP settings are editable in Admin › Settings**. Stored in `app_settings` under `ldap.*` keys; a stored value **overrides**
  the `LDAP_*` env variable, "Reset to .env values" deletes them (`app_settings.effective_settings` merges both; used by the login
  route). The bind password is **write-only** and encrypted with Fernet (`cryptography`), key = SHA-256 of `SECRET_KEY`: rotating
  `SECRET_KEY` makes it unreadable (the UI asks to re-enter; the env value is used meanwhile). The CA certificate can be pasted as
  PEM (`ldap_ca_cert_data`, passed to ldap3 `ca_certs_data`; wins over `LDAP_CA_CERT_FILE`). Validation: ldap:// or ldaps://, not
  ldaps + StartTLS, filter contains `{username}`, timeout 1-120 s, PEM. **Test connection** (`ldap_auth.diagnose`, shown inside the
  LDAP section) uses the saved settings: connect, service bind, optional user lookup with groups/manager — no user password. Every
  change/test is audited (never the password).
- **Local users**: for bootstrap/debugging. `BOOTSTRAP_ADMIN_USERNAME`/`BOOTSTRAP_ADMIN_PASSWORD` create an admin on first start if
  no admin exists. `LOCAL_LOGIN_ENABLED=false` disables them; `LOCAL_LOGIN_ADMIN_ONLY=true` keeps only admins (recommended once LDAP
  works: one local break-glass admin).
- **LDAP / Active Directory** (core usage): search-then-bind with a service account (`LDAP_BIND_DN`), user filter `LDAP_USER_FILTER`
  (default `(&(objectClass=user)(sAMAccountName={username}))`). LDAPS or StartTLS with the Microsoft PKI root/issuing CA (PEM). On
  each login the user is upserted: names, mail, team (`department`), and **manager** from the AD `manager` attribute
  (the manager entry is upserted too, so the hierarchy exists before the manager logs in) unless `users.manager_locked` (admin
  override). Group mapping (nested groups via the AD matching rule `1.2.840.113556.1.4.1941`, not supported by OpenLDAP):
  `LDAP_GROUP_ADMIN` → is_admin, `LDAP_GROUP_EXPERT` → is_expert, `LDAP_GROUP_PROCUREMENT` → is_procurement, `LDAP_GROUP_REQUIRED` → may log in. An empty group variable means
  the flag is managed manually in the admin UI.
- Login order: if the username exists as a **local** user → local check; otherwise LDAP.

## 6. Architecture & technology (all open source, no internet at runtime)

| Component | Choice |
|---|---|
| Backend | Python 3.12, **FastAPI**, Uvicorn |
| UI | **Server-rendered Jinja2** + plain CSS + small vanilla JS (no Node build, no CDN, strict CSP) |
| ORM / DB | **SQLAlchemy 2.0** + **PostgreSQL 16** (SQLite for tests/dev) |
| LDAP | **ldap3** (pure Python) |
| Export | **openpyxl** (`.xlsx`) |
| Reverse proxy | **nginx** (TLS termination) |
| Containers | Docker Compose: `proxy`, `app`, `db`; only `proxy` publishes ports |

- Schema created with `create_all` at startup + idempotent seeding (grades, holidays, TIL rules, bootstrap admin). New tables
  appear automatically and **added columns are added by `db.ensure_columns`** (additive, idempotent; a NOT NULL column with a
  Python-side default is added nullable). Renames, type changes and drops are not covered — add Alembic before the first such change
  in production.
- Python **3.12** everywhere (containers and the dev venv: `/opt/homebrew/bin/python3.12 -m venv app/.venv`).
  `requirements.txt` pins direct **and transitive** dependencies; refresh them deliberately (install, `pip check`, tests, then the
  Dependency-Track scan).
- The UI stays server-rendered (no React/Next.js): it is forms and tables with light interactivity, and a Node stack would add a
  runtime, an npm supply chain to mirror for air-gapped builds, an API + auth/CSRF rework and duplicated validation. If partial-page
  updates are ever wanted, vendor **htmx** (one static file, no build).
- App runs Uvicorn with `--proxy-headers` (`FORWARDED_ALLOW_IPS`) so URLs/redirects/secure cookies are right behind the proxy; the
  app port is not published outside the Docker network.

### 6.1 Design system
- **Visual identity shared with ResOps** (sibling project `../resource-management`). Palette (CSS variables at the top of `static/style.css`; token names from the first design were kept, the values changed, so `--primary`/`--secondary` are now navy):

| Token | Light | Dark |
|---|---|---|
| primary / secondary / `--primary-solid` (buttons, active pill, checked boxes) | `#2C3E50` navy | `#BDC3C7` light grey (`--on-solid` `#17212B`) |
| `--highlight` (focus ring, stat-tile mark, Sign Out) | `#F39C12` orange | same |
| red accent / error | `#E74C3C` / `#B93A2C` | `#EC5B4B` / `#FF8E80` |
| background / surface / text | `#F3F5F8` / `#FFFFFF` / `#1F2D3A` | `#111820` / `#1A2430` / `#ECF0F4` |
| success / warning | `#1B7A4B` / `#9A5B00` | `#66D19A` / `#F6B445` |

- Look: **floating rounded white top bar** (`--chrome`, 20 px radius, shadow, 10 px from the edges; logo + name + tagline on the left, nav tabs in a grey pill group with the active tab a filled navy pill, bell/theme/user and an **orange Sign Out button** on the right, as in ResOps), soft rounded cards (16 px) with a soft shadow, 8 px buttons/inputs with an orange focus ring, sentence-case buttons, soft radial gradient background; **no pattern on the app background** (it looked noisy), a login screen with a navy hero (timeline bars + clock art), rounded toasts/banners, pill badges and tags. Brand mark: navy rounded tile with a clock (grey ring, orange hand, red base bar), also the favicon. **System font stack, no web fonts** (the former self-hosted fonts were removed); monospace only for figures/dates/refs.
- **Dark mode**: automatic from the OS (`prefers-color-scheme: dark`), per-user override with the header button (cycles auto → dark → light, `users.theme`, `POST /preferences/theme`, rendered as `<html data-theme>` so there is no flash and no script; strict CSP kept). The dark values exist twice in `style.css` (OS-driven block and `data-theme="dark"` block): edit both. New colours must be tokens (`--surface-2`, `--ok-bg`, `--accent-text`, `--accent-bg`, `--primary-solid` + `--on-solid`), never hard-coded hex. Signature canvas/images stay white; print stays light. `<meta name="color-scheme" content="light dark">` in `base.html`.
- **Drop-down fields** (`select`) draw their own chevron (`--chevron` token, data-URI SVG, light and dark values; `appearance: none`, 0.95 rem from the edge, extra right padding), so the arrow never touches the border; keep `background-color` (not the `background` shorthand) on `input/select/textarea` rules or the arrow disappears.
- Motion is short and linear (≤ 0.2 s fade-in of rows/cards, toasts slide 16 px, brief colour flash when an amount changes, 0.4 s count-up), all disabled by `prefers-reduced-motion`.
- **The header must stay perfectly static** (user requirement): no entrance animation, **no View Transitions**, fixed height (60 px, nav never wraps), `scrollbar-gutter: stable` + `overflow-y: scroll`. Navigation is plain page loads. Static assets are cache-busted with `?v={{ build.asset_tag }}`. Below 1700 px the header hides the user name and the tagline and tightens the tabs so the nav fits; the payroll deadline box hides ≤ 1440 px. The Admin button stays dark red (`--burgundy`); the User Guide tab is an outlined pill.
- The browser tab title and the nav labels are title-cased (`titlecase` Jinja filter in `web.py`: "Timelog - Overtime Aug 2026", "Sign In", "Statistics").

### 6.2 HTTPS / Microsoft PKI
- nginx terminates TLS with `certs/server.crt` (server cert **+ intermediate chain**, PEM) and `certs/server.key` (unencrypted),
  HTTP → HTTPS redirect, TLS 1.2/1.3, HSTS, security headers. `HTTPS_PORT_SUFFIX=:<port>` keeps the port in the redirect when HTTPS
  is not on 443.
- **The certificate stays file-based** (uploading it from the admin UI was rejected: it would put the TLS private key in an
  app-writable volume and need a way to reload nginx). The PKI usually delivers a **PFX**: `deploy/convert-pfx.*` (or the OpenSSL
  commands in Admin › Documentation) turn it into the PEM files, then `docker compose restart proxy`.
- `scripts/make-csr.sh` generates key + CSR with SAN for AD CS (`certreq -submit -attrib "CertificateTemplate:WebServer"`);
  `scripts/make-selfsigned.sh` for dev. `timelog/certtool.py` does the same inside the app image (see §8.4).

### 6.3 Version & build information
`timelog/__init__.py:__version__` (semver, bump on each release) + build timestamp/commit shown in the footer of every page and
returned by `/healthz` (`ok <version> <timestamp> [commit]`). The Dockerfile writes `timelog/BUILD_TIMESTAMP` and
`timelog/BUILD_COMMIT` (build args; `scripts/build.sh` / `make-offline-package.sh` pass them; env vars of the same names override).
Without them the footer says "dev build, started <server start time>" (`version.py`).

### 6.4 Admin › Backup
- Logical backup = gzip JSON of every table of `Base.metadata` (database independent, one consistent snapshot: REPEATABLE READ on
  PostgreSQL), **encrypted with AES-256-GCM** (`backup.py`): file `"TLBK" | version | salt(16) | nonce(12) | ciphertext+tag`, key =
  PBKDF2-HMAC-SHA256 (600k) of `BACKUP_PASSPHRASE` (else derived from `SECRET_KEY`), header authenticated. Files
  `timelog-YYYYmmdd-HHMMSS[-auto|-pre|-uploaded].tlbk` (strict name regex, no path input, unique per second), mode 600, in
  `BACKUP_DIR` (`/backups`, bind-mounted from `./backups` on the host; the installer chowns it to uid 10001, the app user).
- Create, download, upload (decrypted + validated; optional "passphrase of that file" so a backup from another server is
  re-encrypted with the local key; unencrypted uploads refused), delete, **restore** (typed `RESTORE`, "pre-restore" safety copy,
  one transaction, self-referencing FKs linked after insertion, PostgreSQL sequences reset, session cleared afterwards).
- **Schedule** (admin UI, `app_settings` keys `backup.*`): on/off, daily or weekly (+ weekday), time in 30-minute steps (UTC),
  number of automatic backups kept (1–365; manual/uploaded/pre-restore never pruned). The scheduler thread (`BACKUP_SCHEDULER`, on in
  containers, off in code defaults and tests) re-reads the schedule each minute; saving a schedule does not trigger a catch-up run of
  a slot already passed (`since`).
- Everything is audited. Backups hold personal data and the Fernet-encrypted LDAP password (a restore needs the same `SECRET_KEY`).
  `scripts/backup-db.sh` (native `pg_dump`, unencrypted) remains as an alternative for cron.

### 6.4b User Guide (employees)
`/guide` (`routes/guide.py`, `templates/guide.html`, nav "User Guide" for every signed-in user, last nav item before Admin). A task-oriented guide
for employees: the short version, on-call, overtime, interventions, submitting, statuses, cut-off/late claims, FAQ. It adapts to
the viewer (Experts get no overtime how-to, teams without on-call get no on-call how-to, the signature step only appears when `signature_required` is on, TIL text follows
`features.til`) and reads live values (rotation start/handover, time step, night window, look-back, payroll cut-off). It never prints
rates or amounts (they can be hidden from employees). Six screenshots in `static/guide/` (dashboard, on-call, overtime, submit panel, approvals, out-of-hours; 1560 px wide viewport at 1.25 scale, fictional demo data). Keep it in step with §2/§3 and
Admin › Documentation (the administrator's reference). At ≤ 1700 px the header hides the user name so the admin nav still fits.

### 6.5 Documentation
- **User Guide** (`/guide`): for everyone, employee and manager parts, with screenshots (§6.4b).
- **Admin › Documentation** (`/admin/docs`, `templates/admin/docs.html` + `templates/_admin_docs.html`, anchors `adm-*`): the administrator's reference (how it works, roles, rates, payroll, Import History, SSL/HTTPS incl. PFX conversion, LDAP, operations); reads live values. It is **not** part of the User Guide.
- **Views carry no explanatory paragraphs**: no `.help` / `.subtitle` descriptions under titles, only labels, units and status/validation messages; explanations go to one of the two documents above. Keep them in step with the rules in this file.

## 7. Repository layout

```
CLAUDE.md                this file
README.md                operator guide (install, offline deployment, certificates, LDAP, operations)
docker-compose.yml       proxy + app + db (bind mounts carry :z for SELinux)
.env.example             all configuration variables
app/Dockerfile, app/requirements.txt
app/timelog/             Python package
  config.py              env-based settings
  db.py, models.py       SQLAlchemy engine/session, ORM models
  calendar_utils.py      day-type classification, month helpers
  calc.py                on-call amounts, overtime hours/night/TIL/payroll codes (pure functions)
  access.py              visibility / approval rules (single place for authz)
  security.py            password hashing, sessions, CSRF, current-user deps, login throttle
  app_settings.py        admin-editable settings, feature flags, LDAP overrides, secrets
  ldap_auth.py           LDAP search/bind/sync/diagnose
  history_import.py      backfill of past on-call/overtime from the Finance workbooks (Admin › Import History)
  workflow.py            sheet state transitions + snapshots + audit + conflicts
  signatures.py          signature validation and content digest
  backup.py              encrypted backups, restore, schedule
  certtool.py            self-signed certificate and PFX → PEM (runs inside the app image)
  ics_import.py          .ics holiday import
  export.py              detailed control export (Summary, payroll codes, detail tabs)
  export_finance.py      Finance exports in the layout of the on-call and overtime sheets
  export_templates/      sanitized layouts used by export_finance.py (generated by scripts/make-export-templates.py)
  notify.py              optional SMTP notifications
  web.py, main.py        template helpers; app factory (middleware, scheduler, error handlers)
  version.py             version / build info
  seed.py, demo.py       initial data; `python -m timelog.demo` creates demo local users
  ooh_report.py          ServiceNow export parsing, out-of-hours hours per day type, chart geometry (SVG)
  routes/                auth, home, oncall, overtime, approvals, submission, payroll, team, ooh, guide, admin, common, notifications
  templates/, static/
app/tests/               pytest (SQLite; PostgreSQL via TEST_DATABASE_URL)
proxy/nginx.conf.template
scripts/                 build, coverage, backup-db, make-csr, make-selfsigned, make-offline-package, make-export-templates,
                         make-ooh-example.py, make-backfill-samples.py (fictional test workbooks, output in git-ignored samples/)
docs/screenshots/        README screenshots (demo data, fictional users; regenerate when the UI changes, see §8.5)
deploy/                  files shipped in the offline package (installers, convert-pfx, README-OFFLINE-<target>.md)
certs/                   (git-ignored) server.crt / server.key / ldap-ca.pem
dist/                    (git-ignored) offline packages
```

## 8. Dev commands

```bash
# tests (local, SQLite)
cd app && /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q
# against PostgreSQL 16 (each test starts from an empty schema)
TEST_DATABASE_URL=postgresql+psycopg://user:pw@host:5432/db .venv/bin/pytest -q

# manual testing and review: ONLY the docker deployment (no local uvicorn / dev.db instance)
scripts/make-selfsigned.sh timelog.local && cp .env.example .env && scripts/build.sh && docker compose up -d
docker compose exec app python -m timelog.demo   # optional demo users (prints a random password)
```
The JS (`static/app.js`) has no automated tests; static assets are excluded from the *coverage* measure only.

### 8.1 Repository hygiene (no secrets / personal data in git)
- `.gitignore` excludes: `.env*` (except `.env.example`), all TLS material (`certs/*`, `*.key|pem|crt|csr|p12|pfx`), `/templates/`
  (local reference documents that may contain personal data), `*.xlsx|csv` (exports; except the two sanitized layouts in `app/timelog/export_templates/`), databases/dumps (`*.db`, `*.sql*`,
  `backups/`), `dist/`, build stamps, venvs, editor files.
- `.githooks/pre-commit` (enable per clone: `git config core.hooksPath .githooks`) refuses those files and staged content with
  private keys or `*_PASSWORD=`/`SECRET_KEY=` literals, and checks `.env.example` keeps only empty / `change-me…` placeholders.
- No credentials in code: demo users get a random password, docs use placeholders, and the app refuses to start with the
  `.env.example` placeholder `SECRET_KEY` / `BOOTSTRAP_ADMIN_PASSWORD`. Test-only fake values (`Test-password1`, `test-secret`,
  `S3cret-bind!`) live in `app/tests/` and protect nothing. `app/.dockerignore` keeps the same files out of the image.
- Demo/test identities are fictional (alice, bob, … `@example.test`); real employee names must never appear in the repo.

### 8.2 Code quality (SonarQube)
- The security tools are **separate from this repo**, in the sibling `../security-stack/` (SonarQube Community Build on :9000,
  Dependency-Track on :8081/:8082, Grype, Grafana). Run everything with `../security-stack/stack.sh scan . timelog` from the repo
  root (`stack.sh up` first if the stack is down); each tool also has its own `scan.sh` there.
- In this repo: `sonar-project.properties` (project key `timelog`) and `scripts/coverage.sh` (pytest + coverage → `app/coverage.xml`
  with repo-relative paths), which `scan.sh` runs first. `.scannerwork/` and `app/coverage.xml` are git-ignored.
- Current result (2.0.0, via `../security-stack/stack.sh scan . timelog`): **0 issues, 0 hotspots**, quality gate passed.
- Template rules that keep the scan clean: every input has an `id` + `<label for>` (use `.sr-only` when the label is not displayed),
  `<th scope="col|row">`, totals rows use `<td>` (not `<th>`), no Jinja `{% if %}` blocks inside an HTML start tag (render the
  attribute unconditionally), JSON in attributes via `|tojson` inside **single quotes**, no `autofocus`.

### 8.3 Dependency scanning (Dependency-Track)
- Local OWASP Dependency-Track in `../security-stack/dependency-track/` (UI :8081, API :8082, credentials in its `.local.env`).
  Run `../security-stack/dependency-track/scan.sh .` from the repo root: SBOM of `app/requirements.txt` (resolved, incl. transitive), upload,
  findings (OSV/PyPI mirror enabled). Details in that folder's README.
- Current result (2.0.0): **0 findings**. Re-scan after every dependency change.

### 8.4 Offline deployment package
- **Targets**: Docker containers on **RHEL 9** (Docker Engine; SELinux enforcing supported through `:z` on every bind mount in
  `docker-compose.yml`) or **Windows Server 2022+ running Linux containers** (Docker Engine in WSL 2 or Docker Desktop;
  Windows-native containers are not supported). Both use the same linux images.
- `scripts/make-offline-package.sh` (connected machine) builds `dist/timelog-<version>-offline-<target>-<arch>.zip` + `.sha256`.
  `TARGET=rhel9` (default) or `windows`; `PLATFORM` (default `linux/amd64`) is the **server's** architecture, built with
  `docker buildx --platform`; `docker save --platform` plus a check of every saved image's architecture (aborts on mismatch).
  Contents: images tarball (app, `postgres:16-alpine`, `nginx:1.27-alpine`), `docker-compose.yml` (unchanged: `build:` is ignored
  with `--no-build`), `.env.example`, `proxy/`, `certs/README.md`, `docs/`, installer, `convert-pfx.*`, `README-OFFLINE.md` (from
  `deploy/README-OFFLINE-<target>.md`), `MANIFEST.txt`, `CHECKSUMS.sha256` (verified by the installer). RHEL: `docker-rpms/` (docker-ce,
  cli, containerd.io, compose and buildx plugins + missing dependencies, resolved in an `almalinux:9` container from Docker's RHEL
  repo; `INCLUDE_DOCKER_RPMS=0` to skip). Windows: Docker is a documented prerequisite (not bundled).
- **Install directory and data folders**: `deploy/install.sh` (RHEL) deploys the program files into a fixed directory
  (`--dir`, default `/opt/timelog`) and runs everything from there; the unzipped package is disposable. `docker-compose.yml` bind-mounts
  `./db-data` (PostgreSQL) and `./backups` (both `:z`). The directory holds `.env`, `certs/`, `db-data/`, `backups/`, which the installer never
  overwrites. **An existing `.env` in the directory means upgrade** (no `--server-name` needed; `--upgrade` only asserts it); a directory with
  `db-data` content but no `.env` is refused. `backups/` is chowned to uid 10001 (app user); PostgreSQL fixes `db-data` itself. No migration
  from the old named volumes (fresh installs only). `install.ps1` (Windows, unchanged model: runs in the unzipped folder) only creates the
  folders; Windows bind mounts of the PostgreSQL data dir are untested.
- `deploy/install.sh` (RHEL) / `deploy/install.ps1` (Windows): verify checksums, `docker load`, create `.env` from the template with
  **generated** `SECRET_KEY`, `POSTGRES_PASSWORD`, bootstrap admin password and `BACKUP_PASSPHRASE` (first install needs
  `--server-name`; `.env` mode 600 / ACL Administrators+SYSTEM), temporary self-signed certificate if none, firewall
  (`--open-firewall` / `-OpenFirewall`), `docker compose up -d --no-build --pull never`, wait for healthy. `--upgrade` keeps
  `.env`/volumes and only moves `TIMELOG_VERSION`. RHEL only: `--install-docker` installs `docker-rpms/` with
  `dnf install --disablerepo='*'`.
- **No openssl on the target**: `timelog/certtool.py` (cryptography) runs inside the app image: `selfsigned <fqdn> <outdir>` and
  `pfx2pem <file.pfx> <outdir>` (server cert first, then chain; unencrypted key; password from env or prompt, never argv), wrapped by
  `deploy/convert-pfx.sh` / `.ps1`. Tests: `app/tests/test_certtool.py`.
- The README section "Offline (air-gapped) deployment" is the operator documentation; keep it and the two
  `deploy/README-OFFLINE-*.md` in step with the scripts. Releasing: bump `__version__`, run the tests, build the package(s).

### 8.5 README screenshots
- `docs/screenshots/*.png` (dashboard, on-call, overtime, approvals, statistics, payroll) are referenced from README.md and are in the ResOps look. The same demo data feeds the user-guide screenshots and `scripts/make-backfill-samples.py` (throwaway SQLite with the demo users, approved on-call/overtime for Jan-Oct, written through the real Finance exporter; use it to test Admin › Import History after `python -m timelog.demo` in the target instance). They are
  taken from a **throwaway SQLite instance** (`python -m timelog.demo` + a few sheets submitted/approved through `workflow`), driven
  headless with Playwright using the installed Brave as the browser (viewport 1360×1000, scale 1.5, notices cleared). Use fictional
  demo users only (§8.1); never capture the real stack. Not part of the offline package.

## 9. Current state

- Version **2.0.0**: Analysis (out-of-hours report, Experts / Officers panes, saved datasets, change request numbers, click-a-chart popups), Statistics Overview / Experts / Officers tabs with charts from logged data, Admin › Import History (backfill; one workbook per month or one sheet per month), ResOps visual identity. 165 pytest tests pass on SQLite; the backup and restore tests also pass on PostgreSQL 16 (last checked on 1.18.5).
- Security checks on 2.0.0 (`../security-stack/stack.sh scan . timelog`, 2026-11-27): SonarQube **0 issues, 0 hotspots**, quality gate passed; Dependency-Track **0 findings** for `timelog@2.0.0`; Grype **0 findings**. (The stack's Grafana ingest still prints "timelog@1.1.0: 73 findings": a stale 1.1.0 project record in Dependency-Track, not the current version.)
- Latest offline package: `dist/timelog-2.0.0-offline-rhel9-amd64.zip` (+ `.sha256`, **without** the Docker RPMs: `INCLUDE_DOCKER_RPMS=0`), built from the 2.0.0 sources after the tests and scans were clean. Upgrade in place with `install.sh --upgrade` (data and `.env` kept; new tables are created at start, no manual migration). Older packages in `dist/` are obsolete.
- The local Docker instance (dev stack, review only) holds `admin`, `tuser` and possibly the demo users; demo data for screenshots
  lives in a throwaway SQLite database, never in the local stack.
- **Verified**: image build; the full stack (nginx HTTPS + app + PostgreSQL 16, all healthy); HTTP→HTTPS redirect; every page for
  each role; the offline package built, unzipped in an empty folder and installed without any repository or registry (RHEL 9 amd64
  package under emulation: install, in-place upgrade with data kept, PFX conversion); the bundled Docker RPMs install on a clean
  RHEL-compatible 9 container without network.
- **Not yet verified** (validate on the target environment): LDAP against a real Active Directory (tests use an in-memory fake),
  SMTP notifications, a certificate issued by the Microsoft PKI, a real RHEL 9 host (SELinux enforcing, firewalld, Docker from the
  bundled RPMs) and any Windows Server (`install.ps1` has never been run).

## 10. Open questions / future work
- Confirm with HR whether 24/12 counts as a holiday for on-call as well; confirm the Sunday-holiday premium rule with Payroll.
- Confirm the Finance cut-off day (default the 5th of the following month, editable) and the 12-month look-back.
- Should expert interventions also be counted (not paid) for reporting? Currently counted in the export.
- Explicit delegation / absence handling beyond skip-level approval (e.g. a named delegate, notification of the skip-level manager when the direct manager is slow).
- Alembic migrations before the first production schema change.
- Import History: late-claim ("(late)") tabs are skipped because their payroll month is unknown; decide how to place them. Overtime imported from the workbooks has no start/end times.
- Restrict the on-call conflict check per team if rotations turn out to be per team.
