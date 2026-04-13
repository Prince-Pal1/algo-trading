#!/usr/bin/env python3
"""Offline builder for the Strategy B momentum rank cache.

Reads:
    config/universes.toml              — list of altcoins (survivorship-bias-free)
    data/historical/<SYMBOL>_1d.parquet — daily OHLCV for each symbol

Writes:
    data/historical/momentum_rank_cache.parquet

Algorithm (per rebalance date, every `rebalance_cadence_days`):
    1. For each symbol in the universe, load the last `lookback_days` of
       daily closes from its historical Parquet.
    2. Compute the Clenow score: `annualized_slope × r_squared` on log(close).
    3. Apply trend filter: current price > trend_ma (default 100d) OR score=0
    4. Apply gap filter: no single-day return > gap_filter_pct OR score=0
    5. Rank the symbols, assign inverse-rank weights to the top-N, tag the
       rest as `in_top_n=False`
    6. Apply regime filter at the end: if BTCUSDT is below its 200-day MA
       on the rebalance date, zero out ALL weights (no positions in bear regime).
    7. Write one row per (date, symbol) into the rank cache Parquet.

Usage:
    python3 scripts/build_momentum_rank_cache.py
    python3 scripts/build_momentum_rank_cache.py --universe altcoin_top30_2020_2026
    python3 scripts/build_momentum_rank_cache.py --lookback-days 60
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from src.strategies.ranking import (
    compute_clenow_score,
    rank_weights_from_scores,
    write_rank_cache_parquet,
)
from src.utils.logger import get_logger

log = get_logger("build_rank_cache")


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "universes.toml"
HIST_DIR = REPO_ROOT / "data" / "historical"
OUT_PATH = HIST_DIR / "momentum_rank_cache.parquet"
_MS_PER_DAY = 86_400_000


def load_universe(path: Path, universe_name: str) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"universes.toml missing: {path}")
    with path.open("rb") as f:
        data = tomllib.load(f)
    if universe_name not in data:
        available = list(data.keys())
        raise ValueError(f"universe '{universe_name}' not in {path}. Available: {available}")
    return data[universe_name]


def load_symbol_history(
    symbol: str,
    *,
    timeframe: str = "1d",
    hist_dir: Path | None = None,
) -> pd.DataFrame | None:
    """Load historical OHLCV for one symbol from data/historical/.

    Returns None if the Parquet doesn't exist (symbol has no data).

    If the requested timeframe is "1d" but only "1h" exists, resamples
    on the fly using last-close-of-day.
    """
    base = hist_dir if hist_dir is not None else HIST_DIR
    path = base / f"{symbol}_{timeframe}.parquet"
    if path.exists():
        df = pq.read_table(path).to_pandas()
        return df.sort_values("timestamp").reset_index(drop=True)

    # Fallback: resample 1h → 1d if requested tf was 1d
    if timeframe == "1d":
        path_1h = base / f"{symbol}_1h.parquet"
        if path_1h.exists():
            df_1h = pq.read_table(path_1h).to_pandas()
            df_1h = df_1h.sort_values("timestamp").reset_index(drop=True)
            # Convert timestamp ms → datetime, resample to daily
            df_1h["dt"] = pd.to_datetime(df_1h["timestamp"], unit="ms", utc=True)
            df_1h = df_1h.set_index("dt")
            daily = df_1h.resample("1D").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum", "timestamp": "last",
            }).dropna().reset_index(drop=True)
            return daily
    return None


def slice_up_to(df: pd.DataFrame, ts_ms: int, n: int) -> np.ndarray | None:
    """Return the last `n` close prices at or before `ts_ms`. None if insufficient."""
    mask = df["timestamp"] <= ts_ms
    slice_df = df[mask]
    if len(slice_df) < n:
        return None
    return slice_df["close"].tail(n).to_numpy()


def btc_above_200ma(df_btc: pd.DataFrame, ts_ms: int, window: int = 200) -> bool:
    """Regime filter: is BTC above its 200-day MA on the rebalance date?"""
    closes = slice_up_to(df_btc, ts_ms, window)
    if closes is None:
        return False  # not enough data → risk-off by default
    return float(closes[-1]) > float(np.mean(closes))


def build_rank_cache(
    *,
    universe_name: str = "altcoin_top30_2020_2026",
    lookback_days: int = 90,
    rebalance_cadence_days: int = 2,
    top_n: int = 20,
    trend_ma_window: int = 100,
    gap_filter_pct: float = 0.15,
    regime_ma_window: int = 200,
    regime_symbol: str = "BTCUSDT",
    regime_filter_enabled: bool = True,
    start_date: str | None = None,
    end_date: str | None = None,
    dry_run: bool = False,
    hist_dir: Path | None = None,
    out_path: Path | None = None,
    histories_override: dict | None = None,
) -> list[dict]:
    """Build the rank cache rows in memory and optionally write to Parquet.

    Args:
        histories_override: when provided, skips disk loading and uses the
            given {symbol: DataFrame} dict directly. Used by tests.
        hist_dir / out_path: optional override paths (for tests).

    Returns the list of row dicts. Pass `dry_run=True` to skip the Parquet write.
    """
    universe_cfg = load_universe(CONFIG_PATH, universe_name)
    symbols = list(universe_cfg.get("symbols", []))
    if "delisted" in universe_cfg:
        delisted = universe_cfg["delisted"].get("symbols", [])
        symbols.extend(delisted)

    log.info("rank_build_start", universe=universe_name, n_symbols=len(symbols),
             lookback=lookback_days, cadence=rebalance_cadence_days, top_n=top_n)

    # Load all symbol history upfront
    histories: dict[str, pd.DataFrame] = {}
    if histories_override is not None:
        histories = {k.upper(): v for k, v in histories_override.items()}
    else:
        for sym in symbols:
            df = load_symbol_history(sym, hist_dir=hist_dir)
            if df is not None and not df.empty:
                histories[sym] = df
            else:
                log.warning("symbol_history_missing", symbol=sym)

    df_regime: pd.DataFrame | None = None
    if regime_filter_enabled:
        if regime_symbol not in histories:
            raise RuntimeError(
                f"regime symbol {regime_symbol} history required for regime filter; "
                f"pass regime_filter_enabled=False to skip, or choose a different "
                f"regime_symbol"
            )
        df_regime = histories[regime_symbol]

    # Determine rebalance timestamps
    min_ts = max(df["timestamp"].iloc[0] for df in histories.values())
    max_ts = min(df["timestamp"].iloc[-1] for df in histories.values())

    if start_date:
        start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        min_ts = max(min_ts, start_ts)
    if end_date:
        end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        max_ts = min(max_ts, end_ts)

    # Skip forward to allow the first lookback window
    first_rebalance = min_ts + max(lookback_days, regime_ma_window) * _MS_PER_DAY
    cadence_ms = rebalance_cadence_days * _MS_PER_DAY

    rebalance_ts_list: list[int] = []
    t = first_rebalance
    while t <= max_ts:
        rebalance_ts_list.append(t)
        t += cadence_ms

    log.info("rebalance_schedule", n_rebalances=len(rebalance_ts_list),
             first=first_rebalance, last=max_ts)

    rows: list[dict] = []
    for ts_ms in rebalance_ts_list:
        if regime_filter_enabled and df_regime is not None:
            regime_ok = btc_above_200ma(df_regime, ts_ms, window=regime_ma_window)
        else:
            regime_ok = True  # filter disabled → always pass

        scores_this_rebalance: dict[str, float] = {}
        for sym, df_sym in histories.items():
            closes = slice_up_to(df_sym, ts_ms, max(lookback_days, trend_ma_window))
            if closes is None:
                scores_this_rebalance[sym] = 0.0
                continue
            score = compute_clenow_score(
                closes,
                annualization_factor=365.0,
                trend_ma_window=trend_ma_window,
                gap_filter_pct=gap_filter_pct,
            )
            # Apply trend + gap filters (zero the score if either fails)
            if not score.passes_trend_filter or not score.passes_gap_filter:
                scores_this_rebalance[sym] = 0.0
            else:
                scores_this_rebalance[sym] = score.score

        # Apply regime filter at the end (zero EVERYTHING if BTC < 200MA)
        if not regime_ok:
            for sym in scores_this_rebalance:
                scores_this_rebalance[sym] = 0.0

        rank_info = rank_weights_from_scores(
            scores_this_rebalance, top_n=top_n, weighting="inverse_rank",
        )
        for sym, (rank, weight, in_top) in rank_info.items():
            rows.append({
                "timestamp": int(ts_ms),
                "symbol": sym,
                "score": float(scores_this_rebalance.get(sym, 0.0)),
                "rank": int(rank),
                "weight": float(weight),
                "in_top_n": bool(in_top),
            })

    log.info("rank_build_complete", rows=len(rows),
             n_rebalances=len(rebalance_ts_list), regime_filter_window=regime_ma_window)

    if not dry_run:
        write_rank_cache_parquet(rows, out_path or OUT_PATH)

    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default="altcoin_top30_2020_2026")
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--cadence-days", type=int, default=2)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        rows = build_rank_cache(
            universe_name=args.universe,
            lookback_days=args.lookback_days,
            rebalance_cadence_days=args.cadence_days,
            top_n=args.top_n,
            start_date=args.start_date,
            end_date=args.end_date,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        log.error("rank_build_failed", error=str(e))
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    n_active = sum(1 for r in rows if r["in_top_n"])
    print(f"OK: wrote {len(rows)} rows ({n_active} active top-N positions) "
          f"to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
