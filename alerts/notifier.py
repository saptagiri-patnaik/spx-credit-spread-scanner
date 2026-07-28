"""Alert delivery: console/log always, plus optional Telegram / Discord."""
from __future__ import annotations

import requests

_DISCORD_LIMIT = 2000  # hard cap Discord enforces on webhook `content`
_FENCE = "```"


class Notifier:
    def __init__(self, settings, logger):
        self.s = settings
        self.log = logger

    def send(self, text: str, external: bool = True, trade: bool = False) -> None:
        """Always log; fan out to Telegram/Discord when `external` is True.

        `trade` routes to the dedicated trade-signal webhook when one is
        configured, so actionable alerts land in a channel you can afford to
        have notifications on for, separate from routine outlook noise.
        """
        self.log.info("ALERT:\n%s", text)
        if not external:
            return
        self._telegram(text)
        self._discord(text, trade=trade)

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

    def _discord(self, text: str, trade: bool = False) -> None:
        url = self.s.discord_webhook_url
        channel = "routine"
        if trade:
            trade_url = getattr(self.s, "discord_trade_webhook_url", None)
            # Fall back to the routine hook rather than dropping a trade signal:
            # a missed actionable alert is worse than one in the wrong channel.
            url = trade_url or url
            channel = "trade" if trade_url else "trade->routine"
        if not url:
            return
        try:
            resp = requests.post(url, json={"content": self._fence(text)}, timeout=15)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Discord send failed (%s): %s", channel, exc)

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
