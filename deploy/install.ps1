<#
.SYNOPSIS
  Install or upgrade Timelog from the offline package on Windows Server (Docker with Linux containers).
.DESCRIPTION
  Run in an elevated PowerShell inside the unzipped package folder.
    .\install.ps1 -ServerName timelog.corp.example.lu               # first installation
    .\install.ps1 -ServerName timelog.corp.example.lu -OpenFirewall # also opens the Windows Firewall ports
    .\install.ps1 -Upgrade                                           # move an existing installation to this version
  Needs Docker with Linux containers (Docker Engine inside WSL 2, or Docker Desktop) and the Compose v2 plugin.
#>
param(
  [string]$ServerName = "",
  [int]$HttpPort = 0,
  [int]$HttpsPort = 0,
  [switch]$Upgrade,
  [switch]$OpenFirewall,
  [switch]$NoStart
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
function Say($m) { Write-Host "==> $m" }
function Die($m) { Write-Error "install: $m"; exit 1 }
function Run-Docker { & docker @args; if ($LASTEXITCODE -ne 0) { Die "docker $($args -join ' ') failed" } }

# 0. Docker with Linux containers
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Die "Docker is not installed. See README-OFFLINE.md (prerequisites)." }
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Die "The Docker daemon is not reachable (is Docker running?)" }
$osType = (& docker info --format '{{.OSType}}').Trim()
if ($osType -ne "linux") { Die "Docker is in '$osType' containers mode: Timelog needs LINUX containers (WSL 2 backend). See README-OFFLINE.md." }
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Die "The Docker Compose v2 plugin is required ('docker compose')." }
$Version = (Get-Content VERSION -Raw).Trim()

# 1. integrity of the package
Say "Verifying the package checksums"
foreach ($line in Get-Content CHECKSUMS.sha256) {
  if ($line -match '^([0-9a-f]{64})\s+\*?(.+)$') {
    $file = $Matches[2]
    $actual = (Get-FileHash -Algorithm SHA256 -Path $file).Hash.ToLower()
    if ($actual -ne $Matches[1]) { Die "checksum mismatch for $file: the package is damaged" }
  }
}

# 2. images (docker load reads the .tar.gz directly)
Say "Loading the container images (a few minutes)"
Run-Docker load -i images/timelog-images.tar.gz

# 3. configuration
function New-Secret([int]$length) {
  $chars = [char[]]'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $bytes = New-Object byte[] $length
  $rng.GetBytes($bytes)
  -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}
