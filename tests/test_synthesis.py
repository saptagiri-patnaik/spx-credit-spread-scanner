"""Tests for story clustering and the tier-2 synthesis aggregator."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from analysis.aggregator import Aggregator
from analysis.synthesis import SynthesisAggregator, build_aggregator, story_key


class _Log:
    def __init__(self):
        self.warnings = []

    def info(self, *a, **k):
        pass

    def warning(self, msg, *a):
        self.warnings.append(msg % a if a else msg)


class _LLM:
    def __init__(self, response=None):
        self.response = response
        self.prompt = None
        self.schema = None

    def generate_json(self, prompt, system=None, schema=None):
        self.prompt, self.schema = prompt, schema
        return self.response


def _settings(**kw):
    base = dict(horizon_days=6, dte_max=25, macro_weight=0.5, confidence_dir_scale=0.6,
                event_risk_confidence_factor=0.92, synthesis_max_stories=40)
    base.update(kw)
    return SimpleNamespace(**base)


def _item(title, source_type="news", direction=-0.3, confidence=0.8):
    now = dt.datetime.now(dt.timezone.utc)
    return (
        SimpleNamespace(title=title, content=title, source_type=source_type,
                        published_at=now, engagement=0.0),
        SimpleNamespace(direction=direction, confidence=confidence, magnitude=0.3),
    )


def _ok(**over):
    body = {"direction": -0.35, "confidence": 0.72,
            "downside_risk": 0.6, "upside_risk": 0.3,
            "rationale": "Macro turning.", "key_drivers": ["CPI", "Fed"]}
    body.update(over)
    return body


# ---------------------------------------------------------------- clustering --
def test_identical_headlines_collapse_to_one_story():
    assert story_key("Fed Holds Rates Steady Amid Inflation") == \
           story_key("Fed Holds Rates Steady Amid Inflation")


def test_stopwords_do_not_separate_stories():
    assert story_key("The Fed holds rates steady") == story_key("Fed holds rates steady")


def test_different_headlines_are_different_stories():
    assert story_key("Fed holds rates steady") != story_key("Oil prices plunge on supply glut")


def test_repeated_coverage_becomes_one_story_with_a_count():
    items = [_item("Fed holds rates steady after inflation report") for _ in range(8)]
    stories, _ = SynthesisAggregator(_settings(), _Log(), _LLM()).cluster(items)
    assert len(stories) == 1
    assert stories[0]["count"] == 8


def test_macro_stories_outrank_equal_weight_news():
    items = [_item("Federal Reserve signals policy shift", source_type="macro")]
    items += [_item("Widget maker beats earnings estimates", source_type="news")]
    stories, _ = SynthesisAggregator(_settings(), _Log(), _LLM()).cluster(items)
    assert "Federal" in stories[0]["title"]


# ---------------------------------------------------------------- prompting --
def test_prompt_carries_counts_and_first_pass_scores():
    agg = SynthesisAggregator(_settings(), _Log(), _LLM(_ok()))
    agg.aggregate([_item("Fed holds rates steady after inflation report")] * 3,
                  {"trend_score": 0.1, "vix": 18.0}, [])
    prompt = agg.llm.prompt
    assert "3 source(s)" in prompt
    assert "VIX: 18.0" in prompt


def test_missing_market_data_is_stated_not_faked():
    agg = SynthesisAggregator(_settings(), _Log(), _LLM(_ok()))
    agg.aggregate([_item("Fed holds rates steady")], {"trend_score": None, "vix": None}, [])
    assert "unavailable" in agg.llm.prompt


# --------------------------------------------------------------- aggregate --
def test_model_output_drives_direction_and_label():
    agg = SynthesisAggregator(_settings(), _Log(), _LLM(_ok(direction=-0.35)))
    out = agg.aggregate([_item("Fed holds rates steady")], {}, [])
    assert out["direction"] == -0.35
    assert out["label"] == "DOWN"


def test_neutral_band_is_respected():
    agg = SynthesisAggregator(_settings(), _Log(), _LLM(_ok(direction=-0.04)))
    assert agg.aggregate([_item("Fed holds")], {}, [])["label"] == "NEUTRAL"


def test_both_tails_are_carried_in_market_context():
    agg = SynthesisAggregator(_settings(), _Log(), _LLM(_ok(downside_risk=0.81, upside_risk=0.2)))
    out = agg.aggregate([_item("Fed holds")], {}, [])
    ctx = out["market_context"]
    assert ctx["downside_risk"] == 0.81
    assert ctx["upside_risk"] == 0.2
    assert ctx["tail_risk"] == 0.81          # summary = the fatter tail
    assert ctx["aggregator"] == "synthesis"


def test_output_keys_match_the_mean_aggregator():
    # save_prediction does Prediction(**pred): an extra key would break the write.
    items = [_item("Fed holds rates steady")]
    mean = Aggregator(_settings(), _Log()).aggregate(items, {}, [])
    synth = SynthesisAggregator(_settings(), _Log(), _LLM(_ok())).aggregate(items, {}, [])
    assert set(mean) == set(synth)


def test_failed_call_falls_back_to_the_mean_aggregator():
    log = _Log()
    agg = SynthesisAggregator(_settings(), log, _LLM(None))
    out = agg.aggregate([_item("Fed holds")], {}, [])
    assert out["market_context"].get("aggregator") != "synthesis"
    assert any("falling back" in w for w in log.warnings)


def test_unusable_numbers_fall_back():
    log = _Log()
    agg = SynthesisAggregator(_settings(), log, _LLM(_ok(direction="not a number")))
    agg.aggregate([_item("Fed holds")], {}, [])
    assert any("unusable" in w for w in log.warnings)


def test_no_items_returns_the_baseline():
    agg = SynthesisAggregator(_settings(), _Log(), _LLM(_ok()))
    assert agg.aggregate([], {}, [])["num_new_items"] == 0


def test_headlines_differing_only_by_a_number_are_one_story():
    # Digits are stripped from the key, so "CPI rises 3.1%" and "CPI rises 4.2%"
    # cluster together. That is intended -- they are the same event.
    a = story_key("Consumer prices climbed 3.1 percent last month")
    b = story_key("Consumer prices climbed 4.2 percent last month")
    assert a == b


def test_story_cap_is_respected():
    subjects = ["inflation", "employment", "housing", "manufacturing", "banking",
                "shipping", "semiconductors", "utilities", "retail", "insurance",
                "airlines", "biotech", "mining", "telecom", "agriculture"]
    items = [_item(f"Regulators publish fresh {s} guidance") for s in subjects]
    agg = SynthesisAggregator(_settings(synthesis_max_stories=10), _Log(), _LLM(_ok()))
    out = agg.aggregate(items, {}, [])
    assert out["market_context"]["stories_considered"] == 10
    assert out["market_context"]["stories_total"] == len(subjects)


# --------------------------------------------------------------- selection --
def test_build_aggregator_defaults_to_mean():
    assert isinstance(build_aggregator(_settings(), _Log(), None), Aggregator)


def test_build_aggregator_selects_synthesis():
    s = _settings(aggregator_mode="synthesis")
    assert isinstance(build_aggregator(s, _Log(), _LLM()), SynthesisAggregator)


def test_synthesis_without_a_client_falls_back_to_mean():
    s = _settings(aggregator_mode="synthesis")
    assert isinstance(build_aggregator(s, _Log(), None), Aggregator)


# ------------------------------------------------------------ chatter split --
def test_untitled_social_posts_do_not_become_stories():
    # Every social post has unique text; keying on it produced one story per
    # item (6185 items -> 5265 stories) and defeated de-duplication.
    items = [(SimpleNamespace(title=None, content=f"$SPY thoughts number {i} here today",
                              source_type="social",
                              published_at=dt.datetime.now(dt.timezone.utc), engagement=0.0),
              SimpleNamespace(direction=-0.2, confidence=0.6, magnitude=0.2))
             for i in range(50)]
    stories, chatter = SynthesisAggregator(_settings(), _Log(), _LLM()).cluster(items)
    assert stories == []
    assert chatter["count"] == 50


def test_chatter_is_summarised_into_the_prompt():
    items = [_item("Fed holds rates steady after inflation report")]
    items += [(SimpleNamespace(title=None, content=f"chatter {i}", source_type="social",
                               published_at=dt.datetime.now(dt.timezone.utc), engagement=0.0),
               SimpleNamespace(direction=0.4, confidence=0.5, magnitude=0.2))
              for i in range(20)]
    agg = SynthesisAggregator(_settings(), _Log(), _LLM(_ok()))
    out = agg.aggregate(items, {}, [])
    assert "20 posts" in agg.llm.prompt
    assert out["market_context"]["chatter_posts"] == 20


def test_titled_items_still_cluster_normally():
    items = [_item("Fed holds rates steady after inflation report") for _ in range(6)]
    stories, chatter = SynthesisAggregator(_settings(), _Log(), _LLM()).cluster(items)
    assert len(stories) == 1 and stories[0]["count"] == 6
    assert chatter["count"] == 0
