#!/usr/bin/env sh
# Build the OFFLINE DEPLOYMENT PACKAGE. Run it on a machine with internet access and Docker.
#
#   scripts/make-offline-package.sh                    # RHEL 9, x86_64 -> dist/timelog-<version>-offline-rhel9-amd64.zip
#   TARGET=windows scripts/make-offline-package.sh     # Windows Server (Linux containers) -> ...-offline-windows-amd64.zip
#   PLATFORM=linux/arm64 scripts/make-offline-package.sh   # other CPU architecture (default linux/amd64)
#   INCLUDE_DOCKER_RPMS=0 scripts/make-offline-package.sh  # RHEL: do not bundle the Docker Engine RPMs
#
# Targets (the application always runs in Docker containers):
#   rhel9    Red Hat Enterprise Linux 9: shell installer, SELinux-ready, Docker Engine RPMs bundled (installed by
#            install.sh --install-docker, no repository needed)
#   windows  Windows Server with Docker running LINUX containers (WSL 2): PowerShell installer. Docker itself must
#            already be installed on the server.
#
# The zip contains everything needed on an air-gapped host: the container images (app, nginx, PostgreSQL), the compose
# file, configuration template, install / upgrade script, certificate helper, and the documentation.
set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)

TARGET="${TARGET:-rhel9}"
PLATFORM="${PLATFORM:-linux/amd64}"
INCLUDE_DOCKER_RPMS="${INCLUDE_DOCKER_RPMS:-1}"
VERSION="${TIMELOG_VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app/timelog/__init__.py)}"
BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || true)"
PG_IMAGE="postgres:16-alpine"
NGINX_IMAGE="nginx:1.27-alpine"
NAME="timelog-${VERSION}-offline-${TARGET}-${PLATFORM#linux/}"
OUT="$ROOT/dist"
STAGE="$OUT/$NAME"

die() { echo "make-offline-package: $*" >&2; exit 1; }
case "$TARGET" in rhel9|windows) ;; *) die "unknown TARGET '$TARGET' (rhel9 or windows)";; esac
command -v docker >/dev/null || die "docker not found"
docker buildx version >/dev/null 2>&1 || die "docker buildx not available"
command -v zip >/dev/null || die "zip not found"
command -v python3 >/dev/null || die "python3 not found (used to verify the image architectures)"
# the images named here must be the ones the compose file uses
grep -q "image: $PG_IMAGE" docker-compose.yml || die "docker-compose.yml no longer uses $PG_IMAGE: update this script"
grep -q "image: $NGINX_IMAGE" docker-compose.yml || die "docker-compose.yml no longer uses $NGINX_IMAGE: update this script"

echo "==> Building timelog-app:$VERSION for $PLATFORM"
docker buildx build --platform "$PLATFORM" --load -t "timelog-app:$VERSION" \
  --build-arg "BUILD_TIMESTAMP=$BUILD_TIMESTAMP" --build-arg "GIT_COMMIT=$GIT_COMMIT" \
  --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.org/simple}" \
  ${PIP_TRUSTED_HOST:+--build-arg "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST"} app

echo "==> Pulling base images for $PLATFORM"
docker pull --platform "$PLATFORM" "$PG_IMAGE"
docker pull --platform "$PLATFORM" "$NGINX_IMAGE"

echo "==> Assembling $STAGE"
rm -rf "$STAGE" "$OUT/$NAME.zip" "$OUT/$NAME.zip.sha256"
mkdir -p "$STAGE/images" "$STAGE/proxy" "$STAGE/certs" "$STAGE/docs"
# --platform: the image store can hold several architectures under one tag; save exactly the target one
docker save --platform "$PLATFORM" "timelog-app:$VERSION" "$PG_IMAGE" "$NGINX_IMAGE" \
  | gzip > "$STAGE/images/timelog-images.tar.gz" || die "docker save failed (--platform needs Docker 28 or later)"

