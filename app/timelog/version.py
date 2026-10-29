"""Application version and build information shown in the page footer.

The build timestamp / commit are baked into the image by the Dockerfile (files BUILD_TIMESTAMP and
BUILD_COMMIT next to this module) or given as environment variables. Without them (local development)
the server start time is shown and the build is labelled "dev".
"""
import datetime as dt
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from . import __version__

_HERE = Path(__file__).parent


def _read(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        try:
            value = (_HERE / name).read_text().strip()
        except OSError:
            value = ""
    return value


@dataclass(frozen=True)
class BuildInfo:
    version: str
    timestamp: str  # ISO-8601 UTC
    commit: str
    dev: bool

    @property
    def display_time(self) -> str:
        try:
            return dt.datetime.fromisoformat(self.timestamp.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M UTC")
        except ValueError:
            return self.timestamp

    @property
    def asset_tag(self) -> str:
        """Cache-busting suffix for static assets: changes with every build / dev restart."""
        return hashlib.sha1(f"{self.version}{self.timestamp}{self.commit}".encode()).hexdigest()[:10]


def _load() -> BuildInfo:
    timestamp, commit = _read("BUILD_TIMESTAMP"), _read("BUILD_COMMIT")
    dev = not timestamp
    if dev:
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return BuildInfo(version=__version__, timestamp=timestamp, commit=commit[:12], dev=dev)


BUILD = _load()
