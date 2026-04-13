"""Meta-labeling feature builder — Phase 0 MVP.

Builds a feature dict from (Signal, features pd.Series, optional
PortfolioSnapshot) at signal emission time. These features are stored
into `signal_audit.features_json` and read later by the meta-labeling
training pipeline (Phase 2).

**Phase 0 scope:** ~15 easily-available features from the existing
FeatureEngine output + signal metadata + optional portfolio snapshot.
The full 24-feature target set from the research memo is the Phase 2
goal — when we actually train, we can extend the builder without a
schema change because features are stored as JSON.

**Design rules:**
1. No lookahead. Every feature is computable from data available at the
   signal's candle close time — nothing from future bars.
2. Missing features get `None`, not a default value. `None` in JSON is
   explicit "not available"; 0 is ambiguous.
3. Feature keys are stable. Once a key appears in `FEATURE_KEYS` it
   never changes meaning. Deprecated features are tombstoned with a
   `_deprecated` suffix.
4. Feature values are floats (or None). No strings, no nested dicts.
   This makes DataFrame construction for training painless.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import pandas as pd

from src.m3s.types import PortfolioSnapshot
from src.utils.types import Signal, SignalAction

if TYPE_CHECKING:
    from src.m3s.hooks import M3S
    from src.m3s.signal_filter.strategy_history import StrategyHistoryCache


# Stable feature key list. Order matters for reproducibility; new
# features go at the END of the list. Old harvested rows are
# backward-compatible: they'll have `None` for new features, which the
# training pipeline fills with 0 (equivalent to ignoring the feature).
FEATURE_KEYS: list[str] = [
    # Signal-derived (2)
    "signal_direction",         # +1 LONG / -1 SHORT / 0 CLOSE
    "signal_confidence",        # [0, 1]

    # Market microstructure at signal close (4)
    "atr_14_pct",               # ATR_14 / close
    "bb_width_pct",             # (BBU_20 - BBL_20) / close
    "rsi_14",                   # 0-100
    "close_over_ema_21",        # (close / EMA_21) - 1

    # Regime (1)
    "adx_14",                   # 0-100

    # Time-of-day (3)
    "hour_of_day_sin",          # sin(2π × hour / 24)
    "hour_of_day_cos",          # cos(2π × hour / 24)
    "day_of_week",               # 0-6

    # Book-level (4) — require a PortfolioSnapshot
    "portfolio_equity",          # float
    "portfolio_drawdown_pct",    # [0, 1]
    "portfolio_hwm",             # float
    "n_strategies_tracked",      # int

    # Price reference (1) — not a feature for training but useful for
    # reconstruction and debugging
    "entry_price_ref",

    # ── Session 22 sprint Day 0.5 enrichment (9 keys → 24 total) ──
    # These are populated where computation is cheap and deferred to None
    # where data is not yet available. Backward-compatible: old rows = None.

    # Market microstructure extended (2) — from FeatureEngine auto-cols
    "volume_zscore_20",          # (volume - v20_mean) / v20_std
    "macd_hist_zscore_50",       # (MACD_hist - mh50_mean) / mh50_std

    # Regime extended (1) — from M3S RegimeClassifier (None until wired)
    "vol_regime_idx",            # 0=low, 1=mid, 2=high volatility regime

    # Funding (1) — from features Series if strategy has funding feed
    "funding_rate_abs",          # |funding_rate| at signal time

    # Strategy history (4) — from audit lookup at signal time (None until wired)
    "strategy_win_rate_last_20", # [0, 1]
    "strategy_pnl_z_last_20",    # z-score of last 20 PnLs
    "hours_since_last_signal",   # gap between signals
    "bars_since_last_trade_close", # gap between trade closes

    # M3S allocator (1) — current weight for this strategy (None until wired)
    "m3s_alloc_weight_now",      # [0, 1] alloc weight assigned now
]


def build_meta_features(
    signal: Signal,
    features: pd.Series | None,
    snapshot: PortfolioSnapshot | None = None,
    *,
    strategy_history: "StrategyHistoryCache | None" = None,
    m3s: "M3S | None" = None,
) -> dict[str, Any]:
    """Build a flat feature dict for one signal.

    Args:
        signal: the primary model's Signal. Must have `symbol`, `action`,
            `confidence`. `timestamp` (ms) is used for time-of-day features.
        features: the pd.Series passed to `on_features` — contains the
            strategy's view of indicators at signal time. May be None
            if the signal didn't come from the normal feature pipeline
            (e.g., funding carry synthetic feed).
        snapshot: optional M3S PortfolioSnapshot. If None, book-level
            features are None.
        strategy_history: optional in-process ring buffer that tracks
            recent win rate / pnl stats / signal cadence per strategy.
            If None, the 4 strategy-history keys stay None (backward
            compat — equivalent to ignoring those features at train time).
        m3s: optional M3S facade. If provided and has a `last_allocation()`,
            `m3s_alloc_weight_now` is populated for this signal's strategy.

    Returns:
        dict with keys from FEATURE_KEYS; missing values are None.
        Numeric values are always Python floats (not numpy scalars) so
        the dict is JSON-serializable.
    """
    out: dict[str, Any] = {k: None for k in FEATURE_KEYS}

    # ── Signal-derived ─────────────────────────────────────────────
    out["signal_direction"] = _direction_of(signal.action)
    out["signal_confidence"] = (
        float(signal.confidence) if signal.confidence is not None else None
    )

    # ── Market microstructure ──────────────────────────────────────
    if features is not None:
        close = _getf(features, "close")
        if close is not None and close > 0:
            atr_14 = _getf(features, "ATR_14")
            if atr_14 is not None:
                out["atr_14_pct"] = atr_14 / close

            bbu = _getf(features, "BBU_20")
            bbl = _getf(features, "BBL_20")
            if bbu is not None and bbl is not None:
                out["bb_width_pct"] = (bbu - bbl) / close

            ema_21 = _getf(features, "EMA_21")
            if ema_21 is not None and ema_21 > 0:
                out["close_over_ema_21"] = (close / ema_21) - 1.0

        out["rsi_14"] = _getf(features, "RSI_14")
        out["adx_14"] = _getf(features, "ADX_14")

    # ── Time-of-day ────────────────────────────────────────────────
    ts_ms = int(signal.timestamp or 0)
    if ts_ms > 0:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        hour = dt.hour
        out["hour_of_day_sin"] = math.sin(2 * math.pi * hour / 24.0)
        out["hour_of_day_cos"] = math.cos(2 * math.pi * hour / 24.0)
        out["day_of_week"] = float(dt.weekday())

    # ── Book-level ─────────────────────────────────────────────────
    if snapshot is not None:
        out["portfolio_equity"] = float(snapshot.equity)
        out["portfolio_drawdown_pct"] = float(snapshot.drawdown_pct)
        out["portfolio_hwm"] = float(snapshot.hwm)
        out["n_strategies_tracked"] = float(len(snapshot.per_strategy))

    # ── Reference price ────────────────────────────────────────────
    if signal.entry_price is not None:
        out["entry_price_ref"] = float(signal.entry_price)
    elif features is not None:
        close = _getf(features, "close")
        if close is not None:
            out["entry_price_ref"] = close

    # ── Session 22 enrichment (populate what we can) ───────────────
    if features is not None:
        # Rolling z-scores from FeatureEngine auto-computed columns
        out["volume_zscore_20"] = _getf(features, "VOL_ZSCORE_20")
        out["macd_hist_zscore_50"] = _getf(features, "MACD_HIST_ZSCORE_50")

        # Funding rate — present on strategies consuming the funding feed
        funding = _getf(features, "funding_rate")
        if funding is not None:
            out["funding_rate_abs"] = abs(funding)

    # ── Strategy history (populated when cache is provided) ───────
    if strategy_history is not None:
        ts_for_history = int(signal.timestamp or 0)
        if ts_for_history > 0:
            stats = strategy_history.stats_for(
                signal.strategy_name or "", ts_for_history
            )
            if stats.strategy_win_rate_last_20 is not None:
                out["strategy_win_rate_last_20"] = float(stats.strategy_win_rate_last_20)
            if stats.strategy_pnl_z_last_20 is not None:
                out["strategy_pnl_z_last_20"] = float(stats.strategy_pnl_z_last_20)
            if stats.hours_since_last_signal is not None:
                out["hours_since_last_signal"] = float(stats.hours_since_last_signal)
            if stats.bars_since_last_trade_close is not None:
                out["bars_since_last_trade_close"] = float(
                    stats.bars_since_last_trade_close
                )

    # ── M3S allocator current weight ──────────────────────────────
    if m3s is not None:
        try:
            alloc = m3s.last_allocation()
            if alloc is not None and signal.strategy_name:
                w = alloc.weights.get(signal.strategy_name)
                if w is not None:
                    out["m3s_alloc_weight_now"] = float(w)
        except Exception:
            # M3S facade missing method or allocation not yet computed —
            # silently skip. Not a bug, just early-boot.
            pass

    # vol_regime_idx: still intentionally None. Wiring it requires a
    # stateful RegimeClassifier service that updates on every rebalance
    # — deferred to a future phase with its own hot-path perf budget.

    return out


def _direction_of(action: SignalAction) -> float | None:
    """Map SignalAction to ±1 / 0 / None."""
    if action == SignalAction.LONG:
        return 1.0
    if action == SignalAction.SHORT:
        return -1.0
    if action == SignalAction.CLOSE:
        return 0.0
    return None


def _getf(features: pd.Series, key: str) -> float | None:
    """Safely extract a float from a pd.Series. Returns None if missing
    or NaN. Handles numpy scalars via .item() when needed."""
    try:
        if key not in features.index:
            return None
    except AttributeError:
        return None
    val = features[key]
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN check (NaN != NaN)
        return None
    return f
