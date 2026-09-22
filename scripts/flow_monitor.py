"""Live level-gated flow monitor.

You supply levels — in config/levels.toml, or from the live page, which writes
through to the same file. This watches what flow does when price reaches them
and reports evidence — for and against. It never places an order.

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
from src.flow.level_memory import LevelMemory
from src.flow.level_registry import DEFAULT_LEVELS_PATH, Level, LevelRegistry, LevelSide
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

        # Off unless asked for: a new heuristic that changes scoring should
        # not start affecting signals by default.
        self.memory = LevelMemory(enabled=args.level_memory)

        self.engine = FlowEngine(
            registry=self.registry, symbol=self.symbol, threshold=args.threshold,
            memory=self.memory, require_turn=not args.no_turn,
        )
        self.engine.sync_levels(now_ms=_now_ms())

        self.signal_log = SignalLog(target_mult=args.target_mult)
        self.server = LiveServer(
            get_snapshot=self._snapshot,
            port=args.port, hz=args.hz,
            on_command=self._on_command,
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

    def _snapshot(self) -> dict:
        """Engine state plus what the editor needs.

        The registry list is separate from `levels` on purpose: the engine only
        holds monitors for ACTIVE levels, so a parked or expired one would
        vanish from the page and could never be re-armed from there.
        """
        snap = self.engine.snapshot(_now_ms())
        snap["depth"] = bool(self.args.depth)
        snap["levels_path"] = str(self.args.levels)
        snap["registry"] = [
            {
                "id": lv.id,
                "symbol": lv.symbol,
                "price": lv.price,
                "width": lv.width,
                # Sent, not re-derived in the page: the zone is directional and
                # a second copy of that rule in JavaScript is a second place to
                # get it wrong.
                "low": lv.low,
                "high": lv.high,
                "side": lv.side.value,
                "note": lv.note,
                "enabled": lv.enabled,
                "first_seen_ms": lv.first_seen_ms,
                "expires_ms": lv.expires_ms,
                "expired": lv.is_expired(_now_ms()),
            }
            for lv in sorted(self.registry.levels.values(),
                             key=lambda lv: (lv.symbol, lv.price))
        ]
        return snap

    # ── commands from the page ─────────────────────────────────────────
    #
    # These run on the server task, not the tick path. Everything here is
    # synchronous and short: one asyncio loop means no lock is needed, but it
    # also means a slow command would stall ingestion, so nothing here waits
    # on anything.

    def _on_command(self, payload: dict) -> dict:
        cmd = str(payload.get("cmd", ""))
        now = _now_ms()

        if cmd == "level.upsert":
            level = self._level_from(payload.get("level") or {})
            existed = self.registry.get(level.id) is not None
            self.registry.upsert(level, now_ms=now)
            self.engine.sync_levels(now_ms=now)
            result = {"ok": True, "id": level.id}
            # Adding a level that price is already inside is the one move that
            # breaks the forward-test guarantee: you cannot claim you marked it
            # before price arrived when price is standing in it. It is allowed —
            # sometimes that is genuinely the trade — but it is said out loud,
            # and first_seen in the log makes it detectable afterwards.
            if not existed and level.contains(self.engine.last_price):
                result["warning"] = (
                    "price is inside this zone right now — this level is not "
                    "forward-clean, and the log will show it"
                )
            return result

        if cmd == "level.remove":
            lid = str(payload.get("id", ""))
            if not self.registry.remove(lid):
                return {"ok": False, "error": f"no such level: {lid}"}
            self.engine.sync_levels(now_ms=now)
            return {"ok": True, "id": lid}

        if cmd == "level.enable":
            lid = str(payload.get("id", ""))
            enabled = bool(payload.get("enabled", True))
            if not self.registry.set_enabled(lid, enabled):
                return {"ok": False, "error": f"no such level: {lid}"}
            self.engine.sync_levels(now_ms=now)
            return {"ok": True, "id": lid, "enabled": enabled}

        if cmd == "config.set":
            return self._apply_config(payload)

        return {"ok": False, "error": f"unknown command: {cmd!r}"}

    def _apply_config(self, payload: dict) -> dict:
        changed = {}
        if "threshold" in payload:
            threshold = float(payload["threshold"])
            if not 0.0 <= threshold <= 1.0:
                return {"ok": False, "error": "threshold must be between 0 and 1"}
            self.engine.apply_settings(threshold=threshold)
            changed["threshold"] = threshold
        if "require_turn" in payload:
            require_turn = bool(payload["require_turn"])
            self.engine.apply_settings(require_turn=require_turn)
            changed["require_turn"] = require_turn
        if "level_memory" in payload:
            enabled = bool(payload["level_memory"])
            # Turning it on mid-session has to read the file: the object was
            # constructed disabled and skipped its load, so without this the
            # first hour of "memory on" would report no history at all.
            if enabled and not self.memory.enabled:
                self.memory.enabled = True
                self.memory.load()
            else:
                self.memory.enabled = enabled
            changed["level_memory"] = enabled
        if not changed:
            return {"ok": False, "error": "nothing to set"}
        log.info("flow_config_changed", **changed)
        return {"ok": True, "changed": changed}

    def _level_from(self, raw: dict) -> Level:
        """Build a Level from page input. Raises ValueError with a readable
        message — the page shows it verbatim, so it has to read like English.

        An edit INHERITS every field the caller did not send. The page's form
        carries price, width, side and note; it has no box for `enabled` or
        `expires`. Defaulting those instead of inheriting them means editing a
        note re-arms a level you deliberately parked, and clears an expiry you
        set — both silent, both discovered later at the worst moment.
        """
        try:
            price = float(raw.get("price"))
            width = float(raw.get("width"))
        except (TypeError, ValueError):
            raise ValueError("price and width must be numbers")
        side = str(raw.get("side", "long")).strip().lower()
        if side not in ("long", "short"):
            raise ValueError("side must be long or short")
        symbol = str(raw.get("symbol") or self.symbol).strip().upper()
        lid = str(raw.get("id") or "").strip()
        if not lid:
            lid = self._make_id(symbol, price, side)

        prior = self.registry.get(lid)
        enabled = raw["enabled"] if "enabled" in raw else (
            prior.enabled if prior else True)
        expires_ms = prior.expires_ms if prior else None
        return Level(
            id=lid,
            symbol=symbol,
            price=price,
            width=width,
            side=LevelSide(side),
            note=str(raw.get("note", "")).strip(),
            enabled=bool(enabled),
            expires_ms=expires_ms,
        )

    def _make_id(self, symbol: str, price: float, side: str) -> str:
        """Readable, stable, and unique — outcomes are keyed by it, so a
        collision would silently merge two levels' histories."""
        base = f"{symbol.lower()}-{price:g}-{side}"
        if self.registry.get(base) is None:
            return base
        n = 2
        while self.registry.get(f"{base}-{n}") is not None:
            n += 1
        return f"{base}-{n}"

    # ── hot path ───────────────────────────────────────────────────────

    async def _on_tick(self, tick: Tick) -> None:
        signals = self.engine.on_tick(tick, local_ms=_now_ms())

        # Null hypothesis: log an entry for EVERY zone entry, whether or not
        # the scorer ever confirms it. Without this the scorer cannot be shown
        # to beat simply taking the level.
        for lid, monitor in self.engine.monitors.items():
            prev = self._prev_states.get(lid)
            if monitor.state is ZoneState.EVALUATING and prev is not ZoneState.EVALUATING:
                # Same definition the state machine and every signal use —
                # a baseline scored against a different stop is not a baseline.
                self.signal_log.record_baseline(
                    monitor.level, tick.price, tick.timestamp,
                    monitor.invalidation_price(),
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
            level_memory=self.args.level_memory, require_turn=not self.args.no_turn,
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
                        help="also subscribe to the order book (enables iceberg detection)")
    parser.add_argument("--level-memory", action="store_true",
                        help="remember how each level resolved before, across restarts "
                             "(off by default — it changes scoring)")
    parser.add_argument("--no-turn", action="store_true",
                        help="fire on score alone instead of requiring the "
                             "ABSORBING->TURNING sequence (comparison arm)")
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
