"""Tests for the broker-accurate Book + LeveragedPosition margin model.

The critical test is the worked example from the plan:
  $1000 account, 5% position, 1000× leverage on XAUUSD at $2400
  → margin $50, notional $50,000, quantity 20.833 oz
  → 0.1% adverse: equity $950, margin level 1900% (trade OPEN)
  → 1.0% adverse: equity $500, margin level 1000% (trade OPEN)
  → ~1.95% adverse: equity ~$25, margin level 50% (STOP-OUT)

If any of those numbers are wrong, the margin model is broken.
"""

from __future__ import annotations

import pytest

from src.backtest.book import (
    DEFAULT_STOP_OUT_LEVEL,
    SUB_BOOK_AGGRESSIVE,
    SUB_BOOK_INSTITUTIONAL,
    Book,
    LeveragedPosition,
    SubBookState,
)


class TestLeveragedPosition:
    def test_notional_long(self):
        pos = LeveragedPosition(
            id=0, side="LONG", entry_idx=0, entry_price=2400.0,
            quantity=20.833, leverage=1000.0,
            stop_loss=None, take_profit=None, entry_ts_ms=1_700_000_000_000,
            entry_margin=50.0, strategy_name="test",
        )
        assert pos.notional() == pytest.approx(50_000.0, rel=1e-4)

    def test_unrealized_pnl_long_favorable(self):
        pos = LeveragedPosition(
            id=0, side="LONG", entry_idx=0, entry_price=2400.0,
            quantity=10.0, leverage=100.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0, entry_margin=240.0,
            strategy_name="test",
        )
        assert pos.unrealized_pnl(2410.0) == pytest.approx(100.0)

    def test_unrealized_pnl_long_adverse(self):
        pos = LeveragedPosition(
            id=0, side="LONG", entry_idx=0, entry_price=2400.0,
            quantity=10.0, leverage=100.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0, entry_margin=240.0,
            strategy_name="test",
        )
        assert pos.unrealized_pnl(2390.0) == pytest.approx(-100.0)

    def test_unrealized_pnl_short(self):
        pos = LeveragedPosition(
            id=0, side="SHORT", entry_idx=0, entry_price=2400.0,
            quantity=10.0, leverage=100.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0, entry_margin=240.0,
            strategy_name="test",
        )
        # Short gains when price drops
        assert pos.unrealized_pnl(2390.0) == pytest.approx(100.0)
        assert pos.unrealized_pnl(2410.0) == pytest.approx(-100.0)

    def test_pnl_per_unit_margin(self):
        pos = LeveragedPosition(
            id=0, side="LONG", entry_idx=0, entry_price=2400.0,
            quantity=10.0, leverage=100.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0, entry_margin=240.0,
            strategy_name="test",
        )
        # +$240 P&L / $240 margin = +1.0 (100% of margin)
        assert pos.unrealized_pnl_per_unit_margin(2424.0) == pytest.approx(1.0)
        # -$240 P&L / $240 margin = -1.0
        assert pos.unrealized_pnl_per_unit_margin(2376.0) == pytest.approx(-1.0)


class TestBookConstruction:
    def test_new_splits_cash(self):
        book = Book.new(institutional_cash=700.0, aggressive_cash=300.0)
        assert book.institutional.cash == 700.0
        assert book.aggressive.cash == 300.0
        assert book.total_cash() == 1000.0

    def test_default_stop_out_level(self):
        book = Book.new(institutional_cash=1000.0, aggressive_cash=0.0)
        assert book.stop_out_level == DEFAULT_STOP_OUT_LEVEL
        assert DEFAULT_STOP_OUT_LEVEL == 0.50  # 50% per XM / IC Markets

    def test_custom_stop_out_level(self):
        book = Book.new(
            institutional_cash=1000.0, aggressive_cash=0.0, stop_out_level=0.80
        )
        assert book.stop_out_level == 0.80

    def test_sub_book_accessor(self):
        book = Book.new(institutional_cash=500.0, aggressive_cash=500.0)
        assert book.sub_book(SUB_BOOK_INSTITUTIONAL) is book.institutional
        assert book.sub_book(SUB_BOOK_AGGRESSIVE) is book.aggressive

    def test_sub_book_bad_name_raises(self):
        book = Book.new(institutional_cash=1000.0, aggressive_cash=0.0)
        with pytest.raises(ValueError, match="unknown sub_book"):
            book.sub_book("nonsense")


