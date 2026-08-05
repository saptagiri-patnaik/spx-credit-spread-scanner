"""The scorer that produced a score must be recorded as such.

Mislabelled provenance is silent and corrosive: it makes it impossible to tell
which scorer produced which row, which is the entire basis for comparing them.
"""
from __future__ import annotations

from types import SimpleNamespace

from main import Pipeline


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _LLM:
    def __init__(self, model):
        self.model = model

    def available(self):
        return True


class _Repo:
    def __init__(self, items):
        self.items = items
        self.saved = []

    def fetch_unscored(self, limit=80):
        return self.items

    def save_score(self, item_id, score, model, prompt=None):
        self.saved.append({"item_id": item_id, "model": model, "prompt": prompt})


def _pipeline(llm, repo, prompt_name="current"):
    p = Pipeline.__new__(Pipeline)
    p.llm, p.repo, p.log = llm, repo, _Log()
    p.s = SimpleNamespace(ollama_model="llama3.1:8b")
    p.analyzer = SimpleNamespace(
        score=lambda item: {"direction": 0.1}, prompt_name=prompt_name
    )
    return p


def _items(n=3):
    return [SimpleNamespace(id=i) for i in range(1, n + 1)]


def test_claude_scores_are_recorded_against_the_claude_model():
    repo = _Repo(_items())
    _pipeline(_LLM("claude-haiku-4-5"), repo).score_new()
    assert {row["model"] for row in repo.saved} == {"claude-haiku-4-5"}


def test_ollama_scores_are_recorded_against_the_ollama_model():
    repo = _Repo(_items())
    _pipeline(_LLM("llama3.1:8b"), repo).score_new()
    assert {row["model"] for row in repo.saved} == {"llama3.1:8b"}


def test_provenance_does_not_fall_back_to_the_ollama_setting():
    # The bug this guards: the model name came from settings.ollama_model
    # regardless of which client actually ran.
    repo = _Repo(_items())
    _pipeline(_LLM("claude-sonnet-5"), repo).score_new()
    assert "llama3.1:8b" not in {row["model"] for row in repo.saved}


def test_a_client_without_a_model_attribute_is_marked_unknown():
    repo = _Repo(_items())
    _pipeline(SimpleNamespace(available=lambda: True), repo).score_new()
    assert {row["model"] for row in repo.saved} == {"unknown"}


# ------------------------------------------------------ prompt attribution --
def test_the_prompt_is_recorded_alongside_the_model():
    # The model was already recorded; the prompt is at least as large a lever and
    # was not, so a production A/B could not be attributed to either arm.
    repo = _Repo(_items())
    _pipeline(_LLM("claude-haiku-4-5"), repo, prompt_name="theta").score_new()
    assert {row["prompt"] for row in repo.saved} == {"theta"}


def test_a_scorer_without_a_prompt_name_records_none():
    repo = _Repo(_items())
    p = _pipeline(_LLM("claude-haiku-4-5"), repo)
    p.analyzer = SimpleNamespace(score=lambda item: {"direction": 0.1})
    p.score_new()
    assert {row["prompt"] for row in repo.saved} == {None}


def test_attribution_uses_the_prompt_that_ran_not_the_one_configured():
    # get_prompt() falls back to the default for an unknown name, so recording
    # the configured value would label a whole run with a prompt that never ran.
    from analysis.prompts import DEFAULT, resolve_prompt_name

    assert resolve_prompt_name("theta") == "theta"
    assert resolve_prompt_name("typo-not-a-prompt") == DEFAULT
    assert resolve_prompt_name(None) == DEFAULT
