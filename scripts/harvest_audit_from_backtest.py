#!/usr/bin/env python3
"""Harvest signal_audit training data by running backtests on historical OHLCV.

This is the Session 22 compressed-sprint workaround for the 4-8 week wait
that Phase 1 would otherwise require. Bit-exact backtest-live parity
(Session 20) makes the harvested audit rows legitimate training data —
the features and outcomes match what live execution would produce on the
same OHLCV.

What it does:
1. Load each live strategy's config from strategies.toml
2. For each (strategy, symbol) pair, load historical 1h OHLCV Parquet
3. Run BacktestEngine with `audit_db_path=data/trades.db` and a unique
   `audit_run_id` so harvested rows don't mix with live ones
4. Report per-strategy harvest counts

Usage:
    python3 scripts/harvest_audit_from_backtest.py
    python3 scripts/harvest_audit_from_backtest.py --strategies bb_rsi_mr
    python3 scripts/harvest_audit_from_backtest.py --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.strategies.router import STRATEGY_REGISTRY
from src.utils.config import get_config
from src.utils.logger import get_logger

log = get_logger("harvest_audit")


REPO_ROOT = Path(__file__).resolve().parent.parent
HIST_DIR = REPO_ROOT / "data" / "historical"
DB_PATH = REPO_ROOT / "data" / "trades.db"


def _load_ohlcv(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Load historical OHLCV for a symbol. Returns None if not available."""
    path = HIST_DIR / f"{symbol}_{timeframe}.parquet"
    if not path.exists():
        return None
    return pq.read_table(path).to_pandas().sort_values("timestamp").reset_index(drop=True)


def _load_carry_ohlcv(symbol: str) -> pd.DataFrame | None:
    """Build synthetic OHLCV from funding rate Parquet for -CARRY symbols.

    The underlying BTC symbol is parsed from e.g. 'BTCUSDT-CARRY' → 'BTCUSDT'.
    """
    base_symbol = symbol.upper().replace("-CARRY", "")
    try:
        from src.data.funding_synthetic import load_synthetic_series
        series = load_synthetic_series(base_symbol, friction_pct=0.00005)
        if series.empty:
            return None
        # Add intra-bar spread so indicators don't warn on constant close
        eps = 0.00001
        series["high"] = series["close"] * (1 + eps)
        series["low"] = series["close"] * (1 - eps)
        series["open"] = series["close"] * (1 - eps / 2)
        return series
    except (FileNotFoundError, ImportError) as e:
        log.warning("load_carry_ohlcv_failed", symbol=symbol, error=str(e))
        return None


