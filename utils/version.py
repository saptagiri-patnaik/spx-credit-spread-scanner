"""The running software's version, for log lines and alerts.

Derived from git rather than hand-maintained, so it can never drift from the code
that is actually running -- the failure mode of a manual version constant is that
someone forgets to bump it and every log line then lies.

Format: `r<commits>-g<hash>`, optionally `-dirty`, e.g. `r9-g535dc6c`. The commit
count sorts monotonically at a glance, and the hash identifies the exact tree.
`-dirty` means uncommitted changes were present, so the build is not reproducible.

Two resolution paths, because the two deployments differ:
  - Lambda: the image has no `.git` (excluded by .dockerignore) and no git binary,
    so `deploy.ps1` computes the version at build time and bakes it into the
    APP_VERSION environment variable.
  - Laptop: no APP_VERSION, so it is read from the working tree at runtime.
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

UNKNOWN = "unknown"

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ("git", *args),
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None  # no git binary, or it hung
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _from_git() -> str | None:
    revision = _git("rev-parse", "--short=7", "HEAD")
    if not revision:
        return None
    count = _git("rev-list", "--count", "HEAD")
    version = f"r{count}-g{revision}" if count else f"g{revision}"
    # An empty porcelain listing means clean; None means the check itself failed,
    # which should not be reported as clean.
    status = _git("status", "--porcelain")
    if status is None or status:
        version += "-dirty"
    return version


@functools.lru_cache(maxsize=1)
def get_version() -> str:
    """Version string for this process. Cached: the answer cannot change at runtime."""
    # Imported here rather than at module scope so tests can monkeypatch the
    # environment without fighting import order.
    import os

    baked = os.environ.get("APP_VERSION", "").strip()
    if baked:
        return baked
    return _from_git() or UNKNOWN
