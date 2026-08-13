"""Schwab Trader API client: reads externally-refreshed token, quotes, chains."""
from __future__ import annotations

import datetime as dt
import math

import requests
from sqlalchemy import create_engine, text

MARKETDATA_BASE = "https://api.schwabapi.com/marketdata/v1"

# Trading days in a year: converts a daily return stdev to the annualised figure
# that option implied vol is quoted in, so the two are comparable.
TRADING_DAYS = 252

# Schwab returns sentinel values (-999.0, 0.0) for greeks and vol on illiquid or
# stale contracts. Anything outside this band is a placeholder, not a quote.
_IV_MIN_PCT = 0.5
_IV_MAX_PCT = 300.0

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

    def market_hours(self, market: str, date: dt.date) -> dict | None:
        """Schwab's own session open/close/early-close data for `market`.

        See market/session_calendar.py for what this feeds and why: the actual
        Cboe SPX index-options session, not a generic weekday rule.

        Plural /markets with `market` as a query param, not singular
        /markets/{market} as a path segment -- confirmed against a live
        account on 13 Aug 2026. The path form 400s on this account with
        "markets param cannot be null or empty", every single time,
        regardless of date; the previous version of this method had never
        been exercised against a real response and enshrined that shape in
        its own test's mock. A caller relying on the documented fail-closed
        behavior would have seen every session lookup come back UNCERTAIN
        forever, silently, since a 400 degrades to None exactly like a real
        outage does.
        """
        headers = self._headers()
        if not headers:
            return None
        try:
            resp = requests.get(
                f"{MARKETDATA_BASE}/markets",
                headers=headers,
                params={"markets": market, "date": date.isoformat()},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Schwab market hours fail (%s, %s): %s", market, date, exc)
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

    def daily_closes(self, symbol: str, months: int = 3) -> list[float]:
        """Daily closes, oldest first. One fetch feeds both trend and realised vol.

        `months` is 3 rather than 1 because a 20-day realised-vol window needs 21
        closes and a single month of calendar days yields only ~21 trading days --
        no headroom for holidays, and none at all if the window is widened.
        """
        headers = self._headers()
        if not headers:
            return []
        try:
            resp = requests.get(
                f"{MARKETDATA_BASE}/pricehistory",
                headers=headers,
                params={
                    "symbol": symbol,
                    "periodType": "month",
                    "period": str(months),
                    "frequencyType": "daily",
                    "frequency": "1",
                },
                timeout=20,
            )
            resp.raise_for_status()
            candles = resp.json().get("candles", [])
            return [float(c["close"]) for c in candles if c.get("close")]
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Schwab price history fail (%s): %s", symbol, exc)
            return []

    @staticmethod
    def trend_score(closes: list[float]) -> float:
        """~5-day percent change mapped to [-1, 1] (a ~3% move saturates)."""
        if len(closes) < 6 or not closes[-6]:
            return 0.0
        pct = (closes[-1] - closes[-6]) / closes[-6] * 100.0
        return max(-1.0, min(1.0, pct / 3.0))

    @staticmethod
    def realized_vol(closes: list[float], window: int = 20) -> float | None:
        """Annualised stdev of daily log returns, as a decimal (0.14 = 14%).

        Quoted the same way implied vol is, so the two divide into a ratio that
        means something: above 1 the market is charging more for a move than the
        index has recently delivered, which is the premium seller's entire edge.
        """
        if window < 2 or len(closes) < window + 1:
            return None
        returns = []
        for i in range(len(closes) - window, len(closes)):
            prev, cur = closes[i - 1], closes[i]
            if prev <= 0 or cur <= 0:
                return None
            returns.append(math.log(cur / prev))
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(variance) * math.sqrt(TRADING_DAYS)

    @staticmethod
    def atm_iv(chain: dict | None) -> float | None:
        """Implied vol at the money, as a decimal, averaged over calls and puts.

        Averaging every contract that sits at the minimum distance from spot
        folds together the call and the put at that strike -- and, where the
        chain spans several expiries in the DTE band, across those too. That is
        deliberate smoothing: one strike's quote on one expiry is noisy, and this
        number is only ever read as a regime level.
        """
        if not chain:
            return None
        try:
            price = float(chain.get("underlyingPrice") or 0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None

        best_distance: float | None = None
        vols: list[float] = []
        for map_key in ("putExpDateMap", "callExpDateMap"):
            for strikes in (chain.get(map_key) or {}).values():
                for options in strikes.values():
                    for opt in options:
                        try:
                            strike = float(opt.get("strikePrice"))
                            vol = float(opt.get("volatility"))
                        except (TypeError, ValueError):
                            continue
                        if not _IV_MIN_PCT <= vol <= _IV_MAX_PCT:
                            continue
                        distance = abs(strike - price)
                        if best_distance is None or distance < best_distance:
                            best_distance, vols = distance, [vol]
                        elif distance == best_distance:
                            vols.append(vol)
        if not vols:
            return None
        return sum(vols) / len(vols) / 100.0  # Schwab quotes vol in percent

    def market_context(self, underlying: str, chain: dict | None = None) -> dict:
        """Price/vol regime for the synthesis prompt and the side gates.

        `chain` is passed in rather than fetched: run_once already pulls one for
        the scan, and a second call would cost a request for data in hand.
        """
        ctx: dict = {"trend_score": 0.0}
        symbol = self.symbol(underlying)
        try:
            closes = self.daily_closes(symbol)
            ctx["trend_score"] = self.trend_score(closes)
            ctx["price"] = self._last_price(symbol)
            ctx["vix"] = self._last_price("$VIX")
            ctx["vix3m"] = self._last_price("$VIX3M")

            # Above 1.0 the near month is bid over the three month: backwardation,
            # the regime where premium selling has historically been punished.
            if ctx["vix"] and ctx["vix3m"]:
                ctx["vix_term_structure"] = round(ctx["vix"] / ctx["vix3m"], 3)

            window = int(getattr(self.s, "realized_vol_window", 20))
            realized = self.realized_vol(closes, window)
            implied = self.atm_iv(chain)
            if realized is not None:
                ctx["realized_vol"] = round(realized, 4)
            if implied is not None:
                ctx["atm_iv"] = round(implied, 4)
            if realized and implied:
                ctx["iv_rv_ratio"] = round(implied / realized, 3)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("market_context failed: %s", exc)
        return ctx
