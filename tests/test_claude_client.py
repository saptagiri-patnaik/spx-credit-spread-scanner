"""Tests for the Claude scorer and provider selection."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from analysis.claude_client import SCORE_SCHEMA, ClaudeClient, build_llm
from analysis.llm import OllamaClient


class _Log:
    def __init__(self):
        self.warnings = []

    def info(self, *a, **k):
        pass

    def warning(self, msg, *a):
        self.warnings.append(msg % a if a else msg)


def _payload(**over):
    body = {"direction": -0.4, "magnitude": 0.3, "confidence": 0.7,
            "macro_impact": "risk-off", "catalysts": ["cpi"]}
    body.update(over)
    return body


def _response(payload=None, stop_reason="end_turn", text=None):
    block = SimpleNamespace(type="text", text=text if text is not None
                            else json.dumps(payload or _payload()))
    return SimpleNamespace(content=[block], stop_reason=stop_reason)


def _client(log, response=None, raises=None):
    c = ClaudeClient.__new__(ClaudeClient)   # bypass SDK construction
    c.model, c.log, c.max_tokens = "claude-haiku-4-5", log, 512
    c._import_error = None

    class _Messages:
        def create(self, **kwargs):
            c.last_kwargs = kwargs
            if raises:
                raise raises
            return response

    c._client = SimpleNamespace(messages=_Messages())
    return c


# ------------------------------------------------------------------ schema --
def test_schema_requires_every_field_sentiment_reads():
    for key in ("direction", "magnitude", "confidence", "macro_impact", "catalysts"):
        assert key in SCORE_SCHEMA["properties"]
        assert key in SCORE_SCHEMA["required"]
    assert SCORE_SCHEMA["additionalProperties"] is False


def test_schema_omits_numeric_bounds():
    # Structured outputs reject minimum/maximum; clamping is sentiment.py's job.
    for field in ("direction", "magnitude", "confidence"):
        prop = SCORE_SCHEMA["properties"][field]
        assert "minimum" not in prop and "maximum" not in prop


# ----------------------------------------------------------------- happy ---
def test_parses_a_valid_response():
    out = _client(_Log(), _response()).generate_json("p", system="s")
    assert out["direction"] == -0.4
    assert out["macro_impact"] == "risk-off"


def test_sends_the_schema_and_the_system_prompt():
    c = _client(_Log(), _response())
    c.generate_json("the prompt", system="the system")
    kwargs = c.last_kwargs
    assert kwargs["system"] == "the system"
    assert kwargs["messages"] == [{"role": "user", "content": "the prompt"}]
    assert kwargs["output_config"]["format"]["schema"] == SCORE_SCHEMA
    assert kwargs["model"] == "claude-haiku-4-5"


# ---------------------------------------------------------------- failure --
def test_refusal_returns_none_and_warns():
    log = _Log()
    assert _client(log, _response(stop_reason="refusal")).generate_json("p") is None
    assert any("declined" in w for w in log.warnings)


def test_truncation_returns_none_rather_than_partial_json():
    log = _Log()
    assert _client(log, _response(stop_reason="max_tokens")).generate_json("p") is None
    assert any("truncated" in w for w in log.warnings)


def test_unparseable_text_returns_none():
    log = _Log()
    assert _client(log, _response(text="not json")).generate_json("p") is None
    assert any("unparseable" in w for w in log.warnings)


def test_empty_content_returns_none():
    resp = SimpleNamespace(content=[], stop_reason="end_turn")
    assert _client(_Log(), resp).generate_json("p") is None


def test_rate_limit_is_swallowed_not_raised():
    anthropic = pytest.importorskip("anthropic")
    httpx = pytest.importorskip("httpx")
    # The SDK's exception types need a real httpx.Response to construct.
    response = httpx.Response(
        429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    err = anthropic.RateLimitError("slow down", response=response, body=None)
    log = _Log()
    assert _client(log, raises=err).generate_json("p") is None
    assert any("rate limited" in w.lower() for w in log.warnings)


def test_unavailable_client_returns_none():
    c = ClaudeClient.__new__(ClaudeClient)
    c._client, c._import_error, c.log = None, "no key", _Log()
    assert c.available() is False
    assert c.generate_json("p") is None


# --------------------------------------------------------------- selection --
def test_build_llm_defaults_to_ollama():
    s = SimpleNamespace(ollama_base_url="http://x", ollama_model="m")
    assert isinstance(build_llm(s, _Log()), OllamaClient)


def test_build_llm_selects_anthropic():
    s = SimpleNamespace(llm_provider="anthropic", anthropic_model="claude-haiku-4-5",
                        anthropic_api_key="sk-test", anthropic_max_tokens=512,
                        ollama_base_url="http://x", ollama_model="m")
    client = build_llm(s, _Log())
    assert isinstance(client, ClaudeClient)
    assert client.model == "claude-haiku-4-5"


def test_unknown_provider_falls_back_to_ollama():
    s = SimpleNamespace(llm_provider="nonsense", ollama_base_url="http://x", ollama_model="m")
    assert isinstance(build_llm(s, _Log()), OllamaClient)
