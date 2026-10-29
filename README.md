# TimeLog: on-call and overtime declarations

This web app replaces the `on-call-template.xlsx` / `IT_OnCall` / `IT_Overtime` spreadsheets.
- **Employees** declare on-call days and overtime.
- **Line managers** validate them and export a monthly `.xlsx` for Finance.

Business rules and design decisions are in [CLAUDE.md](CLAUDE.md).

## Stack
The app runs as three containers (Docker Compose). Only `proxy` publishes ports.

- `proxy`: nginx, which terminates HTTPS with your Microsoft PKI certificate.
- `app`: Python / FastAPI. It has no CDN or external resources and needs no internet at runtime.
- `db`: PostgreSQL 16 (volume `db-data`).

## Installation

```bash
cp .env.example .env            # then edit: SERVER_NAME, SECRET_KEY, POSTGRES_PASSWORD, admin password
openssl rand -hex 32            # -> SECRET_KEY

# TLS certificate (choose one)
scripts/make-selfsigned.sh timelog.corp.example.lu      # temporary / dev
scripts/make-csr.sh timelog.corp.example.lu             # production: submit CSR to AD CS (see below)

scripts/build.sh                                         # builds timelog-app:<version> (version + build time in the footer)
docker compose up -d
# non-standard ports (e.g. HTTPS_PORT=8443): also set HTTPS_PORT_SUFFIX=:8443 so the HTTP->HTTPS redirect keeps the port
docker compose ps                                        # all services "healthy"
```

Open `https://<SERVER_NAME>` and sign in with `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD`.

Optional demo accounts for testing (`head`, `manager`, `alice`, `bob` (Expert), `carol`, `dave`):
```bash
docker compose exec app python -m timelog.demo            # prints a random password
docker compose exec app python -m timelog.demo '<password>' # or set one
```

### Air-gapped server
The target host needs no internet. Build on a connected machine, or set `PIP_INDEX_URL` to an internal
PyPI mirror and pull the base images from an internal registry.
```bash
scripts/offline-bundle.sh                  # -> dist/timelog-images.tar.gz + dist/timelog-config.tar.gz
# on the target host:
docker load -i timelog-images.tar.gz
tar xzf timelog-config.tar.gz && cp .env.example .env   # edit
docker compose up -d --no-build
```

## HTTPS with a Microsoft PKI certificate
1. `scripts/make-csr.sh timelog.corp.example.lu [alias ...]` creates `certs/server.key` and `certs/server.csr`.
   The CSR has its SAN set, uses serverAuth and an RSA 3072 key.
2. On a domain-joined Windows host:
   `certreq -submit -attrib "CertificateTemplate:WebServer" server.csr server.cer`.
   You can also use the `/certsrv` web enrolment page.
3. Convert the certificate and build the chain, server certificate first:
   ```bash
   openssl x509 -inform DER -in server.cer -out server.pem     # if DER
   cat server.pem issuing-ca.pem > certs/server.crt
   ```
4. Run `docker compose restart proxy`. Clients trust the certificate through the domain's root CA (GPO).

nginx redirects HTTP to HTTPS, uses TLS 1.2 and 1.3, and sends HSTS. The app trusts `X-Forwarded-*` only from the proxy,
because the app port is not published. Session cookies are `Secure` and `HttpOnly`.

## LDAP / Active Directory
You can configure LDAP in the app under **Admin › Settings**, or in `.env`. Values saved in the UI override `.env`,
and "Reset to .env values" goes back to the file. In the UI:
- The bind password is stored encrypted and is never displayed back.
- You can paste the CA certificate as PEM.
- **Test connection** checks the bind and looks up a user, showing their groups and manager.

With `.env`, set the `LDAP_*` variables and `LDAP_ENABLED=true`. Then:
- Use a read-only **service account** (`LDAP_BIND_DN`) to search for users.
- Use LDAPS (`ldaps://dc:636`), or `ldap://` with `LDAP_START_TLS=true`.
- Export the PKI root CA as Base-64 into `certs/ldap-ca.pem` (the path inside the container is `/certs/ldap-ca.pem`).
- Groups (nested membership supported, AD only):
  - `LDAP_GROUP_REQUIRED` decides who may log in.
  - `LDAP_GROUP_ADMIN` grants administrators.
  - `LDAP_GROUP_EXPERT` marks Expert employees.

  If you leave one empty, you manage that flag manually under Admin › Users.
- The **line manager** is read from the AD `manager` attribute at each login. To override it, set it in
  Admin › Users and tick "Lock manager".

After LDAP works, set `LOCAL_LOGIN_ADMIN_ONLY=true`. This keeps only the local break-glass admin account.

## Using the app
| Role | What they can do |
|---|---|
| Employee | **On-call** grid per month: book a whole rotation week (Friday → Thursday) with the week switch, or tick individual days, then "intervention" and incident # / comment. **Overtime** entries: date, from/to in 30-minute steps, PAID or TIL. Then *Submit for validation*. Overtime can be logged for previous months, up to 12 months back. If that month is already approved, a supplementary sheet (late claim) is created. |
| Signature | If the administrator enables it (Admin › Settings), employees draw a signature and confirm a statement when submitting; managers see it with an integrity check. |
| Employee (cont.) | When called during on-call duty, log the intervention (hours, incident #, description) on the **overtime** sheet only. Entries on on-call days are recognised automatically. |
| Expert | On-call only. An intervention doubles that day's on-call rate. No overtime. |
| Line manager | **Approvals**: approve, or reject with a comment, the sheets of direct reports. **Team & export**: status of everyone below them, and the `.xlsx` export of approved sheets. |
| Admin | Users (manager, grade, Expert/Admin flags, local accounts), grades and on-call rates, legal holidays (with `.ics` import), TIL factors, settings (e.g. hide on-call amounts from employees), audit log. Can reopen an approved sheet. |

Colleagues never see each other's declarations. Managers see only the people below them in the hierarchy.

### Export content (`IT_OnCall_Overtime_YYYYMM.xlsx`)
The export is per **payroll period**. Sheets approved by the `PAYROLL_CUTOFF_DAY` (default the 10th) of the following month
are paid with their work month. Anything approved later (including late claims for earlier months) goes into the next open
payroll period and is shaded and labelled "Late claim".
- **Summary**: status of every person in scope, so missing or pending sheets are visible.
- **On-Call**: day counts W/Sat/Sun/H, Expert interventions, amount.
- **On-Call Grid**: the legacy layout, with an extra "– Incidents/calls" row for Experts.
- **Overtime Payroll**: hours by day type, night hours, HL100 / HL101 / HL111 / HL130 / HL121, TIL hours and TIL credit.
- **Overtime Detail** and **On-Call Detail**: line by line.

## Operations
- Backup: `scripts/backup-db.sh` writes `backups/timelog-*.sql.gz` (schedule it with cron).
- Logs: `docker compose logs -f app proxy`.
- Health: `GET /healthz`.
- Update: `scripts/build.sh` (or load the new image), then `docker compose up -d`. The footer of every page shows
  the version and build timestamp, which `/healthz` also returns.

## Development
```bash
cd app && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q
DATABASE_URL=sqlite:///./dev.db SECRET_KEY=$(openssl rand -hex 32) SESSION_COOKIE_SECURE=false \
  BOOTSTRAP_ADMIN_USERNAME=admin BOOTSTRAP_ADMIN_PASSWORD='<choose-a-local-password>' \
  .venv/bin/uvicorn --factory timelog.main:create_app --reload
```
