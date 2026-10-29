# TimeLog — On-call & Overtime tracking

Web application replacing the Excel-based process used by the IT department to declare
**on-call duty** and **overtime**, have them validated by the line manager, and export an
aggregated monthly view for Finance/Payroll.

This file is the single source of truth for business rules and design decisions.
Update it whenever a rule or decision changes.

---

## 1. Source spreadsheets (analysed 2026-10-28)

The original workbooks are kept locally in the top-level `templates/` folder, which is **git-ignored**
(real employee names). Do not commit them or copy names from them into code, tests or docs.

| File | Used by | Content |
|---|---|---|
| `on-call-template.xlsx` | Employee | One sheet per month. One row per day: `Date`, `Week Day`, `On-Call?` (Yes), counters per day type (Week / Saturday / Sunday / Legal Holiday), `Comment`, `Incident number`. Header shows name + "Expert/Mgr". Signed by employee and manager. Plain values, no formulas. |
| `Y26M01_IT_OnCall.xlsx` | Line manager | One tab per month (`Dec25`, `JAN`…`DEC`) + hidden `Support` tab. Rows = employees grouped by team (Service Desk, Production, Data & Analytics, Informatica, Business Support, Deployment Support). **Expert employees have a second row "`<Name> - Incidents/calls`"**. Columns = days of month; value `1` = on-call that day. Pricing block multiplies each cell by the day price. |
| `Y26M01_IT_Overtime.xlsx` | Line manager → Payroll | `READ FIRST` tab (rules) + one tab per month. Rows = employee (`LAST Name`, `First Name`, `PAID or TIL`). Columns = days with hours (0.25 granularity). Day header suffixed `LH` for legal holidays. Manager fills night-work hours. Payroll columns HL100…HL121 + TIL. |

### 1.1 On-call rules extracted (`Support` tab)
- Day type: `H` if date in legal-holiday list, else `Sat` / `Sun`, else `W`. **Holiday takes precedence.**
- Daily stipend (single table in the sheet): **W = 45, Sat = 70, Sun = 70, H = 90** (EUR).
- Expert: a `1` in the "Incidents/calls" row on a day adds another day price → **daily rate is doubled** when there was an intervention, whatever its duration.
- Non-expert: interventions are **not** paid through on-call; they are declared as overtime.

