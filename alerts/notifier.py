"""Alert delivery: console/log always, plus optional Telegram / Discord."""
from __future__ import annotations

import requests


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
            requests.post(
                f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage",
                json={"chat_id": self.s.telegram_chat_id, "text": text[:4000]},
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Telegram send failed: %s", exc)

    def _discord(self, text: str) -> None:
        if not self.s.discord_webhook_url:
            return
        try:
            requests.post(
                self.s.discord_webhook_url,
                json={"content": text[:1900]},
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Discord send failed: %s", exc)
