"""Broker-accurate leveraged position accounting — Phase G.2a of the gold trading plan.

This module implements the CFD broker margin model as it actually works at
IC Markets / XM / Pepperstone / Tickmill — NOT as my earlier (wrong) mental
model where positions force-close at per-position margin wipe. The correct
model was caught by Prince during plan review:

CORRECT CFD BROKER MODEL:
1. Position uses `notional / leverage` as locked margin at open.
2. Floating P&L updates equity in real-time; the position STAYS OPEN even
   when floating P&L greatly exceeds the initial position margin.
3. The broker force-closes ONLY when ACCOUNT-LEVEL margin level drops below
   the stop-out threshold (50% at XM and IC Markets):
       margin_level = equity / used_margin
4. Between open and stop-out, there is a LARGE adverse runway the trader can
   sit through. Only a strategy-set SL or manual close takes the position
   off before that.

Worked example that proves the model:
  Account: $1000, position 5% = $50 margin, leverage 1000×
  → notional = $50,000 = 20.8 oz of gold at $2400
  → floating P&L at 0.1% adverse = -$50 → equity $950, margin level 1900% → OPEN
  → floating P&L at 1.0% adverse = -$500 → equity $500, margin level 1000% → OPEN
  → floating P&L at 1.95% adverse ≈ -$975 → equity $25, margin level 50% → STOP-OUT

The trader's edge in this style: ~2% adverse runway on a 5% position at 1000×
is ~10× the typical M5 gold ATR. Plenty of breathing room for judgment-based
exits.

This module provides the `Book` data structure that tracks margin correctly
and enables honest leveraged backtests.

Two sub-books are supported via the `sub_book` field on each position:
- "institutional": Tiers 1-4, subject to M3S Compounder, aggregate leverage
  caps, vol-targeted sizing, HWM-gated compounding.
- "aggressive": Tier 5, subject to AggressiveRetailCompounder, fixed-%
  sizing, daily/weekly kill switches, weekly refund from main account.

The two sub-books have INDEPENDENT margin accounting. Aggressive sub-book
hitting its 50% DD kill switch does NOT force-close institutional positions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterator

# ── Constants ────────────────────────────────────────────────────────────

# Broker stop-out threshold as a ratio of equity to used margin.
# XM: 50%. IC Markets: 50%. Pepperstone: 50%. Most offshore CFD brokers use
# 50% stop-out. Institutional/ECN sometimes uses 100% (no breathing room at
# all). We default to 50% as the retail norm.
DEFAULT_STOP_OUT_LEVEL: float = 0.50

# Sub-book labels — these must match config keys in settings.toml
SUB_BOOK_INSTITUTIONAL = "institutional"
SUB_BOOK_AGGRESSIVE = "aggressive"


# ── Data types ───────────────────────────────────────────────────────────


@dataclass
class LeveragedPosition:
    """A single open leveraged position.

    Fields:
        id: unique integer ID within a Book (auto-assigned)
        side: "LONG" or "SHORT"
        entry_idx: bar index at which the position was opened (for backtest
            path reconstruction). Live engine can use ts_ms instead.
        entry_price: fill price at open (includes slippage)
        quantity: position size in INSTRUMENT UNITS (e.g., oz of gold), not
            lots. Caller converts lots ↔ units via the Instrument registry.
        leverage: effective leverage ratio at which the position was opened
        stop_loss: trader-set SL price, or None for "no hard stop" (use
            broker stop-out as last resort)
        take_profit: trader-set TP price, or None
        entry_ts_ms: unix ms timestamp of the open
        entry_margin: locked margin at open = notional / leverage.
            Once set, this is IMMUTABLE for the life of the position
            (floating P&L doesn't change the initial margin requirement).
        strategy_name: which strategy opened this position (for attribution)
        sub_book: "institutional" | "aggressive" — determines which sub-book
            accounting applies
    """
    id: int
    side: str
    entry_idx: int
    entry_price: float
    quantity: float
    leverage: float
    stop_loss: float | None
    take_profit: float | None
    entry_ts_ms: int
    entry_margin: float
    strategy_name: str
    sub_book: str = SUB_BOOK_INSTITUTIONAL

    def notional(self) -> float:
        """Current notional exposure = entry_price × quantity.

        Note: we use ENTRY price, not current mark, for the notional in the
        margin calculation. Brokers lock initial margin at open and don't
        re-evaluate as the price moves — only the floating P&L changes.
        """
        return self.entry_price * self.quantity

    def unrealized_pnl(self, mark_price: float) -> float:
        """Floating P&L at the given mark price.

        Long: (mark - entry) × quantity
        Short: (entry - mark) × quantity
        """
        if self.side == "LONG":
            return (mark_price - self.entry_price) * self.quantity
        return (self.entry_price - mark_price) * self.quantity

    def unrealized_pnl_per_unit_margin(self, mark_price: float) -> float:
        """P&L / initial margin. Used for worst-first stop-out ordering.

        A position with -100% unrealized (wiped its margin) has
        pnl_per_unit_margin = -1.0. One still up 50% has +0.5.
        Ascending sort → worst positions first.
        """
        if self.entry_margin <= 0:
            return 0.0
        return self.unrealized_pnl(mark_price) / self.entry_margin


@dataclass
class SubBookState:
    """Per-sub-book accounting (institutional or aggressive).

    A Book contains TWO of these — one per sub-book — and tracks them
    independently. Cash and P&L are NOT fungible across sub-books during
    normal operation; only the weekly Monday sweep in the Aggressive
    Compounder can move cash between them.

    Accounting model (matches CFD broker conventions at XM/IC Markets):
        cash       = total account balance. CFD brokers don't deduct
                     margin from cash on position open; cash only moves
                     on REALIZED P&L (position close). Opening a position
                     locks margin VIEW but doesn't reduce the cash balance.
        used_margin = sum(entry_margin) across open positions — computed
                     on-demand, not stored, so adding/closing positions
                     automatically updates it.
        floating_pnl = sum(unrealized_pnl) across open positions — computed
                     on-demand from current mark prices.
        equity     = cash + floating_pnl. This is what the broker shows in
                     the "Equity" field on the account summary.
        free_margin = equity - used_margin. What's available for new positions.
        margin_level = equity / used_margin. Broker uses this for stop-out.

    Fields:
        cash: total account balance for this sub-book. Starts at initial_cash,
            only changes on position close (realized P&L).
        initial_cash: starting cash allocated to this sub-book at engine start.
            Used for DD % calculations.
        positions: dict of id → LeveragedPosition for this sub-book only.
        realized_pnl_today: running sum of realized P&L for the current
            trading day. Reset at the daily rollover. Used for daily
            loss limit checks.
        realized_pnl_total: running sum of realized P&L over the entire
            backtest. Used for total DD calculations.
        peak_equity: highest equity this sub-book has ever reached. Used
            for DD % calculation.
        stop_out_count: how many times this sub-book has hit a broker
            stop-out during the backtest. Reported in the final metrics.
    """
    cash: float
    initial_cash: float
    positions: dict[int, LeveragedPosition] = field(default_factory=dict)
    realized_pnl_today: float = 0.0
    realized_pnl_total: float = 0.0
    peak_equity: float = 0.0
    stop_out_count: int = 0

    def __post_init__(self) -> None:
        if self.peak_equity <= 0:
            self.peak_equity = self.cash

    def used_margin(self) -> float:
        """Sum of locked margin across all open positions in this sub-book."""
        return sum(p.entry_margin for p in self.positions.values())

    def floating_pnl(self, mark_prices: dict[int, float]) -> float:
        """Total unrealized P&L across all open positions.

        Args:
            mark_prices: dict of position_id → current mark price. Missing
                positions contribute 0 to floating P&L (caller's bug).
        """
        return sum(
            p.unrealized_pnl(mark_prices.get(p.id, p.entry_price))
            for p in self.positions.values()
        )

    def equity(self, mark_prices: dict[int, float]) -> float:
        """Total account equity = cash + floating P&L.

        Matches the broker's "Equity" field on the account summary. This
        is the number that drives the margin-level calculation.
        """
        return self.cash + self.floating_pnl(mark_prices)

    def free_margin(self, mark_prices: dict[int, float]) -> float:
        """Free margin available for new positions = equity - used_margin."""
        return self.equity(mark_prices) - self.used_margin()

    def margin_level(self, mark_prices: dict[int, float]) -> float:
        """Broker's margin level as a ratio (0.50 = 50% stop-out).

        Returns +inf if no margin is currently in use (no open positions).
        This means "infinitely safe" in broker terms — stop-out never fires
        when you have no positions.
        """
        um = self.used_margin()
        if um <= 0:
            return float("inf")
        return self.equity(mark_prices) / um

    def drawdown_pct(self, mark_prices: dict[int, float]) -> float:
        """Current drawdown as a fraction in [0, 1].

        Uses the max(peak_equity, current_equity) to account for the case
        where the sub-book has grown above its starting allocation.
        """
        current = self.equity(mark_prices)
        peak = max(self.peak_equity, current)
        if peak <= 0:
            return 0.0
        dd = (peak - current) / peak
        return max(0.0, dd)

    def update_peak(self, mark_prices: dict[int, float]) -> None:
        """Refresh the peak equity tracker."""
        current = self.equity(mark_prices)
        if current > self.peak_equity:
            self.peak_equity = current


@dataclass
class Book:
    """Top-level leveraged book tracking two independent sub-books.

    The Book is the main entry point from BacktestEngine and PaperExecutor.
    It owns two SubBookState instances and provides convenience methods
    that aggregate across both.

    Usage:
        book = Book.new(institutional_cash=7_000, aggressive_cash=3_000)
        pos = book.open_position(
            side="LONG", entry_price=2400.0, quantity=10.0,
            leverage=50.0, stop_loss=2380.0, take_profit=2450.0,
            entry_ts_ms=1700000000000, strategy_name="donchian_gold",
            sub_book="institutional",
        )
        # ... time passes, price moves to 2410 ...
        marks = {pos.id: 2410.0}
        print(book.total_equity(marks))  # cash + floating P&L
        print(book.sub_book(pos.sub_book).margin_level(marks))  # ~2x above stop-out

        # Close the position
        closed = book.close_position(pos.id, exit_price=2410.0, commission=6.0)
    """
    institutional: SubBookState
    aggressive: SubBookState
    stop_out_level: float = DEFAULT_STOP_OUT_LEVEL
    _next_id: int = 0

    @classmethod
    def new(
        cls,
        *,
        institutional_cash: float,
        aggressive_cash: float,
        stop_out_level: float = DEFAULT_STOP_OUT_LEVEL,
    ) -> "Book":
        """Construct a Book with the given initial allocation per sub-book."""
        return cls(
            institutional=SubBookState(
                cash=institutional_cash,
                initial_cash=institutional_cash,
                peak_equity=institutional_cash,
            ),
            aggressive=SubBookState(
                cash=aggressive_cash,
                initial_cash=aggressive_cash,
                peak_equity=aggressive_cash,
            ),
            stop_out_level=stop_out_level,
        )

    def sub_book(self, name: str) -> SubBookState:
        """Return the named sub-book state. Raises ValueError on bad name."""
        if name == SUB_BOOK_INSTITUTIONAL:
            return self.institutional
        if name == SUB_BOOK_AGGRESSIVE:
            return self.aggressive
        raise ValueError(
            f"unknown sub_book {name!r}; must be "
            f"{SUB_BOOK_INSTITUTIONAL!r} or {SUB_BOOK_AGGRESSIVE!r}"
        )

    def all_positions(self) -> Iterator[LeveragedPosition]:
        """Iterate over all open positions across both sub-books."""
        yield from self.institutional.positions.values()
        yield from self.aggressive.positions.values()

    def total_cash(self) -> float:
        """Sum of cash across both sub-books."""
        return self.institutional.cash + self.aggressive.cash

    def total_used_margin(self) -> float:
        """Sum of used margin across both sub-books."""
        return self.institutional.used_margin() + self.aggressive.used_margin()

    def total_equity(self, mark_prices: dict[int, float]) -> float:
        """Sum of equity across both sub-books."""
        return (
            self.institutional.equity(mark_prices)
            + self.aggressive.equity(mark_prices)
        )

    # ── Position lifecycle ──────────────────────────────────────────

    def open_position(
        self,
        *,
        side: str,
        entry_price: float,
        quantity: float,
        leverage: float,
        stop_loss: float | None,
        take_profit: float | None,
        entry_ts_ms: int,
        entry_idx: int = 0,
        strategy_name: str = "",
        sub_book: str = SUB_BOOK_INSTITUTIONAL,
    ) -> LeveragedPosition:
        """Open a new leveraged position.

        Validates:
          - side ∈ {"LONG", "SHORT"}
          - leverage >= 1
          - entry_price > 0
          - quantity > 0
          - SL on the correct side of entry (longs: below; shorts: above)
          - there is enough free margin in the chosen sub-book to cover
            the initial margin requirement

        Returns the newly-created LeveragedPosition.
        Raises ValueError for any of the validation failures.
        """
        if side not in ("LONG", "SHORT"):
            raise ValueError(f"side must be LONG or SHORT, got {side!r}")
        if leverage < 1.0:
            raise ValueError(f"leverage must be >= 1, got {leverage}")
        if entry_price <= 0:
            raise ValueError(f"entry_price must be > 0, got {entry_price}")
        if quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {quantity}")
        if stop_loss is not None:
            if side == "LONG" and stop_loss >= entry_price:
                raise ValueError(
                    f"LONG stop_loss {stop_loss} must be below entry {entry_price}"
                )
            if side == "SHORT" and stop_loss <= entry_price:
                raise ValueError(
                    f"SHORT stop_loss {stop_loss} must be above entry {entry_price}"
                )

        sb = self.sub_book(sub_book)
        notional = entry_price * quantity
        entry_margin = notional / leverage

        # CFD broker accounting: cash does NOT move on position open.
        # We only check that free margin (equity - current used margin) is
        # sufficient to cover the new margin requirement.
        current_free_margin = sb.free_margin({
            p.id: p.entry_price for p in sb.positions.values()  # mark at entry for simplicity
        })
        # Use a 1-cent epsilon to avoid rejecting positions where margin
        # equals free margin to the nearest cent. Float precision can
        # produce $10000.0 > $10000.0 = True due to ULP differences,
        # which incorrectly rejects 1x-leverage strategies whose notional
        # exactly equals available equity (e.g., SwiftAlmaStrategy with
        # risk_pct == sl_pct → notional = equity at 1x). Discovered task #109.
        if entry_margin > current_free_margin + 0.01:
            raise ValueError(
                f"insufficient cash in {sub_book} sub-book: "
                f"need margin ${entry_margin:.2f}, "
                f"have free margin ${current_free_margin:.2f}"
            )

        pos = LeveragedPosition(
            id=self._next_id,
            side=side,
            entry_idx=entry_idx,
            entry_price=entry_price,
            quantity=quantity,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_ts_ms=entry_ts_ms,
            entry_margin=entry_margin,
            strategy_name=strategy_name,
            sub_book=sub_book,
        )
        self._next_id += 1
        sb.positions[pos.id] = pos
        # Cash is unchanged — CFD brokers don't move cash on position open.
        # The new position adds entry_margin to sb.used_margin() automatically
        # because used_margin() is computed from sb.positions on demand.
        return pos

    def close_position(
        self,
        position_id: int,
        exit_price: float,
        commission: float = 0.0,
    ) -> dict:
        """Close a position at the given exit price.

        Releases the locked initial margin back to cash and realizes the
        P&L (less commission) into cash. Returns a dict with the closed
        trade record for downstream attribution.
        """
        # Find which sub-book owns this position
        owning_sub_book_name = None
        owning_sub_book = None
        for name in (SUB_BOOK_INSTITUTIONAL, SUB_BOOK_AGGRESSIVE):
            sb = self.sub_book(name)
            if position_id in sb.positions:
                owning_sub_book_name = name
                owning_sub_book = sb
                break

        if owning_sub_book is None:
            raise KeyError(f"position {position_id} not found in any sub-book")

        pos = owning_sub_book.positions.pop(position_id)
        pnl = pos.unrealized_pnl(exit_price)
        realized = pnl - commission

        # CFD broker accounting: only the realized P&L moves cash. The
        # "entry_margin" was never deducted from cash in the first place
        # (it was just locked as "used_margin" view). Removing the position
        # from sb.positions automatically releases the used_margin view
        # because used_margin() is computed on demand.
        owning_sub_book.cash += realized
        owning_sub_book.realized_pnl_today += realized
        owning_sub_book.realized_pnl_total += realized

        return {
            "position_id": position_id,
            "sub_book": owning_sub_book_name,
            "strategy_name": pos.strategy_name,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "quantity": pos.quantity,
            "leverage": pos.leverage,
            "entry_margin": pos.entry_margin,
            "pnl": pnl,
            "commission": commission,
            "realized_pnl": realized,
            "entry_ts_ms": pos.entry_ts_ms,
        }

    # ── Broker stop-out logic ───────────────────────────────────────

    def check_stop_out(
        self,
        mark_prices: dict[int, float],
        sub_book_name: str,
    ) -> list[LeveragedPosition]:
        """Return positions that would be force-closed by broker stop-out.

        Runs the cascade-close loop: if the sub-book's margin level is
        below the stop-out threshold, repeatedly close the worst position
        (by unrealized_pnl / entry_margin) until margin level is back
        above threshold (or no positions remain).

        Returns the list of positions that WOULD be closed — caller is
        responsible for actually calling close_position() on each and
        recording the stop-out events.

        This split lets tests verify the ordering without mutating state.
        """
        sb = self.sub_book(sub_book_name)
        to_close: list[LeveragedPosition] = []

        # Simulate: iteratively pick the worst position and "close it"
        # from a working copy until margin level is safe. CFD accounting:
        # closing a position moves realized P&L into cash (the entry_margin
        # is just a view, not actual cash movement).
        working_positions = dict(sb.positions)
        working_cash = sb.cash

        def _used_margin() -> float:
            return sum(p.entry_margin for p in working_positions.values())

        def _floating_pnl() -> float:
            return sum(
                p.unrealized_pnl(mark_prices.get(p.id, p.entry_price))
                for p in working_positions.values()
            )

        def _margin_level() -> float:
            um = _used_margin()
            if um <= 0:
                return float("inf")
            equity = working_cash + _floating_pnl()
            return equity / um

        while _margin_level() < self.stop_out_level and working_positions:
            # Find worst position by P&L per unit of margin
            worst_id = min(
                working_positions.keys(),
                key=lambda pid: working_positions[pid].unrealized_pnl_per_unit_margin(
                    mark_prices.get(pid, working_positions[pid].entry_price)
                ),
            )
            worst_pos = working_positions[worst_id]
            to_close.append(worst_pos)

            # Simulate closing: realized P&L moves into cash
            mark = mark_prices.get(worst_id, worst_pos.entry_price)
            realized = worst_pos.unrealized_pnl(mark)
            working_cash += realized
            del working_positions[worst_id]

        return to_close

    def execute_stop_outs(
        self,
        mark_prices: dict[int, float],
        sub_book_name: str,
    ) -> list[dict]:
        """Actually execute the stop-outs that check_stop_out would return.

        Closes each position at its mark price and records the stop-out
        event. Returns the list of closed trade records (one per stop-out).

        Increments sub_book.stop_out_count by the number of positions closed.
        """
        to_close = self.check_stop_out(mark_prices, sub_book_name)
        closed: list[dict] = []
        for pos in to_close:
            mark = mark_prices.get(pos.id, pos.entry_price)
            trade = self.close_position(pos.id, exit_price=mark, commission=0.0)
            trade["stop_out"] = True
            closed.append(trade)
        self.sub_book(sub_book_name).stop_out_count += len(closed)
        return closed

    # ── Drawdown + kill switches ────────────────────────────────────

    def institutional_drawdown(self, mark_prices: dict[int, float]) -> float:
        return self.institutional.drawdown_pct(mark_prices)

    def aggressive_drawdown(self, mark_prices: dict[int, float]) -> float:
        return self.aggressive.drawdown_pct(mark_prices)

    def update_peaks(self, mark_prices: dict[int, float]) -> None:
        """Refresh peak-equity trackers on both sub-books."""
        self.institutional.update_peak(mark_prices)
        self.aggressive.update_peak(mark_prices)

    def reset_daily_pnl(self) -> None:
        """Called at daily rollover. Reset per-day realized P&L counters."""
        self.institutional.realized_pnl_today = 0.0
        self.aggressive.realized_pnl_today = 0.0
