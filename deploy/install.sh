#!/usr/bin/env sh
# Install or upgrade Timelog from the offline package on a Linux server (RHEL 9). Run inside the unzipped folder.
#
#   sudo ./install.sh --server-name timelog.corp.example.lu --install-docker --open-firewall   # first installation
#   ./install.sh --upgrade                                                                      # move to this package version
#
# Options:
#   --server-name FQDN   name in the TLS certificate (required for a first installation)
#   --http-port N / --https-port N   non-standard published ports
#   --install-docker     install Docker Engine from docker-rpms/ when it is missing (needs root)
#   --open-firewall      open the HTTP/HTTPS ports in firewalld (needs root)
#   --upgrade            keep .env, certificates and data; load the new images and restart
#   --no-start           prepare only
set -eu
cd "$(dirname "$0")"

SERVER_NAME=""; HTTP_PORT=""; HTTPS_PORT=""; UPGRADE=0; START=1; INSTALL_DOCKER=0; OPEN_FW=0
while [ $# -gt 0 ]; do
  case "$1" in
    --server-name) SERVER_NAME="${2:?}"; shift 2 ;;
    --http-port) HTTP_PORT="${2:?}"; shift 2 ;;
    --https-port) HTTPS_PORT="${2:?}"; shift 2 ;;
    --install-docker) INSTALL_DOCKER=1; shift ;;
    --open-firewall) OPEN_FW=1; shift ;;
    --upgrade) UPGRADE=1; shift ;;
    --no-start) START=0; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option $1" >&2; exit 2 ;;
  esac
done
die() { echo "install: $*" >&2; exit 1; }
say() { echo "==> $*"; }

# 0. Docker Engine (RHEL 9: from the RPMs shipped in docker-rpms/, no repository access needed)
if ! command -v docker >/dev/null; then
  [ "$INSTALL_DOCKER" = 1 ] || die "Docker is not installed. Re-run with --install-docker (as root) to install it from docker-rpms/, or install Docker Engine yourself."
  [ "$(id -u)" = 0 ] || die "--install-docker needs root (use sudo)"
  ls docker-rpms/*.rpm >/dev/null 2>&1 || die "docker-rpms/ is empty: this package was built without Docker (INCLUDE_DOCKER_RPMS=0)"
  say "Installing Docker Engine from docker-rpms/ (offline)"
  dnf install -y --disablerepo='*' docker-rpms/*.rpm
  systemctl enable --now docker
fi
docker info >/dev/null 2>&1 || die "The Docker daemon is not reachable (is it running, and are you root or in the 'docker' group?)"
docker compose version >/dev/null 2>&1 || die "The Docker Compose v2 plugin is required ('docker compose')."
VERSION=$(cat VERSION)

# 1. integrity of the package
say "Verifying the package checksums"
if command -v sha256sum >/dev/null; then sha256sum -c CHECKSUMS.sha256 --quiet || die "checksum mismatch: the package is damaged"
elif command -v shasum >/dev/null; then shasum -a 256 -c CHECKSUMS.sha256 --quiet || die "checksum mismatch: the package is damaged"
else echo "   (no sha256sum/shasum available: skipped)"; fi

# 2. images
say "Loading the container images (a few minutes)"
docker load -i images/timelog-images.tar.gz

# 3. configuration
setenv() {  # setenv KEY VALUE : replace or append KEY=VALUE in .env
  if grep -q "^$1=" .env; then sed "s|^$1=.*|$1=$2|" .env > .env.tmp && mv .env.tmp .env; else echo "$1=$2" >> .env; fi
}
rand() { LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$1"; }
NEW_ADMIN_PASSWORD=""
if [ ! -f .env ]; then
  [ -n "$SERVER_NAME" ] || die "first installation: --server-name <fqdn> is required (the name in the TLS certificate)"
  say "Creating .env with generated secrets"
  cp .env.example .env
  NEW_ADMIN_PASSWORD="$(rand 20)"
  setenv SERVER_NAME "$SERVER_NAME"
  setenv APP_BASE_URL "https://$SERVER_NAME"
  setenv SECRET_KEY "$(rand 64)"
  setenv POSTGRES_PASSWORD "$(rand 32)"
  setenv BOOTSTRAP_ADMIN_PASSWORD "$NEW_ADMIN_PASSWORD"
  setenv BACKUP_PASSPHRASE "$(rand 40)"
  [ -z "$HTTP_PORT" ] || setenv HTTP_PORT "$HTTP_PORT"
  if [ -n "$HTTPS_PORT" ]; then setenv HTTPS_PORT "$HTTPS_PORT"; [ "$HTTPS_PORT" = 443 ] || setenv HTTPS_PORT_SUFFIX ":$HTTPS_PORT"; fi
elif [ "$UPGRADE" = 0 ]; then
  say ".env already exists: kept (use --upgrade to move an existing installation to this version)"
fi
setenv TIMELOG_VERSION "$VERSION"
chmod 600 .env   # setenv rewrites the file
SERVER_NAME=$(sed -n 's/^SERVER_NAME=//p' .env)

# 4. TLS certificate: a temporary self-signed one is made with the application image (no openssl needed)
mkdir -p certs
if [ ! -f certs/server.crt ] || [ ! -f certs/server.key ]; then
  say "No certificate found: creating a SELF-SIGNED one for $SERVER_NAME (replace it with the PKI certificate, see README-OFFLINE.md)"
  docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/certs:/out:z" --entrypoint python "timelog-app:$VERSION" \
    -m timelog.certtool selfsigned "$SERVER_NAME" /out
fi

# 5. firewall (RHEL: firewalld)
HP=$(sed -n 's/^HTTPS_PORT=//p' .env); HP=${HP:-443}
LP=$(sed -n 's/^HTTP_PORT=//p' .env); LP=${LP:-80}
if [ "$OPEN_FW" = 1 ]; then
  command -v firewall-cmd >/dev/null || die "--open-firewall: firewall-cmd not found"
  [ "$(id -u)" = 0 ] || die "--open-firewall needs root"
  say "Opening TCP $LP and $HP in firewalld"
  firewall-cmd --permanent --add-port="$LP/tcp" --add-port="$HP/tcp" && firewall-cmd --reload
fi

# 6. start
if [ "$START" = 1 ]; then
  say "Starting Timelog $VERSION"
  docker compose up -d --no-build --pull never
  printf "Waiting for the services to become healthy"
  i=0
  while [ $i -lt 60 ]; do
    # app (and through it the database) healthy, proxy up
    if docker compose ps app --format '{{.Status}}' | grep -q '(healthy)' \
       && docker compose ps proxy --format '{{.Status}}' | grep -q '^Up'; then break; fi
    printf "."; sleep 3; i=$((i + 1))
  done
  echo
  docker compose ps
fi

say "Done: https://$SERVER_NAME$( [ "$HP" = 443 ] || echo ":$HP")"
if [ -n "$NEW_ADMIN_PASSWORD" ]; then
  echo "   First sign-in: user '$(sed -n 's/^BOOTSTRAP_ADMIN_USERNAME=//p' .env)', password: $NEW_ADMIN_PASSWORD"
  echo "   Change it after the first login. All secrets are in .env (mode 600): keep a protected copy,"
  echo "   in particular SECRET_KEY and BACKUP_PASSPHRASE (without them backups cannot be read)."
fi
