"""Main entry point — wires the data pipeline and starts the trading engine.

Pipeline: BinanceWS → CandleBuilder → FeatureEngine → StrategyRouter → PaperExecutor
                                                                       ↘ Storage (signal/trade log)

Usage:
    python -m src.main                          # Run with defaults from config
    python -m src.main --symbols BTCUSDT ETHUSDT --timeframes 1m 5m
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from datetime import datetime, timezone

import pandas as pd

from src.data.candle_builder import CandleBuilder
from src.data.feature_engine import FeatureEngine
from src.data.feeds.binance_ws import BinanceWebSocketFeed
from src.data.feeds.funding_synthetic_feed import FundingSyntheticFeed
from src.data.storage import Storage
from src.data.warmup import warmup
from src.execution.paper_executor import PaperExecutor
from src.m3s.allocator import Allocator as M3SAllocator
from src.m3s.compounder import Compounder as M3SCompounder
from src.m3s.conviction import ConvictionScorer
from src.m3s.edge_decay import EdgeDecayMonitor
from src.m3s.hooks import M3S
from src.m3s.modes import MODE_PRESETS, M3SMode
from src.m3s.portfolio import PortfolioTracker
from src.m3s.scheduler import M3SScheduler, load_state, save_state
from src.m3s.state import M3SStore
from src.monitoring.heartbeat import Heartbeat
from src.strategies.router import StrategyRouter
from src.utils.config import get_config
from src.utils.logger import get_logger
from src.risk.client import RiskClient
from src.utils.types import Candle, Signal, SignalAction, Tick

log = get_logger("main")


class TradingEngine:
    """Wires all data pipeline components together and manages lifecycle."""

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str],
        indicators: list[str],
        needed_pairs: set[tuple[str, str]] | None = None,
    ):
        self.symbols = symbols
        self.timeframes = timeframes
        self.needed_pairs = needed_pairs

        # ── Components ──
        binance_cfg = get_config().get_exchange("binance")
        use_testnet = binance_cfg.get("testnet", False)
        self.feed = BinanceWebSocketFeed(
            symbols=symbols,
            timeframes=timeframes,
            testnet=use_testnet,
            needed_pairs=needed_pairs,
        )
        log.info("feed_configured", testnet=use_testnet,
                 kline_streams=len(needed_pairs) if needed_pairs else "all")
        self.candle_builder = CandleBuilder(timeframes=timeframes)
        self.feature_engine = FeatureEngine(indicators=indicators)
        self.storage = Storage()

        # ── Strategy + Risk + Execution (wired in start()) ──
        self.strategy_router: StrategyRouter | None = None
        self.paper_executor: PaperExecutor | None = None
        self.risk_client: RiskClient | None = None

        # ── M3S (sub-phase 0.8: wired but disabled by default) ──
        self.m3s: M3S | None = None
        self.m3s_store: M3SStore | None = None
        self.m3s_scheduler: M3SScheduler | None = None
        self._m3s_scheduler_task: asyncio.Task | None = None

        # ── Funding Carry live feed (Phase 3b-3 Strategy A) ──
        self.funding_feed: FundingSyntheticFeed | None = None
        self._funding_feed_task: asyncio.Task | None = None

        # ── Monitoring ──
        self._heartbeat: Heartbeat | None = None
        self._heartbeat_task: asyncio.Task | None = None

        # ── Stats ──
        self._tick_count = 0
        self._candle_count = 0
        self._feature_count = 0
        self._signal_count = 0
        self._rejection_count = 0
        self._last_candle_time: float = 0
        self._start_time: float = 0

    def _maybe_init_m3s(self, cfg) -> None:
        """Initialize M3S if `cfg.m3s.enabled`. Default: disabled (no-op).

        Sub-phase 0.8 ships M3S wired but turned off. Setting
        `m3s.enabled=true` in config/settings.toml activates it in shadow
        mode — which still has no functional effect on trades because
        shadow_mode=true by default. Authoritative mode requires a second
        explicit config change (`m3s.shadow_mode=false`) after the BT gate
        series has passed.
        """
        settings = cfg.settings if hasattr(cfg, "settings") else {}
        m3s_cfg = settings.get("m3s", {}) if isinstance(settings, dict) else {}
        enabled = bool(m3s_cfg.get("enabled", False))

        if not enabled:
            log.info("m3s_disabled")
            return

        mode_name = str(m3s_cfg.get("mode", "STANDARD")).upper()
        shadow_mode = bool(m3s_cfg.get("shadow_mode", True))
        db_path = str(m3s_cfg.get("db_path", "data/m3s.sqlite"))
        cadence_hours = float(m3s_cfg.get("rebalance_cadence_hours", 24.0))

        try:
            mode = MODE_PRESETS[M3SMode(mode_name)]
        except (ValueError, KeyError) as e:
            log.warning("m3s_mode_invalid", mode=mode_name, fallback="STANDARD", error=str(e))
            mode = MODE_PRESETS[M3SMode.STANDARD]

        initial_equity = self.paper_executor.equity if self.paper_executor else 10_000.0
        tracker = PortfolioTracker(initial_equity=initial_equity)
        compounder = M3SCompounder(mode=mode, tracker=tracker)
        allocator = M3SAllocator(mode=mode, tracker=tracker)
        edge_decay = EdgeDecayMonitor()
        conviction = ConvictionScorer()

        self.m3s = M3S(
            mode=mode,
            tracker=tracker,
            compounder=compounder,
            allocator=allocator,
            edge_decay=edge_decay,
            conviction_scorer=conviction,
            shadow_mode=shadow_mode,
        )
        self.m3s_store = M3SStore(db_path)
        load_state(self.m3s, self.m3s_store)

        # Wire paper executor → M3S trade-close hook (advances compounder).
        if self.paper_executor:
            self.paper_executor.on_trade_close_hook = self.m3s.on_trade_close

        # Seed state on boot so the shadow checker has something to audit
        # before the first scheduled tick. Safe — rebalance() on a fresh
        # tracker produces a cold-start equal-weight allocation and records
        # the compound base from initial equity.
        try:
            decision = self.m3s.rebalance()
            save_state(self.m3s, self.m3s_store)
            # Append a startup event so the shadow checker sees activity
            # immediately (avoids a 48h "no events" warning on fresh boot).
            self.m3s_store.append_event(
                "allocation",
                {
                    "ts_ms": decision.ts_ms,
                    "method": decision.method,
                    "weights": decision.weights,
                    "inputs_hash": decision.inputs_hash,
                    "reasoning": f"boot-seed: {decision.reasoning}",
                },
                ts_ms=decision.ts_ms,
                inputs_hash=decision.inputs_hash,
            )
            log.info("m3s_seeded_on_boot",
                     base_equity=self.m3s._compounder.state.base_equity,
                     method=decision.method)
        except Exception as e:
            log.warning("m3s_seed_failed", error=str(e))

        self.m3s_scheduler = M3SScheduler(
            self.m3s, self.m3s_store, cadence_seconds=cadence_hours * 3600.0,
        )
        self._m3s_scheduler_task = asyncio.create_task(self.m3s_scheduler.run())

        log.info(
            "m3s_initialized",
            mode=mode.name.value,
            shadow=shadow_mode,
            cadence_hours=cadence_hours,
            db_path=db_path,
        )

    def _maybe_start_funding_feed(self) -> None:
        """Start the FundingSyntheticFeed if funding_carry is enabled.

        The feed polls Binance Futures premium-index endpoint every 5 min
        and emits a synthetic carry candle at each 8h settlement. Signals
        flow through `_handle_carry_signal` → risk_client → paper_executor.
        Runs as an async task and shuts down cleanly with the engine.
        """
        cfg = get_config()
        strat_cfg = cfg.get_strategy("funding_carry")
        if not strat_cfg.get("enabled", False):
            log.info("funding_feed_disabled")
            return

        # Build strategy from config + start the feed task
        try:
            from src.strategies.carry.funding_carry import FundingCarryStrategy
            carry_strategy = FundingCarryStrategy.from_config("funding_carry")
        except Exception as e:
            log.warning("funding_feed_strategy_load_failed", error=str(e))
            return

        self.funding_feed = FundingSyntheticFeed(
            strategy=carry_strategy,
            on_signal=self._handle_carry_signal,
            symbol="BTCUSDT",
            friction_pct=float(strat_cfg.get("friction_pct", 0.00005)),
        )
        self._funding_feed_task = asyncio.create_task(self.funding_feed.run())
        log.info("funding_feed_started", symbol="BTCUSDT")

    async def _handle_carry_signal(self, signal: Signal) -> None:
        """Route a funding-carry signal through M3S + risk + paper executor.

        Called by FundingSyntheticFeed on every new synthetic carry bar.
        Mirrors the _on_features signal-handling path but for 8h-cadence
        carry bars that don't come from the normal candle pipeline.
        """
        self._signal_count += 1

        # M3S hook (shrink risk_pct, shadow-mode default)
        if self.m3s is not None:
            try:
                signal = self.m3s.on_signal(signal)
            except Exception as e:
                log.warning("carry_m3s_on_signal_failed", error=str(e))

        # Risk gate
        if self.risk_client:
            try:
                decision = self.risk_client.check_signal(signal)
            except Exception as e:
                log.warning("carry_risk_check_failed", error=str(e))
                return
            if not decision.approved:
                self._rejection_count += 1
                log.warning("carry_signal_rejected", reason=decision.reason,
                            symbol=signal.symbol, action=signal.action.value)
                return
            if decision.adjusted_risk_pct is not None:
                signal = Signal(
                    symbol=signal.symbol, action=signal.action,
                    confidence=signal.confidence, strategy_name=signal.strategy_name,
                    timeframe=signal.timeframe, entry_price=signal.entry_price,
                    stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                    risk_pct=decision.adjusted_risk_pct,
                    metadata=signal.metadata, timestamp=signal.timestamp,
                )

        # Paper executor — carries still route through the same execute() path.
        # Note: the synthetic BTCUSDT-CARRY symbol has no real order book; the
        # executor will produce a simulated fill at the synthetic close price,
        # which is the correct shadow-mode behavior.
        if self.paper_executor:
            fill = await self.paper_executor.execute(signal)
            if fill and self.risk_client:
                self.risk_client.report_fill(fill, signal.strategy_name)

    async def _on_tick(self, tick: Tick) -> None:
        """Handle raw tick from exchange."""
        self._tick_count += 1
        await self.candle_builder.handle_tick(tick)

        # Update paper executor with latest price
        if self.paper_executor:
            self.paper_executor.update_prices(tick.symbol, tick.price)

        # Log every 1000 ticks
        if self._tick_count % 1000 == 0:
            log.info("tick_stats", count=self._tick_count, symbol=tick.symbol, price=tick.price)

    def _get_heartbeat_stats(self) -> dict:
        """Collect stats for heartbeat writer."""
        return {
            "tick_count": self._tick_count,
            "candle_count": self._candle_count,
            "open_positions": len(self.paper_executor._positions) if self.paper_executor else 0,
            "equity": self.paper_executor.equity if self.paper_executor else 0,
            "last_candle_time": self._last_candle_time,
            "risk_server_ok": self.risk_client is not None,
            "signal_count": self._signal_count,
            "rejection_count": self._rejection_count,
            "strategy_exceptions": self.strategy_router._exception_count if self.strategy_router else 0,
        }

    async def _on_candle(self, candle: Candle) -> None:
        """Handle completed candle from builder or exchange kline."""
        self._candle_count += 1
        self._last_candle_time = time.time()
        log.info(
            "candle",
            symbol=candle.symbol,
            tf=candle.timeframe,
            o=candle.open,
            h=candle.high,
            l=candle.low,
            c=candle.close,
            v=round(candle.volume, 4),
        )

        # Feed to indicator engine
        await self.feature_engine.handle_candle(candle)

    async def _on_features(self, symbol: str, timeframe: str, features: pd.Series) -> None:
        """Handle computed features — this is where strategies will plug in."""
        self._feature_count += 1

        # Log key indicator values (non-NaN only)
        indicator_vals = {}
        for col in features.index:
            if col not in ("open", "high", "low", "close", "volume", "timestamp"):
                val = features[col]
                if pd.notna(val):
                    indicator_vals[col] = round(float(val), 4)

        if indicator_vals:
            log.info("features", symbol=symbol, tf=timeframe, **indicator_vals)

        # Route to strategies → risk check → executor
        if self.strategy_router:
            signals = await self.strategy_router.on_features(symbol, timeframe, features)
            if signals and self.paper_executor:
                self._signal_count += len(signals)
                for sig in signals:
                    # M3S hook (sub-phase 0.8): shrink risk_pct before risk gate.
                    # Safe no-op when disabled or in shadow mode.
                    if self.m3s is not None:
                        try:
                            sig = self.m3s.on_signal(sig)
                        except Exception as e:
                            log.warning("m3s_on_signal_failed", error=str(e),
                                        strategy=sig.strategy_name)
                    if self.risk_client:
                        decision = self.risk_client.check_signal(sig)
                        if not decision.approved:
                            self._rejection_count += 1
                            log.warning("signal_rejected", reason=decision.reason,
                                        symbol=sig.symbol, strategy=sig.strategy_name,
                                        action=sig.action.value)
                            continue
                        # Use risk-manager-computed quantity if available
                        if decision.adjusted_risk_pct is not None:
                            sig = Signal(
                                symbol=sig.symbol, action=sig.action,
                                confidence=sig.confidence, strategy_name=sig.strategy_name,
                                timeframe=sig.timeframe, entry_price=sig.entry_price,
                                stop_loss=sig.stop_loss, take_profit=sig.take_profit,
                                risk_pct=decision.adjusted_risk_pct,
                                metadata=sig.metadata, timestamp=sig.timestamp,
                            )
                    fill = await self.paper_executor.execute(sig)
                    if fill and self.risk_client:
                        self.risk_client.report_fill(fill, sig.strategy_name)

    async def start(self) -> None:
        """Initialize all components and start the data pipeline."""
        self._start_time = time.time()

        cfg = get_config()
        log.info(
            "engine_starting",
            mode=cfg.mode,
            symbols=self.symbols,
            timeframes=self.timeframes,
            indicators=self.feature_engine.indicators,
        )

        # Initialize storage
        await self.storage.init()

        # Initialize strategy router + paper executor + risk client
        self.paper_executor = PaperExecutor(storage=self.storage)
        restored = self.paper_executor.restore_state()
        if restored:
            log.info("paper_positions_restored", count=restored,
                     equity=round(self.paper_executor.equity, 2))
        self.strategy_router = StrategyRouter.from_config(storage=self.storage)

        # Connect to risk server (fail-closed if unreachable)
        try:
            risk_cfg = cfg.risk if hasattr(cfg, "risk") else {}
            zmq_addr = risk_cfg.get("zmq_addr", "tcp://127.0.0.1:5555") if isinstance(risk_cfg, dict) else "tcp://127.0.0.1:5555"
            self.risk_client = RiskClient(server_addr=zmq_addr)
            self.risk_client.connect()
            log.info("risk_client_connected", addr=zmq_addr)
        except Exception as e:
            log.warning("risk_client_failed", error=str(e))
            self.risk_client = None

        # Initialize M3S (sub-phase 0.8: disabled by default; ships as scaffolding).
        self._maybe_init_m3s(cfg)

        # Initialize funding carry live feed if enabled (Strategy A paper activation).
        self._maybe_start_funding_feed()

        # Historical warmup: load candles to prime indicators + strategy state
        warmup_result = await warmup(
            self.feature_engine, self.strategy_router,
            self.symbols, self.timeframes,
            needed_pairs=self.needed_pairs,
        )
        log.info("warmup_done", result=warmup_result)

        # Wire the callback chain:
        #   feed.on_tick → _on_tick → candle_builder.handle_tick
        #   feed.on_candle → candle_builder.handle_candle → _on_candle
        #   candle_builder.on_candle → _on_candle → feature_engine.handle_candle
        #   feature_engine.on_features → _on_features
        self.feed.on_tick = self._on_tick
        self.feed.on_candle = self.candle_builder.handle_candle
        self.candle_builder.on_candle = self._on_candle
        self.feature_engine.on_features = self._on_features

        log.info("pipeline_wired", flow="feed → candle_builder → feature_engine → strategies")

        # Start heartbeat monitoring
        self._heartbeat = Heartbeat(
            get_stats=self._get_heartbeat_stats,
            risk_client=self.risk_client,
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat.run())
        log.info("heartbeat_started")

        # Start the feed (blocks until stop)
        await self.feed.start()

    async def stop(self) -> None:
        """Graceful shutdown — feed first, heartbeat last."""
        log.info("engine_stopping")

        # 1. Stop feed (no new data)
        await self.feed.stop()

        # 2. Persist paper executor state (positions + equity)
        if self.paper_executor:
            self.paper_executor._persist_equity()
            for pos in self.paper_executor._positions.values():
                self.paper_executor._persist_position(pos)
            log.info("paper_state_persisted",
                     positions=len(self.paper_executor._positions),
                     equity=round(self.paper_executor.equity, 2))

        # 3. Persist M3S state and stop scheduler (sub-phase 0.8)
        if self.m3s is not None and self.m3s_store is not None:
            try:
                save_state(self.m3s, self.m3s_store)
                log.info("m3s_state_persisted")
            except Exception as e:
                log.warning("m3s_persist_failed", error=str(e))
        if self.m3s_scheduler is not None:
            self.m3s_scheduler.stop()
        if self._m3s_scheduler_task is not None:
            self._m3s_scheduler_task.cancel()
            try:
                await self._m3s_scheduler_task
            except asyncio.CancelledError:
                pass
        if self.m3s_store is not None:
            try:
                self.m3s_store.close()
            except Exception as e:
                log.warning("m3s_store_close_failed", error=str(e))

        # 3b. Stop funding carry live feed (Strategy A)
        if self.funding_feed is not None:
            self.funding_feed.stop()
            await self.funding_feed.close()
        if self._funding_feed_task is not None:
            self._funding_feed_task.cancel()
            try:
                await self._funding_feed_task
            except asyncio.CancelledError:
                pass

        # 4. Close storage
        await self.storage.close()

        # 5. Stop heartbeat last (external monitors see us alive until cleanup done)
        if self._heartbeat:
            self._heartbeat.stop()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        elapsed = time.time() - self._start_time if self._start_time else 0
        log.info(
            "engine_stopped",
            uptime_seconds=round(elapsed, 1),
            ticks=self._tick_count,
            candles=self._candle_count,
            features=self._feature_count,
            executor_stats=self.paper_executor.stats if self.paper_executor else {},
        )

    def print_status(self) -> None:
        """Print current pipeline status."""
        import time
        elapsed = time.time() - self._start_time if self._start_time else 0
        print(f"\n{'='*60}")
        print(f"  Algo Trading Engine — Status")
        print(f"  Mode: {get_config().mode} | Uptime: {elapsed:.0f}s")
        print(f"  Symbols: {self.symbols}")
        print(f"  Timeframes: {self.timeframes}")
        print(f"  Ticks: {self._tick_count}")
        print(f"  Candles: {self._candle_count}")
        print(f"  Features: {self._feature_count}")
        print(f"  Builder: {self.candle_builder.stats}")
        print(f"  Engine: {self.feature_engine.stats}")
        print(f"{'='*60}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Algo Trading Engine")
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Symbols to trade (default: from strategies.toml)",
    )
    parser.add_argument(
        "--timeframes", nargs="+", default=None,
        help="Timeframes to track (default: from strategies.toml)",
    )
    parser.add_argument(
        "--indicators", nargs="+",
        default=[
            "ema_9", "ema_21", "rsi_7", "rsi_14", "bbands_20", "macd",
            "atr_14", "adx_14", "donchian_20", "donchian_55", "donchian_120",
        ],
        help="Indicators to compute",
    )
    return parser.parse_args()


def _get_symbols_from_config() -> list[str]:
    """Pull enabled strategy symbols from strategies.toml (excluding synthetics)."""
    pairs = _get_needed_pairs_from_config()
    return sorted({sym.lower() for sym, _tf in pairs}) or ["btcusdt"]


def _get_timeframes_from_config() -> list[str]:
    """Pull enabled strategy timeframes from strategies.toml (excluding synthetics)."""
    pairs = _get_needed_pairs_from_config()
    return sorted({tf for _sym, tf in pairs}) or ["1m"]


def _get_needed_pairs_from_config() -> set[tuple[str, str]]:
    """Pull exact (symbol, timeframe) pairs needed by enabled strategies.

    Used to avoid Cartesian-product waste: if only ETHUSDT needs 5m,
    don't subscribe/warmup/compute 5m for all 9 symbols.

    Synthetic symbols (those containing "-CARRY", "-SYNTH", etc.) are
    excluded — they're served by dedicated feeds, not by the normal
    Binance WS subscription.
    """
    cfg = get_config()
    pairs: set[tuple[str, str]] = set()
    for name, strat in cfg.strategies.items():
        if not strat.get("enabled", False):
            continue
        tf = strat.get("timeframe")
        if not tf:
            continue
        for market in strat.get("markets", []):
            m = market.upper()
            if "-CARRY" in m or "-SYNTH" in m:
                continue  # skip synthetic symbols — served by dedicated feeds
            pairs.add((market.lower(), tf))
    return pairs




async def run(args: argparse.Namespace) -> None:
    symbols = args.symbols or _get_symbols_from_config()
    timeframes = args.timeframes or _get_timeframes_from_config()
    needed_pairs = _get_needed_pairs_from_config()

    engine = TradingEngine(
        symbols=symbols,
        timeframes=timeframes,
        indicators=args.indicators,
        needed_pairs=needed_pairs,
    )

    # Handle Ctrl+C gracefully
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("shutdown_signal_received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Run feed in background, wait for shutdown signal
    feed_task = asyncio.create_task(engine.start())

    await shutdown_event.wait()
    await engine.stop()

    # Cancel the feed task if still running
    if not feed_task.done():
        feed_task.cancel()
        try:
            await feed_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    args = parse_args()

    print(f"\n  Algo Trading Engine v0.1.0")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Mode: {get_config().mode}")
    print(f"  Press Ctrl+C to stop\n")

    try:
        import uvloop
        uvloop.install()
        log.info("uvloop_installed")
    except ImportError:
        pass

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