class TestOpenPositionValidation:
    def _book(self) -> Book:
        return Book.new(institutional_cash=1000.0, aggressive_cash=1000.0)

    def test_open_valid_long(self):
        book = self._book()
        pos = book.open_position(
            side="LONG",
            entry_price=2400.0,
            quantity=10.0,
            leverage=100.0,
            stop_loss=2380.0,
            take_profit=2440.0,
            entry_ts_ms=1_700_000_000_000,
            strategy_name="test",
            sub_book=SUB_BOOK_INSTITUTIONAL,
        )
        assert pos.id == 0
        assert pos.entry_margin == pytest.approx(240.0)  # 2400*10/100
        # CFD accounting: cash UNCHANGED on open, only used_margin moves
        assert book.institutional.cash == pytest.approx(1000.0)
        assert book.institutional.used_margin() == pytest.approx(240.0)
        assert book.institutional.free_margin({pos.id: 2400.0}) == pytest.approx(760.0)
        assert pos.id in book.institutional.positions

    def test_open_invalid_side_raises(self):
        book = self._book()
        with pytest.raises(ValueError, match="side must"):
            book.open_position(
                side="BUY", entry_price=2400.0, quantity=1.0, leverage=10.0,
                stop_loss=None, take_profit=None, entry_ts_ms=0,
            )

    def test_open_low_leverage_raises(self):
        book = self._book()
        with pytest.raises(ValueError, match="leverage must be >= 1"):
            book.open_position(
                side="LONG", entry_price=2400.0, quantity=1.0, leverage=0.5,
                stop_loss=None, take_profit=None, entry_ts_ms=0,
            )

    def test_open_long_sl_above_entry_raises(self):
        book = self._book()
        with pytest.raises(ValueError, match="LONG stop_loss"):
            book.open_position(
                side="LONG", entry_price=2400.0, quantity=1.0, leverage=10.0,
                stop_loss=2410.0, take_profit=None, entry_ts_ms=0,
            )

    def test_open_short_sl_below_entry_raises(self):
        book = self._book()
        with pytest.raises(ValueError, match="SHORT stop_loss"):
            book.open_position(
                side="SHORT", entry_price=2400.0, quantity=1.0, leverage=10.0,
                stop_loss=2390.0, take_profit=None, entry_ts_ms=0,
            )

    def test_insufficient_margin_raises(self):
        book = Book.new(institutional_cash=100.0, aggressive_cash=0.0)
        with pytest.raises(ValueError, match="insufficient cash"):
            # Need $200 margin but only $100 cash
            book.open_position(
                side="LONG", entry_price=2000.0, quantity=10.0, leverage=100.0,
                stop_loss=None, take_profit=None, entry_ts_ms=0,
                sub_book=SUB_BOOK_INSTITUTIONAL,
            )

    def test_margin_exactly_equal_to_free_margin_succeeds(self):
        """Regression test for task #109 float-precision bug.

        When notional == equity at 1x leverage (e.g. risk-based sizing
        where risk_pct == sl_pct), the margin requirement equals exactly
        the available free margin. Float ULP differences could cause
        `entry_margin > free_margin` to fire spuriously. The 1-cent
        epsilon in book.open_position lets these "exactly at the limit"
        positions succeed.

        Scenario: $10,000 cash, 1x leverage, position notional = $10,000
        (e.g., 2 oz of gold @ $5,000). Margin needed = $10,000.
        Free margin = $10,000. Should succeed (within 1-cent epsilon).
        """
        book = Book.new(institutional_cash=10_000.0, aggressive_cash=0.0)
        # 2 oz @ $5000 = $10,000 notional, 1x lev → $10,000 margin
        pos = book.open_position(
            side="LONG", entry_price=5000.0, quantity=2.0, leverage=1.0,
            stop_loss=4975.0, take_profit=5050.0, entry_ts_ms=0,
            sub_book=SUB_BOOK_INSTITUTIONAL,
        )
        assert pos.id in book.institutional.positions
        assert pos.entry_price == 5000.0
        assert pos.quantity == 2.0
        assert pos.leverage == 1.0

    def test_margin_just_over_free_margin_still_rejects(self):
        """The epsilon is exactly 1 cent — anything more should still reject."""
        book = Book.new(institutional_cash=100.0, aggressive_cash=0.0)
        # Need $100.50 margin (50 cents over the cash) → still rejected
        with pytest.raises(ValueError, match="insufficient cash"):
            book.open_position(
                side="LONG", entry_price=10050.0, quantity=0.01, leverage=1.0,
                stop_loss=10000.0, take_profit=None, entry_ts_ms=0,
                sub_book=SUB_BOOK_INSTITUTIONAL,
            )

    def test_aggressive_sub_book_routing(self):
        book = Book.new(institutional_cash=500.0, aggressive_cash=500.0)
        pos = book.open_position(
            side="LONG", entry_price=2400.0, quantity=5.0, leverage=100.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0,
            strategy_name="scalper",
            sub_book=SUB_BOOK_AGGRESSIVE,
        )
        # CFD accounting: cash UNCHANGED on both sub-books. Only the
        # aggressive sub-book's used_margin view reflects the new position.
        assert book.institutional.cash == 500.0
        assert book.aggressive.cash == 500.0
        assert book.aggressive.used_margin() == pytest.approx(120.0)  # 2400*5/100
        assert pos.id in book.aggressive.positions
        assert pos.id not in book.institutional.positions


