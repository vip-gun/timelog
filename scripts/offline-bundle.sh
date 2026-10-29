#!/usr/bin/env sh
# Build all images on a machine with registry/PyPI access and export them for an air-gapped host.
# On the target:  docker load -i dist/timelog-images.tar.gz && docker compose up -d --no-build
set -eu
cd "$(dirname "$0")/.."
VERSION="${TIMELOG_VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app/timelog/__init__.py)}"
export TIMELOG_VERSION="$VERSION"
export BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || true)"
docker compose --env-file .env.example build app
docker pull postgres:16-alpine
docker pull nginx:1.27-alpine
mkdir -p dist
docker save "timelog-app:$VERSION" postgres:16-alpine nginx:1.27-alpine | gzip > dist/timelog-images.tar.gz
tar czf dist/timelog-config.tar.gz docker-compose.yml .env.example proxy scripts README.md CLAUDE.md certs/README.md
echo "Bundle ready in dist/: timelog-images.tar.gz + timelog-config.tar.gz"
