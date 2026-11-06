# Timelog - offline deployment package for Red Hat Enterprise Linux 9

This package installs Timelog in Docker containers on a RHEL 9 server **without internet access**. It contains the
container images (application, nginx, PostgreSQL), the Docker Engine RPMs (optional, see below), the compose file, the
configuration template, the install script and the documentation (`docs/`).

## Requirements on the server
- RHEL 9 (or a compatible 9.x), x86_64, root or `sudo` access
- Docker Engine with the Compose v2 plugin. `install.sh --install-docker` installs it from `docker-rpms/` when this package
  contains them (`MANIFEST.txt` says). If you already run Docker, nothing is installed.
- A DNS name for the server (for example `timelog.corp.example.lu`) and, for production, a certificate for it from your PKI
- Free ports 80 and 443 (or choose others with `--http-port` / `--https-port`)
- SELinux can stay **enforcing**: the compose file labels its bind mounts (`:z`)

> Docker Engine is not part of RHEL (Red Hat ships Podman). The RPMs in this package come from Docker's own CentOS/RHEL
> repository, built on a RHEL-compatible 9 userland. If your organisation standardises on Podman, or already provides Docker
> RPMs, use those and run the install without `--install-docker`.

## Install
```bash
unzip timelog-<version>-offline-rhel9-amd64.zip && cd timelog-<version>-offline-rhel9-amd64
sha256sum -c ../timelog-<version>-offline-rhel9-amd64.zip.sha256     # optional: compare with the build machine's checksum
sudo ./install.sh --server-name timelog.corp.example.lu --install-docker --open-firewall
```
The script installs Docker (if asked and missing), verifies the package, loads the images, **installs the program into
`/opt/timelog`** (change it with `--dir`), creates `.env` with **generated secrets**, creates a temporary self-signed certificate if none exists, opens the firewall ports, starts the stack and prints
the first administrator password. Open `https://<server-name>` and sign in as `admin`.

Everything the server needs lives in the installation directory (`/opt/timelog`): `docker-compose.yml`, `.env`, `certs/`,
`db-data/` (PostgreSQL) and `backups/`. The unzipped package can be deleted. Run the `docker compose` and `./convert-pfx.sh`
commands below from that directory. Back up `.env`, `certs/` and `backups/` (the database itself is covered by the backups).

Then, in this order:
1. **Install the PKI certificate.** The PKI usually hands out a `.pfx`: run `./convert-pfx.sh /path/server.pfx` (asks for its
   password; no openssl needed), then `docker compose restart proxy`. If you already have PEM files, put the server certificate
   followed by the chain in `certs/server.crt` and the unencrypted key in `certs/server.key`. Details: `docs/README.md`.
2. **Configure LDAP** in Admin > Settings > LDAP (paste the PKI root CA in the form, or copy it to `certs/ldap-ca.pem`) and use
   *Test connection*. Afterwards set `LOCAL_LOGIN_ADMIN_ONLY=true` in `.env` and run `docker compose up -d`.
3. **Protect the secrets.** `.env` holds `SECRET_KEY`, `POSTGRES_PASSWORD` and `BACKUP_PASSPHRASE`: keep a protected copy
   elsewhere. Without `SECRET_KEY` the stored LDAP password cannot be read; without `BACKUP_PASSPHRASE` backups cannot be restored.
4. **Schedule backups** in Admin > Backup and copy the files off the host regularly.

## Upgrade to a newer package
1. Take a backup (Admin > Backup > *Back up now*) and download it.
2. Unzip the new package anywhere and run the installer again. It finds the installation (`/opt/timelog/.env`, or the folder
   given with `--dir`), keeps `.env`, `certs/`, `db-data/` and `backups/`, replaces the program files and images and restarts:
   ```bash
   unzip timelog-<new-version>-offline-rhel9-amd64.zip && cd timelog-<new-version>-offline-rhel9-amd64
   sudo ./install.sh
   ```
   No `--server-name` is needed for an upgrade. New tables are created by the application at start-up.
3. Check the version in the page footer or at `https://<server>/healthz`.
To go back, run `install.sh` from the previous package (and restore the backup if needed).

## Daily operation
```bash
docker compose ps                      # state of the three services
docker compose logs -f app proxy       # logs
docker compose restart proxy           # after changing the certificate
docker compose down                    # stop (data is kept); never add -v unless you want to delete the data
```
Start at boot: Docker is enabled as a service and the containers have `restart: unless-stopped`.

## Troubleshooting
| Symptom | Check |
|---|---|
| `Permission denied` reading `/etc/nginx/certs` or `/certs` | SELinux: the compose file must keep the `:z` suffix on the bind mounts; check `ausearch -m avc -ts recent` |
| Cannot reach the site from other machines | `firewall-cmd --list-ports`: run `install.sh --open-firewall` or `firewall-cmd --permanent --add-port=443/tcp && firewall-cmd --reload` |
| `dnf` complains about a missing dependency while installing the Docker RPMs | Install the named package from your RHEL repositories / Satellite, then re-run |
| Checksum mismatch | The zip was damaged in transfer: copy it again and compare the SHA-256 |
| `exec format error` / `no matching manifest` | The package was built for another CPU architecture |
| Cannot sign in with LDAP | Admin > Settings > LDAP > *Test connection*; local accounts still work |
