"""Lock-free versioned snapshot of portfolio state (RCU-style)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PortfolioViewSnapshot:
    version: int
    ts_ms: int
    equity: float
    cash: float
    floating_pnl: float
    used_margin: float
    aggregate_leverage: float
    open_position_count: int
    institutional_equity: float
    aggressive_equity: float


_EMPTY = PortfolioViewSnapshot(
    version=0,
    ts_ms=0,
    equity=0.0,
    cash=0.0,
    floating_pnl=0.0,
    used_margin=0.0,
    aggregate_leverage=0.0,
    open_position_count=0,
    institutional_equity=0.0,
    aggressive_equity=0.0,
)


class VersionedPortfolioView:
    def __init__(self) -> None:
        self._current: PortfolioViewSnapshot = _EMPTY

    def read(self) -> PortfolioViewSnapshot:
        return self._current

    def version(self) -> int:
        return self._current.version

    def publish(
        self,
        *,
        ts_ms: int,
        equity: float,
        cash: float,
        floating_pnl: float,
        used_margin: float,
        aggregate_leverage: float,
        open_position_count: int,
        institutional_equity: float,
        aggressive_equity: float,
    ) -> PortfolioViewSnapshot:
        new_version = self._current.version + 1
        snap = PortfolioViewSnapshot(
            version=new_version,
            ts_ms=ts_ms,
            equity=equity,
            cash=cash,
            floating_pnl=floating_pnl,
            used_margin=used_margin,
            aggregate_leverage=aggregate_leverage,
            open_position_count=open_position_count,
            institutional_equity=institutional_equity,
            aggressive_equity=aggressive_equity,
        )
        self._current = snap
        return snap
