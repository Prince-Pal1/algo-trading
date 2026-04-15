"""Shadow mode orchestrator — feed-driven paper trading loop for gold."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.backtest.book import (
    SUB_BOOK_INSTITUTIONAL,
    Book,
    LeveragedPosition,
)
from src.backtest.costs import ICMarketsMetalFeeModel, ZeroCostFeeModel
from src.backtest.fee_profiles import make_fee_model
from src.backtest.path import Bar, BrownianBridgeModel, check_sl_tp_hits
from src.data.feature_engine import _compute_indicators
from src.data.feeds.parquet_replay_feed import ParquetReplayFeed
from src.strategies.base import BaseStrategy
from src.utils.logger import get_logger
from src.utils.types import Candle, SignalAction

log = get_logger("shadow_orchestrator")


@dataclass
class ShadowOrchestratorState:
    bar_index: int = 0
    trades: list = field(default_factory=list)
    equity_institutional: list[float] = field(default_factory=list)
    equity_aggressive: list[float] = field(default_factory=list)
    equity_total: list[float] = field(default_factory=list)
    margin_level: list[float] = field(default_factory=list)
    broker_stop_outs: int = 0
    leverage_grant_count: int = 0


@dataclass
class StrategyRoute:
    strategy: BaseStrategy
    sub_book: str
    leverage: float


class ShadowOrchestrator:
    """Event-driven shadow engine: async feed → strategies → Book → parquet dump.

    The orchestrator accepts ANY feed that matches the on_candle async callback
    contract of BinanceWebSocketFeed. Swap ParquetReplayFeed ↔ ICMarketsFeed
    ↔ BinanceWebSocketFeed with zero code changes here.
    """

    def __init__(
        self,
        *,
        feed: ParquetReplayFeed,
        strategy_routes: list[StrategyRoute],
        initial_institutional_cash: float = 7_000.0,
        initial_aggressive_cash: float = 3_000.0,
        symbol: str = "XAUUSD",
        timeframe: str = "1h",
        indicators: list[str] | None = None,
        state_dump_path: Path | str | None = None,
        state_dump_interval: int = 100,
        run_id: str = "shadow_default",
        fee_model: ICMarketsMetalFeeModel | ZeroCostFeeModel | None = None,
        fee_profile: str = "ic_markets_mt4_xauusd_normal",
        path_model: BrownianBridgeModel | None = None,
    ) -> None:
        self._feed = feed
        self._routes = strategy_routes
        self._symbol = symbol
        self._timeframe = timeframe
        self._indicators = indicators or [
            "donchian_20", "donchian_55", "donchian_120",
            "atr_14", "atr_20", "adx_14",
        ]
        self._state_dump_path = Path(state_dump_path) if state_dump_path else None
        self._state_dump_interval = state_dump_interval
        self._run_id = run_id
        # Explicit fee profile per CLAUDE.md — never use the default
        # ICMarketsMetalFeeModel() constructor (the 90× slippage bug history,
        # commit 6ae48c8 fix). Volume-based cTrader schedules also require
        # reference_price in commission_usd(); MT4 profile is safest default.
        self._fee_model = fee_model if fee_model is not None else make_fee_model(fee_profile)
        self._path_model = path_model or BrownianBridgeModel(run_id=run_id)

        self._book = Book.new(
            institutional_cash=initial_institutional_cash,
            aggressive_cash=initial_aggressive_cash,
        )
        self._state = ShadowOrchestratorState()

        # Rolling history of received candles + precomputed indicators.
        # For the parquet replay use case, indicators are precomputed for
        # the full window upfront to match batch backtest semantics.
        self._precomputed_df: pd.DataFrame | None = None

    def precompute_indicators(self, parquet_path: Path | str) -> None:
        df = pd.read_parquet(parquet_path).sort_values("timestamp").reset_index(drop=True)
        self._precomputed_df = _compute_indicators(df, self._indicators)

    def _row_for_candle(self, candle: Candle) -> pd.Series | None:
        if self._precomputed_df is None:
            return None
        matches = self._precomputed_df[self._precomputed_df["timestamp"] == candle.timestamp]
        if matches.empty:
            return None
        return matches.iloc[0]

    async def _on_candle(self, candle: Candle) -> None:
        self._state.bar_index += 1
        bar = Bar(
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            ts_ms=candle.timestamp,
        )

        # Step 1: intrabar SL/TP check on existing positions
        self._apply_intrabar_sl_tp(bar)

        # Step 2: broker stop-out per sub-book (worst-case intrabar marks)
        worst_marks = self._worst_case_marks(bar)
        for sb_name in ("institutional", "aggressive"):
            stop_outs = self._book.execute_stop_outs(worst_marks, sb_name)
            self._state.broker_stop_outs += len(stop_outs)
            for t in stop_outs:
                self._state.trades.append({
                    **t,
                    "exit_reason": "broker_stop_out",
                    "bar_index": self._state.bar_index,
                })

        # Step 3: call each strategy with the precomputed indicator row
        row = self._row_for_candle(candle)
        if row is None:
            return
        for route in self._routes:
            signal = route.strategy.process(
                self._symbol, self._timeframe, row,
            )
            if signal is not None:
                self._handle_signal(signal, bar, route)

        # Step 4: update equity curves
        all_marks = {pos.id: bar.close for pos in self._book.all_positions()}
        self._book.update_peaks(all_marks)
        inst_eq = self._book.institutional.equity(all_marks)
        aggr_eq = self._book.aggressive.equity(all_marks)
        self._state.equity_institutional.append(inst_eq)
        self._state.equity_aggressive.append(aggr_eq)
        self._state.equity_total.append(inst_eq + aggr_eq)

        used = self._book.total_used_margin()
        ml = (inst_eq + aggr_eq) / used if used > 0 else float("inf")
        self._state.margin_level.append(ml if ml != float("inf") else 0.0)

        # Step 5: periodic state dump
        if (
            self._state_dump_path is not None
            and self._state.bar_index > 0
            and self._state.bar_index % self._state_dump_interval == 0
        ):
            self._dump_state()

    def _apply_intrabar_sl_tp(self, bar: Bar) -> None:
        for pos in list(self._book.all_positions()):
            hit_label, hit_price = check_sl_tp_hits(
                bar=bar,
                side=pos.side,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                path_model=self._path_model,
                bar_idx=self._state.bar_index,
            )
            if hit_label is None or hit_price is None:
                continue
            fee_side = "SELL" if pos.side == "LONG" else "BUY"
            fill = self._fee_model.fill_price(
                side=fee_side, reference_price=hit_price,
                atr=0.0, ts_ms=bar.ts_ms,
            )
            commission = self._fee_model.commission_usd(
                quantity_units=pos.quantity, reference_price=fill,
            )
            closed = self._book.close_position(pos.id, exit_price=fill, commission=commission)
            self._state.trades.append({
                **closed,
                "exit_reason": "stop_loss" if hit_label == "sl" else "take_profit",
                "bar_index": self._state.bar_index,
            })

    def _worst_case_marks(self, bar: Bar) -> dict[int, float]:
        return {
            pos.id: self._path_model.worst_adverse_price(bar, pos.side)
            for pos in self._book.all_positions()
        }

    def _handle_signal(self, signal, bar: Bar, route: StrategyRoute) -> None:
        action = signal.action
        if action == SignalAction.CLOSE:
            sb = self._book.sub_book(route.sub_book)
            for pid in [
                p.id for p in sb.positions.values()
                if p.strategy_name == route.strategy.name
            ]:
                pos = sb.positions[pid]
                fee_side = "SELL" if pos.side == "LONG" else "BUY"
                fill = self._fee_model.fill_price(
                    side=fee_side, reference_price=bar.close, atr=0.0, ts_ms=bar.ts_ms,
                )
                commission = self._fee_model.commission_usd(
                    quantity_units=pos.quantity, reference_price=fill,
                )
                closed = self._book.close_position(pid, exit_price=fill, commission=commission)
                self._state.trades.append({
                    **closed,
                    "exit_reason": "signal",
                    "bar_index": self._state.bar_index,
                })
            return

        if action not in (SignalAction.LONG, SignalAction.SHORT):
            return

        sb = self._book.sub_book(route.sub_book)
        existing = [
            p for p in sb.positions.values() if p.strategy_name == route.strategy.name
        ]
        if existing:
            return

        entry_reference = signal.entry_price or bar.close
        fee_side = "BUY" if action == SignalAction.LONG else "SELL"
        fill_price = self._fee_model.fill_price(
            side=fee_side, reference_price=entry_reference,
            atr=abs(bar.high - bar.low), ts_ms=bar.ts_ms,
        )

        risk_pct = signal.risk_pct if signal.risk_pct is not None else 0.01
        sub_equity = sb.cash
        risk_amount = sub_equity * risk_pct
        stop_distance = (
            abs(fill_price - signal.stop_loss) if signal.stop_loss is not None else fill_price * 0.01
        )
        if stop_distance <= 0:
            return
        quantity = risk_amount / stop_distance
        if quantity <= 0:
            return

        leverage = float(signal.leverage) if signal.leverage else route.leverage

        try:
            self._book.open_position(
                side="LONG" if action == SignalAction.LONG else "SHORT",
                entry_price=fill_price,
                quantity=quantity,
                leverage=leverage,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                entry_ts_ms=bar.ts_ms,
                entry_idx=self._state.bar_index,
                strategy_name=route.strategy.name,
                sub_book=route.sub_book,
            )
        except ValueError as e:
            log.warning(
                "shadow_open_rejected",
                strategy=route.strategy.name,
                reason=str(e),
                bar_index=self._state.bar_index,
            )

    def _dump_state(self) -> None:
        if self._state_dump_path is None:
            return
        self._state_dump_path.mkdir(parents=True, exist_ok=True)
        dump = pd.DataFrame({
            "bar_index": range(1, len(self._state.equity_total) + 1),
            "equity_institutional": self._state.equity_institutional,
            "equity_aggressive": self._state.equity_aggressive,
            "equity_total": self._state.equity_total,
            "margin_level": self._state.margin_level,
        })
        dump.to_parquet(self._state_dump_path / "equity.parquet")
        log.info(
            "shadow_state_dumped",
            run_id=self._run_id,
            bars=self._state.bar_index,
            equity=self._state.equity_total[-1] if self._state.equity_total else 0.0,
            trades=len(self._state.trades),
            stop_outs=self._state.broker_stop_outs,
        )

    async def run(self, parquet_path: Path | str) -> ShadowOrchestratorState:
        self.precompute_indicators(parquet_path)
        self._feed.on_candle = self._on_candle
        await self._feed.start()
        self._dump_state()
        return self._state
