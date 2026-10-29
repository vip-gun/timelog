#!/usr/bin/env sh
# Run the test suite with coverage and write app/coverage.xml with repo-relative paths (used by SonarQube,
# see ../sonar/scan.sh and sonar-project.properties).
set -eu
cd "$(dirname "$0")/../app"
.venv/bin/pytest -q --cov=timelog --cov-report=xml:coverage.xml
sed -i.bak "s#<source>.*</source>#<source>app/timelog</source>#" coverage.xml && rm -f coverage.xml.bak
