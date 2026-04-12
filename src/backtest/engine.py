"""Event-driven backtest engine — replays historical candles through strategies.

Key principle: calls the exact same strategy.on_features() method as the live pipeline.
The strategy doesn't know it's being backtested.

Usage:
    engine = BacktestEngine()
    result = engine.run(strategy, data, symbol="BTCUSDT", timeframe="1m")
    print(result.metrics)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.backtest.metrics import compute_metrics
from src.data.feature_engine import _compute_indicators
from src.strategies.base import BaseStrategy
from src.utils.logger import get_logger
from src.utils.types import Signal, SignalAction

log = get_logger("backtest")


@dataclass
class BacktestConfig:
    initial_capital: float = 10_000.0
    commission_pct: float = 0.001      # 0.1% per trade (Binance spot)
    slippage_pct: float = 0.0002       # 0.02% slippage
    risk_per_trade: float = 0.01       # 1% equity risk per trade
    max_notional_pct: float = 2.0      # max position notional as multiple of equity


@dataclass
class Trade:
    entry_idx: int
    exit_idx: int
    side: str                  # "LONG" or "SHORT"
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    commission: float
    exit_reason: str           # "signal", "stop_loss", "take_profit"


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    metrics: dict = field(default_factory=dict)
    total_candles: int = 0
    risk_rejections: int = 0
    risk_rejection_reasons: dict = field(default_factory=dict)


class BacktestEngine:
    """Event-driven backtest engine that reuses the live strategy code."""

    def __init__(self, config: BacktestConfig | None = None, risk_manager=None):
        """Args:
            config: Backtest configuration.
            risk_manager: Optional RiskManager instance. When provided, all
                signals are gated through it (same checks as live trading).
        """
        self.config = config or BacktestConfig()
        self._risk_manager = risk_manager

    def run(
        self,
        strategy: BaseStrategy,
        data: pd.DataFrame,
        symbol: str = "BTCUSDT",
        timeframe: str = "1m",
        indicators: list[str] | None = None,
    ) -> BacktestResult:
        """Run backtest on historical data.

        Args:
            strategy: Strategy instance (must implement on_features)
            data: DataFrame with columns: timestamp, open, high, low, close, volume
            symbol: Symbol name for the strategy
            timeframe: Timeframe string
            indicators: List of indicators to compute (default: strategy-appropriate set)

        Returns:
            BacktestResult with trades, equity curve, and metrics
        """
        if indicators is None:
            indicators = ["ema_9", "ema_21", "rsi_7", "bbands_20", "macd", "atr_14"]

        cfg = self.config
        n = len(data)
        if n < 50:
            log.warning("insufficient_data", rows=n, min_required=50)
            return BacktestResult(total_candles=n)

        # ── Step 1: Compute indicators on full dataset (vectorized, fast) ──
        df = data.copy()
        df = _compute_indicators(df, indicators)

        log.info("backtest_start", symbol=symbol, timeframe=timeframe, candles=n,
                 capital=cfg.initial_capital)

        # ── Step 2: Iterate row-by-row ──
        trades: list[Trade] = []
        equity = cfg.initial_capital
        equity_history: list[float] = []
        risk_rejections = 0
        rejection_reasons: dict[str, int] = {}

        # Position state
        position_side: str | None = None   # "LONG" or "SHORT"
        position_entry_price: float = 0
        position_quantity: float = 0
        position_entry_idx: int = 0
        position_stop_loss: float | None = None
        position_take_profit: float | None = None

        for i in range(n):
            row = df.iloc[i]

            # ── Check SL/TP on current bar (before strategy call) ──
            if position_side is not None:
                high = row["high"]
                low = row["low"]

                # Stop loss check
                if position_stop_loss is not None:
                    hit_sl = (position_side == "LONG" and low <= position_stop_loss) or \
                             (position_side == "SHORT" and high >= position_stop_loss)
                    if hit_sl:
                        exit_price = position_stop_loss
                        trade = self._close_position(
                            position_side, position_entry_price, exit_price,
                            position_quantity, position_entry_idx, i, cfg, "stop_loss",
                        )
                        trades.append(trade)
                        equity += trade.pnl  # pnl is already net of commission
                        if self._risk_manager is not None:
                            self._risk_manager.update_trade_close(
                                strategy.name, trade.pnl, symbol)
                        position_side = None
                        strategy._position = "FLAT"

                # Take profit check (only if SL didn't fire)
                if position_side is not None and position_take_profit is not None:
                    hit_tp = (position_side == "LONG" and high >= position_take_profit) or \
                             (position_side == "SHORT" and low <= position_take_profit)
                    if hit_tp:
                        exit_price = position_take_profit
                        trade = self._close_position(
                            position_side, position_entry_price, exit_price,
                            position_quantity, position_entry_idx, i, cfg, "take_profit",
                        )
                        trades.append(trade)
                        equity += trade.pnl  # pnl is already net of commission
                        if self._risk_manager is not None:
                            self._risk_manager.update_trade_close(
                                strategy.name, trade.pnl, symbol)
                        position_side = None
                        strategy._position = "FLAT"

            # ── Call strategy (same as live pipeline) ──
            signal = strategy.process(symbol, timeframe, row)

            if signal is not None:
                action = signal.action

                # CLOSE existing position
                if action == SignalAction.CLOSE and position_side is not None:
                    # Exit at next bar's open + slippage (if available)
                    if i + 1 < n:
                        exit_price = df.iloc[i + 1]["open"]
                        exit_price *= (1 - cfg.slippage_pct) if position_side == "LONG" else (1 + cfg.slippage_pct)
                    else:
                        exit_price = row["close"]

                    trade = self._close_position(
                        position_side, position_entry_price, exit_price,
                        position_quantity, position_entry_idx, i, cfg, "signal",
                    )
                    trades.append(trade)
                    equity += trade.pnl  # pnl is already net of commission
                    if self._risk_manager is not None:
                        self._risk_manager.update_trade_close(
                            signal.strategy_name, trade.pnl, symbol)
                    position_side = None

                # OPEN new position
                elif action in (SignalAction.LONG, SignalAction.SHORT) and position_side is None:
                    if i + 1 < n:
                        # Entry at next bar's open + slippage
                        entry_price = df.iloc[i + 1]["open"]
                        if action == SignalAction.LONG:
                            entry_price *= (1 + cfg.slippage_pct)
                        else:
                            entry_price *= (1 - cfg.slippage_pct)

                        # Gate through risk manager if present
                        if self._risk_manager is not None:
                            # Build signal with entry price for risk evaluation
                            # Use candle timestamp so duplicate filter works in backtests
                            sig_ts = int(row["timestamp"]) if "timestamp" in df.columns else 0
                            risk_signal = Signal(
                                symbol=symbol, action=signal.action,
                                confidence=signal.confidence,
                                strategy_name=signal.strategy_name,
                                timeframe=signal.timeframe,
                                entry_price=entry_price,
                                stop_loss=signal.stop_loss,
                                take_profit=signal.take_profit,
                                risk_pct=signal.risk_pct or cfg.risk_per_trade,
                                metadata=signal.metadata,
                                timestamp=sig_ts or signal.timestamp,
                            )
                            self._risk_manager.state.update_equity(equity)
                            decision = self._risk_manager.evaluate(risk_signal)
                            if not decision.approved:
                                risk_rejections += 1
                                key = decision.reason.split(":")[0]
                                rejection_reasons[key] = rejection_reasons.get(key, 0) + 1
                                equity_history.append(equity)
                                continue
                            # Use risk-manager-computed quantity
                            if decision.adjusted_quantity and decision.adjusted_quantity > 0:
                                quantity = decision.adjusted_quantity
                            else:
                                equity_history.append(equity)
                                continue
                        else:
                            # Original sizing: risk-based with notional cap
                            risk_pct = signal.risk_pct or cfg.risk_per_trade
                            sl = signal.stop_loss
                            if sl and entry_price != sl:
                                risk_per_unit = abs(entry_price - sl)
                                risk_amount = equity * risk_pct
                                quantity = risk_amount / risk_per_unit
                            else:
                                quantity = (equity * risk_pct) / entry_price

                        # Cap: notional value cannot exceed max_notional_pct × equity
                        max_qty = (equity * cfg.max_notional_pct) / entry_price
                        quantity = min(quantity, max_qty)

                        position_side = action.value
                        position_entry_price = entry_price
                        position_quantity = quantity
                        position_entry_idx = i
                        position_stop_loss = signal.stop_loss
                        position_take_profit = signal.take_profit

            equity_history.append(equity)

        # ── Close any open position at end ──
        if position_side is not None:
            exit_price = df.iloc[-1]["close"]
            trade = self._close_position(
                position_side, position_entry_price, exit_price,
                position_quantity, position_entry_idx, n - 1, cfg, "end_of_data",
            )
            trades.append(trade)
            equity += trade.pnl  # pnl is already net of commission
            if self._risk_manager is not None:
                self._risk_manager.update_trade_close(
                    strategy.name, trade.pnl, symbol)
            equity_history[-1] = equity

        equity_curve = pd.Series(equity_history, index=df["timestamp"].values)
        metrics = compute_metrics(equity_curve, trades, cfg.initial_capital)

        log.info("backtest_complete", trades=len(trades), final_equity=round(equity, 2),
                 return_pct=round(metrics.get("total_return_pct", 0), 2),
                 sharpe=round(metrics.get("sharpe", 0), 3))

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
            total_candles=n,
            risk_rejections=risk_rejections,
            risk_rejection_reasons=rejection_reasons,
        )

    @staticmethod
    def _close_position(
        side: str,
        entry_price: float,
        exit_price: float,
        quantity: float,
        entry_idx: int,
        exit_idx: int,
        cfg: BacktestConfig,
        reason: str,
    ) -> Trade:
        if side == "LONG":
            pnl = (exit_price - entry_price) * quantity
        else:
            pnl = (entry_price - exit_price) * quantity

        commission = (entry_price * quantity + exit_price * quantity) * cfg.commission_pct
        net_pnl = pnl - commission
        pnl_pct = net_pnl / (entry_price * quantity) if entry_price * quantity > 0 else 0

        return Trade(
            entry_idx=entry_idx,
            exit_idx=exit_idx,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            pnl=net_pnl,
            pnl_pct=pnl_pct,
            commission=commission,
            exit_reason=reason,
        )

