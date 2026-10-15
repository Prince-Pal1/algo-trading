"""Depth recorder — turns a live order book into resting liquidity over time.

A heatmap is not a picture of the book *now*; it is the book sampled
repeatedly and stacked along a time axis. This module does the sampling and
the persistence. `src/data/order_book.py` keeps the book correct; this decides
how often to photograph it and where the photographs go.

Storage is long-form, one row per (sample, price level)::

    timestamp | price | size | side

which is the shape a heatmap pivots directly, and which survives a book whose
level count changes between samples.

NOT ParquetStore
----------------
Flow bars and OHLCV both go through `ParquetStore`, but depth cannot: that
store deduplicates on ``timestamp`` alone (`storage.py`), and every depth
sample writes dozens of rows sharing one timestamp. Saving depth through it
would silently keep one level per sample and discard the rest — a heatmap
rendered from the result would look plausible and be almost entirely missing.
`DepthStore` below deduplicates on the composite key instead.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.utils.logger import get_logger
from src.utils.types import OrderBookSnapshot

log = get_logger("depth_recorder")

DEPTH_COLUMNS = ["timestamp", "price", "size", "side"]

# Composite identity of a depth row. Deduplicating on timestamp alone would
# collapse each sample to one level — see the module docstring.
_DEDUPE_KEY = ["timestamp", "side", "price"]

SIDE_BID = "bid"
SIDE_ASK = "ask"


class DepthStore:
    """Parquet storage for long-form depth rows.

    Deliberately separate from `ParquetStore`: same file format, different
    primary key.
    """

    def __init__(self, data_dir: str = "data/depth"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self.data_dir / f"{symbol.upper()}_depth.parquet"

    def exists(self, symbol: str) -> bool:
        return self._path(symbol).exists()

    def list_symbols(self) -> list[str]:
        return sorted(
            f.stem[: -len("_depth")]
            for f in self.data_dir.glob("*_depth.parquet")
        )

    def save(self, df: pd.DataFrame, symbol: str) -> Path:
        """Append rows, deduplicating on (timestamp, side, price)."""
        path = self._path(symbol)
        if df.empty:
            return path

        table = pa.Table.from_pandas(df[DEPTH_COLUMNS], preserve_index=False)
        if path.exists():
            existing = pq.read_table(path)
            combined = pa.concat_tables([existing, table]).to_pandas()
            combined = combined.drop_duplicates(subset=_DEDUPE_KEY, keep="last")
            combined = combined.sort_values(_DEDUPE_KEY).reset_index(drop=True)
            table = pa.Table.from_pandas(combined, preserve_index=False)

        pq.write_table(table, path, compression="snappy")
        log.info("depth_saved", symbol=symbol, rows=table.num_rows, path=str(path))
        return path

    def load(self, symbol: str) -> pd.DataFrame:
        path = self._path(symbol)
        if not path.exists():
            return pd.DataFrame(columns=DEPTH_COLUMNS)
        return pq.read_table(path).to_pandas().sort_values("timestamp").reset_index(drop=True)


class DepthRecorder:
    """Decimates a 100ms depth stream into periodic book photographs.

    The stream updates far faster than a heatmap needs — recording every
    update would be mostly redundant rows. `sample_ms` sets the cadence and
    `depth` how many levels per side each sample keeps.
    """

    def __init__(
        self,
        symbol: str,
        sample_ms: int = 1000,
        depth: int = 40,
        flush_rows: int = 50_000,
    ):
        if sample_ms < 1:
            raise ValueError(f"sample_ms must be >= 1, got {sample_ms}")
        if depth < 1:
            raise ValueError(f"depth must be >= 1, got {depth}")
        self.symbol = symbol.upper()
        self.sample_ms = sample_ms
        self.depth = depth
        self.flush_rows = flush_rows
        self._rows: list[tuple[int, float, float, str]] = []
        self._last_sample_ms = 0
        self._samples = 0

    def record(self, snapshot: OrderBookSnapshot | None) -> bool:
        """Offer a book snapshot. Returns True if it was sampled.

        Snapshots arriving inside the sampling interval are skipped, as are
        ones carrying no timestamp (an un-stamped book cannot be placed on a
        time axis).
        """
        if snapshot is None or not snapshot.timestamp:
            return False
        ts = int(snapshot.timestamp)
        if ts - self._last_sample_ms < self.sample_ms:
            return False

        for level in snapshot.bids[: self.depth]:
            self._rows.append((ts, level.price, level.quantity, SIDE_BID))
        for level in snapshot.asks[: self.depth]:
            self._rows.append((ts, level.price, level.quantity, SIDE_ASK))

        self._last_sample_ms = ts
        self._samples += 1
        return True

    def drain(self) -> pd.DataFrame:
        """Take everything recorded so far and clear the buffer."""
        if not self._rows:
            return pd.DataFrame(columns=DEPTH_COLUMNS)
        df = pd.DataFrame(self._rows, columns=DEPTH_COLUMNS)
        df["timestamp"] = df["timestamp"].astype("int64")
        self._rows = []
        return df

    def flush(self, store: DepthStore) -> int:
        """Persist and clear. Returns rows written."""
        df = self.drain()
        if df.empty:
            return 0
        store.save(df, self.symbol)
        return len(df)

    @property
    def should_flush(self) -> bool:
        return len(self._rows) >= self.flush_rows

    @property
    def pending_rows(self) -> int:
        return len(self._rows)

    @property
    def samples(self) -> int:
        return self._samples


def infer_tick_size(prices: pd.Series) -> float | None:
    """Most common gap between adjacent distinct prices — the book's tick.

    Returns None when there are too few distinct prices to tell.
    """
    unique = pd.Series(sorted(set(prices.astype(float))))
    if len(unique) < 3:
        return None
    gaps = unique.diff().dropna().round(10)
    gaps = gaps[gaps > 0]
    if gaps.empty:
        return None
    tick = float(gaps.mode().iloc[0])
    return tick if tick > 0 else None


def to_heatmap_grid(
    depth: pd.DataFrame,
    price_bins: int = 120,
    time_bins: int | None = None,
    tick_size: float | None = None,
) -> tuple[pd.DataFrame, str]:
    """Pivot long-form depth rows into a price x time grid for rendering.

    Prices must be bucketed, because an order book's exact levels drift and an
    unbucketed pivot is almost entirely empty. The bucket width matters more
    than it looks: equal-width bins that do not divide the instrument's tick
    size put two price levels in some rows and one in others, which renders as
    moire banding that reads like real structure. So by default the tick size
    is inferred from the data and used directly, and equal-width binning is
    only a fallback for when that would produce an unmanageable number of rows.

    Args:
        depth: long-form rows (timestamp, price, size, side).
        price_bins: ceiling on price rows; also the equal-width bin count when
            the fallback is used.
        time_bins: downsample to at most this many time columns (None = all).
        tick_size: force this bucket width instead of inferring it.

    Returns:
        (grid, note) — grid indexed by bucketed price (ascending), columns are
        timestamps, values are resting size. `note` records which bucketing was
        used and any downsampling, for the caller to surface.
    """
    if depth.empty:
        return pd.DataFrame(), "no depth rows"
    if price_bins < 2:
        raise ValueError(f"price_bins must be >= 2, got {price_bins}")

    df = depth.copy()
    prices = df["price"].astype(float)
    lo, hi = float(prices.min()), float(prices.max())
    span = hi - lo

    notes: list[str] = []
    tick = tick_size if tick_size is not None else infer_tick_size(prices)

    if span < 1e-12:
        df["bucket"] = prices
        notes.append("single price level")
    elif tick and tick > 0 and (span / tick) + 1 <= price_bins:
        # Tick-aligned: one row per real price level, no banding.
        df["bucket"] = (prices / tick).round() * tick
        notes.append(f"tick-aligned at {tick:g}")
    else:
        step = span / price_bins
        df["bucket"] = ((prices - lo) / step).round() * step + lo
        why = "range too wide for tick alignment" if tick else "tick size not inferable"
        notes.append(f"{price_bins} equal-width bins ({why})")

    df["bucket"] = df["bucket"].round(10)

    timestamps = sorted(df["timestamp"].unique())
    if time_bins is not None and len(timestamps) > time_bins:
        stride = len(timestamps) // time_bins + 1
        keep = set(timestamps[::stride])
        df = df[df["timestamp"].isin(keep)]
        notes.append(f"downsampled {len(timestamps)} samples to {len(keep)} columns")

    grid = df.pivot_table(
        index="bucket", columns="timestamp", values="size", aggfunc="sum"
    ).sort_index()
    return grid, " · ".join(notes)
