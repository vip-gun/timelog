#!/usr/bin/env sh
# Build the app image with version / build timestamp / commit baked in (shown in the page footer).
# Usage: scripts/build.sh [extra docker compose build args]
set -eu
cd "$(dirname "$0")/.."
export TIMELOG_VERSION="${TIMELOG_VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app/timelog/__init__.py)}"
export BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || true)"
docker compose build "$@" app
docker tag "timelog-app:$TIMELOG_VERSION" timelog-app:latest   # what `docker compose up` uses by default
echo "Built timelog-app:$TIMELOG_VERSION ($BUILD_TIMESTAMP${GIT_COMMIT:+, $GIT_COMMIT})"
