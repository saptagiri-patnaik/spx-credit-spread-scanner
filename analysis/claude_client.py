"""Claude-backed scorer, interface-compatible with OllamaClient.

Same `generate_json(prompt, system)` contract, so sentiment.py, the aggregator
and the eval harness are unchanged -- which is what makes a provider swap
measurable: run the eval set against each and compare on the same labels.

Uses structured outputs rather than "please return JSON". The schema is
enforced server-side, so the silent parse failures the Ollama path swallows
should not occur here.
"""
from __future__ import annotations

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        # Bounds are enforced by _clamp in sentiment.py: the structured-output
        # schema does not support numeric minimum/maximum.
        "direction": {"type": "number", "description": "-1 bearish .. +1 bullish for SPX"},
        "magnitude": {"type": "number", "description": "0..1 expected size of the SPX move"},
        "confidence": {"type": "number", "description": "0..1 confidence in this read"},
        # Asked directly because it is the field the book is actually graded on.
        # It used to be inferred from |direction| > 0.3 or magnitude > 0.5 -- a
        # proxy built from two fields answering different questions, so an item
        # could only register as risky by also claiming a direction or a big
        # move. A shock with no clear direction, which is the exact case a
        # premium seller must not miss, scored zero on the proxy.
        "risk": {
            "type": "number",
            "description": (
                "0..1 chance this RAISES the odds of a LARGE adverse move in SPX "
                "within ~3 weeks, in either direction. Independent of direction."
            ),
        },
        "macro_impact": {"type": "string", "enum": ["risk-on", "risk-off", "neutral"]},
        "catalysts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["direction", "magnitude", "confidence", "risk", "macro_impact", "catalysts"],
    "additionalProperties": False,
}


class ClaudeClient:
    def __init__(
        self,
        model: str,
        logger,
        api_key: str | None = None,
        max_tokens: int = 512,
        timeout: float | None = 30.0,
    ):
        self.model = model
        self.log = logger
        self.max_tokens = max_tokens
        self._client = None
        self._api_key = api_key
        self._import_error: str | None = None
        # Per-cycle usage, reset by the caller (Pipeline.score_new,
        # SynthesisAggregator.aggregate) at the start of each cycle -- the
        # client itself is reused across warm Lambda invocations, so without
        # an explicit reset these would accumulate for the container's whole
        # lifetime instead of describing one cycle.
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        try:
            import anthropic

            # A bare constructor resolves ANTHROPIC_API_KEY from the environment,
            # which is how .env-driven config reaches it.
            #
            # The timeout is set explicitly because the SDK's default read timeout
            # is 600s -- identical to the Lambda budget, so a single hung request
            # ends the cycle with nothing done rather than degrading it. One such
            # timeout is on record (4 Aug 2026, 15:55 PDT); the automatic retry on
            # unchanged code finished in 127s.
            kwargs = {"timeout": timeout} if timeout else {}
            if api_key:
                kwargs["api_key"] = api_key
            self._client = anthropic.Anthropic(**kwargs)
        except ImportError:
            self._import_error = "anthropic package not installed (pip install anthropic)"
        except Exception as exc:  # noqa: BLE001 - missing/!invalid key
            self._import_error = str(exc)

    def reset_usage(self) -> None:
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def available(self) -> bool:
        if self._client is None:
            self.log.warning("Claude client unavailable: %s", self._import_error)
            return False
        return True

    def generate_json(
        self, prompt: str, system: str | None = None, schema: dict | None = None
    ) -> dict | None:
        if self._client is None:
            return None
        import anthropic

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system or "",
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": schema or SCORE_SCHEMA}
                },
            )
        except anthropic.RateLimitError:
            # The SDK already retried with backoff; a miss here is a dropped
            # item, not a crashed cycle.
            self.log.warning("Claude rate limited; skipping item.")
            return None
        except anthropic.APIStatusError as exc:
            self.log.warning("Claude API error %s: %s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError as exc:
            self.log.warning("Claude connection error: %s", exc)
            return None

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.requests += 1
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0

        if response.stop_reason == "refusal":
            self.log.warning("Claude declined to score an item.")
            return None
        if response.stop_reason == "max_tokens":
            self.log.warning("Claude response truncated; raising max_tokens would help.")
            return None

        import json

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            self.log.warning("Claude returned unparseable JSON: %s", exc)
            return None


def build_llm(settings, logger):
    """Pick a scorer from config. Falls back to Ollama on an unknown provider."""
    from .llm import OllamaClient

    provider = getattr(settings, "llm_provider", "ollama")
    if provider == "anthropic":
        return ClaudeClient(
            model=getattr(settings, "anthropic_model", "claude-haiku-4-5"),
            logger=logger,
            api_key=getattr(settings, "anthropic_api_key", None),
            max_tokens=getattr(settings, "anthropic_max_tokens", 512),
            timeout=getattr(settings, "anthropic_timeout_seconds", 30.0),
        )
    return OllamaClient(settings.ollama_base_url, settings.ollama_model, logger)
