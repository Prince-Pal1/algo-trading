"""Layer 1 — Deterministic unit tests for the backtest engine.

Each test uses synthetic data with known prices and ProgrammableStrategy
to verify a specific engine mechanic in isolation. No network, no indicators.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.backtest.metrics import compute_metrics
from src.utils.types import SignalAction

# Import helpers from conftest (auto-discovered by pytest)
from tests.conftest import ProgrammableStrategy, make_signal, make_ohlcv


# ── Test 1: Long trade PnL, zero costs ──────────────────────────────────

def test_long_trade_pnl_zero_costs(zero_cost_config):
    """LONG signal on bar 2, CLOSE on bar 5 → PnL = (exit - entry) * qty."""
    # Prices: bars 0-9 with opens that set clear entry/exit
    closes = [100, 100, 100, 105, 110, 115, 120, 120, 120, 120]
    opens  = [100, 100, 100, 105, 110, 115, 120, 120, 120, 120]
    highs  = [101] * 10  # high enough to never trigger any TP
    lows   = [99] * 10   # low enough but never trigger SL
    # Override highs/lows to be safe
    highs = [max(o, c) + 1 for o, c in zip(opens, closes)]
    lows  = [min(o, c) - 1 for o, c in zip(opens, closes)]
    df = make_ohlcv(closes, opens, highs, lows)

    # Signal LONG on bar 2 → fills on bar 3 open (105)
    # Signal CLOSE on bar 5 → fills on bar 6 open (120)
    signals = {
        2: make_signal(SignalAction.LONG, risk_pct=0.01),
        5: make_signal(SignalAction.CLOSE),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(zero_cost_config)
    result = engine.run(strategy, df, indicators=[])

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.side == "LONG"
    assert trade.entry_price == 105.0  # bar 3 open, no slippage
    assert trade.exit_price == 120.0   # bar 6 open, no slippage
    assert trade.exit_reason == "signal"

    # qty = (10000 * 0.01) / 105 = 0.952380...
    expected_qty = (10_000 * 0.01) / 105.0
    assert abs(trade.quantity - expected_qty) < 1e-8

    expected_pnl = (120.0 - 105.0) * expected_qty
    assert abs(trade.pnl - expected_pnl) < 0.01

    expected_equity = 10_000 + expected_pnl
    assert abs(result.metrics["final_equity"] - expected_equity) < 0.01


# ── Test 2: Long trade with commission ──────────────────────────────────

def test_long_trade_with_commission():
    """Verify commission is subtracted exactly once, not twice."""
    cfg = BacktestConfig(
        commission_pct=0.001,  # 0.1%
        slippage_pct=0.0,      # no slippage for clarity
        risk_per_trade=0.01,
        max_notional_pct=2.0,
    )
    closes = [100, 100, 100, 105, 110, 115, 120, 120, 120, 120]
    opens  = [100, 100, 100, 105, 110, 115, 120, 120, 120, 120]
    highs  = [max(o, c) + 5 for o, c in zip(opens, closes)]
    lows   = [min(o, c) - 5 for o, c in zip(opens, closes)]
    df = make_ohlcv(closes, opens, highs, lows)

    signals = {
        2: make_signal(SignalAction.LONG, risk_pct=0.01),
        5: make_signal(SignalAction.CLOSE),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(cfg)
    result = engine.run(strategy, df, indicators=[])

    trade = result.trades[0]
    entry, exit_ = 105.0, 120.0
    qty = (10_000 * 0.01) / entry
    gross_pnl = (exit_ - entry) * qty
    commission = (entry * qty + exit_ * qty) * 0.001
    net_pnl = gross_pnl - commission

    assert abs(trade.pnl - net_pnl) < 0.01, f"trade.pnl={trade.pnl}, expected net_pnl={net_pnl}"
    assert abs(trade.commission - commission) < 0.01

    expected_equity = 10_000 + net_pnl
    assert abs(result.metrics["final_equity"] - expected_equity) < 0.01, \
        f"equity={result.metrics['final_equity']}, expected={expected_equity} (double-commission bug?)"


# ── Test 3: Short trade PnL ─────────────────────────────────────────────

def test_short_trade_pnl(zero_cost_config):
    """SHORT signal → PnL = (entry - exit) * qty."""
    closes = [100, 100, 100, 95, 90, 85, 80, 80, 80, 80]
    opens  = [100, 100, 100, 95, 90, 85, 80, 80, 80, 80]
    highs  = [max(o, c) + 5 for o, c in zip(opens, closes)]
    lows   = [min(o, c) - 5 for o, c in zip(opens, closes)]
    df = make_ohlcv(closes, opens, highs, lows)

    signals = {
        2: make_signal(SignalAction.SHORT, risk_pct=0.01),
        5: make_signal(SignalAction.CLOSE),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(zero_cost_config)
    result = engine.run(strategy, df, indicators=[])

    trade = result.trades[0]
    assert trade.side == "SHORT"
    assert trade.entry_price == 95.0  # bar 3 open, SHORT fills at open (no slippage)
    assert trade.exit_price == 80.0   # bar 6 open

    qty = (10_000 * 0.01) / 95.0
    expected_pnl = (95.0 - 80.0) * qty
    assert abs(trade.pnl - expected_pnl) < 0.01


# ── Test 4: Slippage on entries and signal exits ────────────────────────

def test_slippage_applied_correctly():
    """Entry and signal-exit prices include slippage."""
    cfg = BacktestConfig(
        commission_pct=0.0,
        slippage_pct=0.001,  # 0.1% for easy math
        risk_per_trade=0.01,
        max_notional_pct=2.0,
    )
    closes = [100] * 10
    opens  = [100] * 10
    highs  = [105] * 10
    lows   = [95] * 10
    df = make_ohlcv(closes, opens, highs, lows)

    signals = {
        2: make_signal(SignalAction.LONG, risk_pct=0.01),
        5: make_signal(SignalAction.CLOSE),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(cfg)
    result = engine.run(strategy, df, indicators=[])

    trade = result.trades[0]
    # LONG entry: open * (1 + slippage) = 100 * 1.001 = 100.1
    assert abs(trade.entry_price - 100.1) < 1e-8
    # LONG exit (signal): open * (1 - slippage) = 100 * 0.999 = 99.9
    assert abs(trade.exit_price - 99.9) < 1e-8


# ── Test 5: Stop loss triggers at exact price ───────────────────────────

def test_stop_loss_triggers_at_exact_price(zero_cost_config):
    """SL set at 96, bar dips to low=95 → exit at exactly 96."""
    closes = [100, 100, 100, 100, 90, 100, 100, 100, 100, 100]
    opens  = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
    highs  = [101, 101, 101, 101, 101, 101, 101, 101, 101, 101]
    lows   = [99,  99,  99,  99,  88,  99,  99,  99,  99,  99]  # bar 4 low=88
    df = make_ohlcv(closes, opens, highs, lows)

    # LONG on bar 1, with SL at 96
    signals = {
        1: make_signal(SignalAction.LONG, stop_loss=96.0, risk_pct=0.01),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(zero_cost_config)
    result = engine.run(strategy, df, indicators=[])

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == 96.0  # exact SL price, not 88 (the low)


# ── Test 6: Take profit triggers at exact price ─────────────────────────

def test_take_profit_triggers_at_exact_price(zero_cost_config):
    """TP set at 110, bar high=115 → exit at exactly 110."""
    closes = [100, 100, 100, 100, 108, 100, 100, 100, 100, 100]
    opens  = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
    highs  = [101, 101, 101, 101, 115, 101, 101, 101, 101, 101]  # bar 4 high=115
    lows   = [99,  99,  99,  99,  99,  99,  99,  99,  99,  99]
    df = make_ohlcv(closes, opens, highs, lows)

    # LONG on bar 1, with TP at 110
    signals = {
        1: make_signal(SignalAction.LONG, take_profit=110.0, risk_pct=0.01),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(zero_cost_config)
    result = engine.run(strategy, df, indicators=[])

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "take_profit"
    assert trade.exit_price == 110.0  # exact TP price, not 115


# ── Test 7: SL checked before TP ────────────────────────────────────────

def test_sl_checked_before_tp(zero_cost_config):
    """When both SL and TP are breached on same bar, SL fires."""
    closes = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
    opens  = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
    highs  = [101, 101, 101, 101, 115, 101, 101, 101, 101, 101]  # bar 4 high=115
    lows   = [99,  99,  99,  99,  88,  99,  99,  99,  99,  99]   # bar 4 low=88
    df = make_ohlcv(closes, opens, highs, lows)

    # LONG on bar 1, SL=95, TP=110 — both breach on bar 4
    signals = {
        1: make_signal(SignalAction.LONG, stop_loss=95.0, take_profit=110.0, risk_pct=0.01),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(zero_cost_config)
    result = engine.run(strategy, df, indicators=[])

    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == 95.0


# ── Test 8: SL/TP fills have no slippage ────────────────────────────────

def test_sl_tp_no_slippage():
    """SL/TP exit at exact price — slippage only applies to signal exits."""
    cfg = BacktestConfig(
        commission_pct=0.0,
        slippage_pct=0.01,  # 1% — exaggerated to make any error obvious
        risk_per_trade=0.01,
        max_notional_pct=2.0,
    )
    closes = [100, 100, 100, 100, 90, 100, 100, 100, 100, 100]
    opens  = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
    highs  = [101] * 10
    lows   = [99,  99,  99,  99,  85,  99,  99,  99,  99,  99]
    df = make_ohlcv(closes, opens, highs, lows)

    signals = {
        1: make_signal(SignalAction.LONG, stop_loss=96.0, risk_pct=0.01),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(cfg)
    result = engine.run(strategy, df, indicators=[])

    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    # SL exit is at exact 96.0, NOT 96.0 * (1 - 0.01)
    assert trade.exit_price == 96.0
    # But entry had slippage: 100 * 1.01 = 101.0
    assert abs(trade.entry_price - 101.0) < 1e-8


# ── Test 9: Risk-based position sizing ──────────────────────────────────

def test_position_sizing_risk_based(zero_cost_config):
    """qty = (equity * risk_pct) / |entry - SL|."""
    closes = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
    opens  = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
    highs  = [105] * 10
    lows   = [95] * 10
    df = make_ohlcv(closes, opens, highs, lows)

    # LONG at 100, SL at 95 → risk per unit = 5
    # risk_pct=0.01, equity=10000 → risk_amount=100 → qty = 100/5 = 20
    signals = {
        1: make_signal(SignalAction.LONG, stop_loss=95.0, risk_pct=0.01),
        5: make_signal(SignalAction.CLOSE),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(zero_cost_config)
    result = engine.run(strategy, df, indicators=[])

    trade = result.trades[0]
    expected_qty = (10_000 * 0.01) / abs(100.0 - 95.0)  # = 20.0
    assert abs(trade.quantity - expected_qty) < 1e-6, \
        f"qty={trade.quantity}, expected={expected_qty}"


# ── Test 10: Max notional cap ───────────────────────────────────────────

def test_position_sizing_max_notional_cap():
    """Very tight SL → huge risk-based qty → capped by max_notional_pct."""
    cfg = BacktestConfig(
        commission_pct=0.0,
        slippage_pct=0.0,
        risk_per_trade=0.05,     # 5% risk
        max_notional_pct=1.0,    # max position = 1x equity
    )
    closes = [100] * 10
    opens  = [100] * 10
    highs  = [105] * 10
    lows   = [95] * 10
    df = make_ohlcv(closes, opens, highs, lows)

    # SL very tight: entry 100, SL 99.99 → risk_per_unit = 0.01
    # risk_amount = 10000 * 0.05 = 500 → qty = 500 / 0.01 = 50000
    # But max_qty = (10000 * 1.0) / 100 = 100
    signals = {
        1: make_signal(SignalAction.LONG, stop_loss=99.99, risk_pct=0.05),
        5: make_signal(SignalAction.CLOSE),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(cfg)
    result = engine.run(strategy, df, indicators=[])

    trade = result.trades[0]
    max_qty = (10_000 * 1.0) / 100.0  # = 100
    assert abs(trade.quantity - max_qty) < 1e-6, \
        f"qty={trade.quantity}, expected max_qty={max_qty}"


# ── Test 11: Multi-trade equity accumulation ────────────────────────────

def test_multi_trade_equity_accumulation():
    """3 trades (win, loss, win) → final equity matches hand calculation."""
    cfg = BacktestConfig(
        commission_pct=0.001,
        slippage_pct=0.0,
        risk_per_trade=0.02,
        max_notional_pct=2.0,
    )
    # Design 3 trades:
    # Trade 1: LONG bar 1, CLOSE bar 3 (win: 100→110)
    # Trade 2: LONG bar 4, CLOSE bar 6 (loss: 110→105)
    # Trade 3: LONG bar 7, CLOSE bar 9 (win: 105→115)
    opens  = [100, 100, 100, 110, 110, 110, 105, 105, 105, 115]
    closes = [100, 100, 100, 110, 110, 110, 105, 105, 105, 115]
    highs  = [c + 5 for c in closes]
    lows   = [c - 5 for c in closes]

    # Need 11 bars (signals + next-bar fills for trade 3 close)
    opens.append(115)
    closes.append(115)
    highs.append(120)
    lows.append(110)

    df = make_ohlcv(closes, opens, highs, lows)

    signals = {
        1: make_signal(SignalAction.LONG, risk_pct=0.02),
        3: make_signal(SignalAction.CLOSE),
        4: make_signal(SignalAction.LONG, risk_pct=0.02),
        6: make_signal(SignalAction.CLOSE),
        7: make_signal(SignalAction.LONG, risk_pct=0.02),
        9: make_signal(SignalAction.CLOSE),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(cfg)
    result = engine.run(strategy, df, indicators=[])

    assert len(result.trades) == 3

    # Manually compute equity after each trade
    equity = 10_000.0

    # Trade 1: entry=100 (bar 2 open), exit=110 (bar 4 open)
    e1, x1 = 100.0, 110.0
    q1 = (equity * 0.02) / e1  # no SL → fixed fraction
    q1 = min(q1, (equity * 2.0) / e1)
    pnl1 = (x1 - e1) * q1
    comm1 = (e1 * q1 + x1 * q1) * 0.001
    equity += pnl1 - comm1

    # Trade 2: entry=110 (bar 5 open), exit=105 (bar 7 open)
    e2, x2 = 110.0, 105.0
    q2 = (equity * 0.02) / e2
    q2 = min(q2, (equity * 2.0) / e2)
    pnl2 = (x2 - e2) * q2
    comm2 = (e2 * q2 + x2 * q2) * 0.001
    equity += pnl2 - comm2

    # Trade 3: entry=105 (bar 8 open), exit=115 (bar 10 open)
    e3, x3 = 105.0, 115.0
    q3 = (equity * 0.02) / e3
    q3 = min(q3, (equity * 2.0) / e3)
    pnl3 = (x3 - e3) * q3
    comm3 = (e3 * q3 + x3 * q3) * 0.001
    equity += pnl3 - comm3

    assert abs(result.metrics["final_equity"] - equity) < 0.01, \
        f"engine equity={result.metrics['final_equity']}, expected={equity}"


# ── Test 12: Sharpe ratio on known returns ──────────────────────────────

def test_sharpe_ratio_known_returns():
    """Verify Sharpe computation against a hand-calculated value."""
    engine = BacktestEngine()

    # Create an equity curve with known daily values
    # 5 days: 10000, 10100, 10050, 10200, 10150
    # Use timestamps one day apart (86400000 ms)
    start_ts = 1_735_689_600_000  # 2025-01-01 00:00 UTC
    day_ms = 86_400_000
    timestamps = [start_ts + i * day_ms for i in range(5)]
    equity_values = [10000.0, 10100.0, 10050.0, 10200.0, 10150.0]
    equity_curve = pd.Series(equity_values, index=timestamps)

    metrics = compute_metrics(equity_curve, [], 10_000.0)

    # Daily returns: [0.01, -0.00495, 0.01493, -0.00490]
    daily_returns = pd.Series(equity_values).pct_change().dropna()
    expected_sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(365)

    assert abs(metrics["sharpe"] - round(expected_sharpe, 3)) < 0.01, \
        f"sharpe={metrics['sharpe']}, expected={round(expected_sharpe, 3)}"


# ── Test 13: Max drawdown on known curve ────────────────────────────────

def test_max_drawdown_known_curve():
    """Equity: [10000, 10500, 9500, 10000] → DD = (10500-9500)/10500 = 9.52%."""
    engine = BacktestEngine()

    start_ts = 1_735_689_600_000
    day_ms = 86_400_000
    timestamps = [start_ts + i * day_ms for i in range(4)]
    equity_values = [10000.0, 10500.0, 9500.0, 10000.0]
    equity_curve = pd.Series(equity_values, index=timestamps)

    metrics = compute_metrics(equity_curve, [], 10_000.0)

    expected_dd = (10500.0 - 9500.0) / 10500.0 * 100  # 9.523...%
    assert abs(metrics["max_drawdown_pct"] - expected_dd) < 0.1, \
        f"max_dd={metrics['max_drawdown_pct']}, expected={expected_dd}"


# ── Test 14: Signal on last bar ignored ─────────────────────────────────

def test_signal_on_last_bar_ignored(zero_cost_config):
    """No bar i+1 available → no trade opened."""
    n = 60
    closes = [100] * n
    opens  = [100] * n
    highs  = [105] * n
    lows   = [95] * n
    df = make_ohlcv(closes, opens, highs, lows)

    # Signal on the very last bar (index n-1) — no next bar to fill
    signals = {
        n - 1: make_signal(SignalAction.LONG, risk_pct=0.01),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(zero_cost_config)
    result = engine.run(strategy, df, indicators=[])

    assert len(result.trades) == 0


# ── Test 15: End-of-data force close ────────────────────────────────────

def test_end_of_data_force_close(zero_cost_config):
    """Open position at data end → closes at last bar's close."""
    closes = [100, 100, 100, 100, 100, 100, 100, 100, 100, 115]
    opens  = [100, 100, 100, 100, 100, 100, 100, 100, 100, 110]
    highs  = [c + 5 for c in closes]
    lows   = [c - 5 for c in closes]
    df = make_ohlcv(closes, opens, highs, lows)

    # LONG on bar 1, never close → force-closed at last bar's close (115)
    signals = {
        1: make_signal(SignalAction.LONG, risk_pct=0.01),
    }
    strategy = ProgrammableStrategy(signals)
    engine = BacktestEngine(zero_cost_config)
    result = engine.run(strategy, df, indicators=[])

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "end_of_data"
    assert trade.exit_price == 115.0  # last bar close, not last bar open
