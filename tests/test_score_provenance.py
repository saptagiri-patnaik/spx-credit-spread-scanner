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

    def save_score(self, item_id, score, model):
        self.saved.append({"item_id": item_id, "model": model})


def _pipeline(llm, repo):
    p = Pipeline.__new__(Pipeline)
    p.llm, p.repo, p.log = llm, repo, _Log()
    p.s = SimpleNamespace(ollama_model="llama3.1:8b")
    p.analyzer = SimpleNamespace(score=lambda item: {"direction": 0.1})
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
