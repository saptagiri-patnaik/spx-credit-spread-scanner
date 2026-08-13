"""The saved winning suggestion must reach the model paper position."""
from __future__ import annotations

from types import SimpleNamespace

from db.models import Base, SpreadSuggestion
from db.repository import Repository
from main import Pipeline

from ._session_fixtures import FakeCalendar


def _prediction() -> dict:
    return {
        "horizon_days": 6,
        "direction": 0.2,
        "label": "UP",
        "confidence": 0.5,
        "sentiment_score": 0.1,
        "macro_score": 0.1,
        "event_risk": False,
        "market_context": {"price": 5000.0},
        "num_new_items": 1,
        "rationale": "test",
    }


def _spread() -> dict:
    return {
        "underlying": "SPX",
        "strategy": "PUT_CREDIT_SPREAD",
        "short_strike": 4900.0,
        "long_strike": 4895.0,
        "call_short_strike": None,
        "call_long_strike": None,
        "expiration": "2026-09-04",
        "dte": 22,
        "width": 5.0,
        "credit": 1.0,
        "max_loss": 4.0,
        "pop": 0.8,
        "short_delta": 0.2,
        "expected_move": 100.0,
        "ror": 0.25,
        "edge": 0.06,
        "premium_edge": 0.0,
        "pop_real": 0.75,
        "premium_edge_measured": -0.05,
        "buffer": 1.0,
        "breakeven": 4899.0,
        "notes": None,
    }


def test_save_prediction_returns_the_saved_suggestion_id():
    repo = Repository("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(repo.engine)
    source_spread = _spread()

    prediction_id, saved_spreads = repo.save_prediction(_prediction(), [source_spread])
    saved_source, spread_id = saved_spreads[0]

    assert prediction_id > 0
    assert spread_id > 0
    assert saved_source is source_spread
    with repo.session() as session:
        saved = session.get(SpreadSuggestion, spread_id)
        assert saved is not None
        assert saved.prediction_id == prediction_id


def test_save_prediction_pairs_multiple_sources_with_their_own_rows():
    repo = Repository("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(repo.engine)
    first = _spread()
    second = {**_spread(), "short_strike": 4800.0, "long_strike": 4795.0}

    _, saved_spreads = repo.save_prediction(_prediction(), [first, second])

    assert [source for source, _ in saved_spreads] == [first, second]
    with repo.session() as session:
        persisted = [
            session.get(SpreadSuggestion, spread_id).short_strike
            for _, spread_id in saved_spreads
        ]
    assert persisted == [4900.0, 4800.0]


class _Paper:
    def __init__(self):
        self.opened_with = None

    def expire_stale(self, now=None):
        pass

    def manage(self, chain, session_state=None):
        pass

    def maybe_open(self, scan, chain, spread_id, session_state=None):
        self.opened_with = spread_id

    def maybe_open_shadow(self, scan, chain, spread_id, session_state=None):
        pass

    def maybe_open_baseline(self, chain, session_state, now=None):
        pass

    def baseline_is_due(self, session_state, now=None):
        return False


def test_pipeline_threads_saved_suggestion_id_into_paper_entry():
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.dry_run = False
    pipeline.s = SimpleNamespace(
        calibration_mode="off",
        schedule_mode="continuous",
        lookback_days=7,
        dte_max=25,
        dte_min=20,
        underlying="SPX",
        alert_only_on_trade=True,
    )
    pipeline.collect_new = lambda: 1
    pipeline.score_new = lambda: None
    def save_prediction(prediction, spreads):
        # The selected winner is deliberately second. Linking by `[0]` would
        # silently attach the paper result to the counterfactual decoy.
        decoy = {**spreads[0], "short_strike": 4800.0, "long_strike": 4795.0}
        return 41, [(decoy, 77), (spreads[0], 99)]

    pipeline.repo = SimpleNamespace(
        recent_scores=lambda since: [],
        fetch_events=lambda start, end: [],
        save_prediction=save_prediction,
    )
    pipeline.schwab = SimpleNamespace(
        symbol=lambda underlying: underlying,
        option_chain=lambda *args: {"underlyingPrice": 5000.0},
        market_context=lambda *args: {"price": 5000.0},
    )
    pipeline.aggregator = SimpleNamespace(aggregate=lambda *args: _prediction())
    pipeline.strategy = SimpleNamespace(
        scan=lambda *args, **kwargs: {"best": _spread(), "recommended": True}
    )
    pipeline.paper = _Paper()
    pipeline.calendar = FakeCalendar()
    pipeline.notifier = SimpleNamespace(send=lambda *args, **kwargs: None)
    pipeline._iv_rank = lambda value: None
    pipeline._log_regime = lambda context: None
    pipeline._calibrate = lambda prediction: prediction
    pipeline._marking_chain = lambda chain: chain
    pipeline._claim_trade_alert = lambda best: False
    pipeline._format = lambda prediction, scan: "test"

    pipeline.run_once()

    assert pipeline.paper.opened_with == 99
