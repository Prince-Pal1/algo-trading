"""Cross-sectional rank cache primitive (Strategy B — v1 scope hack).

The rank cache is a read-only lookup table produced by an offline pre-pass
job (`scripts/build_momentum_rank_cache.py`) and queried at runtime by
per-symbol `ClenowMomentumStrategy` instances.

Schema (Parquet):

    timestamp (int ms)    date of the rebalance epoch
    symbol    (str)       the altcoin ticker
    score     (float)     Clenow score: annualized_slope × R²
    rank      (int)       1-based rank within the rebalance epoch (1 = best)
    weight    (float)     normalized rank-weight (sum of all weights = 1)
    in_top_n  (bool)      whether this symbol made the top-N cutoff

Query API:

    cache = RankCache.from_parquet("data/historical/momentum_rank_cache.parquet")
    weight = cache.get_weight(ts_ms, "BTCUSDT")           # 0.0 if not in top-N
    in_top = cache.in_top_n(ts_ms, "ETHUSDT")
    top_symbols = cache.top_n_at(ts_ms)                   # list of ranked symbols
    latest_ts = cache.latest_rebalance_before(now_ms)     # for "as of" queries

Design notes:
- The cache is immutable once loaded. Rebalance happens offline; the strategy
  only reads.
- Queries are by exact timestamp OR "as of" (latest rebalance ≤ now). Most
  runtime uses "as of" because strategies tick at 1h while rebalances are 2d.
- Missing timestamps return zero weight and `in_top_n=False` (strategy exits).
- Computing Clenow score itself (offline) is `compute_clenow_score()` below.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.utils.logger import get_logger

log = get_logger("ranking")


# ══════════════════════════════════════════════════════════════════════
# Clenow score computation (offline)
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ClenowScore:
    """Output of compute_clenow_score for one symbol at one point in time."""
    annualized_slope: float
    r_squared: float
    score: float                # annualized_slope × r_squared
    passes_trend_filter: bool   # price > 100-day MA
    passes_gap_filter: bool     # no single-day gap > gap_filter_pct
    n_points: int               # observations used


def compute_clenow_score(
    prices: np.ndarray,
    *,
    annualization_factor: float = 252.0,  # trading-day convention; crypto-adjusted below
    trend_ma_window: int = 100,
    gap_filter_pct: float = 0.15,
) -> ClenowScore:
    """Compute the Clenow score for one symbol's price history.

    Args:
        prices: numpy array of closing prices. Must be at least as long as
            max(lookback_length, trend_ma_window). Prices are assumed to be
            at the natural rebalance timeframe (e.g., daily bars for v1).
        annualization_factor: scalar applied to slope to express it as
            an annualized return. For crypto we pass 365 instead of 252.
        trend_ma_window: moving-average window for the trend filter.
        gap_filter_pct: any single-day return exceeding this (absolute)
            causes gap_filter_pass to be False (Clenow's volatility
            stability rule).

    Returns:
        ClenowScore with all components computed. If prices has fewer than
        `trend_ma_window` points, returns zero score.
    """
    n = len(prices)
    if n < trend_ma_window:
        return ClenowScore(
            annualized_slope=0.0,
            r_squared=0.0,
            score=0.0,
            passes_trend_filter=False,
            passes_gap_filter=False,
            n_points=n,
        )

    # Linear regression on log(price) vs time index
    log_prices = np.log(prices)
    x = np.arange(n, dtype=float)
    # Use numpy's polyfit for stability
    slope, intercept = np.polyfit(x, log_prices, deg=1)

    # R² = 1 - SSres / SStot
    fitted = slope * x + intercept
    ss_res = float(np.sum((log_prices - fitted) ** 2))
    ss_tot = float(np.sum((log_prices - log_prices.mean()) ** 2))
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    r_squared = max(0.0, min(1.0, r_squared))

    # Annualized slope: convert per-bar to per-year
    annualized_slope = float(slope) * annualization_factor

    # Trend filter: current price > trend MA
    trend_ma = float(np.mean(prices[-trend_ma_window:]))
    current_price = float(prices[-1])
    passes_trend = current_price > trend_ma

    # Gap filter: no single-bar return > gap_filter_pct in the window
    returns = np.diff(prices) / prices[:-1]
    max_abs_return = float(np.max(np.abs(returns))) if len(returns) > 0 else 0.0
    passes_gap = max_abs_return <= gap_filter_pct

    score = annualized_slope * r_squared
    return ClenowScore(
        annualized_slope=annualized_slope,
        r_squared=r_squared,
        score=score,
        passes_trend_filter=passes_trend,
        passes_gap_filter=passes_gap,
        n_points=n,
    )


# ══════════════════════════════════════════════════════════════════════
# RankCache
# ══════════════════════════════════════════════════════════════════════


class RankCache:
    """Immutable, read-only query layer over a precomputed rank Parquet.

    The cache is built offline by `scripts/build_momentum_rank_cache.py`
    and loaded once at engine boot. Strategies query it via `get_weight`
    or `in_top_n` for their own symbol at each bar.
    """

    def __init__(self, df: pd.DataFrame) -> None:
        """Construct from a DataFrame with schema described above."""
        required = {"timestamp", "symbol", "score", "rank", "weight", "in_top_n"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"RankCache dataframe missing columns: {missing}")
        # Normalize and sort for fast lookup
        df = df.copy()
        df["timestamp"] = df["timestamp"].astype(int)
        df["symbol"] = df["symbol"].astype(str)
        df["rank"] = df["rank"].astype(int)
        df["in_top_n"] = df["in_top_n"].astype(bool)
        self._df = df.sort_values(["timestamp", "rank"]).reset_index(drop=True)

        # Precompute sorted unique rebalance timestamps for "as of" queries
        self._rebalance_ts = np.array(sorted(self._df["timestamp"].unique()))

    @classmethod
    def from_parquet(cls, path: str | Path) -> RankCache:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"RankCache parquet missing: {p}")
        df = pq.read_table(p).to_pandas()
        log.info("rank_cache_loaded", path=str(p), rows=len(df),
                 rebalances=len(df["timestamp"].unique()))
        return cls(df)

    @classmethod
    def from_records(cls, records: list[dict]) -> RankCache:
        """Construct from an in-memory list of dicts — used for tests."""
        return cls(pd.DataFrame(records))

    # ── Query API ───────────────────────────────────────────────

    def latest_rebalance_before(self, now_ms: int) -> int | None:
        """Return the largest rebalance timestamp <= now_ms, or None."""
        idx = np.searchsorted(self._rebalance_ts, now_ms, side="right") - 1
        if idx < 0:
            return None
        return int(self._rebalance_ts[idx])

    def get_weight(self, now_ms: int, symbol: str) -> float:
        """Return the rank-weight for `symbol` as of the most recent rebalance.

        Returns 0.0 if symbol not in top-N at that rebalance or if no
        rebalance exists at/before now_ms.
        """
        ts = self.latest_rebalance_before(now_ms)
        if ts is None:
            return 0.0
        mask = (self._df["timestamp"] == ts) & (self._df["symbol"] == symbol.upper())
        rows = self._df[mask]
        if rows.empty:
            return 0.0
        row = rows.iloc[0]
        if not bool(row["in_top_n"]):
            return 0.0
        return float(row["weight"])

    def in_top_n(self, now_ms: int, symbol: str) -> bool:
        return self.get_weight(now_ms, symbol) > 0.0

    def top_n_at(self, ts_ms: int) -> list[str]:
        """Return the ordered list of top-N symbols at exactly `ts_ms`."""
        mask = (self._df["timestamp"] == ts_ms) & (self._df["in_top_n"])
        rows = self._df[mask].sort_values("rank")
        return [str(s) for s in rows["symbol"].tolist()]

    def score_at(self, ts_ms: int, symbol: str) -> float:
        """Return the Clenow score for `symbol` at exactly `ts_ms`."""
        mask = (self._df["timestamp"] == ts_ms) & (self._df["symbol"] == symbol.upper())
        rows = self._df[mask]
        if rows.empty:
            return 0.0
        return float(rows.iloc[0]["score"])

    def n_rebalances(self) -> int:
        return len(self._rebalance_ts)

    def rebalance_timestamps(self) -> list[int]:
        return [int(t) for t in self._rebalance_ts]


# ══════════════════════════════════════════════════════════════════════
# Offline builder helpers
# ══════════════════════════════════════════════════════════════════════


def rank_weights_from_scores(
    scores: dict[str, float],
    *,
    top_n: int = 20,
    weighting: str = "inverse_rank",
) -> dict[str, tuple[int, float, bool]]:
    """Rank symbols by score, apply top-N cutoff, compute weights.

    Args:
        scores: {symbol: score} — higher is better. Symbols with score <= 0
            are automatically excluded from top-N.
        top_n: number of positions to hold
        weighting: "equal" or "inverse_rank". inverse_rank gives rank 1
            weight N, rank 2 weight N-1, ..., rank N weight 1, then normalized.

    Returns:
        {symbol: (rank, weight, in_top_n)} for ALL input symbols (not just
        top-N). Non-top-N symbols have weight=0.0 and in_top_n=False.
    """
    positive = {s: v for s, v in scores.items() if v > 0}
    sorted_symbols = sorted(positive.keys(), key=lambda s: -positive[s])
    top = sorted_symbols[:top_n]

    if weighting == "equal":
        w = 1.0 / len(top) if top else 0.0
        top_weights = {s: w for s in top}
    elif weighting == "inverse_rank":
        n = len(top)
        raw = [(n - i) for i in range(n)]
        total = sum(raw) or 1
        top_weights = {top[i]: raw[i] / total for i in range(n)}
    else:
        raise ValueError(f"unknown weighting: {weighting}")

    out: dict[str, tuple[int, float, bool]] = {}
    for i, symbol in enumerate(top, start=1):
        out[symbol] = (i, top_weights[symbol], True)
    # Non-top-N symbols get rank beyond top_n with weight 0
    rank_counter = len(top) + 1
    for symbol, score in sorted(scores.items(), key=lambda x: -x[1]):
        if symbol in out:
            continue
        out[symbol] = (rank_counter, 0.0, False)
        rank_counter += 1
    return out


def write_rank_cache_parquet(
    rebalance_rows: list[dict],
    out_path: str | Path,
) -> None:
    """Write a list of rebalance rows to Parquet in the expected schema."""
    if not rebalance_rows:
        raise ValueError("rebalance_rows is empty")
    df = pd.DataFrame(rebalance_rows)
    required = {"timestamp", "symbol", "score", "rank", "weight", "in_top_n"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"rebalance_rows missing columns: {missing}")
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df), p, compression="snappy")
    log.info("rank_cache_written", path=str(p), rows=len(df),
             rebalances=df["timestamp"].nunique())
