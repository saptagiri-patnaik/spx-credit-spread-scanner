"""The version string has to be trustworthy: it is what a log line claims is running."""
from __future__ import annotations

import re

from utils import version as version_mod


def _fresh(monkeypatch, **env):
    """get_version is lru_cached, so each case needs the cache cleared."""
    version_mod.get_version.cache_clear()
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return version_mod.get_version()


def test_baked_env_var_wins(monkeypatch):
    # This is the Lambda path: no .git in the image, so APP_VERSION is the truth.
    assert _fresh(monkeypatch, APP_VERSION="r42-gabc1234") == "r42-gabc1234"


def test_blank_env_var_falls_through_to_git(monkeypatch):
    # An empty APP_VERSION must not be mistaken for a real version.
    assert _fresh(monkeypatch, APP_VERSION="   ") != "   "


def test_git_derived_shape(monkeypatch):
    version = _fresh(monkeypatch, APP_VERSION=None)
    # Running from a git checkout, so expect the derived form rather than "unknown".
    assert re.fullmatch(r"(r\d+-)?g[0-9a-f]{7}(-dirty)?", version), version


def test_unknown_when_git_unavailable(monkeypatch):
    monkeypatch.setattr(version_mod, "_git", lambda *args: None)
    assert _fresh(monkeypatch, APP_VERSION=None) == version_mod.UNKNOWN


def test_failed_status_check_is_not_reported_clean(monkeypatch):
    """A git failure must not silently produce a version that claims to be clean."""
    def fake_git(*args):
        if args[0] == "rev-parse":
            return "abc1234"
        if args[0] == "rev-list":
            return "7"
        return None  # the status check fails

    monkeypatch.setattr(version_mod, "_git", fake_git)
    assert _fresh(monkeypatch, APP_VERSION=None) == "r7-gabc1234-dirty"


def test_cached_within_a_process(monkeypatch):
    first = _fresh(monkeypatch, APP_VERSION="r1-g1111111")
    monkeypatch.setenv("APP_VERSION", "r2-g2222222")
    assert version_mod.get_version() == first