class TestCriticalMarginMath:
    """The worked example from the plan — this is the load-bearing test.

    If this fails, the CFD broker margin model is broken and everything
    downstream is wrong. This test encodes Prince's corrected mental model.
    """

    def _setup(self) -> tuple[Book, LeveragedPosition]:
        """$1000 account, 5% position (= $50 margin), 1000× on XAUUSD $2400."""
        book = Book.new(institutional_cash=1000.0, aggressive_cash=0.0)
        # $50,000 notional / 1000× = $50 margin
        # 20.833... oz at $2400 = $50,000
        pos = book.open_position(
            side="LONG",
            entry_price=2400.0,
            quantity=50_000.0 / 2400.0,  # = 20.833...
            leverage=1000.0,
            stop_loss=None,  # no strategy SL; test pure broker behavior
            take_profit=None,
            entry_ts_ms=0,
            sub_book=SUB_BOOK_INSTITUTIONAL,
        )
        return book, pos

    def test_margin_locked_correctly(self):
        book, pos = self._setup()
        assert pos.entry_margin == pytest.approx(50.0, rel=1e-6)
        # CFD accounting: cash UNCHANGED at open, used_margin reflects the lock
        assert book.institutional.cash == pytest.approx(1000.0, rel=1e-6)
        assert book.institutional.used_margin() == pytest.approx(50.0, rel=1e-6)
        # Free margin at open = equity - used_margin = 1000 - 50 = 950
        marks = {pos.id: 2400.0}
        assert book.institutional.free_margin(marks) == pytest.approx(950.0, rel=1e-6)

    def test_equity_at_zero_move(self):
        """At open price, equity = starting cash."""
        book, pos = self._setup()
        marks = {pos.id: 2400.0}
        assert book.institutional.equity(marks) == pytest.approx(1000.0, rel=1e-6)
        # Margin level = $1000 / $50 = 2000% (20.0 as ratio)
        assert book.institutional.margin_level(marks) == pytest.approx(20.0, rel=1e-6)

    def test_01_percent_adverse_stays_open(self):
        """0.1% adverse: floating P&L -$50, equity $950, trade STAYS OPEN."""
        book, pos = self._setup()
        # 0.1% down: $2400 → $2397.60
        marks = {pos.id: 2397.60}
        expected_pnl = (2397.60 - 2400.0) * pos.quantity  # ≈ -$50
        assert book.institutional.floating_pnl(marks) == pytest.approx(-50.0, rel=1e-3)
        equity = book.institutional.equity(marks)
        assert equity == pytest.approx(950.0, rel=1e-3)
        # Margin level = $950 / $50 = 1900% = 19.0x
        ml = book.institutional.margin_level(marks)
        assert ml == pytest.approx(19.0, rel=1e-3)
        # FAR above 50% stop-out threshold → no forced close
        assert ml > 0.50
        # No stop-outs in the check
        assert book.check_stop_out(marks, SUB_BOOK_INSTITUTIONAL) == []

    def test_10_percent_adverse_stays_open(self):
        """1.0% adverse: floating P&L -$500, equity $500, trade STAYS OPEN."""
        book, pos = self._setup()
        marks = {pos.id: 2376.0}  # 1.0% down
        expected_pnl = (2376.0 - 2400.0) * pos.quantity  # ≈ -$500
        assert book.institutional.floating_pnl(marks) == pytest.approx(-500.0, rel=1e-3)
        equity = book.institutional.equity(marks)
        assert equity == pytest.approx(500.0, rel=1e-3)
        # Margin level = $500 / $50 = 1000% = 10.0x
        ml = book.institutional.margin_level(marks)
        assert ml == pytest.approx(10.0, rel=1e-3)
        # Still WAY above stop-out
        assert book.check_stop_out(marks, SUB_BOOK_INSTITUTIONAL) == []

    def test_195_percent_adverse_triggers_stop_out(self):
        """~1.95% adverse: margin level drops to ~50%, stop-out fires.

        Correct CFD math:
          cash = $1000 (unchanged by open)
          floating_pnl at stop-out = equity - cash = 25 - 1000 = -$975
          price_move = -975 / 20.833 oz = -$46.80/oz
          percent_move = 46.80 / 2400 = 1.95%
        """
        book, pos = self._setup()
        marks = {pos.id: 2353.20}  # ~1.95% down → equity ~$25 → margin level ~50%
        floating = book.institutional.floating_pnl(marks)
        equity = book.institutional.equity(marks)
        ml = book.institutional.margin_level(marks)
        # Should be very close to the 50% threshold
        assert ml == pytest.approx(0.5, rel=0.05)
        # check_stop_out should report at least one position to close
        to_close = book.check_stop_out(marks, SUB_BOOK_INSTITUTIONAL)
        assert len(to_close) == 1
        assert to_close[0].id == pos.id

    def test_20_percent_adverse_cascade_close(self):
        """2%+ adverse: cascade close fires, position is closed."""
        book, pos = self._setup()
        marks = {pos.id: 2352.0}  # 2.0% down — below stop-out line
        to_close = book.check_stop_out(marks, SUB_BOOK_INSTITUTIONAL)
        assert len(to_close) == 1
        # Now actually execute
        closed = book.execute_stop_outs(marks, SUB_BOOK_INSTITUTIONAL)
        assert len(closed) == 1
        assert closed[0]["stop_out"] is True
        assert pos.id not in book.institutional.positions
        assert book.institutional.stop_out_count == 1

    def test_favorable_move_no_stop_out(self):
        """Favorable moves never trigger stop-outs — only adverse."""
        book, pos = self._setup()
        marks = {pos.id: 2448.0}  # +2% favorable
        assert book.check_stop_out(marks, SUB_BOOK_INSTITUTIONAL) == []