function Set-Env([string]$key, [string]$value) {
  $lines = [System.Collections.Generic.List[string]](Get-Content .env)
  $i = $lines.FindIndex({ param($l) $l.StartsWith("$key=") })
  if ($i -ge 0) { $lines[$i] = "$key=$value" } else { $lines.Add("$key=$value") }
  [System.IO.File]::WriteAllText("$PWD\.env", (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
}
function Get-Env([string]$key) {
  $l = Get-Content .env | Where-Object { $_.StartsWith("$key=") } | Select-Object -First 1
  if ($l) { $l.Substring($key.Length + 1) } else { "" }
}
$NewAdminPassword = ""
if (-not (Test-Path .env)) {
  if (-not $ServerName) { Die "first installation: -ServerName <fqdn> is required (the name in the TLS certificate)" }
  Say "Creating .env with generated secrets"
  Copy-Item .env.example .env
  $NewAdminPassword = New-Secret 20
  Set-Env SERVER_NAME $ServerName
  Set-Env APP_BASE_URL "https://$ServerName"
  Set-Env SECRET_KEY (New-Secret 64)
  Set-Env POSTGRES_PASSWORD (New-Secret 32)
  Set-Env BOOTSTRAP_ADMIN_PASSWORD $NewAdminPassword
  Set-Env BACKUP_PASSPHRASE (New-Secret 40)
  if ($HttpPort -gt 0) { Set-Env HTTP_PORT $HttpPort }
  if ($HttpsPort -gt 0) { Set-Env HTTPS_PORT $HttpsPort; if ($HttpsPort -ne 443) { Set-Env HTTPS_PORT_SUFFIX ":$HttpsPort" } }
} elseif (-not $Upgrade) {
  Say ".env already exists: kept (use -Upgrade to move an existing installation to this version)"
}
Set-Env TIMELOG_VERSION $Version
# keep the secrets file readable by Administrators and SYSTEM only
& icacls .env /inheritance:r /grant:r "Administrators:F" "SYSTEM:F" *> $null
$ServerName = Get-Env SERVER_NAME

# 4. TLS certificate: a temporary self-signed one is made with the application image
New-Item -ItemType Directory -Force -Path certs | Out-Null
if (-not ((Test-Path certs\server.crt) -and (Test-Path certs\server.key))) {
  Say "No certificate found: creating a SELF-SIGNED one for $ServerName (replace it with the PKI certificate, see README-OFFLINE.md)"
  Run-Docker run --rm --user 0:0 -v "$PWD\certs:/out" --entrypoint python "timelog-app:$Version" -m timelog.certtool selfsigned $ServerName /out
}

# 4b. data folders (bind mounts). Versions before 1.11.1 used Docker named volumes: copy their content once.
function Move-Volume([string]$volume, [string]$folder, [string]$image) {
  & docker volume inspect $volume *> $null
  if ($LASTEXITCODE -ne 0) { return }
  if (Get-ChildItem $folder -Force -ErrorAction SilentlyContinue) { return }
  Say "Copying the data of the Docker volume $volume to .\$folder (the services are stopped meanwhile)"
  & docker compose stop *> $null
  Run-Docker run --rm -v "${volume}:/from:ro" -v "$PWD\${folder}:/to" --entrypoint sh $image -c 'cp -a /from/. /to/'
}
New-Item -ItemType Directory -Force -Path db-data, backups | Out-Null
Move-Volume timelog_db-data db-data "postgres:16-alpine"
Move-Volume timelog_backups backups "timelog-app:$Version"

if ($Upgrade -and -not (Get-ChildItem db-data -Force -ErrorAction SilentlyContinue)) {
  Die "-Upgrade found no data in .\db-data. Copy db-data\ and backups\ from the previous installation folder, or run the upgrade inside that folder"
}

# 5. firewall
$hp = Get-Env HTTPS_PORT; if (-not $hp) { $hp = "443" }
$lp = Get-Env HTTP_PORT; if (-not $lp) { $lp = "80" }
if ($OpenFirewall) {
  Say "Opening TCP $lp and $hp in Windows Firewall"
  New-NetFirewallRule -DisplayName "Timelog HTTP/HTTPS" -Direction Inbound -Protocol TCP -LocalPort $lp, $hp -Action Allow -ErrorAction SilentlyContinue | Out-Null
}

# 6. start
if (-not $NoStart) {
  Say "Starting Timelog $Version"
  Run-Docker compose up -d --no-build --pull never
  Write-Host -NoNewline "Waiting for the services to become healthy"
  for ($i = 0; $i -lt 60; $i++) {
    $app = (& docker compose ps app --format '{{.Status}}') -join ""
    $proxy = (& docker compose ps proxy --format '{{.Status}}') -join ""
    if ($app -match '\(healthy\)' -and $proxy -match '^Up') { break }
    Write-Host -NoNewline "."; Start-Sleep -Seconds 3
  }
  Write-Host ""
  & docker compose ps
}

$suffix = ""; if ($hp -ne "443") { $suffix = ":$hp" }
Say "Done: https://$ServerName$suffix"
if ($NewAdminPassword) {
  Write-Host "   First sign-in: user '$(Get-Env BOOTSTRAP_ADMIN_USERNAME)', password: $NewAdminPassword"
  Write-Host "   Change it after the first login. All secrets are in .env: keep a protected copy,"
  Write-Host "   in particular SECRET_KEY and BACKUP_PASSPHRASE (without them backups cannot be read)."
}
