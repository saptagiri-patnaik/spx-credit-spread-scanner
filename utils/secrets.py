"""Fetch the credential bundle from AWS Secrets Manager.

Lambda environment variables are readable by anyone holding
`lambda:GetFunctionConfiguration` -- which is every principal that can describe the
function, not just those that can invoke it -- and they show in plaintext in the
console. Secrets Manager gates the same values behind `secretsmanager:GetSecretValue`
on one resource ARN, and one secret holding a JSON object costs $0.40/month
regardless of how many keys are inside it. One secret per key would be $0.40 each,
which is the whole reason this is a single blob rather than thirteen.

The blob is flat `{"DB_PASSWORD": "...", ...}` -- uppercase names matching what the
variables were called in .env, so config.py's field mapping is unchanged and a value
can still be overridden by exporting the same name.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

log = logging.getLogger("spx")

# Name of the environment variable naming the secret. Kept out of the secret for
# obvious reasons: this is the one pointer that has to travel as plain config.
SECRET_ID_VAR = "SPX_SECRET_ID"


class SecretsUnavailable(RuntimeError):
    """The secret was named but could not be read."""


@lru_cache(maxsize=1)
def load_secret_bundle(secret_id: str | None = None) -> dict[str, str]:
    """Return the secret's key/value pairs, or {} when no secret is configured.

    Cached for the life of the process. A warm Lambda container serves many
    invocations, so this is one API call per cold start (~16/day at the current
    grid, well under a cent a month) rather than one per cycle.

    Returns {} rather than raising when SPX_SECRET_ID is unset: that is the local
    path for someone who has a populated .env and no AWS credentials, and it must
    not turn `import config` into a stack trace. A secret that is *named* but
    unreadable does raise -- that is a misconfiguration, and silently falling back
    to empty credentials would surface as a confusing auth failure several
    seconds later inside an HTTP client.
    """
    secret_id = secret_id or os.environ.get(SECRET_ID_VAR, "").strip()
    if not secret_id:
        return {}

    try:
        import boto3  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the install
        # The Lambda Python runtime ships boto3, so this is the laptop path.
        raise SecretsUnavailable(
            f"{SECRET_ID_VAR}={secret_id} is set but boto3 is not installed. "
            "Install it (pip install -r requirements-dev.txt) or unset the variable "
            "to fall back to .env."
        ) from exc

    # Region: the secret lives beside the function. AWS_REGION is set by the Lambda
    # runtime; on a laptop boto3 falls back to the configured profile's region.
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    client = boto3.client("secretsmanager", region_name=region) if region else \
        boto3.client("secretsmanager")

    try:
        response = client.get_secret_value(SecretId=secret_id)
    except Exception as exc:
        raise SecretsUnavailable(f"Could not read secret {secret_id!r}: {exc}") from exc

    payload = response.get("SecretString")
    if payload is None:
        raise SecretsUnavailable(f"Secret {secret_id!r} holds binary data, not JSON.")

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SecretsUnavailable(f"Secret {secret_id!r} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SecretsUnavailable(f"Secret {secret_id!r} is a {type(parsed).__name__}, not an object.")

    # Values are stringified because pydantic parses from strings anyway, and a
    # JSON number here would otherwise reach a `str | None` field as an int.
    bundle = {str(k): "" if v is None else str(v) for k, v in parsed.items()}
    log.debug("Loaded %d key(s) from secret %s", len(bundle), secret_id)
    return bundle
