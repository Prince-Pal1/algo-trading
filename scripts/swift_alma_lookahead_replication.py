#!/usr/bin/env python3
"""Reproduce TradingView's SWIFT backtest numbers by replicating the lookahead bias.

TV's Pine Script for SWIFT uses `lookahead=barmerge.lookahead_on` on its
`request.security()` call. On a 5m chart with a 40-min alt-TF signal,
this means: at every M5 bar inside a still-forming 40-min bar, the
Pine script sees the alt-TF bar's FINAL close (which includes future
M5 bars within that period). This is a repainting bias — in live
trading the signal would repaint, but in a historical backtest it
fires the crossover 1-8 M5 bars EARLIER than a non-repainting port.

This script:
  1. Loads 1 year of XAUUSD 5m data (Apr 14 2025 - Apr 14 2026)
  2. Resamples to 40-min bars via pandas.resample
  3. Computes ALMA(close, 2, 0.85, 5) and ALMA(open, 2, 0.85, 5) on 40m
  4. At each M5 bar, LOOKS UP the alt bar containing it (with lookahead)
  5. Detects ALMA crossover on the alt-TF series
  6. Fires the signal at the FIRST M5 bar within that alt period
  7. Simulates a reversal-only strategy (no SL/TP): enter at signal bar
     close, exit at next signal bar close, flip direction
  8. Compares stats to the TV trade file summary

If this replicates TV's 88% win / PF 24 / +223% return numbers, we've
confirmed the TV backtest is repainting and the edge is not deployable
in live trading.

Usage:
    python3 scripts/swift_alma_lookahead_replication.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.data.feature_engine import _alma  # noqa: E402


# ── Parameters matching Pine Script + TV's run ─────────────────────────
START_DATE = "2025-04-14"
END_DATE = "2026-04-14"
ALT_TF_MINUTES = 40          # 8 × 5m (Pine default intRes=8 on M5 chart)
ALMA_LENGTH = 2
ALMA_OFFSET = 0.85
ALMA_SIGMA = 5
INITIAL_EQUITY = 1_000_000.0  # Pine strategy() default
POSITION_PCT = 0.10           # default_qty_value=10 → 10% of equity per trade
COMMISSION = 0.0              # Pine default


def load_m5_data() -> pd.DataFrame:
    """Load XAUUSD 5m data for the TV backtest window."""
    df = pd.read_parquet(REPO_ROOT / "data" / "historical" / "XAUUSD_5m.parquet")
    df = df.sort_values("timestamp").reset_index(drop=True)
    # Convert timestamp (ms) to datetime
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    # Filter to TV's window
    mask = (df["dt"] >= START_DATE) & (df["dt"] < END_DATE)
    return df[mask].reset_index(drop=True)


def resample_to_alt_tf(m5: pd.DataFrame, alt_tf_min: int) -> pd.DataFrame:
    """Resample M5 → alt-TF using integer-divide groupby on timestamp (ms).

    Returns a DataFrame with one row per alt bar, plus a 'group' column
    so we can map M5 bars back to their alt bar.
    """
    bar_duration_ms = alt_tf_min * 60 * 1000
    m5 = m5.copy()
    m5["group"] = (m5["timestamp"] // bar_duration_ms) * bar_duration_ms
    alt = m5.groupby("group").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).reset_index().rename(columns={"group": "alt_ts"})
    return alt


def compute_alma_pair(alt: pd.DataFrame, length: int, offset: float, sigma: float) -> pd.DataFrame:
    """Add alma_close and alma_open columns to the alt-TF DataFrame."""
    alt = alt.copy()
    alt["alma_close"] = _alma(alt["close"], length, offset, sigma)
    alt["alma_open"] = _alma(alt["open"], length, offset, sigma)
    return alt


def detect_lookahead_signals(
    m5: pd.DataFrame,
    alt_with_alma: pd.DataFrame,
    alt_tf_min: int,
) -> pd.DataFrame:
    """For each M5 bar, attach the ALMA values from the ALT BAR IT BELONGS TO.

    With lookahead_on behavior: each M5 bar sees the final close of its
    containing alt bar — including alt bars that haven't finished yet in
    real time, but are known in a historical backtest.
    """
    bar_duration_ms = alt_tf_min * 60 * 1000
    m5 = m5.copy()
    m5["alt_ts"] = (m5["timestamp"] // bar_duration_ms) * bar_duration_ms
    # Left-join the alt ALMA values
    alt_lookup = alt_with_alma[["alt_ts", "alma_close", "alma_open"]]
    m5 = m5.merge(alt_lookup, on="alt_ts", how="left")
    # Detect crossovers on the alt-TF ALMA series, exposed at each M5 bar
    m5["alma_close_prev"] = m5["alma_close"].shift(1)
    m5["alma_open_prev"] = m5["alma_open"].shift(1)
    m5["le_trigger"] = (
        (m5["alma_close_prev"] <= m5["alma_open_prev"])
        & (m5["alma_close"] > m5["alma_open"])
    )
    m5["se_trigger"] = (
        (m5["alma_close_prev"] >= m5["alma_open_prev"])
        & (m5["alma_close"] < m5["alma_open"])
    )
    return m5


def simulate_reversal_strategy(m5: pd.DataFrame) -> list[dict]:
    """Simulate a simple reversal-only strategy matching TV's behavior.

    State: position_side ∈ {0, +1 (long), -1 (short)}.
    On LE trigger while not long → close current short (if any), open long.
    On SE trigger while not short → close current long (if any), open short.
    Position enters at the signal bar's close; exits at the REVERSAL signal
    bar's close (same bar as the new entry — matches Pine's same-bar flip).

    Returns a list of trade dicts.
    """
    trades = []
    side = 0  # 0 flat, +1 long, -1 short
    entry_price = 0.0
    entry_ts = 0
    entry_bar_idx = -1
    equity = INITIAL_EQUITY

    for i, row in m5.iterrows():
        close = float(row["close"])
        if row["le_trigger"] and side != 1:
            # Close existing short (if any)
            if side == -1:
                pnl_frac = (entry_price - close) / entry_price  # short P&L
                notional = equity * POSITION_PCT
                pnl_usd = notional * pnl_frac
                equity += pnl_usd
                trades.append({
                    "trade_num": len(trades) + 1,
                    "side": "short",
                    "entry_ts": entry_ts,
                    "exit_ts": int(row["timestamp"]),
                    "entry_price": entry_price,
                    "exit_price": close,
                    "pnl_pct": pnl_frac * 100.0,
                    "pnl_usd": pnl_usd,
                    "equity_after": equity,
                })
            # Open long
            side = 1
            entry_price = close
            entry_ts = int(row["timestamp"])
            entry_bar_idx = i
        elif row["se_trigger"] and side != -1:
            if side == 1:
                pnl_frac = (close - entry_price) / entry_price
                notional = equity * POSITION_PCT
                pnl_usd = notional * pnl_frac
                equity += pnl_usd
                trades.append({
                    "trade_num": len(trades) + 1,
                    "side": "long",
                    "entry_ts": entry_ts,
                    "exit_ts": int(row["timestamp"]),
                    "entry_price": entry_price,
                    "exit_price": close,
                    "pnl_pct": pnl_frac * 100.0,
                    "pnl_usd": pnl_usd,
                    "equity_after": equity,
                })
            side = -1
            entry_price = close
            entry_ts = int(row["timestamp"])
            entry_bar_idx = i

    return trades


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    losses = sum(1 for t in trades if t["pnl_usd"] < 0)
    win_pnl = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    loss_pnl = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))
    total = win_pnl - loss_pnl
    pf = win_pnl / loss_pnl if loss_pnl > 0 else float("inf")
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / len(trades) * 100.0,
        "win_pnl_usd": win_pnl,
        "loss_pnl_usd": loss_pnl,
        "net_pnl_usd": total,
        "profit_factor": pf,
        "avg_win": win_pnl / max(wins, 1),
        "avg_loss": -loss_pnl / max(losses, 1),
        "final_equity": trades[-1]["equity_after"],
        "return_pct": (trades[-1]["equity_after"] - INITIAL_EQUITY) / INITIAL_EQUITY * 100,
    }


def main() -> int:
    print("=" * 70)
    print("SWIFT lookahead replication — reproducing TV's backtest")
    print("=" * 70)
    print(f"Window: {START_DATE} → {END_DATE}")
    print(f"Alt TF: {ALT_TF_MINUTES}min (M5 chart × intRes=8)")
    print(f"ALMA: length={ALMA_LENGTH}, offset={ALMA_OFFSET}, sigma={ALMA_SIGMA}")
    print(f"Initial equity: ${INITIAL_EQUITY:,.0f}, position %: {POSITION_PCT*100:.0f}%")
    print()

    m5 = load_m5_data()
    print(f"M5 bars in window: {len(m5):,}")
    alt = resample_to_alt_tf(m5, ALT_TF_MINUTES)
    print(f"Alt {ALT_TF_MINUTES}min bars: {len(alt):,}")

    alt_with_alma = compute_alma_pair(alt, ALMA_LENGTH, ALMA_OFFSET, ALMA_SIGMA)
    m5_with_signals = detect_lookahead_signals(m5, alt_with_alma, ALT_TF_MINUTES)

    # Count raw signals before simulation
    le_count = int(m5_with_signals["le_trigger"].sum())
    se_count = int(m5_with_signals["se_trigger"].sum())
    print(f"Raw signals: LE={le_count}, SE={se_count}, total={le_count+se_count}")
    print()

    trades = simulate_reversal_strategy(m5_with_signals)
    stats = summarize(trades)

    print("── Simulation results ──")
    print(f"Total trades:  {stats['trades']}")
    print(f"Wins / Losses: {stats['wins']} / {stats['losses']}")
    print(f"Win rate:      {stats['win_rate_pct']:.2f}%")
    print(f"Net P&L:       ${stats['net_pnl_usd']:,.2f}")
    print(f"Return:        {stats['return_pct']:+.2f}%")
    print(f"Profit factor: {stats['profit_factor']:.3f}")
    print(f"Avg win:       ${stats['avg_win']:.2f}")
    print(f"Avg loss:      ${stats['avg_loss']:.2f}")
    print(f"Final equity:  ${stats['final_equity']:,.2f}")
    print()
    print("── Comparison to TV strategy tester ──")
    print(f"TV trades:      4,808")
    print(f"TV win rate:    88.85%")
    print(f"TV profit fct:  24.099")
    print(f"TV net P&L:     $2,237,852")
    print(f"TV return:      +223.78%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
