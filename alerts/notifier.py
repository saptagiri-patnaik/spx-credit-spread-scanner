"""Alert delivery: console/log always, plus optional Telegram / Discord."""
from __future__ import annotations

import requests

_DISCORD_LIMIT = 2000  # hard cap Discord enforces on webhook `content`
_FENCE = "```"


class Notifier:
    def __init__(self, settings, logger):
        self.s = settings
        self.log = logger

    def send(self, text: str, external: bool = True) -> None:
        """Always log; only fan out to Telegram/Discord when `external` is True."""
        self.log.info("ALERT:\n%s", text)
        if not external:
            return
        self._telegram(text)
        self._discord(text)

    def _telegram(self, text: str) -> None:
        if not (self.s.telegram_bot_token and self.s.telegram_chat_id):
            return
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage",
                json={"chat_id": self.s.telegram_chat_id, "text": text[:4000]},
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Telegram send failed: %s", exc)

    def _discord(self, text: str) -> None:
        if not self.s.discord_webhook_url:
            return
        try:
            resp = requests.post(
                self.s.discord_webhook_url,
                json={"content": self._fence(text)},
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Discord send failed: %s", exc)

    @staticmethod
    def _fence(text: str) -> str:
        """Wrap in a code block so Discord keeps the alert's column alignment.

        Discord renders `content` proportionally, which ragged-edges the rules
        and the `Direction :` label column. Truncation happens inside the fence
        so an over-long alert can never emit an unclosed block.
        """
        budget = _DISCORD_LIMIT - 2 * len(_FENCE) - 2  # fences + their newlines
        body = text if len(text) <= budget else text[: budget - 1] + "…"
        return f"{_FENCE}\n{body}\n{_FENCE}"
