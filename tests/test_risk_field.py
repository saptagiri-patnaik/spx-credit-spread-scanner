"""The scorer reports risk directly instead of it being inferred from a proxy.

Risk is weighted 8x in the eval cost function, and it used to be derived from
|direction| > 0.3 or magnitude > 0.5 -- two fields answering different questions.
An item could only register as risky by also committing to a direction or a large
expected move, so a shock with no clear direction, the exact case a premium
seller must not miss, scored zero.
"""
from __future__ import annotations

from types import SimpleNamespace

from analysis.claude_client import SCORE_SCHEMA
from analysis.prompts import PROMPTS
from analysis.sentiment import SentimentAnalyzer
from tools.evalset import _flags_risk


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate_json(self, prompt, system=None):
        return self.responses.pop(0) if self.responses else None


def _item(content="body"):
    return SimpleNamespace(content=content, title="t", source_type="news", category="markets")


def _analyzer(responses, chunk_chars=1000):
    return SentimentAnalyzer(
        _FakeLLM(responses), _Log(),
        SimpleNamespace(llm_chunk_chars=chunk_chars, llm_max_chunks=3),
    )


def _out(**kw):
    base = {"direction": 0.0, "magnitude": 0.0, "confidence": 0.8,
            "macro_impact": "neutral", "catalysts": []}
    base.update(kw)
    return base


# ----------------------------------------------------------------- schema --
def test_risk_is_a_required_schema_field():
    assert "risk" in SCORE_SCHEMA["properties"]
    assert "risk" in SCORE_SCHEMA["required"]


def test_every_prompt_variant_asks_for_risk():
    # Parsing is shared, so a variant that omits the key silently falls back.
    for name, (_system, template) in PROMPTS.items():
        assert "risk:" in template, f"{name} does not ask for risk"


# ------------------------------------------------------------- extraction --
def test_explicit_risk_is_used_verbatim():
    score = _analyzer([_out(risk=0.9)]).score(_item())
    assert score["risk"] == 0.9


def test_risk_is_independent_of_direction():
    # The case the proxy could not express: no directional view, high risk.
    score = _analyzer([_out(direction=0.0, magnitude=0.1, risk=0.8)]).score(_item())
    assert score["risk"] == 0.8


def test_risk_is_clamped():
    assert _analyzer([_out(risk=5)]).score(_item())["risk"] == 1.0
    assert _analyzer([_out(risk=-2)]).score(_item())["risk"] == 0.0


def test_missing_risk_falls_back_to_the_old_proxy():
    # A prompt predating the field must not read as "definitely safe" on the
    # metric weighted 8x.
    assert _analyzer([_out(direction=0.6)]).score(_item())["risk"] == 1.0
    assert _analyzer([_out(magnitude=0.7)]).score(_item())["risk"] == 1.0
    assert _analyzer([_out(direction=0.1, magnitude=0.1)]).score(_item())["risk"] == 0.0


def test_unparseable_risk_falls_back_rather_than_crashing():
    assert _analyzer([_out(risk="high", direction=0.6)]).score(_item())["risk"] == 1.0


# ------------------------------------------------------------ combination --
def test_risk_takes_the_max_across_chunks():
    # One paragraph naming a shock makes the article risky; averaging it against
    # unremarkable chunks is how a warning gets diluted into nothing.
    a = _analyzer(
        [_out(risk=0.0), _out(risk=0.9), _out(risk=0.0)], chunk_chars=4
    )
    assert a.score(_item("aaaabbbbcccc"))["risk"] == 0.9


def test_other_fields_still_average_while_risk_does_not():
    a = _analyzer(
        [_out(direction=0.0, confidence=1.0, risk=0.0),
         _out(direction=1.0, confidence=1.0, risk=1.0)],
        chunk_chars=4,
    )
    score = a.score(_item("aaaabbbb"))
    assert score["risk"] == 1.0
    assert 0.0 < score["direction"] < 1.0


# ---------------------------------------------------------- eval grading --
def test_eval_prefers_the_explicit_field_over_the_proxy():
    # Proxy would say risky (direction 0.6); the scorer's own answer is 0.1.
    assert _flags_risk((0.6, 0.0, 0.8, 0.1)) is False
    # Proxy would say safe; the scorer says risky.
    assert _flags_risk((0.0, 0.0, 0.8, 0.9)) is True


def test_eval_falls_back_to_the_proxy_without_a_risk_field():
    assert _flags_risk((0.6, 0.0, 0.8, None)) is True
    assert _flags_risk((0.0, 0.0, 0.8)) is False
