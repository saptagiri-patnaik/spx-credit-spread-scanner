"""The Secrets Manager settings source: precedence, shape, and failure modes.

The precedence tests are the point of this file. A secret that silently *overrode*
an exported variable would make local debugging baffling -- you would change
DB_HOST in your shell, watch the run connect to production anyway, and have
nothing in the logs to explain it.
"""
from __future__ import annotations

import json

import pytest

from utils import secrets as secrets_mod


@pytest.fixture(autouse=True)
def _clear_cache():
    """The bundle is process-cached; without this every test sees the first one."""
    secrets_mod.load_secret_bundle.cache_clear()
    yield
    secrets_mod.load_secret_bundle.cache_clear()


class _FakeClient:
    def __init__(self, payload, error=None):
        self._payload = payload
        self._error = error
        self.calls = 0

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3's parameter name
        self.calls += 1
        if self._error:
            raise self._error
        return {"SecretString": self._payload}


def _install_fake_boto3(monkeypatch, client):
    import sys
    import types

    fake = types.ModuleType("boto3")
    fake.client = lambda service, **kwargs: client
    monkeypatch.setitem(sys.modules, "boto3", fake)


def test_no_secret_id_returns_empty(monkeypatch):
    """No SPX_SECRET_ID means a laptop with a populated .env: must not raise."""
    monkeypatch.delenv(secrets_mod.SECRET_ID_VAR, raising=False)
    assert secrets_mod.load_secret_bundle() == {}


def test_reads_and_stringifies(monkeypatch):
    client = _FakeClient(json.dumps({"DB_PASSWORD": "pw", "DB_PORT": 5432, "EMPTY": None}))
    _install_fake_boto3(monkeypatch, client)
    monkeypatch.setenv(secrets_mod.SECRET_ID_VAR, "spx-scanner/prod")

    bundle = secrets_mod.load_secret_bundle()
    assert bundle["DB_PASSWORD"] == "pw"
    # A JSON number would otherwise reach a `str | None` field as an int.
    assert bundle["DB_PORT"] == "5432"
    assert bundle["EMPTY"] == ""


def test_cached_across_calls(monkeypatch):
    """One API call per cold start, not one per cycle -- this is the cost control."""
    client = _FakeClient(json.dumps({"DB_PASSWORD": "pw"}))
    _install_fake_boto3(monkeypatch, client)
    monkeypatch.setenv(secrets_mod.SECRET_ID_VAR, "spx-scanner/prod")

    secrets_mod.load_secret_bundle()
    secrets_mod.load_secret_bundle()
    secrets_mod.load_secret_bundle()
    assert client.calls == 1


def test_named_but_unreadable_raises(monkeypatch):
    """A misconfigured secret must fail loudly, not degrade to empty credentials."""
    client = _FakeClient(None, error=RuntimeError("AccessDeniedException"))
    _install_fake_boto3(monkeypatch, client)
    monkeypatch.setenv(secrets_mod.SECRET_ID_VAR, "spx-scanner/prod")

    with pytest.raises(secrets_mod.SecretsUnavailable, match="Could not read secret"):
        secrets_mod.load_secret_bundle()


def test_non_json_raises(monkeypatch):
    _install_fake_boto3(monkeypatch, _FakeClient("not json at all"))
    monkeypatch.setenv(secrets_mod.SECRET_ID_VAR, "spx-scanner/prod")

    with pytest.raises(secrets_mod.SecretsUnavailable, match="not valid JSON"):
        secrets_mod.load_secret_bundle()


def test_json_array_raises(monkeypatch):
    """Valid JSON but the wrong shape: a list has no key/value pairs to bind."""
    _install_fake_boto3(monkeypatch, _FakeClient(json.dumps(["a", "b"])))
    monkeypatch.setenv(secrets_mod.SECRET_ID_VAR, "spx-scanner/prod")

    with pytest.raises(secrets_mod.SecretsUnavailable, match="not an object"):
        secrets_mod.load_secret_bundle()


def test_secret_fills_unset_field(monkeypatch):
    """The whole point: a credential absent from the environment comes from the secret."""
    _install_fake_boto3(monkeypatch, _FakeClient(json.dumps({"FINNHUB_KEY": "from-secret"})))
    monkeypatch.setenv(secrets_mod.SECRET_ID_VAR, "spx-scanner/prod")
    monkeypatch.delenv("FINNHUB_KEY", raising=False)

    from config import Settings

    assert Settings(_env_file=None).finnhub_key == "from-secret"


def test_environment_beats_secret(monkeypatch):
    """Exported values win, so a scratch run can be pointed elsewhere safely."""
    _install_fake_boto3(monkeypatch, _FakeClient(json.dumps({"FINNHUB_KEY": "from-secret"})))
    monkeypatch.setenv(secrets_mod.SECRET_ID_VAR, "spx-scanner/prod")
    monkeypatch.setenv("FINNHUB_KEY", "from-env")

    from config import Settings

    assert Settings(_env_file=None).finnhub_key == "from-env"


def test_blank_dotenv_line_falls_through_to_secret(monkeypatch, tmp_path):
    """The documented end state of the migration: credential lines left empty in .env.

    Pydantic reads `FINNHUB_KEY=` as a provided empty string and .env outranks the
    secret, so without BlankIsUnsetSource this returns "" -- every credential blank
    on a laptop that followed .env.example, failing at auth several seconds later.
    """
    _install_fake_boto3(monkeypatch, _FakeClient(json.dumps({"FINNHUB_KEY": "from-secret"})))
    monkeypatch.setenv(secrets_mod.SECRET_ID_VAR, "spx-scanner/prod")
    monkeypatch.delenv("FINNHUB_KEY", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text("FINNHUB_KEY=\n", encoding="utf-8")

    from config import Settings

    assert Settings(_env_file=str(env_file)).finnhub_key == "from-secret"


def test_blank_environment_variable_still_wins(monkeypatch):
    """Blank in the *environment* must stay blank -- that is how alerts get muted.

    local-lambda.ps1 blanks DISCORD_WEBHOOK_URL so a debugging run cannot push to a
    channel you actually watch. If "" fell through to the secret the way a blank
    .env line does, that run would post for real.
    """
    _install_fake_boto3(monkeypatch, _FakeClient(json.dumps({
        "DISCORD_WEBHOOK_URL": "https://discord.example/hook",
    })))
    monkeypatch.setenv(secrets_mod.SECRET_ID_VAR, "spx-scanner/prod")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")

    from config import Settings

    assert Settings(_env_file=None).discord_webhook_url == ""


def test_secret_feeds_assembled_database_url(monkeypatch):
    """DB_* arrive from the secret and still assemble, with the password encoded."""
    _install_fake_boto3(monkeypatch, _FakeClient(json.dumps({
        "DB_HOST": "db.example.com",
        "DB_USER": "spx",
        "DB_PASSWORD": "p@ss:word/x",
        "DB_NAME": "spxdb",
    })))
    monkeypatch.setenv(secrets_mod.SECRET_ID_VAR, "spx-scanner/prod")
    for key in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        monkeypatch.delenv(key, raising=False)

    from config import Settings

    url = Settings(_env_file=None).database_url
    assert "db.example.com" in url and "spxdb" in url
    # Raw special characters here would produce a URL that parses to a different host.
    assert "p%40ss%3Aword%2Fx" in url
