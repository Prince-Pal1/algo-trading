"""Layer 2 — RSI(2) cross-validation: engine vs reference implementation.
   Layer 3 — Manual trade audit against raw OHLCV data.

The same RSI(2) strategy is run in two ways:
  A) Through our BacktestEngine with RSI2MeanRevStrategy
  B) Through a simple Python loop ("reference") with identical logic

If trade lists match → engine is verified.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.feature_engine import _compute_indicators
from src.strategies.verification.rsi2_mr import RSI2MeanRevStrategy

DATA_PATH = Path("data/historical/BTCUSDT_1h.parquet")
RSI_ENTRY = 10.0
RISK_PCT = 0.02

CFG = BacktestConfig(
    initial_capital=10_000.0,
    commission_pct=0.001,
    slippage_pct=0.0002,
    risk_per_trade=RISK_PCT,
    max_notional_pct=2.0,
)


# ── Reference Implementation ────────────────────────────────────────────
# Simple Python loop, no engine, no framework. "Obviously correct."


def rsi2_reference(df: pd.DataFrame, cfg: BacktestConfig) -> tuple[list[dict], float]:
    """Run RSI(2) mean reversion in a flat Python loop.

    Returns (trades_list, final_equity).
    """
    # Compute indicators using same ta library
    close = df["close"]
    rsi = RSIIndicator(close=close, window=2).rsi()
    sma = SMAIndicator(close=close, window=5).sma_indicator()

    n = len(df)
    equity = cfg.initial_capital
    trades = []
    position = None  # None or dict with entry info

    for i in range(n):
        rsi_val = rsi.iloc[i]
        sma_val = sma.iloc[i]
        close_val = close.iloc[i]

        if pd.isna(rsi_val) or pd.isna(sma_val):
            continue

        # Exit check
        if position is not None and close_val > sma_val:
            if i + 1 < n:
                exit_price = df.iloc[i + 1]["open"] * (1 - cfg.slippage_pct)
            else:
                exit_price = close_val  # end of data

            qty = position["quantity"]
            gross_pnl = (exit_price - position["entry_price"]) * qty
            commission = (position["entry_price"] * qty + exit_price * qty) * cfg.commission_pct
            net_pnl = gross_pnl - commission

            trades.append({
                "entry_idx": position["entry_idx"],
                "exit_idx": i,
                "entry_price": position["entry_price"],
                "exit_price": exit_price,
                "quantity": qty,
                "pnl": net_pnl,
                "commission": commission,
                "side": "LONG",
                "exit_reason": "signal",
            })
            equity += net_pnl
            position = None
            continue  # don't check entry on same bar as exit

        # Entry check
        if position is None and rsi_val < RSI_ENTRY:
            if i + 1 >= n:
                continue  # can't enter on last bar
            entry_price = df.iloc[i + 1]["open"] * (1 + cfg.slippage_pct)

            # Fixed-fraction sizing (no SL)
            qty = (equity * RISK_PCT) / entry_price
            max_qty = (equity * cfg.max_notional_pct) / entry_price
            qty = min(qty, max_qty)

            position = {
                "entry_idx": i,
                "entry_price": entry_price,
                "quantity": qty,
            }

    # Force close at end of data
    if position is not None:
        exit_price = df.iloc[-1]["close"]
        qty = position["quantity"]
        gross_pnl = (exit_price - position["entry_price"]) * qty
        commission = (position["entry_price"] * qty + exit_price * qty) * cfg.commission_pct
        net_pnl = gross_pnl - commission
        trades.append({
            "entry_idx": position["entry_idx"],
            "exit_idx": n - 1,
            "entry_price": position["entry_price"],
            "exit_price": exit_price,
            "quantity": qty,
            "pnl": net_pnl,
            "commission": commission,
            "side": "LONG",
            "exit_reason": "end_of_data",
        })
        equity += net_pnl

    return trades, equity


# ── Layer 2: Cross-Validation ────────────────────────────────────────────


@pytest.mark.skipif(not DATA_PATH.exists(), reason="BTCUSDT_1h.parquet not downloaded")
class TestRSI2CrossValidation:

    @pytest.fixture(autouse=True)
    def setup(self):
        self.df = pd.read_parquet(DATA_PATH)
        # Run engine
        strategy = RSI2MeanRevStrategy(rsi_entry=RSI_ENTRY, risk_pct=RISK_PCT)
        engine = BacktestEngine(CFG)
        self.engine_result = engine.run(
            strategy, self.df,
            symbol="BTCUSDT", timeframe="1h",
            indicators=["rsi_2", "sma_5"],
        )
        # Run reference
        self.ref_trades, self.ref_equity = rsi2_reference(self.df, CFG)

    def test_trade_count_matches(self):
        """Engine and reference produce same number of trades."""
        assert len(self.engine_result.trades) == len(self.ref_trades), \
            f"Engine: {len(self.engine_result.trades)}, Reference: {len(self.ref_trades)}"

    def test_entry_indices_match(self):
        """Every trade enters on the same bar."""
        for i, (et, rt) in enumerate(zip(self.engine_result.trades, self.ref_trades)):
            assert et.entry_idx == rt["entry_idx"], \
                f"Trade {i}: engine entry_idx={et.entry_idx}, ref={rt['entry_idx']}"

    def test_exit_indices_match(self):
        """Every trade exits on the same bar."""
        for i, (et, rt) in enumerate(zip(self.engine_result.trades, self.ref_trades)):
            assert et.exit_idx == rt["exit_idx"], \
                f"Trade {i}: engine exit_idx={et.exit_idx}, ref={rt['exit_idx']}"

    def test_entry_prices_match(self):
        """Entry prices match within floating-point tolerance."""
        for i, (et, rt) in enumerate(zip(self.engine_result.trades, self.ref_trades)):
            assert abs(et.entry_price - rt["entry_price"]) < 1e-6, \
                f"Trade {i}: engine entry={et.entry_price}, ref={rt['entry_price']}"

    def test_exit_prices_match(self):
        """Exit prices match within floating-point tolerance."""
        for i, (et, rt) in enumerate(zip(self.engine_result.trades, self.ref_trades)):
            assert abs(et.exit_price - rt["exit_price"]) < 1e-6, \
                f"Trade {i}: engine exit={et.exit_price}, ref={rt['exit_price']}"

    def test_quantities_match(self):
        """Position sizes match within tolerance (float accumulation)."""
        for i, (et, rt) in enumerate(zip(self.engine_result.trades, self.ref_trades)):
            assert abs(et.quantity - rt["quantity"]) < 1e-4, \
                f"Trade {i}: engine qty={et.quantity}, ref={rt['quantity']}"

    def test_pnl_match(self):
        """Per-trade PnL matches within $0.01."""
        for i, (et, rt) in enumerate(zip(self.engine_result.trades, self.ref_trades)):
            assert abs(et.pnl - rt["pnl"]) < 0.01, \
                f"Trade {i}: engine pnl={et.pnl:.4f}, ref={rt['pnl']:.4f}"

    def test_final_equity_match(self):
        """Final equity matches within 0.1% relative tolerance."""
        engine_eq = self.engine_result.metrics["final_equity"]
        ref_eq = self.ref_equity
        rel_diff = abs(engine_eq - ref_eq) / ref_eq
        assert rel_diff < 0.001, \
            f"Engine equity={engine_eq:.2f}, Ref equity={ref_eq:.2f}, diff={rel_diff*100:.4f}%"

    def test_all_sides_are_long(self):
        """Verify all trades are LONG (long-only strategy)."""
        for t in self.engine_result.trades:
            assert t.side == "LONG"

    def test_sanity_trade_count(self):
        """RSI(2) on 1h crypto should produce a reasonable number of trades."""
        n = len(self.engine_result.trades)
        assert n >= 5, f"Only {n} trades — too few for meaningful verification"

    def test_sanity_win_rate(self):
        """Win rate should be in reasonable range for mean reversion."""
        metrics = self.engine_result.metrics
        wr = metrics["win_rate_pct"]
        # Print for human review (not strict assertion)
        print(f"\n  RSI(2) stats: trades={metrics['total_trades']}, "
              f"WR={wr:.1f}%, PF={metrics['profit_factor']:.2f}, "
              f"return={metrics['total_return_pct']:.2f}%, "
              f"sharpe={metrics['sharpe']:.3f}")
        # Loose sanity — should be somewhere 30-80%
        assert 10 < wr < 90, f"Win rate {wr}% seems unrealistic"


# ── Layer 3: Manual Trade Audit ──────────────────────────────────────────


@pytest.mark.skipif(not DATA_PATH.exists(), reason="BTCUSDT_1h.parquet not downloaded")
class TestManualTradeAudit:
    """Pick specific trades and verify each field against raw OHLCV data."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.df = pd.read_parquet(DATA_PATH)
        strategy = RSI2MeanRevStrategy(rsi_entry=RSI_ENTRY, risk_pct=RISK_PCT)
        engine = BacktestEngine(CFG)
        self.result = engine.run(
            strategy, self.df,
            symbol="BTCUSDT", timeframe="1h",
            indicators=["rsi_2", "sma_5"],
        )

    def _audit_trade(self, trade_idx: int):
        """Manually verify a single trade against raw data."""
        if trade_idx >= len(self.result.trades):
            pytest.skip(f"Only {len(self.result.trades)} trades, skipping index {trade_idx}")

        trade = self.result.trades[trade_idx]
        entry_bar_idx = trade.entry_idx
        exit_bar_idx = trade.exit_idx

        # Entry: should be next-bar open + slippage
        if entry_bar_idx + 1 < len(self.df):
            expected_entry = self.df.iloc[entry_bar_idx + 1]["open"] * (1 + CFG.slippage_pct)
            assert abs(trade.entry_price - expected_entry) < 1e-6, \
                f"Trade {trade_idx}: entry_price={trade.entry_price}, " \
                f"expected next-bar open+slip={expected_entry}"

        # Exit: signal exit → next-bar open - slippage
        if trade.exit_reason == "signal" and exit_bar_idx + 1 < len(self.df):
            expected_exit = self.df.iloc[exit_bar_idx + 1]["open"] * (1 - CFG.slippage_pct)
            assert abs(trade.exit_price - expected_exit) < 1e-6, \
                f"Trade {trade_idx}: exit_price={trade.exit_price}, " \
                f"expected next-bar open-slip={expected_exit}"

        # End-of-data exit → last bar close
        if trade.exit_reason == "end_of_data":
            expected_exit = self.df.iloc[-1]["close"]
            assert abs(trade.exit_price - expected_exit) < 1e-6

        # Commission
        expected_comm = (trade.entry_price * trade.quantity +
                        trade.exit_price * trade.quantity) * CFG.commission_pct
        assert abs(trade.commission - expected_comm) < 0.01, \
            f"Trade {trade_idx}: commission={trade.commission}, expected={expected_comm}"

        # PnL
        gross = (trade.exit_price - trade.entry_price) * trade.quantity
        expected_pnl = gross - expected_comm
        assert abs(trade.pnl - expected_pnl) < 0.01, \
            f"Trade {trade_idx}: pnl={trade.pnl}, expected={expected_pnl}"

    def test_audit_trade_0(self):
        self._audit_trade(0)

    def test_audit_trade_1(self):
        self._audit_trade(1)

    def test_audit_trade_2(self):
        self._audit_trade(2)

    def test_audit_trade_minus_3(self):
        n = len(self.result.trades)
        self._audit_trade(max(0, n - 3))

    def test_audit_trade_minus_2(self):
        n = len(self.result.trades)
        self._audit_trade(max(0, n - 2))

    def test_audit_trade_minus_1(self):
        n = len(self.result.trades)
        self._audit_trade(max(0, n - 1))