def harvest_strategy(
    strategy_name: str,
    *,
    run_id: str,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run a single strategy on its configured markets and harvest audit rows.

    Returns dict {symbol: row_count} for this strategy's harvest output.
    """
    if strategy_name not in STRATEGY_REGISTRY:
        log.warning("strategy_not_registered", strategy=strategy_name)
        return {}

    cfg = get_config()
    strat_cfg = cfg.get_strategy(strategy_name)
    if not strat_cfg:
        log.warning("strategy_no_config", strategy=strategy_name)
        return {}

    markets = strat_cfg.get("markets", [])
    timeframe = strat_cfg.get("timeframe", "1h")
    tracked_counts: dict[str, int] = {}

    for symbol in markets:
        # Synthetic carry symbol — build OHLCV from funding Parquet instead.
        if "-CARRY" in symbol.upper():
            df = _load_carry_ohlcv(symbol)
            if df is None:
                log.warning("harvest_no_funding_data", symbol=symbol)
                tracked_counts[symbol] = 0
                continue
        elif "-SYNTH" in symbol.upper():
            log.info("harvest_skip_synthetic", symbol=symbol)
            tracked_counts[symbol] = 0
            continue
        else:
            df = _load_ohlcv(symbol, timeframe)
        if df is None or df.empty:
            log.warning("harvest_no_data", symbol=symbol, timeframe=timeframe)
            tracked_counts[symbol] = 0
            continue
        if len(df) < 100:
            log.warning("harvest_data_too_short", symbol=symbol, len=len(df))
            tracked_counts[symbol] = 0
            continue

        # Fresh strategy instance per symbol (no state leakage)
        try:
            strat_cls = STRATEGY_REGISTRY[strategy_name]
            strategy = strat_cls.from_config(strategy_name)
            # Override markets to just this one symbol
            strategy.markets = [symbol.upper()]
        except Exception as e:
            log.warning("strategy_build_failed", strategy=strategy_name, error=str(e))
            tracked_counts[symbol] = 0
            continue

        # Count rows before/after for this run
        before = _count_audit_rows(run_id, strategy_name, symbol)

        engine = BacktestEngine(
            config=BacktestConfig(
                initial_capital=10_000.0,
                commission_pct=0.001,
                slippage_pct=0.0002,
                risk_per_trade=0.01,
                max_notional_pct=2.0,
            ),
            audit_db_path=str(DB_PATH),
            audit_run_id=run_id,
        )
        # Full indicator set so donchian, adx, rsi-14, etc. are available
        indicators = [
            "ema_9", "ema_21", "sma_20", "rsi_7", "rsi_14",
            "bbands_20", "macd", "atr_14", "adx_14",
            "donchian_20", "donchian_55", "donchian_120",
        ]
        try:
            result = engine.run(
                strategy, df,
                symbol=symbol.upper(),
                timeframe=timeframe,
                indicators=indicators,
            )
            log.info(
                "harvest_backtest_complete",
                strategy=strategy_name,
                symbol=symbol,
                trades=len(result.trades),
                candles=result.total_candles,
            )
        except Exception as e:
            log.warning("harvest_backtest_failed",
                        strategy=strategy_name, symbol=symbol, error=str(e))
            tracked_counts[symbol] = 0
            continue

        after = _count_audit_rows(run_id, strategy_name, symbol)
        tracked_counts[symbol] = after - before

    return tracked_counts


def _count_audit_rows(run_id: str, strategy: str, symbol: str) -> int:
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.execute(
            "SELECT COUNT(*) FROM signal_audit "
            "WHERE run_id = ? AND strategy = ? AND symbol = ?",
            (run_id, strategy, symbol.upper()),
        )
        count = int(cursor.fetchone()[0])
        conn.close()
        return count
    except sqlite3.Error:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategies",
        nargs="*",
        default=None,
        help="Specific strategies to harvest (default: all enabled)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = get_config()
    all_enabled = [
        name for name, strat in cfg.strategies.items()
        if strat.get("enabled", False)
    ]
    strategies_to_run = args.strategies or all_enabled

    run_id = f"harvest_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    print(f"Harvest run_id: {run_id}")
    print(f"Strategies to harvest: {strategies_to_run}")
    print()

    total_counts: dict[str, dict[str, int]] = {}
    for strategy_name in strategies_to_run:
        print(f"─── {strategy_name} ───")
        counts = harvest_strategy(strategy_name, run_id=run_id, dry_run=args.dry_run)
        total_counts[strategy_name] = counts
        total_rows = sum(counts.values())
        for symbol, n in counts.items():
            print(f"  {symbol}: {n} audit rows")
        print(f"  TOTAL: {total_rows}")
        print()

    print("═══ HARVEST SUMMARY ═══")
    grand_total = 0
    for strategy_name, counts in total_counts.items():
        strategy_total = sum(counts.values())
        grand_total += strategy_total
        print(f"  {strategy_name}: {strategy_total} audit rows")
    print(f"  Grand total: {grand_total}")
    print()

    # Label population check
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT strategy, COUNT(*) as total, "
        "       SUM(CASE WHEN meta_label IS NOT NULL THEN 1 ELSE 0 END) as labeled "
        "FROM signal_audit WHERE run_id = ? GROUP BY strategy",
        (run_id,),
    ).fetchall()
    conn.close()

    print("Label population (must be > 0 for training):")
    for strategy, total, labeled in rows:
        pct = 100.0 * labeled / total if total > 0 else 0
        print(f"  {strategy}: {labeled}/{total} ({pct:.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
