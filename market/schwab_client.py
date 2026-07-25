"""Schwab Trader API client: reads externally-refreshed token, quotes, chains."""
from __future__ import annotations

import datetime as dt

import requests
from sqlalchemy import create_engine, text

MARKETDATA_BASE = "https://api.schwabapi.com/marketdata/v1"

# Map friendly underlyings to Schwab index/ETF symbols.
SYMBOL_MAP = {"XSP": "$XSP", "SPX": "$SPX", "SPY": "SPY"}


class SchwabClient:
    def __init__(self, settings, logger):
        self.s = settings
        self.log = logger
        self._token: str | None = None
        self._fetched_at = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        self._engine = None

    # --- auth: read the externally-refreshed access token from app_data ------
    def _token_engine(self):
        if self._engine is None:
            url = self.s.schwab_token_db_url or self.s.database_url
            self._engine = create_engine(url, pool_pre_ping=True)
        return self._engine

    def _load_token(self) -> str | None:
        """SELECT value, timestamp FROM app_data WHERE key = <schwab_token_key>."""
        try:
            with self._token_engine().connect() as conn:
                row = conn.execute(
                    text("SELECT value, timestamp FROM app_data WHERE key = :k"),
                    {"k": self.s.schwab_token_key},
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Schwab token DB read failed: %s", exc)
            return None
        if not row:
            self.log.warning(
                "Schwab token key '%s' not found in app_data", self.s.schwab_token_key
            )
            return None
        value, ts = row[0], row[1]
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
            if age > self.s.schwab_token_max_age_seconds:
                self.log.warning(
                    "Schwab access token is stale (%.0fs old) - is the refresher running?",
                    age,
                )
                return None
        return value

    def _headers(self) -> dict | None:
        age = (dt.datetime.now(dt.timezone.utc) - self._fetched_at).total_seconds()
        if not self._token or age >= self.s.schwab_token_cache_seconds:
            token = self._load_token()
            if not token:
                return None
            self._token = token
            self._fetched_at = dt.datetime.now(dt.timezone.utc)
        return {"Authorization": f"Bearer {self._token}"}

    def available(self) -> bool:
        return self._headers() is not None

    # --- market data ------------------------------------------------------
    def symbol(self, underlying: str) -> str:
        return SYMBOL_MAP.get(underlying, underlying)

    def quote(self, symbol: str) -> dict | None:
        headers = self._headers()
        if not headers:
            return None
        try:
            resp = requests.get(
                f"{MARKETDATA_BASE}/{symbol}/quotes", headers=headers, timeout=20
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Schwab quote fail (%s): %s", symbol, exc)
            return None

    def option_chain(self, symbol: str, from_date: dt.date, to_date: dt.date) -> dict | None:
        headers = self._headers()
        if not headers:
            return None
        try:
            resp = requests.get(
                f"{MARKETDATA_BASE}/chains",
                headers=headers,
                params={
                    "symbol": symbol,
                    "contractType": "ALL",
                    "strategy": "SINGLE",
                    "fromDate": from_date.isoformat(),
                    "toDate": to_date.isoformat(),
                    "includeUnderlyingQuote": True,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Schwab option chain fail (%s): %s", symbol, exc)
            return None

    def _last_price(self, symbol: str) -> float | None:
        data = self.quote(symbol)
        if not data:
            return None
        try:
            node = next(iter(data.values()))
            quote = node.get("quote", {})
            return quote.get("lastPrice") or quote.get("mark")
        except (StopIteration, AttributeError):
            return None

    def _trend_score(self, symbol: str) -> float:
        """~5-day percent change mapped to [-1, 1] (a ~3% move saturates)."""
        headers = self._headers()
        if not headers:
            return 0.0
        try:
            resp = requests.get(
                f"{MARKETDATA_BASE}/pricehistory",
                headers=headers,
                params={
                    "symbol": symbol,
                    "periodType": "month",
                    "period": "1",
                    "frequencyType": "daily",
                    "frequency": "1",
                },
                timeout=20,
            )
            resp.raise_for_status()
            candles = resp.json().get("candles", [])
            if len(candles) < 6:
                return 0.0
            closes = [c["close"] for c in candles]
            pct = (closes[-1] - closes[-6]) / closes[-6] * 100.0
            return max(-1.0, min(1.0, pct / 3.0))
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Schwab trend fail (%s): %s", symbol, exc)
            return 0.0

    def market_context(self, underlying: str) -> dict:
        ctx: dict = {"trend_score": 0.0}
        symbol = self.symbol(underlying)
        try:
            ctx["trend_score"] = self._trend_score(symbol)
            ctx["price"] = self._last_price(symbol)
            ctx["vix"] = self._last_price("$VIX")
        except Exception as exc:  # noqa: BLE001
            self.log.warning("market_context failed: %s", exc)
        return ctx
