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

from src.data.feature_engine import _compute_indicators
from src.strategies.base import BaseStrategy
from src.utils.logger import get_logger
from src.utils.types import SignalAction

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


class BacktestEngine:
    """Event-driven backtest engine that reuses the live strategy code."""

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()

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
                        equity += trade.pnl - trade.commission
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
                        equity += trade.pnl - trade.commission
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
                    equity += trade.pnl - trade.commission
                    position_side = None

                # OPEN new position
                elif action in (SignalAction.LONG, SignalAction.SHORT) and position_side is None:
                    # Entry at next bar's open + slippage
                    if i + 1 < n:
                        entry_price = df.iloc[i + 1]["open"]
                        if action == SignalAction.LONG:
                            entry_price *= (1 + cfg.slippage_pct)
                        else:
                            entry_price *= (1 - cfg.slippage_pct)
                    else:
                        continue  # can't enter on last bar

                    # Position sizing: risk-based with notional cap
                    risk_pct = signal.risk_pct or cfg.risk_per_trade
                    sl = signal.stop_loss
                    if sl and entry_price != sl:
                        risk_per_unit = abs(entry_price - sl)
                        risk_amount = equity * risk_pct
                        quantity = risk_amount / risk_per_unit
                    else:
                        # Fallback: fixed fraction of equity
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
            equity += trade.pnl - trade.commission
            equity_history[-1] = equity

        equity_curve = pd.Series(equity_history, index=df["timestamp"].values)
        metrics = self._compute_metrics(equity_curve, trades, cfg.initial_capital)

        log.info("backtest_complete", trades=len(trades), final_equity=round(equity, 2),
                 return_pct=round(metrics.get("total_return_pct", 0), 2),
                 sharpe=round(metrics.get("sharpe", 0), 3))

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
            total_candles=n,
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

    @staticmethod
    def _compute_metrics(
        equity_curve: pd.Series,
        trades: list[Trade],
        initial_capital: float,
    ) -> dict:
        """Compute standard backtest performance metrics."""
        if len(equity_curve) < 2:
            return {}

        final_equity = equity_curve.iloc[-1]
        total_return = final_equity - initial_capital
        total_return_pct = (total_return / initial_capital) * 100

        # Daily returns for Sharpe/Sortino
        # Convert timestamp index (Unix ms) to datetime and resample to daily
        try:
            equity_dt = equity_curve.copy()
            equity_dt.index = pd.to_datetime(equity_dt.index, unit="ms")
            daily_equity = equity_dt.resample("D").last().dropna()
            daily_returns = daily_equity.pct_change().dropna()
        except Exception:
            # Fallback: use per-candle returns if timestamp conversion fails
            daily_returns = equity_curve.pct_change().dropna()

        avg_return = daily_returns.mean()
        std_return = daily_returns.std()
        downside_returns = daily_returns[daily_returns < 0]
        downside_std = downside_returns.std() if len(downside_returns) > 0 else 0

        # Sharpe ratio (annualized, 365 days for crypto which trades 24/7)
        annualization = np.sqrt(365)
        sharpe = (avg_return / std_return * annualization) if std_return > 0 else 0
        sortino = (avg_return / downside_std * annualization) if downside_std > 0 else 0

        # Max drawdown
        peak = equity_curve.cummax()
        drawdown = (equity_curve - peak) / peak
        max_drawdown = abs(drawdown.min())

        # Trade statistics
        n_trades = len(trades)
        if n_trades > 0:
            winners = [t for t in trades if t.pnl > 0]
            losers = [t for t in trades if t.pnl <= 0]
            win_rate = len(winners) / n_trades * 100
            gross_profit = sum(t.pnl for t in winners) if winners else 0
            gross_loss = abs(sum(t.pnl for t in losers)) if losers else 0
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
            avg_win = gross_profit / len(winners) if winners else 0
            avg_loss = gross_loss / len(losers) if losers else 0
            avg_win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")
        else:
            win_rate = 0
            profit_factor = 0
            avg_win_loss_ratio = 0

        return {
            "initial_capital": initial_capital,
            "final_equity": round(final_equity, 2),
            "total_return": round(total_return, 2),
            "total_return_pct": round(total_return_pct, 2),
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
            "max_drawdown_pct": round(max_drawdown * 100, 2),
            "total_trades": n_trades,
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": round(profit_factor, 3),
            "avg_win_loss_ratio": round(avg_win_loss_ratio, 3),
            "total_commission": round(sum(t.commission for t in trades), 2),
        }