echo "==> Verifying the architecture of every saved image"
CHECK=$(mktemp -d)
gunzip -c "$STAGE/images/timelog-images.tar.gz" | tar -x -C "$CHECK"
python3 - "$CHECK" "${PLATFORM#linux/}" <<'PY' || die "the saved images do not match $PLATFORM"
import json, sys
root, want = sys.argv[1], sys.argv[2]
bad = False
for m in json.load(open(root + "/manifest.json")):
    arch = json.load(open(root + "/" + m["Config"])).get("architecture")
    print("   %-28s %s" % (", ".join(m.get("RepoTags") or ["?"]), arch))
    bad |= arch != want
sys.exit(1 if bad else 0)
PY

cp docker-compose.yml .env.example "$STAGE/"
cp proxy/nginx.conf.template "$STAGE/proxy/"
cp certs/README.md "$STAGE/certs/README.md"
cp README.md "$STAGE/docs/README.md"
cp CLAUDE.md "$STAGE/docs/DESIGN-AND-BUSINESS-RULES.md"
cp app/requirements.txt "$STAGE/docs/python-dependencies.txt"
echo "$VERSION" > "$STAGE/VERSION"
DOCKER_NOTE="Docker Engine must already be installed on the target"

case "$TARGET" in
rhel9)
  mkdir -p "$STAGE/scripts"
  cp deploy/install.sh deploy/convert-pfx.sh "$STAGE/"
  cp scripts/backup-db.sh "$STAGE/scripts/"
  cp deploy/README-OFFLINE-rhel9.md "$STAGE/README-OFFLINE.md"
  chmod +x "$STAGE/install.sh" "$STAGE/convert-pfx.sh" "$STAGE"/scripts/*.sh
  if [ "$INCLUDE_DOCKER_RPMS" = 1 ]; then
    echo "==> Downloading the Docker Engine RPMs for RHEL 9 (${PLATFORM#linux/})"
    mkdir -p "$STAGE/docker-rpms"
    docker run --rm --platform "$PLATFORM" -v "$STAGE/docker-rpms:/out" almalinux:9 sh -c '
      set -e
      dnf -y -q install dnf-plugins-core
      dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
      dnf -y -q download --resolve --destdir /out docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin' \
      || die "could not download the Docker RPMs (use INCLUDE_DOCKER_RPMS=0 if the target already has Docker)"
    ls "$STAGE"/docker-rpms/*.rpm >/dev/null 2>&1 || die "no Docker RPM was downloaded"
    DOCKER_NOTE="Docker Engine RPMs included in docker-rpms/ (install.sh --install-docker): $(ls "$STAGE"/docker-rpms | grep -c rpm) packages"
  else
    DOCKER_NOTE="Docker Engine RPMs NOT included: Docker must already be installed on the target"
  fi
  ;;
windows)
  cp deploy/install.ps1 deploy/convert-pfx.ps1 "$STAGE/"
  cp deploy/README-OFFLINE-windows.md "$STAGE/README-OFFLINE.md"
  DOCKER_NOTE="Docker with LINUX containers (WSL 2) must already be installed on the Windows Server"
  ;;
esac

APP_ID=$(docker image inspect --format '{{.Id}}' "timelog-app:$VERSION")
cat > "$STAGE/MANIFEST.txt" <<MANIFEST
Timelog offline deployment package
version:    $VERSION
built:      $BUILD_TIMESTAMP
commit:     ${GIT_COMMIT:-n/a}
platform:   $PLATFORM
images:     timelog-app:$VERSION ($APP_ID)
            $PG_IMAGE
            $NGINX_IMAGE
target:     $TARGET
docker:     $DOCKER_NOTE
requires:   Docker Engine 24+ with the Compose v2 plugin on a $PLATFORM host. No internet access is needed on the
            target and no openssl: certificates are handled with the application image.
MANIFEST

# integrity: checksums of every file of the package (verified by install.sh)
( cd "$STAGE" && find . -type f ! -name CHECKSUMS.sha256 | sort | xargs shasum -a 256 > CHECKSUMS.sha256 )

( cd "$OUT" && zip -q -r "$NAME.zip" "$NAME" )
( cd "$OUT" && shasum -a 256 "$NAME.zip" > "$NAME.zip.sha256" )
echo "==> Done"
ls -lh "$OUT/$NAME.zip"
cat "$OUT/$NAME.zip.sha256"
echo "Copy the zip to the target host (and compare the SHA-256), then follow README-OFFLINE.md."
