"""Thin client for a local Ollama server (JSON-mode generation)."""
from __future__ import annotations

import json

import requests


class OllamaClient:
    def __init__(self, base_url: str, model: str, logger, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.log = logger
        self.timeout = timeout

    def available(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=10)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def generate_json(self, prompt: str, system: str | None = None) -> dict | None:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }
        if system:
            payload["system"] = system
        try:
            resp = requests.post(
                f"{self.base_url}/api/generate", json=payload, timeout=self.timeout
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()
            return json.loads(raw)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            self.log.warning("Ollama generate failed: %s", exc)
            return None
