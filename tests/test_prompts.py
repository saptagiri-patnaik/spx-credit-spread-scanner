"""Tests for the scoring-prompt registry."""
from __future__ import annotations

from types import SimpleNamespace

from analysis.prompts import PROMPTS, get_prompt
from analysis.sentiment import SentimentAnalyzer


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _CapturingLLM:
    """Records the system prompt it was called with."""

    def __init__(self):
        self.system = None

    def generate_json(self, prompt, system=None):
        self.system = system
        return {"direction": 0.0, "magnitude": 0.0, "confidence": 1.0,
                "macro_impact": "neutral", "catalysts": []}


def _item():
    return SimpleNamespace(content="some text", title="t", source_type="news", category="markets")


def test_every_variant_accepts_the_shared_fields():
    for name, (system, template) in PROMPTS.items():
        assert system.strip(), f"{name} has an empty system prompt"
        rendered = template.format(stype="news", category="markets", title="T", text="body")
        assert "body" in rendered, f"{name} dropped the text field"
        for key in ("direction", "magnitude", "confidence", "macro_impact", "catalysts"):
            assert key in rendered, f"{name} does not request {key}"


def test_unknown_name_falls_back_to_current():
    assert get_prompt("does-not-exist") == PROMPTS["current"]
    assert get_prompt(None) == PROMPTS["current"]


def test_analyzer_uses_the_configured_variant():
    llm = _CapturingLLM()
    analyzer = SentimentAnalyzer(
        llm, _Log(), SimpleNamespace(llm_chunk_chars=1000, llm_max_chunks=1, scoring_prompt="theta")
    )
    analyzer.score(_item())
    assert llm.system == PROMPTS["theta"][0]


def test_analyzer_defaults_to_current_when_unset():
    llm = _CapturingLLM()
    analyzer = SentimentAnalyzer(
        llm, _Log(), SimpleNamespace(llm_chunk_chars=1000, llm_max_chunks=1)
    )
    analyzer.score(_item())
    assert llm.system == PROMPTS["current"][0]
