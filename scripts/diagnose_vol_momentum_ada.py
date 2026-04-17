#!/usr/bin/env python3
"""Diagnostic for Finding #3 of the merry-horizon plan.

Pulls last 200 ADAUSDT 1h candles from Binance, replays them through a
fresh VolMomentumStrategy instance, and logs the _closes buffer state
+ computed momentum at every signal-emission point. Answers:

    Is vol_momentum's computed momentum value of -1.60 (observed 6 times
    in 18h) stuck due to a stale `_closes` deque, or is it a legitimate
    response to real price action?

Behavior (pure read-only):
    - Hits https://api.binance.com/api/v3/klines (public, no auth)
    - Instantiates the production VolMomentumStrategy class unchanged
    - Feeds 1 candle per iteration via the public on_features() path
    - Prints the internal state at every `momentum` compute

Expected outcomes:
    BUG CONFIRMED: momentum hovers around -1.60 for the last N signals
                   despite close price changing normally
    WORKING AS DESIGNED: momentum drifts naturally with price movement
                          and the repeated -1.60 observation was just
                          the current regime (unlikely given flat prices)

Usage:
    python3 scripts/diagnose_vol_momentum_ada.py
    python3 scripts/diagnose_vol_momentum_ada.py --symbol DOTUSDT
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import certifi
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategies.momentum.vol_momentum import VolMomentumStrategy
from src.utils.types import RiskProfile


def fetch_binance_klines(symbol: str, interval: str = "1h", limit: int = 200) -> pd.DataFrame:
    """Fetch last `limit` klines from Binance spot public API. No auth."""
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(
        url,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
        verify=certifi.where(),
    )
    r.raise_for_status()
    rows = r.json()
    df = pd.DataFrame(rows, columns=[
        "open_ts", "open", "high", "low", "close", "volume",
        "close_ts", "_qav", "_trades", "_tbbv", "_tbqv", "_ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_ts"] = df["open_ts"].astype(int)
    df["close_ts"] = df["close_ts"].astype(int)
    return df[["open_ts", "close_ts", "open", "high", "low", "close", "volume"]]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="ADAUSDT")
    p.add_argument("--limit", type=int, default=200, help="Number of 1h bars to replay")
    p.add_argument("--verbose", action="store_true", help="Print state at every bar, not just signals")
    args = p.parse_args()

    print(f"=== vol_momentum diagnostic: {args.symbol} 1h ===\n")
    print(f"Fetching last {args.limit} 1h candles from Binance...")
    df = fetch_binance_klines(args.symbol, "1h", args.limit)
    print(f"  got {len(df)} bars, range: {datetime.fromtimestamp(df.iloc[0]['open_ts']/1000, tz=timezone.utc).isoformat()} → {datetime.fromtimestamp(df.iloc[-1]['open_ts']/1000, tz=timezone.utc).isoformat()}")

    # Compute ATR(14) — vol_momentum requires features['ATR_14'] (per
    # self._atr_col attribute). Without this it bails at on_features:127
    # before computing momentum, which is what made the first run silent.
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR_14"] = tr.ewm(alpha=1/14, adjust=False).mean()  # Wilder's smoothing
    print(f"  computed ATR_14, ready to replay\n")

    # Instantiate production strategy with defaults
    strategy = VolMomentumStrategy(
        name="vol_momentum_diag",
        markets=[args.symbol],
        timeframe="1h",
        risk_profile=RiskProfile.SAFE,
        max_risk_per_trade=0.012,
    )

    print(f"Strategy config:")
    print(f"  momentum_window: {strategy.momentum_window} bars (7 days @ 1h)")
    print(f"  vol_lookback: {strategy.vol_lookback} bars")
    print(f"  momentum_threshold: {strategy.momentum_threshold}")
    print(f"  max_closes buffer: {strategy._closes.maxlen}")
    print()

    # Replay bar-by-bar
    print(f"{'bar':>4s}  {'ts (UTC)':>20s}  {'close':>9s}  {'buf_len':>7s}  {'buf[0]':>9s}  {'buf[-1]':>9s}  {'momentum':>10s}  {'action':>6s}  {'notes'}")
    signals = []
    prev_closes_oldest = None
    prev_closes_newest = None

    for i, row in df.iterrows():
        close = float(row["close"])
        close_ts = int(row["close_ts"])
        ts_iso = datetime.fromtimestamp(close_ts / 1000.0, tz=timezone.utc).isoformat(timespec="minutes").replace("+00:00", "Z")

        features = pd.Series({
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": close,
            "volume": row["volume"],
            "ATR_14": float(row["ATR_14"]),
        })

        # Call the strategy exactly as the router does
        sig = strategy.process(args.symbol, "1h", features)

        buf_len = len(strategy._closes)
        buf0 = strategy._closes[0] if buf_len > 0 else 0.0
        bufN = strategy._closes[-1] if buf_len > 0 else 0.0
        momentum = strategy._compute_momentum()

        # Detect drift/stale
        drift_flag = ""
        if prev_closes_oldest is not None and buf_len > strategy.momentum_window:
            if abs(buf0 - prev_closes_oldest) < 1e-12:
                drift_flag = "STALE-OLDEST"
        prev_closes_oldest = buf0

        action = sig.action.name if sig is not None else "-"

        if args.verbose or sig is not None or drift_flag:
            momentum_s = f"{momentum:+.4f}" if momentum is not None else "  -   "
            print(f"{i:>4d}  {ts_iso:>20s}  {close:>9.4f}  {buf_len:>7d}  {buf0:>9.4f}  {bufN:>9.4f}  {momentum_s:>10s}  {action:>6s}  {drift_flag}")

        if sig is not None:
            signals.append({
                "bar": i, "ts": ts_iso, "close": close,
                "momentum": momentum,
                "buf_oldest": buf0, "buf_newest": bufN,
                "action": action,
            })

    print()
    print("=" * 72)
    print(f"  Summary: {len(signals)} signals in {len(df)} bars")
    print("=" * 72)

    if signals:
        sigs_df = pd.DataFrame(signals)
        print(sigs_df.to_string(index=False))

        # Check for stuck momentum
        momenta = [s["momentum"] for s in signals if s["momentum"] is not None]
        if len(momenta) >= 3:
            print()
            last3 = momenta[-3:]
            if max(last3) - min(last3) < 0.01:
                print(f"  ⚠️  VERDICT: last 3 momentum values {last3} are nearly identical")
                print(f"     This would CONFIRM the stale-buffer hypothesis.")
            else:
                print(f"  ✅ VERDICT: momentum values drift naturally across signals: {last3}")
                print(f"     The strategy is working as designed; repeated SHORT signals")
                print(f"     reflect a sustained short regime, not a stuck buffer.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
