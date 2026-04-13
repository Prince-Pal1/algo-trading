"""Funding Carry Scheduled Feed (Strategy A live adapter).

The trading engine's main pipeline is candle-driven from Binance WS. Funding
carry is a different beast: it sees 8h-cadence data, not 1h candles. This
feed is a lightweight async task that:

1. On boot, loads the historical funding Parquet via funding_synthetic.
2. Starts a long-running async task that:
   - Polls `/fapi/v1/premiumIndex` every N minutes for the current funding
     rate + mark price.
   - At each 8h settlement boundary (00:00, 08:00, 16:00 UTC), builds a
     synthetic "carry candle":
         close[i+1] = close[i] × (1 + realized_funding - friction)
   - Calls the strategy's `on_features()` directly with the synthetic bar.
   - Routes the resulting Signal through the engine's normal risk_client
     and paper_executor path.

This keeps the funding carry strategy in-process with the engine without
requiring changes to `BinanceWebSocketFeed`, `CandleBuilder`, or the
normal feature pipeline. The feed is a thin adapter — it calls the same
strategy.process() contract and emits the same Signal objects.

**Safety rails:**
- Feed is OFF by default (only starts if funding_carry is in the registry
  and `cfg.m3s.funding_feed_enabled = true` or strategy is enabled in
  config/strategies.toml)
- All network calls are caught — a single failed poll must not crash the
  engine. Failures log and retry at the next tick.
- On 3+ consecutive errors, the feed pauses for 10 minutes before retry.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable

import certifi
import httpx
import pandas as pd

from src.data.funding_synthetic import (
    SyntheticSeriesConfig,
    load_synthetic_series,
)
from src.strategies.carry.funding_carry import FundingCarryStrategy
from src.utils.logger import get_logger

log = get_logger("funding_feed")


BINANCE_FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
_MS_PER_EPOCH = 8 * 3600 * 1000
_POLL_INTERVAL_SECONDS = 300          # 5 min — well under rate limit
_ERROR_BACKOFF_SECONDS = 600          # 10 min pause after 3 consecutive errors
_MAX_CONSECUTIVE_ERRORS = 3


class FundingSyntheticFeed:
    """Async scheduled task that feeds synthetic carry candles to funding_carry.

    Usage:
        feed = FundingSyntheticFeed(
            strategy=strategy,
            on_signal=engine._handle_carry_signal,
            symbol="BTCUSDT",
        )
        task = asyncio.create_task(feed.run())
        # ...
        feed.stop()
        await task
    """

    def __init__(
        self,
        *,
        strategy: FundingCarryStrategy,
        on_signal: Callable[[Any], Any],
        symbol: str = "BTCUSDT",
        friction_pct: float = 0.00005,
        poll_interval_seconds: int = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self._strategy = strategy
        self._on_signal = on_signal
        self._symbol = symbol.upper()
        self._friction_pct = float(friction_pct)
        self._poll_interval = int(poll_interval_seconds)

        self._running = False
        self._consecutive_errors = 0
        self._last_processed_funding_time_ms: int = 0
        self._synthetic_close: float = 100.0          # seed; replaced from history on boot

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self._client = httpx.AsyncClient(verify=ssl_ctx, timeout=15.0)

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start_and_seed(self) -> None:
        """Load the historical synthetic series to seed `self._synthetic_close`.

        Called once before `run()`. If the Parquet is missing or empty,
        seeds from 100.0 and logs a warning.
        """
        try:
            series = load_synthetic_series(
                self._symbol, friction_pct=self._friction_pct,
            )
            if not series.empty:
                self._synthetic_close = float(series["close"].iloc[-1])
                last_ts = int(series["timestamp"].iloc[-1])
                self._last_processed_funding_time_ms = last_ts
                log.info(
                    "funding_feed_seeded",
                    symbol=self._symbol,
                    seed_close=self._synthetic_close,
                    last_history_ts=last_ts,
                )
            else:
                log.warning("funding_feed_empty_history", symbol=self._symbol)
        except FileNotFoundError:
            log.warning(
                "funding_feed_no_parquet",
                symbol=self._symbol,
                note="run scripts/download_funding_history.py first",
            )

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception as e:
            log.warning("funding_feed_close_failed", error=str(e))

    # ── Main loop ───────────────────────────────────────────────────

    async def run(self) -> None:
        """Polling loop. Exits when `stop()` is called."""
        await self.start_and_seed()
        self._running = True
        log.info("funding_feed_start",
                 symbol=self._symbol,
                 poll_interval=self._poll_interval,
                 friction_pct=self._friction_pct)

        while self._running:
            try:
                await self._tick()
                self._consecutive_errors = 0
            except asyncio.CancelledError:
                log.info("funding_feed_cancelled")
                break
            except Exception as e:
                self._consecutive_errors += 1
                log.warning(
                    "funding_feed_tick_failed",
                    error=str(e),
                    consecutive=self._consecutive_errors,
                )
                if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    log.warning("funding_feed_backoff",
                                sleep_seconds=_ERROR_BACKOFF_SECONDS)
                    try:
                        await asyncio.sleep(_ERROR_BACKOFF_SECONDS)
                    except asyncio.CancelledError:
                        break
                    self._consecutive_errors = 0

            if not self._running:
                break
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break

        log.info("funding_feed_stopped")

    # ── Tick logic ───────────────────────────────────────────────

    async def _tick(self) -> None:
        """Poll /fapi/v1/fundingRate for the most recent settled funding.

        Each 8h settlement (00:00, 08:00, 16:00 UTC) is a new carry epoch.
        `GET /fapi/v1/fundingRate?symbol=BTCUSDT&limit=1` returns the
        latest settled funding with `fundingTime` (settlement timestamp, ms)
        and `fundingRate` (realized rate).

        When `fundingTime > self._last_processed_funding_time_ms`, we have
        crossed a new epoch. Advance the synthetic close and feed the bar.
        """
        params = {"symbol": self._symbol, "limit": 1}
        resp = await self._client.get(BINANCE_FUNDING_RATE_URL, params=params)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return
        latest = rows[-1]

        last_funding_time = int(latest["fundingTime"])
        current_funding_rate = float(latest["fundingRate"])

        # Only advance if we've crossed a new settlement since the last tick.
        if last_funding_time <= self._last_processed_funding_time_ms:
            return  # nothing new yet

        # Advance the synthetic close by the realized funding − friction.
        prev_close = self._synthetic_close
        new_close = prev_close * (1.0 + current_funding_rate - self._friction_pct)
        self._synthetic_close = new_close
        self._last_processed_funding_time_ms = last_funding_time

        log.info(
            "funding_feed_new_epoch",
            symbol=self._symbol,
            funding_time_ms=last_funding_time,
            funding_rate=current_funding_rate,
            prev_close=prev_close,
            new_close=new_close,
        )

        # Build a synthetic features Series and call the strategy directly
        features = pd.Series({
            "timestamp": last_funding_time,
            "open": prev_close,
            "high": max(prev_close, new_close),
            "low": min(prev_close, new_close),
            "close": new_close,
            "volume": 1.0,
            "funding_rate": current_funding_rate,
        })
        synthetic_symbol = f"{self._symbol}-CARRY"

        try:
            signal = self._strategy.process(synthetic_symbol, "8h", features)
        except Exception as e:
            log.exception("funding_feed_strategy_error", error=str(e))
            return

        if signal is None:
            return

        # Route signal through the main engine's callback (which runs it
        # through risk_client.check_signal → paper_executor.execute).
        try:
            result = self._on_signal(signal)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            log.exception("funding_feed_signal_route_error", error=str(e))