class TestTwoBookIndependence:
    def test_institutional_stop_out_leaves_aggressive_alone(self):
        """Institutional stop-out must NOT close aggressive positions."""
        book = Book.new(institutional_cash=500.0, aggressive_cash=500.0)
        # Open a position in each sub-book
        inst_pos = book.open_position(
            side="LONG", entry_price=2400.0, quantity=20.0, leverage=1000.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0,
            sub_book=SUB_BOOK_INSTITUTIONAL, strategy_name="inst",
        )
        aggr_pos = book.open_position(
            side="LONG", entry_price=2400.0, quantity=20.0, leverage=1000.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0,
            sub_book=SUB_BOOK_AGGRESSIVE, strategy_name="aggr",
        )
        # Move against only the institutional position enough to stop it out
        # Both have the same entry so same price move hurts both — but
        # stop-out is per-sub-book, so it should fire on institutional only
        # if we run check_stop_out for the institutional sub-book.
        marks = {inst_pos.id: 2355.60, aggr_pos.id: 2400.0}
        to_close_inst = book.check_stop_out(marks, SUB_BOOK_INSTITUTIONAL)
        assert len(to_close_inst) >= 1
        assert to_close_inst[0].id == inst_pos.id
        # Aggressive sub-book should be untouched
        to_close_aggr = book.check_stop_out(marks, SUB_BOOK_AGGRESSIVE)
        assert len(to_close_aggr) == 0

    def test_open_positions_across_sub_books(self):
        book = Book.new(institutional_cash=500.0, aggressive_cash=500.0)
        book.open_position(
            side="LONG", entry_price=2400.0, quantity=1.0, leverage=100.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0,
            sub_book=SUB_BOOK_INSTITUTIONAL,
        )
        book.open_position(
            side="LONG", entry_price=2400.0, quantity=1.0, leverage=100.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0,
            sub_book=SUB_BOOK_AGGRESSIVE,
        )
        all_pos = list(book.all_positions())
        assert len(all_pos) == 2


