"""Live level-gated flow monitor.

You supply levels in config/levels.toml. This watches what flow does when price
reaches them and reports evidence — for and against — on a live page. It never
places an order.

    python -m scripts.flow_monitor --symbol BTCUSDT
    python -m scripts.flow_monitor --symbol BTCUSDT --depth      # + order book
    open http://127.0.0.1:8760

Crypto only. A CFD feed carries no trade size or aggressor side, so there is
nothing to read on the gold book (see src/data/cvd.py).

PROCESS SHAPE
-------------
    feed callbacks  ──>  FlowEngine.on_tick()        hot path, pure compute
                           │
                           ├─> SignalLog             append-only, off hot path
                           └─> LiveServer            5 Hz broadcast, own task

The tick handler does arithmetic and nothing else. Disk writes and the browser
push live on separate tasks, so neither can apply backpressure to ingestion.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time

from src.data.feeds.binance_ws import BinanceWebSocketFeed
from src.flow.evidence import DEFAULT_THRESHOLD
from src.flow.level_registry import DEFAULT_LEVELS_PATH, LevelRegistry
from src.flow.live_server import DEFAULT_HZ, DEFAULT_PORT, LiveServer
from src.flow.signal_log import SignalLog
from src.flow.zone_state import FlowEngine, ZoneState
from src.utils.logger import get_logger
from src.utils.types import OrderBookSnapshot, Tick

log = get_logger("flow_monitor")

LEVEL_RELOAD_INTERVAL = 10.0   # seconds


def _now_ms() -> int:
    return int(time.time() * 1000)


class FlowMonitor:
    """Wires the feed, the engine, the log and the UI together."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.symbol = args.symbol.upper()

        self.registry = LevelRegistry(path=args.levels)
        self.registry.load(now_ms=_now_ms(), force=True)

        self.engine = FlowEngine(
            registry=self.registry, symbol=self.symbol, threshold=args.threshold
        )
        self.engine.sync_levels(now_ms=_now_ms())

        self.signal_log = SignalLog(target_mult=args.target_mult)
        self.server = LiveServer(
            get_snapshot=lambda: self.engine.snapshot(_now_ms()),
            port=args.port, hz=args.hz,
        )

        self.feed = BinanceWebSocketFeed(
            symbols=[self.symbol],
            timeframes=["1m"],
            testnet=False,
            depth_symbols=[self.symbol] if args.depth else None,
        )
        self.feed.on_tick = self._on_tick
        if args.depth:
            self.feed.on_depth = self._on_depth

        # Previous states, for detecting zone entry (the null-hypothesis hook).
        self._prev_states: dict[str, ZoneState] = {}

    # ── hot path ───────────────────────────────────────────────────────

    async def _on_tick(self, tick: Tick) -> None:
        signals = self.engine.on_tick(tick, local_ms=_now_ms())

        # Null hypothesis: log an entry for EVERY zone entry, whether or not
        # the scorer ever confirms it. Without this the scorer cannot be shown
        # to beat simply taking the level.
        for lid, monitor in self.engine.monitors.items():
            prev = self._prev_states.get(lid)
            if monitor.state is ZoneState.EVALUATING and prev is not ZoneState.EVALUATING:
                buffer = monitor.level.width * monitor.invalidation_mult
                invalidation = (
                    monitor.level.low - buffer
                    if monitor.level.side.value == "long"
                    else monitor.level.high + buffer
                )
                self.signal_log.record_baseline(
                    monitor.level, tick.price, tick.timestamp, invalidation
                )
            self._prev_states[lid] = monitor.state

        if signals:
            records = []
            for signal in signals:
                self.signal_log.record_signal(signal)
                records.append(signal.to_dict())
                log.info(
                    "flow_signal",
                    level=signal.level_id, side=signal.side.value,
                    price=signal.price, score=round(signal.score, 3),
                    invalidation=signal.invalidation,
                )
            self.server.publish_signals(records)

        resolved = self.signal_log.update_prices(tick.price, tick.timestamp)
        for record in resolved:
            log.info(
                "flow_outcome",
                kind=record["kind"], outcome=record["outcome"],
                win=record["win"], score=record["score"],
                held_s=round(record["held_ms"] / 1000),
            )

    async def _on_depth(self, snapshot: OrderBookSnapshot) -> None:
        self.engine.on_book(snapshot)

    # ── background ─────────────────────────────────────────────────────

    async def _reload_levels(self) -> None:
        while True:
            await asyncio.sleep(LEVEL_RELOAD_INTERVAL)
            try:
                now = _now_ms()
                if self.registry.load(now_ms=now):
                    self.engine.sync_levels(now_ms=now)
            except Exception as e:
                log.error("level_reload_failed", error=str(e))

    async def run(self) -> None:
        active = self.registry.active(self.symbol, _now_ms())
        log.info(
            "flow_monitor_start",
            symbol=self.symbol, levels=len(active), depth=self.args.depth,
            threshold=self.args.threshold, ui=f"http://127.0.0.1:{self.args.port}",
        )
        if not active:
            log.warning(
                "no_active_levels",
                path=str(self.args.levels),
                note="monitor will run but has nothing to watch",
            )

        tasks = [
            asyncio.create_task(self.feed.start()),
            asyncio.create_task(self.server.run()),
            asyncio.create_task(self._reload_levels()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.feed.stop()
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scripts.flow_monitor",
        description="Level-gated order flow monitor (advisory only)",
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--levels", type=lambda p: __import__("pathlib").Path(p),
                        default=DEFAULT_LEVELS_PATH)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="score at or above which a zone confirms")
    parser.add_argument("--target-mult", type=float, default=2.0,
                        help="outcome target as a multiple of the risk distance")
    parser.add_argument("--depth", action="store_true",
                        help="also subscribe to the order book (book evidence)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--hz", type=float, default=DEFAULT_HZ,
                        help="UI broadcast rate")
    args = parser.parse_args()

    try:
        import uvloop
        uvloop.install()
        log.info("uvloop_enabled")
    except ImportError:
        pass

    monitor = FlowMonitor(args)
    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        log.info("flow_monitor_stopped")


if __name__ == "__main__":
    main()
