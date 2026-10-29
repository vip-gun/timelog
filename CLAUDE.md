# Timelog — On-call & overtime tracking

Web application to declare **on-call duty** and **overtime**, have them validated by the line manager, and export an
aggregated monthly view for Finance/Payroll. It runs in Docker containers on-premise, without internet access.

This file is the single source of truth for business rules and design decisions.
Update it whenever a rule or decision changes (and keep README.md and Admin › Documentation in step).

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
  - *Non-experts*: **only on the overtime sheet** (hours, incident #, description). Their on-call grid has no
    Incident/Comment/Intervention inputs (the server ignores such fields); the Intervention column shows read-only the overtime
    hours logged on each on-call day, linking to the overtime sheet.
  - Overtime entries are **automatically flagged as on-call interventions** when their date is a booked on-call day
    (`workflow.oncall_dates`, any sheet status, across months). Detected when the entry is added and **re-detected at
    submission** (`recompute_overtime`). The form shows a live "on-call day" hint (`data-oncall-dates`).
- **Overtime entry** = date + start/end time chosen from `<select>` dropdowns in **30-minute steps**
  (`OVERTIME_STEP_MINUTES`; From 00:00–23:30, To 00:30–23:30 and 24:00), so the minimum entry is 30 min. Hours and night hours
  are computed; night window `NIGHT_START`/`NIGHT_END` (default 22:00–06:00). An entry crossing midnight is **split
  automatically** into one entry per calendar day (refused if the second part falls in another month: the user enters it in the
  next month's sheet). Entries of one user may not overlap. No future dates. End `24:00`/`00:00` = end of day.
- **Weekend/holiday overtime** must be on an on-call day (detected automatically) or ticked "pre-approved by manager";
  submission is refused otherwise.
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
- **Month sync**: the month last viewed on the on-call or overtime page is kept in the session (`remember_month`); the nav links
  `/oncall` and `/overtime` open that month (else the current one). A "Current month" button returns to today's month.
- **Approver = direct line manager** (`users.manager_id`) or an admin. Nobody approves their own sheet.
- **Payroll period** (`sheets.payroll_year/month`) is fixed at approval by `calc.payroll_period`: approvals up to
  `PAYROLL_CUTOFF_DAY` (default 10) of month M+1 are paid with work month M; later approvals go to the payroll month that is still
  open (approval month, or the previous one if approved before its cut-off). A sheet paid in a later period than its work month
  is a **late claim** (`Sheet.is_late`).
- The Finance export is per **payroll period**: all approved sheets with that payroll period (regular + late claims, shaded and
  labelled "Late claim" with their work month). The Summary tab shows the status of every team member for the work month and
  counts late claims still awaiting validation. Values are computed in Python (no spreadsheet formulas).
- Every transition is written to `audit_log`.
- Optional e-mail notifications (SMTP relay, disabled unless `SMTP_HOST` is set).

## 4. Security model

- Roles: **employee** (everyone), **manager** (implicit: has direct reports), **admin** (`is_admin`).
- Visibility (`access.py`, the single place for authz): a user sees their own sheets; a manager sees sheets of **all users below
  them in the hierarchy** (recursive, cycle-safe); admin sees all. **Colleagues never see each other.** Unauthorised access to
  another user's data returns **404** (no existence disclosure).
- Approve/reject: direct manager or admin only. Upper managers can view & export.
- Export: manager → their subtree (approved sheets of the payroll period); admin → everyone (optional team filter).
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
  each login the user is upserted: names, mail, employeeID, team (`department`), and **manager** from the AD `manager` attribute
  (the manager entry is upserted too, so the hierarchy exists before the manager logs in) unless `users.manager_locked` (admin
  override). Group mapping (nested groups via the AD matching rule `1.2.840.113556.1.4.1941`, not supported by OpenLDAP):
  `LDAP_GROUP_ADMIN` → is_admin, `LDAP_GROUP_EXPERT` → is_expert, `LDAP_GROUP_REQUIRED` → may log in. An empty group variable means
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
  appear automatically; **altering an existing table needs a recreated dev database** — add Alembic before the first schema
  change in production.
- Python **3.12** everywhere (containers and the dev venv: `/opt/homebrew/bin/python3.12 -m venv app/.venv`).
  `requirements.txt` pins direct **and transitive** dependencies; refresh them deliberately (install, `pip check`, tests, then the
  Dependency-Track scan).
- The UI stays server-rendered (no React/Next.js): it is forms and tables with light interactivity, and a Node stack would add a
  runtime, an npm supply chain to mirror for air-gapped builds, an API + auth/CSRF rework and duplicated validation. If partial-page
  updates are ever wanted, vendor **htmx** (one static file, no build).
- App runs Uvicorn with `--proxy-headers` (`FORWARDED_ALLOW_IPS`) so URLs/redirects/secure cookies are right behind the proxy; the
  app port is not published outside the Docker network.

### 6.1 Design system
- Palette (CSS variables at the top of `static/style.css`; the XLSX export uses the same colours):

| Token | Hex | | Token | Hex |
|---|---|---|---|---|
| primary (gold) | `#8F7237` | | secondary (aubergine) | `#4F4059` |
| primary light | `#9B804A` | | secondary light | `#504159` |
| primary very light | `#C7B99C` | | secondary very light | `#9D95A3` |
| primary tint | `#ECEBE5` / `#F3F1EB` | | secondary tint | `#EDEBEE` |
| background / text / headings | `#F0F0EF` / `#2D3648` / `#1A202C` | | muted / error | `#717D96` / `#C20000` |

- Fonts, all SIL OFL and **self-hosted** in `static/fonts/` (latin subset, woff2, licences alongside; no external requests):
  **Space Grotesk** (headings, brand, month titles), **IBM Plex Sans** 400/500/600 (body), **JetBrains Mono** (figures, stat tiles,
  badges, table headers, eyebrows, dates/times/incident refs).
- **Industrial look**: square corners (2 px radius token), flat surfaces with hard 1 px borders, no blur/gradients/glows, hard
  offset shadow only on toasts, faint engineering grid background, solid aubergine header with a gold bottom rule, uppercase
  buttons, gold top rule on stat tiles. Motion is short and linear (≤ 0.2 s fade-in of rows/cards, toasts slide 16 px, brief colour
  flash when an amount changes, 0.4 s count-up); no ripples, orbs, pulses, lifts or scaling. All disabled by
  `prefers-reduced-motion`. Favicon / brand mark: aubergine square, gold base bar, white square clock face.
- **The header must stay perfectly static** (user requirement): no entrance animation, **no View Transitions**, fixed height (60 px,
  nav never wraps), `scrollbar-gutter: stable` + `overflow-y: scroll`, main fonts `<link rel=preload>`ed. Navigation is plain page
  loads. Static assets are cache-busted with `?v={{ build.asset_tag }}`.
- The browser tab title and the nav labels are title-cased (`titlecase` Jinja filter in `web.py`: "Timelog - Overtime Aug 2026",
  "Sign In", "Team & Export").

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
  `BACKUP_DIR` (`/backups` volume, owned by the app user via the Dockerfile).
- Create, download, upload (decrypted + validated; optional "passphrase of that file" so a backup from another server is
  re-encrypted with the local key; unencrypted uploads refused), delete, **restore** (typed `RESTORE`, "pre-restore" safety copy,
  one transaction, self-referencing FKs linked after insertion, PostgreSQL sequences reset, session cleared afterwards).
- **Schedule** (admin UI, `app_settings` keys `backup.*`): on/off, daily or weekly (+ weekday), time in 30-minute steps (UTC),
  number of automatic backups kept (1–365; manual/uploaded/pre-restore never pruned). The scheduler thread (`BACKUP_SCHEDULER`, on in
  containers, off in code defaults and tests) re-reads the schedule each minute; saving a schedule does not trigger a catch-up run of
  a slot already passed (`since`).
- Everything is audited. Backups hold personal data and the Fernet-encrypted LDAP password (a restore needs the same `SECRET_KEY`).
  `scripts/backup-db.sh` (native `pg_dump`, unencrypted) remains as an alternative for cron.

### 6.5 Admin › Documentation
`/admin/docs` (`templates/admin/docs.html`) explains the application, SSL/HTTPS (incl. PFX conversion), LDAP and operations. It reads
live values (rotation week, step, night window, payroll cut-off). Keep it in step with the rules in this file.

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
  workflow.py            sheet state transitions + snapshots + audit + conflicts
  signatures.py          signature validation and content digest
  backup.py              encrypted backups, restore, schedule
  certtool.py            self-signed certificate and PFX → PEM (runs inside the app image)
  ics_import.py          .ics holiday import
  export.py              monthly XLSX export for Finance
  notify.py              optional SMTP notifications
  web.py, main.py        template helpers; app factory (middleware, scheduler, error handlers)
  version.py             version / build info
  seed.py, demo.py       initial data; `python -m timelog.demo` creates demo local users
  routes/                auth, home, oncall, overtime, approvals, submission, team, admin, common, notifications
  templates/, static/
app/tests/               pytest (SQLite; PostgreSQL via TEST_DATABASE_URL)
proxy/nginx.conf.template
scripts/                 build, coverage, backup-db, make-csr, make-selfsigned, make-offline-package
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

# run locally without docker
DATABASE_URL=sqlite:///./dev.db SECRET_KEY=$(openssl rand -hex 32) SESSION_COOKIE_SECURE=false \
BOOTSTRAP_ADMIN_USERNAME=admin BOOTSTRAP_ADMIN_PASSWORD='<choose-a-local-password>' \
.venv/bin/uvicorn --factory timelog.main:create_app --reload

# full stack
scripts/make-selfsigned.sh timelog.local && cp .env.example .env && scripts/build.sh && docker compose up -d
docker compose exec app python -m timelog.demo   # optional demo users (prints a random password)
```
The JS (`static/app.js`) has no automated tests; static assets are excluded from the *coverage* measure only.

### 8.1 Repository hygiene (no secrets / personal data in git)
- `.gitignore` excludes: `.env*` (except `.env.example`), all TLS material (`certs/*`, `*.key|pem|crt|csr|p12|pfx`), `/templates/`
  (local reference documents that may contain personal data), `*.xlsx|csv` (exports), databases/dumps (`*.db`, `*.sql*`,
  `backups/`), `dist/`, build stamps, venvs, editor files.
- `.githooks/pre-commit` (enable per clone: `git config core.hooksPath .githooks`) refuses those files and staged content with
  private keys or `*_PASSWORD=`/`SECRET_KEY=` literals, and checks `.env.example` keeps only empty / `change-me…` placeholders.
- No credentials in code: demo users get a random password, docs use placeholders, and the app refuses to start with the
  `.env.example` placeholder `SECRET_KEY` / `BOOTSTRAP_ADMIN_PASSWORD`. Test-only fake values (`Test-password1`, `test-secret`,
  `S3cret-bind!`) live in `app/tests/` and protect nothing. `app/.dockerignore` keeps the same files out of the image.
- Demo/test identities are fictional (alice, bob, … `@example.test`); real employee names must never appear in the repo.

### 8.2 Code quality (SonarQube)
- The local SonarQube is a **separate tool outside this repo**: `../sonar/` (Community Build, 127.0.0.1:9000, `scan.sh`,
  `.local.env`, `reports/`). Run `../sonar/scan.sh .` from the repo root.
- In this repo: `sonar-project.properties` (project key `timelog`) and `scripts/coverage.sh` (pytest + coverage → `app/coverage.xml`
  with repo-relative paths), which `scan.sh` runs first. `.scannerwork/` and `app/coverage.xml` are git-ignored.
- Current result: **0 issues, 0 hotspots**, quality gate passed.
- Template rules that keep the scan clean: every input has an `id` + `<label for>` (use `.sr-only` when the label is not displayed),
  `<th scope="col|row">`, totals rows use `<td>` (not `<th>`), no Jinja `{% if %}` blocks inside an HTML start tag (render the
  attribute unconditionally), JSON in attributes via `|tojson` inside **single quotes**, no `autofocus`.

### 8.3 Dependency scanning (Dependency-Track)
- Local OWASP Dependency-Track in the sibling folder `../dependency-track/` (UI :8081, API :8082, credentials in its `.local.env`).
  Run `../dependency-track/scan.sh .` from the repo root: SBOM of `app/requirements.txt` (resolved, incl. transitive), upload,
  findings (OSV/PyPI mirror enabled). Details in that folder's README.
- Current result: **0 findings**. Re-scan after every dependency change.

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

## 9. Current state

- 86 pytest tests pass on SQLite; the backup and restore tests also pass on PostgreSQL 16. SonarQube and Dependency-Track clean.
- **Verified**: image build; the full stack (nginx HTTPS + app + PostgreSQL 16, all healthy); HTTP→HTTPS redirect; every page for
  each role; the offline package built, unzipped in an empty folder and installed without any repository or registry (RHEL 9 amd64
  package under emulation: install, in-place upgrade with data kept, PFX conversion); the bundled Docker RPMs install on a clean
  RHEL-compatible 9 container without network.
- **Not yet verified** (validate on the target environment): LDAP against a real Active Directory (tests use an in-memory fake),
  SMTP notifications, a certificate issued by the Microsoft PKI, a real RHEL 9 host (SELinux enforcing, firewalld, Docker from the
  bundled RPMs) and any Windows Server (`install.ps1` has never been run).

## 10. Open questions / future work
- Confirm with HR whether 24/12 counts as a holiday for on-call as well; confirm the Sunday-holiday premium rule with Payroll.
- Confirm the payroll cut-off day (assumed the 10th of the following month) and the 12-month look-back.
- Should expert interventions also be counted (not paid) for reporting? Currently counted in the export.
- Delegation of approval during manager absence (currently: an admin can approve).
- Finance read-only role (currently managers export and send the file).
- Alembic migrations before the first production schema change.
- Restrict the on-call conflict check per team if rotations turn out to be per team.
