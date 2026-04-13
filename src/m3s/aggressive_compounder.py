"""Fixed-% compounder for the aggressive Tier 5 sub-book."""

from __future__ import annotations

from dataclasses import dataclass, field


_MS_PER_DAY = 86_400_000
_MS_PER_WEEK = 7 * _MS_PER_DAY


@dataclass
class AggressiveCompounderConfig:
    position_size_pct: float = 0.05
    allow_size_override: bool = True
    override_cap_pct: float = 0.10
    max_drawdown_pct: float = 0.50
    daily_loss_limit_pct: float = 0.30
    cooldown_days_after_halt: int = 7
    weekly_refund_enabled: bool = True
    weekly_refund_cap_pct: float = 1.0
    max_concurrent_positions: int = 3


@dataclass
class AggressiveCompounderState:
    initial_equity: float
    current_equity: float
    day_start_equity: float
    day_start_ts_ms: int = 0
    halt_until_ts_ms: int = 0
    last_refund_ts_ms: int = 0

    def day_loss_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return max(0.0, (self.day_start_equity - self.current_equity) / self.day_start_equity)

    def total_drawdown_pct(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return max(0.0, (self.initial_equity - self.current_equity) / self.initial_equity)


class AggressiveRetailCompounder:
    def __init__(
        self,
        *,
        initial_equity: float,
        config: AggressiveCompounderConfig | None = None,
    ) -> None:
        self._cfg = config or AggressiveCompounderConfig()
        self._state = AggressiveCompounderState(
            initial_equity=float(initial_equity),
            current_equity=float(initial_equity),
            day_start_equity=float(initial_equity),
        )

    @property
    def config(self) -> AggressiveCompounderConfig:
        return self._cfg

    @property
    def state(self) -> AggressiveCompounderState:
        return self._state

    def update_equity(self, equity: float, ts_ms: int) -> None:
        self._roll_day_if_needed(ts_ms)
        self._state.current_equity = float(equity)

    def _roll_day_if_needed(self, ts_ms: int) -> None:
        if self._state.day_start_ts_ms == 0:
            self._state.day_start_ts_ms = ts_ms - (ts_ms % _MS_PER_DAY)
            return
        day_boundary = self._state.day_start_ts_ms + _MS_PER_DAY
        if ts_ms >= day_boundary:
            self._state.day_start_ts_ms = ts_ms - (ts_ms % _MS_PER_DAY)
            self._state.day_start_equity = self._state.current_equity

    def size_next_trade(
        self,
        *,
        conviction: float = 0.5,
        open_positions: int = 0,
    ) -> float:
        if open_positions >= self._cfg.max_concurrent_positions:
            return 0.0
        pct = self._cfg.position_size_pct
        if self._cfg.allow_size_override and conviction >= 0.9:
            pct = min(self._cfg.override_cap_pct, pct * 1.5)
        return max(0.0, self._state.current_equity * pct)

    def can_trade(self, ts_ms: int) -> tuple[bool, str]:
        if ts_ms < self._state.halt_until_ts_ms:
            return (False, "cooldown")
        if self._state.current_equity <= 0:
            return (False, "sub_book_zeroed")
        self._roll_day_if_needed(ts_ms)
        # Total DD check BEFORE daily — the total is the more severe kill.
        if self._state.total_drawdown_pct() >= self._cfg.max_drawdown_pct:
            self._state.halt_until_ts_ms = ts_ms + self._cfg.cooldown_days_after_halt * _MS_PER_DAY
            return (False, "max_drawdown_halt")
        if self._state.day_loss_pct() >= self._cfg.daily_loss_limit_pct:
            return (False, "daily_loss_limit")
        return (True, "ok")

    def weekly_refund(self, main_account_equity: float, ts_ms: int) -> float:
        if not self._cfg.weekly_refund_enabled:
            return 0.0
        if ts_ms - self._state.last_refund_ts_ms < _MS_PER_WEEK:
            return 0.0
        if main_account_equity <= 0:
            return 0.0
        target = main_account_equity * self._cfg.weekly_refund_cap_pct
        if self._state.current_equity >= target:
            return 0.0
        refund = target - self._state.current_equity
        self._state.current_equity = target
        self._state.day_start_equity = target
        self._state.initial_equity = max(self._state.initial_equity, target)
        self._state.last_refund_ts_ms = ts_ms
        return refund