### 1.2 Overtime rules extracted (`READ FIRST` + month tabs)
- Hours in multiples of **0.25 h** (15 min).
- Compensation per line: **PAID** (default if blank) or **TIL** (time in lieu, no payment).
- Weekend overtime must be requested by the manager and formally **pre-validated**.
- Night work hours are specified separately (per day type).
- Payroll codes (hours, additive premiums — Finance applies the employee's hourly rate):

| Code | Meaning | Hours counted |
|---|---|---|
| HL100 | Normal (base 100%) | all paid OT hours |
| HL101 | Normal 50% premium | all paid OT hours |
| HL111 | Night 30% premium | paid night hours |
| HL130 | Sunday 70% premium | paid Sunday hours |
| HL121 | Legal holiday 200% premium | paid legal-holiday hours |

- TIL credit multipliers (sheet formula), consistent with the additive premiums above:

| Day type | Day factor | Night factor |
|---|---|---|
| Weekday (W) | 1.5 | 1.8 |
| Saturday | 1.5 | 1.8 |
| Sunday | 2.2 | 2.5 |
| Legal holiday | 3.5 | 3.8 |

### 1.3 Spreadsheet defects deliberately NOT reproduced
- A Saturday/Sunday that is also a legal holiday was counted in both the weekend column and
  the LH column (hours double counted in TIL formula). **App: each day has exactly one type,
  holiday wins** → hours go to HL121 only, not HL130.
- Weekday holidays were counted both in "Normal" and "Legal Holiday". App: one bucket.
- TIL formula `IF(C="Paid",0,…)` produced TIL for blank lines although README says blank = PAID.
  App: compensation is mandatory, default PAID.
- Holiday lists differ between workbooks (On-call list labels 25/12/2026 "Veille de Noël" and
  omits 24/12/2026; Overtime list has 24/12 "Xmas eve (CBA)"). App: **one holiday table**,
  admin-maintained; seeded with the union (incl. 24/12 CBA). ➜ *Open question: confirm with HR
  whether 24/12 counts as holiday for on-call as well.*
- Overtime month tabs had stale years in row 2 (2025). Irrelevant in app (real dates).

---

## 2. Business rules implemented

- **Employee types**: normal and **Expert** (`users.is_expert`). Both can do on-call.
  **Only normal employees can file overtime** (enforced server-side, 403 for experts).
- **Grades**: on-call daily rates are configurable **per grade** (`grades` table: rate W/Sat/Sun/H).
  Seeded default grade `STD` = 45/70/70/90. Each user has a grade (falls back to default grade).
- **Rotation weeks**: cover is normally assigned per week, **Friday → Thursday** by default. The start weekday and a
  handover time (default Friday 18:00, informational: days are booked whole) are editable in Admin › Settings
  (`app_settings` keys `oncall_week_start`, `oncall_handover`; the `ONCALL_WEEK_START` env value is the fallback; existing
  bookings are not changed). The on-call grid groups days by rotation week with a **week switch** (`wk_<start date>`)
  that books all 7 days. A week straddling two months books the other month's days on that month's sheet
  (`workflow.add_oncall_days`, keeps existing days); if that sheet is submitted/approved those days are
  skipped with a warning. The switch shows "on" when all 7 days are booked across both months; client JS
  never switches it on automatically for a spilling week (would silently book the other month).
- **On-call conflicts** (v1.3.0): two employees cannot be on call at the same time. For managers/admins (never for
  the employee), an on-call sheet is flagged where another employee has the same day on a **submitted or approved** sheet
  (`workflow.oncall_conflicts`, drafts do not count): red banner + highlighted rows (names, status) on the on-call sheet,
  and a "N day(s) in conflict" tag on the Approvals list. Colleagues outside the viewer's scope are **not named**
  ("1 other employee outside your team"), to respect the visibility rule. Highlight only: approval is not blocked. The
  check is organisation-wide (not per team); restrict it per team if rotations are per team.
- **On-call amount per day** = `rate[grade][day_type] × (2 if expert and intervention else 1)`.
- **Who records intervention details where** (decision 2026-10-28):
  - *Experts*: on the on-call grid (Intervention checkbox → double rate, Incident #, Comment).
  - *Non-experts*: **only on the overtime sheet** (hours, incident #, description). Their on-call grid has
    no Incident/Comment/Intervention inputs (server ignores such fields: `save_oncall` stores
    intervention=False, empty texts); the Intervention column shows read-only the overtime hours logged on
    each on-call day, linking to the overtime sheet.
  - Overtime entries are **automatically flagged as on-call interventions** when their date is a booked
    on-call day (`workflow.oncall_dates`, any sheet status, across months) - no checkbox. Detected when the
    entry is added and **re-detected at submission** (`recompute_overtime`), since on-call days may change.
    The form shows a live "on-call day" hint (`data-oncall-dates`). Weekend/holiday overtime must be on an
    on-call day or ticked "pre-approved"; submission is refused otherwise.
- **Overtime entry** = date + start/end time in **30-minute steps** (user decision 2026-10-28, overrides the
  spreadsheet's 0.25 h; `OVERTIME_STEP_MINUTES`), so the minimum entry is 30 min. Since v1.1.0 the times are
  `<select>` dropdowns generated from that step (From 00:00-23:30, To 00:30-23:30 and 24:00). Hours and night hours are computed.
  Night window configurable (`NIGHT_START`/`NIGHT_END`, default 22:00–06:00).
  An entry crossing midnight is **split automatically** into two entries (one per calendar day);
  refused if the second part falls in another month (user enters it in next month's sheet).
  Entries of the same user may not overlap. No future dates.
- **Back-dated overtime**: employees may log overtime for any day up to `OVERTIME_LOOKBACK_MONTHS` (default 12)
  back. The entry goes to the sheet of its own work month. If that month's sheet is:
  draft/rejected → added to it; submitted → refused (withdraw first); **approved → a supplementary sheet**
  (`sheets.seq` = 2, 3…) is opened for that month and goes through the normal approval. Supplementary sheets
  exist for overtime only (an on-call month stays a single sheet; admin can reopen it).
- Weekend/holiday overtime requires either "on-call intervention" or "pre-approved by manager" ticked.
- Rates/factors are **snapshotted at submission** (`day_type`, `rate`, `amount` on rows, grade code
  and expert flag on the sheet) so later rate changes never alter submitted/approved figures.
  While in draft, values are recomputed on every save.

- **Public holidays import**: admins upload an `.ics` file (Admin › Legal holidays). Own RFC 5545 reader
  (`ics_import.py`, no dependency): all-day, multi-day (exclusive DTEND / DURATION PnD), timed events (start
  date), folded lines, escapes; RRULE and cancelled events skipped and reported; max 1 MB. Modes: *merge*
  (add/rename by date) or *replace* (for each year present in the file, dates absent from the file are deleted).
  Audited. Draft sheets pick the change up at next save; submitted/approved sheets keep their snapshot.
- **Time in lieu switch** (Admin › Settings, key `til_enabled`, default on). When off: the PAID/TIL choice,
  TIL columns/tiles/credits, export TIL columns and the "TIL factors" admin tab are hidden (`features.til`),
  new TIL entries are refused server-side and a sheet still holding TIL entries cannot be submitted (re-enter
  them as paid). Existing TIL data stays visible and exported (columns reappear when the data contains TIL), so
  nothing already declared is lost.
- **Signature on submission** (Admin › Settings, key `signature_required`, default off). When on, submitting an
  on-call or overtime sheet requires a drawn signature (canvas → PNG data URL, validated server-side: PNG magic,
  ≤300 KB, ≤2000 px) plus a ticked confirmation statement; otherwise the submit is refused (JS also blocks
  the form). Stored in `signatures` with the **SHA-256 of the sheet content** (`signatures.sheet_digest`:
  canonical JSON; numbers normalised to 2 decimals because in-memory and DB-loaded Decimals differ in text),
  time, IP and user agent. History is kept (a resubmission after rejection adds a new signature); only the
  current submission's signature is shown (`Sheet.signature`). Managers/owner/admins see the image
  (`/signatures/<id>.png`, same visibility as the sheet) with a ✓/⚠ "content unchanged since signature"
  check; the export has a "Signed (UTC)" column. This is a *simple electronic signature* (evidence of intent
  and integrity), not a qualified one.
  **Signature picture** (v1.1.0): besides drawing, the user can *upload a picture* (PNG/JPEG/WebP/GIF, ≤5 MB) of their
  signature. It is fitted into the pad canvas client-side and posted as the same PNG data URL, so the server-side
  validation is unchanged (no image library). Ticking "Remember this signature" stores the validated PNG in
  `saved_signatures` (one row per user, new table, created automatically); it is then pre-loaded on the pad for
  later submissions ("Use my saved signature") and can be deleted ("Forget it", `POST /signature/forget`).
  `/signature/saved.png` is visible to its owner only.
- **Amount visibility** (Admin › Settings, `app_settings` key `show_amounts_to_employees`, default on): when
  off, employees do not receive money amounts or rates for their own sheets (stat tiles, grid column, totals,
  dashboard, and the `data-rates` attribute used by JS are not rendered). Admins always see amounts; managers
  see them for people below them (`app_settings.can_see_amounts`). Overtime hours / TIL are not money and stay visible.

## 3. Workflow

One **sheet** per (user, kind ∈ {oncall, overtime}, year, month).

```
draft ──submit──▶ submitted ──approve──▶ approved
  ▲                 │   │
  └──withdraw───────┘   └──reject(comment)──▶ rejected ──(edit)──▶ submit again
approved ──reopen (admin only, audited, latest sheet of the month only)──▶ draft
```
- **Payroll period** (`sheets.payroll_year/month`) is fixed at approval by `calc.payroll_period`: approvals up
  to `PAYROLL_CUTOFF_DAY` (default 10) of month M+1 are paid with work month M; later approvals go to the
  payroll month that is still open (approval month, or the previous one if approved before its cut-off).
  A sheet paid in a later period than its work month is a **late claim** (`Sheet.is_late`).
- The Finance export is per **payroll period**: all approved sheets with that payroll period (regular + late
  claims, shaded and labelled "Late claim" with their work month). The Summary tab shows the status of every
  team member for the work month and counts late claims still awaiting validation.
- **Month sync** (v1.1.0): the month last viewed on the on-call or overtime page is kept in the session
  (`remember_month`); the nav links `/oncall` and `/overtime` open that month (else the current one), so both pages
  stay on the same month until the user changes it on either.
- **One submission for the month** (v1.1.0): on-call and overtime keep **separate drafts/sheets** (approved
  separately) but the employee has a **single submit process**: `POST /submit/<year>/<month>`
  (`workflow.submit_month`) sends every editable sheet of that month that has content (on-call sheet, overtime
  sheet and supplementary overtime sheets) together, with one comment and one signature (a `signatures` row per sheet,
  each with its own content digest). All or nothing: if one sheet is refused (e.g. unjustified weekend overtime)
  nothing is submitted. The panel is on both pages; the on-call page's "Save & submit" button first saves the grid.
  The former per-sheet `/sheets/<id>/submit` was removed. Withdraw / reject / approve remain per sheet.
- Editable only in `draft` / `rejected`.
- **Approver = direct line manager** (`users.manager_id`) or an admin. Nobody approves their own sheet.
- Every transition is written to `audit_log`.
- Optional e-mail notifications (SMTP relay, disabled unless `SMTP_HOST` set).

## 4. Security model

- Roles: **employee** (everyone), **manager** (implicit: has direct reports), **admin** (`is_admin`).
- Visibility: a user sees their own sheets; a manager sees sheets of **all users below them in
  the hierarchy** (recursive, cycle-safe); admin sees all. **Colleagues never see each other.**
  Unauthorised access to another user's data returns **404** (no existence disclosure).
- Approve/reject: direct manager or admin only. Upper managers can view & export.
- Export: manager → their subtree (approved sheets of the payroll period); admin → everyone (optional team filter).
- Sessions: signed cookie (Starlette `SessionMiddleware`), `HttpOnly`, `SameSite=Lax`,
  `Secure` when `SESSION_COOKIE_SECURE=true` (default in compose). Lifetime `SESSION_MAX_AGE`.
- **CSRF**: per-session token, required on every POST (hidden `csrf_token` field).
- Local passwords hashed with **PBKDF2-SHA256, 600k iterations** (stdlib; scrypt rejected because it is
  missing from LibreSSL-linked Pythons such as macOS system Python).
- Login throttling: in-memory per username+IP (5 failures / 15 min). Per-process only (note for
  multi-worker setups; AD lockout policy also applies to LDAP users).
- LDAP: user input escaped with `escape_filter_chars`; **empty passwords rejected** (prevents
  anonymous/unauthenticated bind success).
- CSP `default-src 'self'` — no inline JS/CSS, no external resources (also required for offline).

## 5. Authentication

- **LDAP settings are editable in Admin › Settings** (decision 2026-10-28). Stored in `app_settings` under
  `ldap.*` keys; a stored value **overrides** the `LDAP_*` env variable, "Reset to .env values" deletes them
  (`app_settings.effective_settings` merges both; used by the login route). The bind password is
  **write-only** and encrypted with Fernet (`cryptography`), key = SHA-256 of SECRET_KEY: rotating SECRET_KEY
  makes it unreadable (UI asks to re-enter; env value is used meanwhile). The CA certificate can be pasted
  as PEM (`ldap_ca_cert_data`, passed to ldap3 `ca_certs_data`; wins over `LDAP_CA_CERT_FILE`).
  Validation: ldap:// or ldaps://, not ldaps + StartTLS, filter contains `{username}`, timeout 1-120 s, PEM.
  "Test connection" (`ldap_auth.diagnose`) uses the saved settings: connect, service bind, optional user
  lookup with groups/manager - no user password. Every change/test is audited (never the password).

- **Local users**: for bootstrap/debugging. `BOOTSTRAP_ADMIN_USERNAME`/`BOOTSTRAP_ADMIN_PASSWORD`
  create an admin on first start if no admin exists. Can be disabled with `LOCAL_LOGIN_ENABLED=false`
  (keep one local break-glass admin is recommended → `LOCAL_LOGIN_ADMIN_ONLY=true`).
- **LDAP / Active Directory** (core usage): search-then-bind with a service account
  (`LDAP_BIND_DN`), user filter `LDAP_USER_FILTER` (default `(sAMAccountName={username})`).
  LDAPS or StartTLS with CA file `LDAP_CA_CERT_FILE` (Microsoft PKI root/issuing CA, PEM).
  On each login the user is upserted: names, mail, employeeID, team (`department`), and
  **manager** from the AD `manager` attribute (manager entry is upserted too, so the hierarchy
  exists even before the manager logs in) unless `users.manager_locked` (admin override).
  Group mapping (nested groups supported via AD matching rule `1.2.840.113556.1.4.1941`):
  `LDAP_GROUP_ADMIN` → is_admin, `LDAP_GROUP_EXPERT` → is_expert, `LDAP_GROUP_REQUIRED` → may log in.
  When a group variable is empty, the flag is managed manually in the admin UI.
  Group checks use the AD-specific in-chain matching rule (not supported by OpenLDAP).
- Login order: if the username exists as a **local** user → local check; otherwise LDAP.

## 6. Architecture & technology (all open source, no internet at runtime)

| Component | Choice | Why |
|---|---|---|
| Backend | Python 3.12, **FastAPI**, Uvicorn | simple, typed, mature |
| Templating/UI | **Server-rendered Jinja2** + plain CSS + small vanilla JS | no Node build, no CDN, works air-gapped, strict CSP |
| Visual design | Utmost corporate palette (see §6.1), CSS animations | modern look, respects `prefers-reduced-motion` |
| ORM / DB | **SQLAlchemy 2** + **PostgreSQL 16** (SQLite for tests/dev) | |
| LDAP | **ldap3** (pure Python) | no system libs |
| Excel export | **openpyxl** | |
| Reverse proxy | **nginx** (TLS termination) | accepts MS-PKI certificate |
| Containers | Docker Compose: `proxy`, `app`, `db` | only `proxy` publishes ports |

- Schema created with `create_all` at startup + idempotent seeding (grades, holidays, TIL rules,
  bootstrap admin). *Decision:* no Alembic yet; add it before the first schema change in production.
- Python **3.12** everywhere (containers and the dev venv: `brew install python@3.12`, then
  `/opt/homebrew/bin/python3.12 -m venv app/.venv`). Since v1.1.1 the old "keep it 3.9-compatible" rule no longer
  applies (existing `Optional[...]` style is kept for consistency). `requirements.txt` pins direct **and transitive**
  dependencies; refresh them deliberately (install, `pip check`, tests, then the Dependency-Track scan).
- Air-gapped deployment: build images where a registry/PyPI mirror is reachable
  (`PIP_INDEX_URL` build arg for an internal mirror), then `scripts/make-offline-package.sh` (§8.4).

### 6.1 Design system
Colours copied from the Utmost website's compiled Tailwind theme
(`utmostgroup.com/wp-content/themes/utmost/dist/main.css`, fetched 2026-10-28):

| Token | Hex | | Token | Hex |
|---|---|---|---|---|
| primary (gold) | `#8F7237` | | secondary (aubergine) | `#4F4059` |
| primaryLight | `#9B804A` | | secondaryLight | `#504159` |
| primaryVeryLight | `#C7B99C` | | secondaryVeryLight | `#9D95A3` |
| primaryVVLight | `#ECEBE5` | | secondaryOpacity | `#EDEBEE` |
| primaryOpacity | `#F3F1EB` | | grayBG / grayText / grayHeadings | `#F0F0EF` / `#2D3648` / `#1A202C` |
| error | `#C20000` | | grayLink (muted) | `#717D96` |

Fonts (decision 2026-10-28: "IT/technology oriented", replacing the site's Avenir/Baskervville), all SIL OFL,
**self-hosted** in `static/fonts/` (latin subset, woff2, ~140 KB, licences alongside) - no external requests:
- **Space Grotesk** (variable) - headings, brand, month titles (`--font-display`)
- **IBM Plex Sans** 400/500/600 - body text (`--font-sans`)
- **JetBrains Mono** (variable) - figures, stat tiles, badges, table headers, eyebrows, dates/times/incident refs (`--font-mono`)
All tokens are CSS variables at the top of `static/style.css`; the XLSX export uses the same palette.
Navigation: plain page loads. **The header must stay perfectly static** (user requirement): no entrance
animation and **no View Transitions** (tried, the snapshot of the sticky blurred header "bounced"), fixed
height (60px, nav never wraps), `scrollbar-gutter: stable` + `overflow-y: scroll` so pages of different
lengths don't shift it sideways, and the main fonts are `<link rel=preload>`ed to avoid reflow.
Static assets are cache-busted with `?v={{ build.asset_tag }}`.
The browser tab title and the nav labels are title-cased (first letter of every word, `titlecase` Jinja filter in
`web.py`: "Timelog - Overtime Aug 2026", "Sign In", "Team & Export", "Sign Out").
**Industrial look** (v1.1.2, replaces the earlier rounded/"bubbly" style): square corners (2 px radius token), flat
surfaces with hard 1 px borders, no blur/gradients/glows, hard offset shadow only on toasts, faint engineering grid
background, solid aubergine header with a gold bottom rule, uppercase mono-ish buttons, gold top rule on stat tiles.
Motion is short and linear (<= 0.2 s fade-in of rows/cards, toasts slide 16 px, brief colour flash when an amount
changes, 0.4 s linear count-up); no ripples, floating orbs, pulses, wiggles, lifts or scaling. All disabled by
`prefers-reduced-motion`. Favicon / brand mark: aubergine square, gold base bar, white square clock face.

Frontend framework decision (2026-10-28): **stay server-rendered** (no React/Next.js). Reasons: the UI is
forms + tables with light interactivity; Next.js would add a Node runtime/container, an npm supply chain to
mirror for air-gapped builds, a JSON API + auth/CSRF rework, and duplicated validation - for little UX gain
that CSS + View Transitions don't already give. Revisit if needed: rich client-side interactions (drag-select
calendar, live updates), offline/mobile app, or a separate API consumer. Intermediate step if partial-page
updates are wanted: vendor **htmx** (single static JS file, no build).
JS sets CSS custom properties through CSSOM (allowed by CSP); templates contain no inline `style`/`<script>`.

### HTTPS / Microsoft PKI
- **Decision 2026-10-29: the TLS certificate stays file-based** (`certs/server.crt` + `server.key`, then restart the proxy).
  Uploading a PFX from the admin UI was considered and dropped (it would put the TLS private key in an app-writable
  volume and need a way to reload nginx). Admin › Documentation explains the PFX → PEM conversion with OpenSSL.
- nginx terminates TLS with `certs/server.crt` (server cert **+ intermediate chain**, PEM) and
  `certs/server.key`. `scripts/make-csr.sh` generates key + CSR with SAN; submit to AD CS
  (`certreq -submit -attrib "CertificateTemplate:WebServer"`), convert to PEM, concatenate chain.
  `scripts/make-selfsigned.sh` for dev.
- HTTP → HTTPS redirect, TLS 1.2/1.3, HSTS, security headers.
- App runs Uvicorn with `--proxy-headers` (`FORWARDED_ALLOW_IPS`) so URLs/redirects/secure
  cookies are correct behind the proxy. The app port is not published outside the Docker network.

### 6.2 Version & build information
`timelog/__init__.py:__version__` (semver, bump on each release) + build timestamp/commit shown in the footer
of every page (and the login page) and returned by `/healthz` (`ok <version> <timestamp> [commit]`).
The Dockerfile writes `timelog/BUILD_TIMESTAMP` (build arg, defaulting to `date -u` at build) and
`timelog/BUILD_COMMIT`; `scripts/build.sh` / `make-offline-package.sh` pass them. Env vars of the same names
override. Without them the footer says "dev build, started <server start time>" (`version.py`).

## 7. Repository layout

```
CLAUDE.md                this file
README.md                operator guide (install, config, certificates, LDAP)
docker-compose.yml       proxy + app + db
.env.example             all configuration variables
app/Dockerfile, app/requirements.txt
app/timelog/             Python package
  config.py              env-based settings
  db.py, models.py       SQLAlchemy engine/session, ORM models
  calendar_utils.py      day-type classification, month helpers
  calc.py                on-call amounts, overtime hours/night/TIL/payroll codes (pure functions)
  access.py              visibility / approval rules (single place for authz)
  security.py            password hashing, sessions, CSRF, current-user deps, login throttle
  ldap_auth.py           LDAP search/bind/sync
  workflow.py            sheet state transitions + snapshots + audit
  export.py              monthly XLSX export for Finance
  notify.py              optional SMTP notifications
  web.py, main.py        template helpers; app factory (middleware, error handlers)
  seed.py, demo.py       initial data; `python -m timelog.demo` creates demo local users
  routes/                auth, home, oncall, overtime, approvals, submission, team, admin
  templates/, static/
app/tests/               pytest (SQLite)
proxy/nginx.conf.template
scripts/                 certificate, backup, build and offline-package helpers
deploy/                  files shipped in the offline package (install.sh, README-OFFLINE.md)
certs/                   (git-ignored) server.crt / server.key / ldap-ca.pem
```

## 8. Dev commands

```bash
# tests (local, SQLite)
cd app && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q

# run locally without docker
DATABASE_URL=sqlite:///./dev.db SECRET_KEY=$(openssl rand -hex 32) SESSION_COOKIE_SECURE=false \
BOOTSTRAP_ADMIN_USERNAME=admin BOOTSTRAP_ADMIN_PASSWORD='<choose-a-local-password>' \
.venv/bin/uvicorn --factory timelog.main:create_app --reload

# full stack
scripts/make-selfsigned.sh timelog.local && cp .env.example .env && docker compose up -d --build
docker compose exec app python -m timelog.demo   # optional demo users (prints a random password)
```

## 8.1 Repository hygiene (no secrets / personal data in git)
- `.gitignore` excludes: `.env*` (except `.env.example`), all TLS material (`certs/*`, `*.key|pem|crt|csr|p12|pfx`),
  `/templates/` (source workbooks), `*.xlsx|csv` (exports), databases/dumps (`*.db`, `*.sql*`, `backups/`; they
  hold users and the encrypted LDAP password), build stamps, venvs, editor files.
- `.githooks/pre-commit` (enable per clone: `git config core.hooksPath .githooks`) refuses those files and
  staged content with private keys or `*_PASSWORD=`/`SECRET_KEY=` literals, and checks `.env.example` keeps
  only empty / `change-me…` placeholders.
- No credentials in code: demo users get a random password (`python -m timelog.demo`), docs use
  placeholders, and the app refuses to start with the `.env.example` placeholder `SECRET_KEY` /
  `BOOTSTRAP_ADMIN_PASSWORD`. Test-only fake values (`Test-password1`, `test-secret`, `S3cret-bind!`) live in
  `app/tests/` and protect nothing.
- `app/.dockerignore` keeps the same files out of the image.
- Demo/test identities are fictional (alice, bob, … `@example.test`); real names from the workbooks must
  never appear in the repo.

## 8.2 Code quality (SonarQube)
- The local SonarQube is a **separate tool outside this repo**: `../sonar/` (sibling folder, moved there
  2026-10-29). It holds `docker-compose.yml` (Community Build, 127.0.0.1:9000, embedded H2, Compose project
  name kept as `timelog-sonar` to reuse its volumes), `scan.sh`, `.local.env` (admin password + analysis
  token) and `reports/`. Run: `../sonar/scan.sh .` from the repo root.
- In this repo: `sonar-project.properties` (project key `timelog`, sources/tests/coverage paths) and
  `scripts/coverage.sh` (pytest + coverage → `app/coverage.xml` with repo-relative paths), which `scan.sh`
  runs automatically before scanning. `.scannerwork/` and `app/coverage.xml` are git-ignored.
- `scan.sh` exports with the local admin account (analysis tokens cannot read hotspots) and reaches the
  server via `host.docker.internal`. `sonar.scm.disabled=true` until the repo has commits.
- Baseline 2026-10-29 (SonarQube 26.10 Community): first scan 22 issues (19 accessibility "bugs": inputs
  without labels; 1 table parse; 1 autofocus; 2 JS `Number.parseX`), all fixed → **0 issues, 0 hotspots,
  A/A/A ratings, 0 % duplication, 76.5 % coverage**, quality gate passed.
- Template rules that keep the scan clean: every input has an `id` + `<label for>` (use `.sr-only` when
  the label is not displayed), `<th scope="col|row">`, totals rows use `<td>` (not `<th>`), no Jinja
  `{% if %}` blocks inside an HTML start tag (breaks the HTML parser → render the attribute unconditionally),
  JSON in attributes via `|tojson` inside **single quotes**, no `autofocus`.

## 8.3 Dependency scanning (Dependency-Track)
- Local OWASP Dependency-Track in the sibling folder `../dependency-track/` (UI :8081, API :8082, credentials in its
  `.local.env`). Run `../dependency-track/scan.sh .` from the repo root: SBOM of `app/requirements.txt` (resolved,
  incl. transitive), upload, findings. OSV (PyPI) mirror enabled. Details in that folder's README.
- First scan 2026-10-29 (v1.1.0): 1 critical, 13 high, 18 medium, 6 low (deduplicated), all in dependencies
  (starlette 0.41, python-multipart 0.0.19, cryptography 44, jinja2 3.1.4, anyio 4.12). Fixed in v1.1.1 by moving to
  Python 3.12 and upgrading/pinning (starlette 1.10, fastapi 0.162, python-multipart 0.0.32, cryptography 50.0.2,
  jinja2 3.1.6, anyio 4.15.1; SQLAlchemy kept on 2.0.x). Re-scan of v1.1.1: **0 findings** in Dependency-Track (the 1.1.0 project still shows the old 73 raw findings as control), SonarQube 0 issues / 0 hotspots, gate passed.

## 8.4 Offline deployment package
- **Targets** (decision 2026-10-29): the application runs in Docker containers on **RHEL 9** (Docker Engine, SELinux enforcing
  supported through `:z` on every bind mount in `docker-compose.yml`) or **Windows Server 2022+ running Linux containers**
  (Docker Engine in WSL 2 or Docker Desktop; Windows-native containers are not supported). Both are built from the same
  linux images.
- `scripts/make-offline-package.sh` (connected machine) builds `dist/timelog-<version>-offline-<target>-<arch>.zip` + `.sha256`.
  `TARGET=rhel9` (default) or `windows`; `PLATFORM` (default `linux/amd64`) is the **server's** architecture, built with
  `docker buildx --platform`; `docker save --platform` + a python check of every saved image's architecture (an earlier version
  shipped arm64 nginx/postgres in an "amd64" package; the check now aborts). Contents: images tarball (app, `postgres:16-alpine`,
  `nginx:1.27-alpine`), `docker-compose.yml` (unchanged: `build:` is ignored with `--no-build`), `.env.example`, `proxy/`,
  `certs/README.md`, `docs/`, installer, `convert-pfx.*`, `README-OFFLINE.md` (from `deploy/README-OFFLINE-<target>.md`),
  `MANIFEST.txt`, `CHECKSUMS.sha256` (verified by the installer). RHEL: `docker-rpms/` (docker-ce, cli, containerd.io, compose and
  buildx plugins + missing dependencies, resolved in an `almalinux:9` container from Docker's RHEL repo; `INCLUDE_DOCKER_RPMS=0`
  to skip). Windows: Docker is a documented prerequisite (not bundled).
- `deploy/install.sh` (RHEL) / `deploy/install.ps1` (Windows): verify checksums, `docker load`, create `.env` from the template with
  **generated** `SECRET_KEY`, `POSTGRES_PASSWORD`, bootstrap admin password and `BACKUP_PASSPHRASE` (first install needs
  `--server-name`; `.env` mode 600 / ACL Administrators+SYSTEM), temporary self-signed certificate if none, firewall
  (`--open-firewall` / `-OpenFirewall`), `docker compose up -d --no-build --pull never`, wait for healthy. `--upgrade` keeps
  `.env`/volumes and only moves `TIMELOG_VERSION`. RHEL only: `--install-docker` installs `docker-rpms/` with
  `dnf install --disablerepo='*'`.
- **No openssl on the target**: `timelog/certtool.py` (cryptography) runs inside the app image: `selfsigned <fqdn> <outdir>` and
  `pfx2pem <file.pfx> <outdir>` (server cert first, then chain; unencrypted key; password from env or prompt, never argv).
  Wrapped by `deploy/convert-pfx.sh` / `.ps1`. Tests: `app/tests/test_certtool.py`.
- README section "Offline (air-gapped) deployment" is the operator documentation; keep it and the two
  `deploy/README-OFFLINE-*.md` in step with the scripts. Releasing: bump `__version__`, run the tests, build the package(s).
- Not verified on real targets: a RHEL 9 host with SELinux enforcing and the bundled Docker RPMs (the `almalinux:9` resolution may
  differ slightly from a minimal RHEL 9), and any Windows Server (`install.ps1` is untested; no PowerShell/Windows here).

## 9. Status (2026-10-28)
- 2026-10-29 (**v1.4.0**): offline package per target (RHEL 9 / Windows Server, §8.4), `certtool` (self-signed, PFX to PEM),
  SELinux-ready compose mounts. Replaces the first single-target package of v1.3.1.
- 2026-10-29 (**v1.3.1**): offline deployment package (`scripts/make-offline-package.sh`, `deploy/`), replaces
  `scripts/offline-bundle.sh`. Tested: package built, unzipped in an empty folder, installed (generated secrets, self-signed
  certificate, HTTPS + redirect on custom ports), upgraded in place with data kept, removed.
- 2026-10-29 (**v1.3.0**): admin-configurable on-call week start + handover time, on-call conflict highlighting for
  managers, backup schedule + encryption (v1.2.1), docs without spreadsheet/initial-context wording.
- 2026-10-29 (**v1.2.0**): **Admin › Backup** (`backup.py`, `routes/admin.py`, `admin/backup.html`). Logical backup =
  gzip JSON of every table of `Base.metadata` (database independent, one consistent snapshot: REPEATABLE READ on
  PostgreSQL), **encrypted with AES-256-GCM**: file `MAGIC "TLBK" | version | salt(16) | nonce(12) | ciphertext+tag`, key =
  PBKDF2-HMAC-SHA256(600k) of `BACKUP_PASSPHRASE` (else `"timelog-backup:"+SECRET_KEY`), header authenticated; files
  `timelog-YYYYmmdd-HHMMSS[-auto|-pre|-uploaded].tlbk` (strict name regex, no path input, unique per second), mode 600, in
  `BACKUP_DIR` (`/backups` volume, owned by the app user via the Dockerfile). Unencrypted v1.2.0 `.json.gz` files are
  encrypted at startup. Create, download, upload (decrypted + validated, optional "passphrase of that file" so a backup from
  another server is re-encrypted with the local key; unencrypted uploads refused), delete, **restore** (typed `RESTORE`,
  "pre-restore" safety copy, one transaction, self-referencing FKs linked after insertion, PostgreSQL sequences reset,
  session cleared). **Schedule** (admin UI, stored in `app_settings` keys `backup.*`): on/off, daily or weekly (+ weekday),
  time in 30-minute steps (UTC), number of automatic backups kept (1-365; manual/uploaded/pre-restore never pruned). The
  scheduler thread (`BACKUP_SCHEDULER`, on in containers, off in code defaults/tests) re-reads the schedule each minute; a
  save does not trigger a catch-up run of the slot already passed (`since`). Everything is audited. Tests pass on
  SQLite and PostgreSQL 16. `scripts/backup-db.sh` (pg_dump, unencrypted) stays as an alternative.
- 2026-10-29 (**v1.1.3**): Admin › Documentation page (`/admin/docs`, `templates/admin/docs.html`: how the app works,
  SSL/HTTPS incl. PFX conversion, LDAP, operations); "Current month" button on the month selector. Keep the page in
  step with the rules in this file.
- 2026-10-29 (**v1.1.1**): dependency upgrade on Python 3.12 (see §8.3), transitive dependencies pinned. 68 tests green.
- 2026-10-29 (**v1.1.0**): title case of tab title / nav, signature picture upload + saved signature, 30-minute
  time dropdowns, single month submission for on-call + overtime (`routes/submission.py`). New table
  `saved_signatures`. Tests: `app/tests/test_v11_features.py`.
- 2026-10-29 (**v1.0.0**): tab title "Timelog - <page>" (app name now "Timelog"), TIL switch, signatures,
  60 pytest tests, SonarQube clean (0 issues / 0 hotspots, coverage ~87 %).
  JS (`static/app.js`) has no automated tests; static assets are excluded from the *coverage* measure only.
- 2026-10-28 (v0.6.0): non-expert intervention details moved to overtime only, automatic on-call detection.
  40 pytest tests green.
- 2026-10-28 (v0.5.0): static header fix, LDAP settings UI + test connection, version/build footer;
  new dependency `cryptography`. 35 pytest tests green.
- 2026-10-28 (later): weekly rotation switch, ICS holiday import, amount-visibility setting; new table
  `app_settings` (created automatically). 30 pytest tests green.
- Implemented: all of the above. 22 pytest tests (calc, authz, workflow, 30-min steps, late claims /
  supplementary sheets, payroll period, export, LDAP sync, admin) green on SQLite / Python 3.9; every page
  smoke-rendered for each role.
- Schema changed on 2026-10-28 (`sheets.seq`, `payroll_year`, `payroll_month`): no migration exists — dev
  databases must be recreated. Still no production data, so Alembic is not yet needed.
- **Verified (2026-10-29, v1.0.0)**: `scripts/build.sh` image build; `docker compose up` of the full stack
  (nginx HTTPS + app + PostgreSQL 16, all healthy); HTTP→HTTPS redirect (needs `HTTPS_PORT_SUFFIX=:<port>`
  when HTTPS is not published on 443); login and every page over HTTPS; the full pytest suite against
  PostgreSQL 16 (`TEST_DATABASE_URL=postgresql+psycopg://… pytest`; each test starts from an empty schema).
- **Not yet verified**: LDAP against a real Active Directory (only a fake in-memory directory in tests),
  SMTP notifications and a certificate issued by the Microsoft PKI. Validate these on the target environment.
  The offline package (§8.4) was built and installed from a clean folder, with no repository and no registry, on a
  Docker host (RHEL 9 amd64 package under emulation); a real RHEL 9 / Windows Server has not been tried.
- Local run of the stack used HTTP 8080 / HTTPS 8443, `SERVER_NAME=localhost`, the self-signed
  development certificate, and a generated `.env` (git-ignored, mode 600).
- Implementation notes: app factory `timelog.main:create_app` (run with `uvicorn --factory`);
  overtime end `24:00`/`00:00` = end of day; on-call rows exist only for on-call days;
  export values are computed in Python (no Excel formulas).

## 10. Open questions / future work
- Confirm 24/12 holiday treatment for on-call; confirm Sunday-holiday premium rule with Payroll.
- Confirm the payroll cut-off day (assumed 10th of the following month) and the 12-month look-back.
- Should expert interventions also be counted (not paid) for reporting? Currently counted in export.
- Delegation of approval during manager absence (currently: admin can approve).
- Finance read-only role (currently managers export and send the file).
- Alembic migrations before first production schema change.
