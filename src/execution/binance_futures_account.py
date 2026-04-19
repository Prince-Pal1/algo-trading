"""Account + margin cache for Binance Futures.

Polls /fapi/v2/account periodically (30s TTL by default). Exposes
synchronous lookups to the executor so its hot path doesn't hit REST.

Used for:
  - Available margin check before each order
  - Leverage visibility (configured per symbol)
  - Wallet balance for sizing calculations
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.execution.binance_futures_client import BinanceFuturesClient
from src.utils.logger import get_logger

log = get_logger("binance_futures_account")


@dataclass
class AccountSnapshot:
    total_wallet_balance: float
    total_margin_balance: float
    total_unrealized_pnl: float
    available_balance: float
    positions_count: int
    fetched_at: float = field(default_factory=time.time)


class BinanceFuturesAccountManager:
    """30s-TTL cache over Binance's /fapi/v2/account endpoint."""

    def __init__(self, client: BinanceFuturesClient, ttl_seconds: float = 30.0):
        self._client = client
        self._ttl = ttl_seconds
        self._snapshot: AccountSnapshot | None = None

    async def refresh(self) -> AccountSnapshot:
        """Force-refresh regardless of TTL."""
        data = await self._client.get_account()
        snap = AccountSnapshot(
            total_wallet_balance=float(data.get("totalWalletBalance", 0)),
            total_margin_balance=float(data.get("totalMarginBalance", 0)),
            total_unrealized_pnl=float(data.get("totalUnrealizedProfit", 0)),
            available_balance=float(data.get("availableBalance", 0)),
            positions_count=sum(
                1 for p in data.get("positions", []) if float(p.get("positionAmt", 0)) != 0
            ),
        )
        self._snapshot = snap
        log.debug(
            "account_refreshed",
            wallet=round(snap.total_wallet_balance, 2),
            available=round(snap.available_balance, 2),
            open_positions=snap.positions_count,
        )
        return snap

    async def get(self) -> AccountSnapshot:
        """Return a cached snapshot, refreshing if stale."""
        if self._snapshot is None or (time.time() - self._snapshot.fetched_at) > self._ttl:
            await self.refresh()
        return self._snapshot  # type: ignore[return-value]

    async def available_margin(self) -> float:
        snap = await self.get()
        return snap.available_balance

    async def can_afford(self, notional_usd: float, leverage: int = 1,
                         buffer_mult: float = 1.5) -> bool:
        """Check if notional × (1/leverage) × buffer_mult ≤ available margin."""
        snap = await self.get()
        required = (notional_usd / max(1, leverage)) * buffer_mult
        return snap.available_balance >= required

    def invalidate(self) -> None:
        """Force next get() to refresh (call after a fill changes balance)."""
        self._snapshot = None
