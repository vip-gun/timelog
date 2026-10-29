# Timelog - offline deployment package for Windows Server

This package installs Timelog in Docker containers on Windows Server **without internet access**. Timelog runs as **Linux
containers** (application, nginx, PostgreSQL). It contains the container images, the compose file, the configuration
template, the PowerShell install script and the documentation (`docs/`).

## Requirements on the server
- Windows Server 2022 or later (x64), an elevated PowerShell (Run as administrator)
- **Docker with Linux containers and the Compose v2 plugin.** Docker is not part of this package and has to be installed
  beforehand (the installation media can be copied in like this package). The usual ways on Windows Server:
  - **Docker Engine inside WSL 2** (a Linux distribution such as RHEL-compatible or Ubuntu, with `docker-ce` + `docker-compose-plugin`
    installed from your internal repository), with the `docker` CLI reachable from PowerShell; or
  - **Docker Desktop** with the WSL 2 backend (check the licence terms for server use in your organisation).
  Windows-native "Windows containers" mode is **not** supported: `install.ps1` stops if Docker is not in Linux mode.
- A DNS name for the server and, for production, a certificate for it from your PKI
- Free ports 80 and 443 (or choose others with `-HttpPort` / `-HttpsPort`)

## Install
```powershell
Expand-Archive timelog-<version>-offline-windows-amd64.zip -DestinationPath C:\timelog ; cd C:\timelog\timelog-<version>-offline-windows-amd64
Get-FileHash ..\timelog-<version>-offline-windows-amd64.zip -Algorithm SHA256     # optional: compare with the build machine's checksum
powershell -ExecutionPolicy Bypass -File .\install.ps1 -ServerName timelog.corp.example.lu -OpenFirewall
```
The script verifies the package, loads the images, creates `.env` with **generated secrets**, creates a temporary self-signed
certificate if none exists, opens the Windows Firewall ports, starts the stack and prints the first administrator password.
Open `https://<server-name>` and sign in as `admin`.

Then, in this order:
1. **Install the PKI certificate.** Export or receive the certificate **with its private key as a `.pfx`**, then run
   `.\convert-pfx.ps1 C:\path\server.pfx` (asks for the password) and `docker compose restart proxy`.
2. **Configure LDAP** (Active Directory) in Admin > Settings > LDAP and use *Test connection*. Afterwards set
   `LOCAL_LOGIN_ADMIN_ONLY=true` in `.env` and run `docker compose up -d`.
3. **Protect the secrets.** `.env` holds `SECRET_KEY`, `POSTGRES_PASSWORD` and `BACKUP_PASSPHRASE` (readable by Administrators only):
   keep a protected copy elsewhere. Without them the LDAP password and the backups cannot be read.
4. **Schedule backups** in Admin > Backup and copy the files off the server regularly.

## Upgrade to a newer package
1. Take a backup (Admin > Backup) and download it.
2. Expand the new package next to the installed one, copy `.env` and the `certs` folder into it, then run
   `powershell -ExecutionPolicy Bypass -File .\install.ps1 -Upgrade`. Data stays in the Docker volumes.
3. Check the version in the page footer or at `https://<server>/healthz`.

## Daily operation
```powershell
docker compose ps ; docker compose logs -f app proxy ; docker compose restart proxy
docker compose down        # stop (data is kept); never add -v unless you want to delete the data
```
Make sure the Docker service / WSL distribution starts at boot (a scheduled task or service for the WSL engine) so the containers
come back after a reboot (`restart: unless-stopped`).

## Troubleshooting
| Symptom | Check |
|---|---|
| "Docker is in 'windows' containers mode" | Switch to Linux containers (Docker Desktop) or use Docker Engine in WSL 2 |
| Script blocked | Run it with `powershell -ExecutionPolicy Bypass -File .\install.ps1 ...` |
| Cannot reach the site from other machines | Windows Firewall: `-OpenFirewall`, or allow TCP 80/443; with WSL 2 check the port forwarding to the WSL network |
| Checksum mismatch | The zip was damaged in transfer: copy it again and compare the SHA-256 |
| Cannot sign in with LDAP | Admin > Settings > LDAP > *Test connection*; local accounts still work |
