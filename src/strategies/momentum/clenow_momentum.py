"""Cross-Sectional Altcoin Momentum strategy (Strategy B — Phase 3b-3).

Clenow "Stocks on the Move" playbook adapted for crypto. Per-symbol
instances share a `RankCache` (precomputed offline by
`scripts/build_momentum_rank_cache.py`) that tells each instance its own
symbol's rank-weight at each rebalance. The strategy emits LONG when its
symbol is in the top-N and CLOSE when it drops out.

**v1 scope hack:** no multi-symbol backtest engine. The rank cache is the
scope hack — it's a static lookup table computed once offline, then each
per-symbol `BacktestEngine.run()` reads its own rank from the shared cache.
Per-symbol equity curves are aggregated downstream via `scripts.backtest compare`.

**Signal shape:**
- LONG: when `rank_cache.in_top_n(now_ms, symbol)` is True AND `self._position == "FLAT"`
- CLOSE: when `rank_cache.in_top_n` returns False AND `self._position == "LONG"`
- Position size: `risk_pct = base_risk × rank_weight` (rank-weighted scaling)
- No stop loss (Clenow uses rank-based exits, not SL); ATR trailing stop available
  as optional safety rail

**Config (strategies.toml `[clenow_momentum]` section):**

    enabled = true
    risk_profile = "SAFE"
    markets = [...30 altcoins...]
    timeframe = "1d"
    max_risk_per_trade = 0.01
    rank_cache_path = "data/historical/momentum_rank_cache.parquet"
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.strategies.base import BaseStrategy
from src.strategies.ranking import RankCache
from src.utils.config import get_config
from src.utils.logger import get_logger
from src.utils.types import RiskProfile, Signal, SignalAction

log = get_logger("clenow_momentum")


class ClenowMomentumStrategy(BaseStrategy):
    """Per-symbol Clenow momentum with shared rank cache."""
    fee_style = "position"

    def __init__(
        self,
        name: str,
        markets: list[str],
        timeframe: str = "1d",
        risk_profile: RiskProfile = RiskProfile.SAFE,
        max_risk_per_trade: float = 0.01,
        *,
        rank_cache: RankCache | None = None,
        rank_cache_path: str | None = None,
        cooldown_bars: int = 0,
    ):
        super().__init__(name, markets, timeframe, risk_profile, max_risk_per_trade)

        if rank_cache is None and rank_cache_path is None:
            raise ValueError(
                "ClenowMomentumStrategy requires either `rank_cache` or "
                "`rank_cache_path` — run scripts/build_momentum_rank_cache.py first"
            )
        if rank_cache is None:
            rank_cache = RankCache.from_parquet(rank_cache_path)
        self._rank_cache = rank_cache

        if cooldown_bars < 0:
            raise ValueError(f"cooldown_bars must be >= 0, got {cooldown_bars}")
        self.cooldown_bars = int(cooldown_bars)
        self._bars_since_exit: int = 9999
        self._bars_in_position: int = 0

    # ── Strategy contract ───────────────────────────────────────────

    def on_features(
        self,
        symbol: str,
        timeframe: str,
        features: pd.Series,
    ) -> Signal | None:
        """Check rank cache for this symbol; emit LONG/CLOSE accordingly."""
        close = float(features.get("close", 0.0))
        if close <= 0:
            return None

        # Timestamp of this bar — prefer `timestamp` column, else use 0
        ts_ms = int(features.get("timestamp", 0))

        # Query the rank cache for "as of now"
        weight = self._rank_cache.get_weight(ts_ms, symbol)
        in_top_n = weight > 0.0

        # ── State rotation ─────────────────────────────────────────
        if self._position == "LONG":
            self._bars_in_position += 1
        else:
            self._bars_since_exit += 1

        # ── EXIT logic ─────────────────────────────────────────────
        if self._position == "LONG":
            if not in_top_n:
                return self._exit_signal(symbol, close, "rank_drop")
            return None  # hold

        # ── ENTRY logic ────────────────────────────────────────────
        if self._position == "FLAT" and in_top_n:
            if self._bars_since_exit < self.cooldown_bars:
                return None
            return self._entry_signal(symbol, close, weight)

        return None

    # ── Signal builders ─────────────────────────────────────────────

    def _entry_signal(self, symbol: str, close: float, rank_weight: float) -> Signal:
        """Emit LONG with risk_pct scaled by the symbol's rank_weight.

        rank_weight ∈ (0, 1] where 1 is the highest rank.
        """
        self._bars_in_position = 0
        # Scale: base_risk × rank_weight → top-ranked symbols get higher sizing
        scaled_risk = self.max_risk_per_trade * rank_weight
        return Signal(
            symbol=symbol,
            action=SignalAction.LONG,
            confidence=1.0,
            strategy_name=self.name,
            timeframe=self.timeframe,
            entry_price=close,
            stop_loss=None,              # Clenow uses rank-based exits
            take_profit=None,
            risk_pct=scaled_risk,
            metadata={"entry_reason": "rank_in_top_n", "rank_weight": rank_weight},
        )

    def _exit_signal(self, symbol: str, close: float, reason: str) -> Signal:
        self._bars_since_exit = 0
        self._bars_in_position = 0
        return Signal(
            symbol=symbol,
            action=SignalAction.CLOSE,
            confidence=1.0,
            strategy_name=self.name,
            timeframe=self.timeframe,
            entry_price=close,
            stop_loss=None,
            take_profit=None,
            risk_pct=None,
            metadata={"exit_reason": reason},
        )

    # ── Factory from TOML config ───────────────────────────────────

    @classmethod
    def from_config(cls, name: str) -> ClenowMomentumStrategy:
        cfg = get_config()
        section = cfg.get_strategy(name)
        rank_cache_path = section.get(
            "rank_cache_path", "data/historical/momentum_rank_cache.parquet"
        )
        return cls(
            name=name,
            markets=section.get("markets", []),
            timeframe=section.get("timeframe", "1d"),
            risk_profile=RiskProfile(section.get("risk_profile", "SAFE")),
            max_risk_per_trade=section.get("max_risk_per_trade", 0.01),
            rank_cache_path=rank_cache_path,
            cooldown_bars=section.get("cooldown_bars", 0),
        )
