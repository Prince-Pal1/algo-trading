"""Cointegration-based Pairs Trading Strategy.

References:
    - Optimized Cointegration: https://onlinelibrary.wiley.com/doi/full/10.1002/fut.70018
      (Sharpe 3.97, Calmar, MDD 7.94%, Jan 2019-May 2024)
    - Copula Enhancement: https://arxiv.org/abs/2305.06961
    - Erasmus Thesis: https://thesis.eur.nl/pub/47732
      (Johansen method: 6.81% weekly return incl. transaction costs)

Design:
    1. Test cointegration between two price series (Johansen test)
    2. Compute hedge ratio from cointegrating vector
    3. Build spread = price_A - hedge_ratio * price_B
    4. Normalize spread to z-score using rolling window
    5. Enter when |z| > entry_z, exit when |z| < exit_z
    6. Market-neutral: long one asset, short the other

This strategy requires a custom backtester since our main engine
handles single-asset only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from statsmodels.tsa.vector_ar.vecm import coint_johansen
from statsmodels.tsa.stattools import adfuller


@dataclass
class PairsConfig:
    """Configuration for pairs trading strategy."""
    entry_z: float = 2.0            # Enter when |z-score| > this
    exit_z: float = 0.5             # Exit when |z-score| < this
    stop_z: float = 4.0             # Stop loss at extreme z-score
    lookback: int = 168             # Rolling window for z-score (168h = 7 days)
    recalibrate_interval: int = 720 # Re-estimate hedge ratio every 720 candles (30 days)
    min_half_life: int = 5          # Min half-life of mean reversion (candles)
    max_half_life: int = 168        # Max half-life (1 week of hourly candles)
    commission_pct: float = 0.0004  # 0.04% per trade per side
    initial_capital: float = 10_000.0
    risk_per_trade: float = 0.02    # 2% equity risk per trade


@dataclass
class PairsTrade:
    """A single pairs trade record."""
    entry_idx: int
    exit_idx: int
    entry_z: float
    exit_z: float
    side: str               # "long_spread" or "short_spread"
    pnl: float
    pnl_pct: float
    commission: float
    holding_bars: int
    exit_reason: str         # "mean_revert", "stop_loss", "timeout", "end_of_data"


@dataclass
class PairsResult:
    """Result of a pairs trading backtest."""
    pair: str                       # e.g., "BTCUSDT-ETHUSDT"
    trades: list[PairsTrade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    metrics: dict = field(default_factory=dict)
    cointegration_stats: dict = field(default_factory=dict)


def test_cointegration(price_a: pd.Series, price_b: pd.Series) -> dict:
    """Johansen cointegration test for a crypto pair.

    Returns dict with 'cointegrated', 'hedge_ratio', 'spread',
    'trace_stat', 'critical_value', 'half_life', 'adf_pvalue'.
    """
    data = pd.concat([price_a, price_b], axis=1).dropna()
    if len(data) < 100:
        return {"cointegrated": False, "reason": "insufficient_data"}

    try:
        result = coint_johansen(data, det_order=0, k_ar_diff=1)
    except Exception as e:
        return {"cointegrated": False, "reason": str(e)}

    # Check trace statistic at 5% significance
    trace_stat = result.lr1[0]
    critical_value = result.cvt[0, 1]  # 5% critical value
    is_cointegrated = trace_stat > critical_value

    # Hedge ratio from eigenvector
    hedge_ratio = result.evec[1, 0] / result.evec[0, 0]

    # Spread
    spread = price_a.values - hedge_ratio * price_b.values
    spread_series = pd.Series(spread, index=price_a.index)

    # ADF test on spread
    adf_result = adfuller(spread_series.dropna(), maxlag=20, regression="c")
    adf_pvalue = adf_result[1]

    # Half-life of mean reversion (OU process)
    half_life = _estimate_half_life(spread_series.dropna())

    return {
        "cointegrated": is_cointegrated,
        "hedge_ratio": hedge_ratio,
        "spread": spread_series,
        "trace_stat": trace_stat,
        "critical_value": critical_value,
        "adf_pvalue": adf_pvalue,
        "half_life": half_life,
    }


def _estimate_half_life(spread: pd.Series) -> float:
    """Estimate half-life of mean reversion via OLS on lagged spread."""
    spread_lag = spread.shift(1)
    delta = spread - spread_lag
    df = pd.DataFrame({"delta": delta, "lag": spread_lag}).dropna()
    if len(df) < 20:
        return float("inf")

    # OLS: delta_spread = alpha + beta * spread_lag
    # Half-life = -ln(2) / beta
    X = df["lag"].values
    y = df["delta"].values
    X_mean = X.mean()
    y_mean = y.mean()
    beta = np.sum((X - X_mean) * (y - y_mean)) / np.sum((X - X_mean) ** 2)

    if beta >= 0:
        return float("inf")  # Not mean-reverting

    half_life = -np.log(2) / beta
    return half_life


class PairsBacktester:
    """Backtester specifically for pairs trading strategies.

    Handles two assets simultaneously, position management for both legs,
    and commission on both sides.
    """

    def __init__(self, config: PairsConfig | None = None):
        self.config = config or PairsConfig()

    def run(
        self,
        price_a: pd.DataFrame,
        price_b: pd.DataFrame,
        symbol_a: str = "BTCUSDT",
        symbol_b: str = "ETHUSDT",
    ) -> PairsResult:
        """Run pairs trading backtest.

        Args:
            price_a: OHLCV DataFrame for asset A (must have 'close', 'timestamp')
            price_b: OHLCV DataFrame for asset B (same timestamps)

        Returns:
            PairsResult with trades, equity curve, and metrics
        """
        cfg = self.config
        pair_name = f"{symbol_a}-{symbol_b}"

        # Align timestamps
        merged = pd.DataFrame({
            "close_a": price_a.set_index("timestamp")["close"],
            "close_b": price_b.set_index("timestamp")["close"],
        }).dropna()

        if len(merged) < cfg.lookback + 100:
            return PairsResult(pair=pair_name, cointegration_stats={"error": "insufficient_data"})

        close_a = merged["close_a"]
        close_b = merged["close_b"]
        n = len(merged)

        # Initial cointegration test
        coint_result = test_cointegration(close_a, close_b)
        if not coint_result.get("cointegrated", False):
            return PairsResult(pair=pair_name, cointegration_stats=coint_result)

        hedge_ratio = coint_result["hedge_ratio"]
        half_life = coint_result.get("half_life", 50)

        # Check half-life bounds
        if half_life < cfg.min_half_life or half_life > cfg.max_half_life:
            coint_result["rejected_reason"] = f"half_life={half_life:.1f} outside [{cfg.min_half_life}, {cfg.max_half_life}]"
            return PairsResult(pair=pair_name, cointegration_stats=coint_result)

        # Backtest
        equity = cfg.initial_capital
        equity_history = []
        trades: list[PairsTrade] = []

        # Position state
        position: str | None = None  # "long_spread" or "short_spread"
        entry_idx = 0
        entry_z_score = 0.0
        entry_price_a = 0.0
        entry_price_b = 0.0
        qty_a = 0.0
        qty_b = 0.0
        last_calibration = 0

        for i in range(n):
            pa = close_a.iloc[i]
            pb = close_b.iloc[i]

            # Re-calibrate hedge ratio periodically
            if i - last_calibration >= cfg.recalibrate_interval and i > cfg.lookback:
                window_a = close_a.iloc[max(0, i - cfg.lookback * 4):i]
                window_b = close_b.iloc[max(0, i - cfg.lookback * 4):i]
                recalib = test_cointegration(window_a, window_b)
                if recalib.get("cointegrated", False):
                    hedge_ratio = recalib["hedge_ratio"]
                    last_calibration = i

            # Compute spread and z-score
            if i < cfg.lookback:
                equity_history.append(equity)
                continue

            spread_window = (
                close_a.iloc[i - cfg.lookback:i + 1].values
                - hedge_ratio * close_b.iloc[i - cfg.lookback:i + 1].values
            )
            spread_mean = np.mean(spread_window[:-1])
            spread_std = np.std(spread_window[:-1])

            if spread_std < 1e-10:
                equity_history.append(equity)
                continue

            current_spread = pa - hedge_ratio * pb
            z_score = (current_spread - spread_mean) / spread_std

            # Position management
            if position is not None:
                holding_bars = i - entry_idx

                # Calculate current P&L
                if position == "long_spread":
                    # Long A, Short B
                    pnl_a = (pa - entry_price_a) * qty_a
                    pnl_b = (entry_price_b - pb) * qty_b
                elif position == "short_spread":
                    # Short A, Long B
                    pnl_a = (entry_price_a - pa) * qty_a
                    pnl_b = (pb - entry_price_b) * qty_b
                else:
                    pnl_a = pnl_b = 0

                unrealized_pnl = pnl_a + pnl_b

                # Exit conditions
                should_exit = False
                exit_reason = ""

                # Mean reversion exit
                if position == "long_spread" and z_score <= cfg.exit_z:
                    should_exit = True
                    exit_reason = "mean_revert"
                elif position == "short_spread" and z_score >= -cfg.exit_z:
                    should_exit = True
                    exit_reason = "mean_revert"

                # Stop loss
                if position == "long_spread" and z_score < -cfg.stop_z:
                    should_exit = True
                    exit_reason = "stop_loss"
                elif position == "short_spread" and z_score > cfg.stop_z:
                    should_exit = True
                    exit_reason = "stop_loss"

                if should_exit:
                    # Commission on exit
                    exit_comm = (pa * qty_a + pb * qty_b) * cfg.commission_pct
                    net_pnl = unrealized_pnl - exit_comm
                    notional = entry_price_a * qty_a + entry_price_b * qty_b

                    trades.append(PairsTrade(
                        entry_idx=entry_idx, exit_idx=i,
                        entry_z=entry_z_score, exit_z=z_score,
                        side=position,
                        pnl=net_pnl,
                        pnl_pct=net_pnl / notional * 100 if notional > 0 else 0,
                        commission=exit_comm + (entry_price_a * qty_a + entry_price_b * qty_b) * cfg.commission_pct,
                        holding_bars=holding_bars,
                        exit_reason=exit_reason,
                    ))
                    equity += net_pnl
                    position = None

            # Entry conditions (only if flat)
            if position is None and i >= cfg.lookback:
                if z_score > cfg.entry_z:
                    # Spread is too high → short spread (short A, long B)
                    position = "short_spread"
                    entry_idx = i
                    entry_z_score = z_score
                    entry_price_a = pa
                    entry_price_b = pb

                    # Position sizing: allocate risk_per_trade * equity to each leg
                    alloc = equity * cfg.risk_per_trade
                    qty_a = alloc / (2 * pa)
                    qty_b = alloc / (2 * pb) * abs(hedge_ratio)

                    # Entry commission
                    entry_comm = (pa * qty_a + pb * qty_b) * cfg.commission_pct
                    equity -= entry_comm

                elif z_score < -cfg.entry_z:
                    # Spread is too low → long spread (long A, short B)
                    position = "long_spread"
                    entry_idx = i
                    entry_z_score = z_score
                    entry_price_a = pa
                    entry_price_b = pb

                    alloc = equity * cfg.risk_per_trade
                    qty_a = alloc / (2 * pa)
                    qty_b = alloc / (2 * pb) * abs(hedge_ratio)

                    entry_comm = (pa * qty_a + pb * qty_b) * cfg.commission_pct
                    equity -= entry_comm

            equity_history.append(equity)

        # Close any open position at end
        if position is not None:
            pa = close_a.iloc[-1]
            pb = close_b.iloc[-1]
            if position == "long_spread":
                pnl = (pa - entry_price_a) * qty_a + (entry_price_b - pb) * qty_b
            else:
                pnl = (entry_price_a - pa) * qty_a + (pb - entry_price_b) * qty_b

            exit_comm = (pa * qty_a + pb * qty_b) * cfg.commission_pct
            net_pnl = pnl - exit_comm
            notional = entry_price_a * qty_a + entry_price_b * qty_b

            trades.append(PairsTrade(
                entry_idx=entry_idx, exit_idx=n - 1,
                entry_z=entry_z_score, exit_z=0,
                side=position,
                pnl=net_pnl,
                pnl_pct=net_pnl / notional * 100 if notional > 0 else 0,
                commission=exit_comm,
                holding_bars=n - 1 - entry_idx,
                exit_reason="end_of_data",
            ))
            equity += net_pnl

        # Build equity curve
        timestamps = merged.index.values[:len(equity_history)]
        equity_curve = pd.Series(equity_history, index=timestamps)

        # Compute metrics
        metrics = self._compute_metrics(equity_curve, trades, cfg.initial_capital)

        return PairsResult(
            pair=pair_name,
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
            cointegration_stats={
                "hedge_ratio": hedge_ratio,
                "half_life": half_life,
                "trace_stat": coint_result.get("trace_stat"),
                "adf_pvalue": coint_result.get("adf_pvalue"),
            },
        )

    @staticmethod
    def _compute_metrics(
        equity: pd.Series, trades: list[PairsTrade], initial_capital: float
    ) -> dict:
        """Compute standard performance metrics for the pairs strategy."""
        if len(equity) < 2 or not trades:
            return {"total_trades": len(trades), "total_return_pct": 0}

        returns = equity.pct_change().dropna()
        total_return = (equity.iloc[-1] / initial_capital - 1) * 100

        # Sharpe (hourly → annualized)
        if returns.std() > 1e-10:
            sharpe = returns.mean() / returns.std() * np.sqrt(8760)
        else:
            sharpe = 0.0

        # Sortino
        downside = returns[returns < 0]
        if len(downside) > 0 and downside.std() > 1e-10:
            sortino = returns.mean() / downside.std() * np.sqrt(8760)
        else:
            sortino = 0.0

        # Max drawdown
        cummax = equity.cummax()
        drawdown = (equity - cummax) / cummax * 100
        max_dd = abs(drawdown.min())

        # Trade stats
        pnls = [t.pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / len(pnls) * 100 if pnls else 0

        if losses and sum(abs(l) for l in losses) > 0:
            profit_factor = sum(wins) / sum(abs(l) for l in losses) if wins else 0
        else:
            profit_factor = float("inf") if wins else 0

        avg_holding = np.mean([t.holding_bars for t in trades]) if trades else 0
        total_commission = sum(t.commission for t in trades)

        return {
            "initial_capital": initial_capital,
            "final_equity": equity.iloc[-1],
            "total_return_pct": round(total_return, 2),
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
            "max_drawdown_pct": round(max_dd, 2),
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": round(profit_factor, 3),
            "total_trades": len(trades),
            "avg_holding_bars": round(avg_holding, 1),
            "total_commission": round(total_commission, 2),
            "mean_revert_exits": sum(1 for t in trades if t.exit_reason == "mean_revert"),
            "stop_loss_exits": sum(1 for t in trades if t.exit_reason == "stop_loss"),
        }