class TestClosePosition:
    def test_close_releases_margin_and_realizes_pnl(self):
        book = Book.new(institutional_cash=1000.0, aggressive_cash=0.0)
        pos = book.open_position(
            side="LONG", entry_price=2400.0, quantity=10.0, leverage=100.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0,
        )
        # CFD accounting: cash unchanged at open, used_margin reflects lock
        assert book.institutional.cash == pytest.approx(1000.0)
        assert book.institutional.used_margin() == pytest.approx(240.0)

        # Close at +$100 P&L, $6 commission
        trade = book.close_position(pos.id, exit_price=2410.0, commission=6.0)

        # Cash: $1000 + $100 P&L - $6 commission = $1094
        assert book.institutional.cash == pytest.approx(1094.0)
        # used_margin drops to 0 (position removed)
        assert book.institutional.used_margin() == 0.0
        assert trade["pnl"] == pytest.approx(100.0)
        assert trade["realized_pnl"] == pytest.approx(94.0)
        assert pos.id not in book.institutional.positions

    def test_close_missing_position_raises(self):
        book = Book.new(institutional_cash=1000.0, aggressive_cash=0.0)
        with pytest.raises(KeyError):
            book.close_position(999, exit_price=2400.0)

    def test_close_updates_realized_pnl_counters(self):
        book = Book.new(institutional_cash=1000.0, aggressive_cash=0.0)
        pos = book.open_position(
            side="LONG", entry_price=2400.0, quantity=10.0, leverage=100.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0,
        )
        book.close_position(pos.id, exit_price=2410.0, commission=0.0)
        assert book.institutional.realized_pnl_today == pytest.approx(100.0)
        assert book.institutional.realized_pnl_total == pytest.approx(100.0)

    def test_reset_daily_pnl(self):
        book = Book.new(institutional_cash=1000.0, aggressive_cash=1000.0)
        book.institutional.realized_pnl_today = 50.0
        book.aggressive.realized_pnl_today = -20.0
        book.institutional.realized_pnl_total = 200.0  # should be preserved
        book.reset_daily_pnl()
        assert book.institutional.realized_pnl_today == 0.0
        assert book.aggressive.realized_pnl_today == 0.0
        # Total is NOT reset
        assert book.institutional.realized_pnl_total == 200.0


class TestDrawdown:
    def test_drawdown_zero_at_start(self):
        book = Book.new(institutional_cash=1000.0, aggressive_cash=0.0)
        assert book.institutional_drawdown({}) == 0.0

    def test_drawdown_from_peak(self):
        book = Book.new(institutional_cash=1000.0, aggressive_cash=0.0)
        pos = book.open_position(
            side="LONG", entry_price=2400.0, quantity=10.0, leverage=100.0,
            stop_loss=None, take_profit=None, entry_ts_ms=0,
        )
        # Push equity up to $1100
        marks = {pos.id: 2410.0}  # +$100
        book.update_peaks(marks)
        assert book.institutional.peak_equity == pytest.approx(1100.0)

        # Pull equity back down to $950
        marks = {pos.id: 2385.0}  # -$150 from entry
        dd = book.institutional_drawdown(marks)
        # Peak 1100 → current 850 → DD = (1100 - 850) / 1100 = 22.7%
        assert dd == pytest.approx((1100.0 - 850.0) / 1100.0, rel=1e-3)
