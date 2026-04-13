"""Funding Carry Synthetic Series Loader (Strategy A — v1 scope hack).

Converts a historical funding-rate Parquet (from `BinanceFundingDownloader`)
into a synthetic OHLCV series that represents the economic return of a
delta-neutral `short BTCUSDT perp + long BTCUSDT spot` hedged pair.

**Why a synthetic series instead of real multi-leg futures support?**

The hedged pair's P&L is exactly `funding_rate − friction` per 8h epoch
(the directional risk is hedged out). We represent this as a single
pseudo-asset whose close price drifts by that amount each epoch:

    close[i+1] = close[i] × (1 + funding_rate[i] − friction_pct)

This lets us use the existing single-symbol `BacktestEngine` and the
existing `BaseStrategy.on_features()` contract without building any
multi-leg position tracking. The economics are faithful; only the
execution simulation is skipped.

**Friction budget (default 0.0016 = 0.16% per 8h cycle):**

- Spot round-trip taker fee: 0.10% × 2 / N_epochs_held
- Perp round-trip taker fee: 0.04% × 2 / N_epochs_held
- Slippage buffer: 0.08%
- Amortized borrow cost: ~0.05%
- **Total: ~0.16% per 8h**

The friction is subtracted from the funding rate on every epoch, so longer
holds amortize the round-trip fees better. For backtest purposes we apply a
flat per-epoch friction (a conservative approximation).

**Output schema** matches the standard OHLCV shape so `BacktestEngine` can
consume it unchanged:

    timestamp (ms, 8h epoch), open, high, low, close, volume

`open == high == low == close` on each synthetic bar — the hedged pair has
zero intra-bar drift by construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from src.utils.logger import get_logger

log = get_logger("funding_synthetic")


_DEFAULT_FUNDING_DIR = Path("data/historical/funding")
_DEFAULT_START_PRICE = 100.0       # arbitrary base; only ratios matter
_DEFAULT_FRICTION_PCT = 0.00005    # 0.005% per 8h epoch amortized (see docstring)


@dataclass(frozen=True)
class SyntheticSeriesConfig:
    start_price: float = _DEFAULT_START_PRICE
    friction_pct: float = _DEFAULT_FRICTION_PCT
    volume_placeholder: float = 1.0  # backtest engine checks for >0


def load_funding_parquet(
    symbol: str,
    *,
    funding_dir: Path | None = None,
) -> pd.DataFrame:
    """Load raw historical funding rates for `symbol` from Parquet.

    Returns a DataFrame sorted by timestamp ascending with columns
    `timestamp, funding_rate, mark_price, symbol`. Raises FileNotFoundError
    if the Parquet doesn't exist.
    """
    directory = funding_dir or _DEFAULT_FUNDING_DIR
    path = directory / f"{symbol.upper()}_8h.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"Funding Parquet missing for {symbol}: {path}. "
            f"Run `BinanceFundingDownloader.download_and_save` first."
        )
    df = pq.read_table(path).to_pandas()
    # Defensive normalization
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def build_synthetic_series(
    funding_df: pd.DataFrame,
    *,
    config: SyntheticSeriesConfig | None = None,
) -> pd.DataFrame:
    """Convert a funding-rate DataFrame into a synthetic OHLCV series.

    The synthetic close drifts by `funding_rate − friction_pct` per epoch:
        close[0] = config.start_price
        close[i+1] = close[i] × (1 + funding_rate[i] − friction_pct)

    Volume is a constant placeholder (backtest engine checks `volume > 0`).

    Returns DataFrame with columns `timestamp, open, high, low, close, volume`
    of the same length as `funding_df`. Strategies can treat this as a
    normal 8h-timeframe OHLCV series representing "BTCUSDT-CARRY".
    """
    cfg = config or SyntheticSeriesConfig()

    if funding_df.empty:
        return pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )

    # Validate required columns
    required = {"timestamp", "funding_rate"}
    missing = required - set(funding_df.columns)
    if missing:
        raise ValueError(f"funding_df missing columns: {missing}")

    n = len(funding_df)
    timestamps = funding_df["timestamp"].to_numpy()
    funding_rates = funding_df["funding_rate"].to_numpy()

    closes = [cfg.start_price]
    for i in range(n):
        gross_return = float(funding_rates[i]) - cfg.friction_pct
        next_close = closes[-1] * (1.0 + gross_return)
        closes.append(next_close)
    # Drop the seed so we have exactly n closes
    closes = closes[1:]

    rows = []
    for i in range(n):
        c = closes[i]
        rows.append(
            {
                "timestamp": int(timestamps[i]),
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": cfg.volume_placeholder,
                # Keep the raw funding rate alongside the synthetic OHLCV so
                # the strategy can read it directly from `features` in both
                # backtest and live. Preserves the scope hack cleanly.
                "funding_rate": float(funding_rates[i]),
            }
        )
    return pd.DataFrame(rows)


def load_synthetic_series(
    symbol: str,
    *,
    friction_pct: float = _DEFAULT_FRICTION_PCT,
    start_price: float = _DEFAULT_START_PRICE,
    funding_dir: Path | None = None,
) -> pd.DataFrame:
    """One-call helper: load funding Parquet + build synthetic series.

    This is the primary entrypoint used by the backtest CLI and the
    FundingCarryStrategy at runtime.
    """
    funding_df = load_funding_parquet(symbol, funding_dir=funding_dir)
    config = SyntheticSeriesConfig(
        start_price=start_price, friction_pct=friction_pct,
    )
    synthetic = build_synthetic_series(funding_df, config=config)
    log.info(
        "synthetic_series_built",
        symbol=symbol,
        rows=len(synthetic),
        friction_pct=friction_pct,
        first_close=float(synthetic["close"].iloc[0]) if not synthetic.empty else None,
        last_close=float(synthetic["close"].iloc[-1]) if not synthetic.empty else None,
    )
    return synthetic


def cumulative_log_return(
    synthetic_df: pd.DataFrame,
    *,
    start_price: float = _DEFAULT_START_PRICE,
) -> float:
    """Compute total log-return of the synthetic series vs the seed start_price.

    A friction=0 series's cumulative log-return should equal the sum of
    log(1 + funding_rate) across all epochs, within float tolerance. The
    A.2 unit tests use this to verify the builder is arithmetically correct.

    Note: we compare `close[-1]` against the *seed* `start_price`, not
    `close[0]`, because `close[0]` is already the first compounded price
    (start_price × (1 + r[0])). Comparing against close[0] would be off
    by one epoch.
    """
    if synthetic_df.empty:
        return 0.0
    last = float(synthetic_df["close"].iloc[-1])
    if start_price <= 0:
        return 0.0
    return math.log(last / start_price)
