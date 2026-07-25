"""Tests for chunked sentiment scoring and combination."""
from __future__ import annotations

from types import SimpleNamespace

from analysis.sentiment import SentimentAnalyzer


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _FakeLLM:
    """Returns a queued response per call so we can simulate multiple chunks."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate_json(self, prompt, system=None):
        self.calls += 1
        return self.responses.pop(0) if self.responses else None


def _item(content):
    return SimpleNamespace(content=content, title="t", source_type="news", category="markets")


def _settings(chunk_chars=10, max_chunks=3):
    return SimpleNamespace(llm_chunk_chars=chunk_chars, llm_max_chunks=max_chunks)


def test_single_chunk_passes_through():
    llm = _FakeLLM(
        [{"direction": 0.6, "magnitude": 0.3, "confidence": 0.8, "macro_impact": "risk-on", "catalysts": ["cpi"]}]
    )
    analyzer = SentimentAnalyzer(llm, _Log(), _settings(chunk_chars=1000))
    result = analyzer.score(_item("short text"))
    assert result["direction"] == 0.6
    assert result["catalysts"]["items"] == ["cpi"]
    assert llm.calls == 1


def test_long_text_is_chunked_and_combined():
    # 30 chars with chunk size 10 and max 3 -> exactly 3 chunks / 3 LLM calls.
    llm = _FakeLLM(
        [
            {"direction": 0.9, "magnitude": 0.4, "confidence": 1.0, "macro_impact": "risk-on", "catalysts": ["a"]},
            {"direction": -0.3, "magnitude": 0.2, "confidence": 0.0, "macro_impact": "neutral", "catalysts": ["b"]},
            {"direction": 0.5, "magnitude": 0.6, "confidence": 1.0, "macro_impact": "risk-on", "catalysts": ["a", "c"]},
        ]
    )
    analyzer = SentimentAnalyzer(llm, _Log(), _settings(chunk_chars=10, max_chunks=3))
    result = analyzer.score(_item("x" * 30))
    assert llm.calls == 3
    # confidence-weighted direction = (0.9*1 + -0.3*0 + 0.5*1) / (1+0+1) = 0.7
    assert result["direction"] == 0.7
    assert result["macro_impact"] == "risk-on"  # highest confidence weight
    assert result["catalysts"]["items"] == ["a", "b", "c"]  # unioned, deduped, order-preserved


def test_max_chunks_caps_llm_calls():
    llm = _FakeLLM(
        [{"direction": 0.1, "magnitude": 0.1, "confidence": 0.5, "macro_impact": "neutral", "catalysts": []}]
        * 10
    )
    analyzer = SentimentAnalyzer(llm, _Log(), _settings(chunk_chars=5, max_chunks=2))
    analyzer.score(_item("y" * 100))  # would be 20 chunks, capped at 2
    assert llm.calls == 2


def test_empty_text_returns_none():
    analyzer = SentimentAnalyzer(_FakeLLM([]), _Log(), _settings())
    assert analyzer.score(_item("   ")) is None
