"""Transcript conformance for the MNQ hybrid scalp simulator.

Stage 0 of STRATEGY_DEVELOPMENT_PROCESS.md is parity with the source. The source
here is a video transcript, not Pine, so each rule gets its own test on hand-built
candles where the right answer can be worked out on paper.

The fixture: flat candles at 20,000 with a 10-point range. True range is then
exactly 10 on every candle, so ATR(14) is exactly 10 once it warms up:
    band  = close - 3.1 x 10 = 19,969
    target = 2.0 x 10 = 20 pts (80 ticks), stop = 1.5 x 10 = 15 pts (60 ticks)
    size  = floor($500 / (15 pts x $2)) = 16 contracts
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.strategies.futures.mnq_hybrid import (
    FIVE_MIN_MS, HEURISTIC, RESOLVED, RESOLVED_1M, TZ, UNCERTAIN,
    MNQHybridConfig, is_shooting_star, make_futures_fee_model, module_stats,
    normalise, qty_for, resample_1m_to_5m, round_half_up, simulate, ticks_for,
    to_tick, wilder_atr,
)

RETAIL = make_futures_fee_model("cme_mnq_retail")
ZERO = make_futures_fee_model("cme_mnq_zero")


def _ms(when: str) -> int:
    return int(pd.Timestamp(when, tz=TZ).tz_convert("UTC").timestamp() * 1000)


T0 = _ms("2026-03-03 10:00")          # a Tuesday, mid-session


def flat(n: int, start: int = T0, price: float = 20_000.0, rng: float = 10.0) -> list[dict]:
    return [{"ts": start + k * FIVE_MIN_MS, "open": price, "high": price + rng / 2,
             "low": price - rng / 2, "close": price} for k in range(n)]


def bar(rows: list[dict], o, h, l, c, *, gap_min: int = 5, **extra) -> list[dict]:
    rows.append({"ts": rows[-1]["ts"] + gap_min * 60_000, "open": o, "high": h,
                 "low": l, "close": c, **extra})
    return rows


def run(rows, fee=ZERO, bars1=None, **cfg) -> tuple:
    res = simulate(pd.DataFrame(rows), MNQHybridConfig(**cfg), fee, bars1=bars1)
    return res, res.trades


# ═══════════════════════════════════════════════════════════════════════════
# PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════

class TestWorkedExample:
    """The video's own trade, 12:05-12:45: ATR 17.85 on the previous candle."""

    def test_target_is_two_atr_on_the_tick(self):
        assert ticks_for(2.0 * 17.85, 0.25) == 143            # 35.75 pts
        assert ticks_for(2.0 * 17.85, 0.25) * 0.25 == 35.75

    def test_stop_is_one_and_a_half_atr_on_the_tick(self):
        assert ticks_for(1.5 * 17.85, 0.25) * 0.25 == 26.75   # 26.775 -> tick

    def test_size_at_half_a_percent_of_100k(self):
        assert qty_for(26.75, MNQHybridConfig(), RETAIL) == 9  # $500 / $53.50


class TestPrimitives:
    def test_round_half_up_not_bankers(self):
        """Python's round(2.5) == 2. Pine's math.round(2.5) == 3."""
        assert round_half_up(2.5) == 3 and round_half_up(3.5) == 4
        assert round(2.5) == 2

    def test_to_tick(self):
        assert to_tick(19_968.9, 0.25) == 19_969.0
        assert to_tick(19_968.875, 0.25) == 19_969.0

    def test_wilder_atr_is_tradingviews(self):
        """RMA seeded with the SMA of the first n true ranges."""
        h = np.array([10, 12, 11, 15, 14, 13], dtype=float)
        l = np.array([8, 9, 9, 10, 11, 10], dtype=float)
        c = np.array([9, 11, 10, 14, 12, 11], dtype=float)
        atr = wilder_atr(h, l, c, 3)
        tr = [2, 3, 2, 5, 3, 3]
        assert np.isnan(atr[:2]).all()
        assert atr[2] == pytest.approx(np.mean(tr[:3]))
        assert atr[3] == pytest.approx((atr[2] * 2 + tr[3]) / 3)
        assert atr[5] == pytest.approx((((atr[3] * 2 + tr[4]) / 3) * 2 + tr[5]) / 3)

    def test_fixture_atr_is_exactly_ten(self):
        b = pd.DataFrame(flat(20))
        atr = wilder_atr(b.high.to_numpy(), b.low.to_numpy(), b.close.to_numpy(), 14)
        assert atr[13] == pytest.approx(10.0) and atr[19] == pytest.approx(10.0)


class TestShootingStar:
    cfg = MNQHybridConfig()

    def test_classic_star(self):
        assert is_shooting_star(100, 112, 100, 101, 10, self.cfg)

    def test_either_colour(self):
        assert is_shooting_star(101, 112, 100, 100, 10, self.cfg)

    def test_upper_wick_must_be_twice_the_body(self):
        assert not is_shooting_star(100, 105, 100, 103, 10, self.cfg)   # wick 2, body 3

    def test_lower_wick_must_be_a_tenth_of_the_upper(self):
        assert not is_shooting_star(100, 112, 98, 101, 10, self.cfg)    # 2 > 1.1

    def test_a_flat_candle_is_not_a_star(self):
        """0 >= 2 x 0 and 0 <= 0.1 x 0 both pass; there is no wick at all."""
        assert not is_shooting_star(100, 100, 100, 100, 10, self.cfg)

    def test_min_wick_filter(self):
        cfg = MNQHybridConfig(min_wick_atr=2.0)
        assert not is_shooting_star(100, 112, 100, 101, 10, cfg)        # 11 < 20


class TestSizing:
    def test_floor_not_round(self):
        assert qty_for(15.0, MNQHybridConfig(), ZERO) == 16             # 16.67 -> 16

    def test_below_one_contract_is_zero_so_the_trade_is_skipped(self):
        assert qty_for(300.0, MNQHybridConfig(), ZERO) == 0             # $600 per contract

    def test_fixed_contracts(self):
        assert qty_for(300.0, MNQHybridConfig(fixed_qty=4), ZERO) == 4

    def test_cap(self):
        assert qty_for(1.0, MNQHybridConfig(), ZERO) == 20              # 250 -> capped

    def test_limit_is_on_the_starting_balance(self):
        assert MNQHybridConfig(starting_balance=50_000).risk_limit_usd == 250.0


class TestFees:
    def test_no_default_profile(self):
        with pytest.raises(ValueError, match="unknown futures fee profile"):
            make_futures_fee_model("default")

    def test_mnq_is_two_dollars_a_point(self):
        assert RETAIL.point_value == 2.0 and RETAIL.tick_value == 0.5


# ═══════════════════════════════════════════════════════════════════════════
# LONG MODULE
# ═══════════════════════════════════════════════════════════════════════════

class TestLongEntry:
    def test_limit_fills_at_the_band_then_exits_at_the_target(self):
        rows = flat(20)
        bar(rows, 19_980, 19_985, 19_969, 19_975)      # opens under target, wick tags the band
        bar(rows, 19_976, 19_990, 19_970, 19_988)      # target 19,989 reached
        _, trades = run(rows)
        (t,) = trades
        assert (t.side, t.module, t.entry_price) == ("LONG", "long", 19_969.0)
        assert (t.target, t.stop) == (19_989.0, 19_954.0)
        assert (t.exit_price, t.exit_reason) == (19_989.0, "take_profit")
        assert t.quantity == 16
        assert t.pnl == pytest.approx(20 * 16 * 2)     # zero-cost profile

    def test_the_band_uses_the_previous_candle(self):
        """Entry sits at the previous close - 3.1 ATR, known before the candle opens."""
        rows = flat(20)
        bar(rows, 19_980, 19_985, 19_970, 19_975)      # misses 19,969 by a point
        _, trades = run(rows)
        assert trades == []

    def test_gap_below_the_band_fills_at_the_open(self):
        rows = flat(20)
        bar(rows, 19_960, 19_975, 19_958, 19_970)
        bar(rows, 19_970, 19_985, 19_965, 19_984)      # target 19,960 + 20 = 19,980
        _, trades = run(rows)
        (t,) = trades
        assert t.entry_price == 19_960.0 and t.target == 19_980.0 and t.stop == 19_945.0
        assert t.exit_reason == "take_profit"

    def test_exits_are_static_after_entry(self):
        """A violent candle after the fill moves ATR a lot, and not the exits."""
        rows = flat(20)
        bar(rows, 19_980, 19_985, 19_969, 19_975)
        bar(rows, 19_975, 19_988, 19_960, 19_970)      # range 28: ATR rises
        bar(rows, 19_970, 19_990, 19_965, 19_985)
        _, trades = run(rows)
        (t,) = trades
        assert t.target == 19_989.0 and t.exit_price == 19_989.0
        assert t.atr == pytest.approx(10.0)

    def test_one_long_at_a_time(self):
        rows = flat(20)
        bar(rows, 19_980, 19_985, 19_969, 19_975)
        bar(rows, 19_975, 19_980, 19_956, 19_958)      # would tag a new band; still in the first
        _, trades = run(rows)
        assert len(trades) == 1


class TestLongExits:
    def _filled(self):
        rows = flat(20)
        return bar(rows, 19_980, 19_985, 19_969, 19_975)   # clean fill, nothing else

    def test_stop_with_slippage_and_commission(self):
        rows = bar(self._filled(), 19_970, 19_972, 19_950, 19_952)
        _, (t,) = run(rows, fee=RETAIL)
        assert t.exit_reason == "stop_loss"
        assert t.exit_price == 19_953.75                       # 19,954 - 1 tick
        assert t.commission == pytest.approx(2 * 16 * 0.62)
        assert t.pnl == pytest.approx((19_953.75 - 19_969) * 16 * 2 - 2 * 16 * 0.62)

    def test_the_fill_candle_can_stop_out(self):
        """Price crossed the band to reach the low, so a low under the stop means
        the stop came after the fill. That is certain, not a guess."""
        rows = flat(20)
        bar(rows, 19_980, 19_982, 19_950, 19_952)
        _, (t,) = run(rows)
        assert (t.entry_price, t.exit_reason, t.confidence) == (19_969.0, "stop_loss", RESOLVED)

    def test_fill_candle_target_is_not_counted_conservatively(self):
        """Wick tags the band and the high tags the target: which came first
        is unknown, so the conservative policy doesn't count the target."""
        rows = flat(20)
        bar(rows, 19_985, 19_995, 19_969, 19_990)
        _, (t,) = run(rows)
        # Held to the end of the data, not closed at the target...
        assert t.exit_reason == "end_of_data" and t.exit_price == 19_990
        # ...and marked as a guess, because it was one.
        assert t.confidence == UNCERTAIN

    def test_fill_candle_target_heuristic_follows_candle_colour(self):
        rows = flat(20)
        bar(rows, 19_985, 19_995, 19_969, 19_990)              # green: low then high
        _, (t,) = run(rows, ambiguity="heuristic")
        assert (t.exit_reason, t.confidence) == ("take_profit", HEURISTIC)

    def test_both_levels_in_one_later_candle(self):
        rows = bar(self._filled(), 19_975, 19_995, 19_950, 19_990)
        _, (t,) = run(rows)
        assert (t.exit_reason, t.confidence) == ("stop_loss", UNCERTAIN)
        _, (t,) = run(rows, ambiguity="optimistic")
        assert t.exit_reason == "take_profit"

    def test_heuristic_green_is_low_first(self):
        rows = bar(self._filled(), 19_975, 19_995, 19_950, 19_990)
        _, (t,) = run(rows, ambiguity="heuristic")
        assert (t.exit_reason, t.confidence) == ("stop_loss", HEURISTIC)

    def test_heuristic_red_is_high_first(self):
        rows = bar(self._filled(), 19_975, 19_995, 19_950, 19_960)
        _, (t,) = run(rows, ambiguity="heuristic")
        assert (t.exit_reason, t.confidence) == ("take_profit", HEURISTIC)

    def test_one_minute_bars_settle_it(self):
        """The same ambiguous candle, with 1-minute bars saying the high came first."""
        rows = bar(self._filled(), 19_975, 19_995, 19_950, 19_990)
        start = rows[-1]["ts"]
        m1 = pd.DataFrame([
            {"ts": start + 0 * 60_000, "open": 19_975, "high": 19_995, "low": 19_974, "close": 19_990},
            {"ts": start + 1 * 60_000, "open": 19_990, "high": 19_991, "low": 19_970, "close": 19_972},
            {"ts": start + 2 * 60_000, "open": 19_972, "high": 19_973, "low": 19_950, "close": 19_960},
            {"ts": start + 3 * 60_000, "open": 19_960, "high": 19_975, "low": 19_958, "close": 19_970},
            {"ts": start + 4 * 60_000, "open": 19_970, "high": 19_992, "low": 19_968, "close": 19_990},
        ])
        _, (t,) = run(rows, bars1=m1)
        assert (t.exit_reason, t.confidence) == ("take_profit", RESOLVED_1M)


# ═══════════════════════════════════════════════════════════════════════════
# SHORT MODULE
# ═══════════════════════════════════════════════════════════════════════════

def _star(rows):
    """Open 20,000, high 20,012, close 20,001, low 20,000: upper 11, body 1, lower 0."""
    return bar(rows, 20_000, 20_012, 20_000, 20_001)


class TestShort:
    def test_one_candle_hold_open_to_open(self):
        rows = _star(flat(20))
        bar(rows, 19_990, 19_995, 19_985, 19_986)      # entry candle
        bar(rows, 19_980, 19_985, 19_975, 19_978)      # exit at this open
        bar(rows, 19_978, 19_982, 19_975, 19_980)
        _, trades = run(rows, fee=RETAIL, long_on=False)
        (t,) = trades
        assert (t.side, t.module, t.exit_reason) == ("SHORT", "short", "time_exit")
        assert t.entry_price == 19_989.75 and t.exit_price == 19_980.25   # 1 tick each way
        assert t.exit_ts - t.entry_ts == FIVE_MIN_MS
        # ATR on the star candle = (10 x 13 + 12) / 14; 1.5x that = 61 ticks
        assert t.quantity == math.floor(500 / (61 * 0.25 * 2))
        assert t.pnl == pytest.approx((19_989.75 - 19_980.25) * t.quantity * 2
                                      - 2 * t.quantity * 0.62)

    def test_no_stop_by_default_the_video_has_none(self):
        rows = _star(flat(20))
        bar(rows, 19_990, 20_100, 19_985, 20_090)      # rips 110 points against the short
        bar(rows, 20_090, 20_095, 20_085, 20_088)
        bar(rows, 20_088, 20_090, 20_080, 20_085)
        _, (t,) = run(rows, long_on=False)
        assert t.exit_reason == "time_exit" and t.exit_price == 20_090

    def test_max_loss_stop_when_enabled(self):
        rows = _star(flat(20))
        bar(rows, 19_990, 20_100, 19_985, 20_090)
        bar(rows, 20_090, 20_095, 20_085, 20_088)
        bar(rows, 20_088, 20_090, 20_080, 20_085)
        _, (t,) = run(rows, fee=RETAIL, long_on=False, short_stop_on=True)
        assert t.exit_reason == "max_loss_stop"
        assert t.exit_price == 19_989.75 + 61 * 0.25 + 0.25   # stop + 1 tick

    def test_a_short_signal_pulls_the_long_limit(self):
        """One position at a time: the short's candle has no live long order."""
        rows = _star(flat(20))
        bar(rows, 19_990, 19_992, 19_900, 19_950)      # would tag any long band
        bar(rows, 19_950, 19_960, 19_940, 19_955)
        bar(rows, 19_955, 19_960, 19_950, 19_958)
        _, trades = run(rows)
        assert [t.module for t in trades][0] == "short"
        assert trades[0].entry_ts == rows[21]["ts"]

    def test_no_short_held_across_a_gap(self):
        rows = _star(flat(20))
        bar(rows, 19_990, 19_995, 19_985, 19_986)
        bar(rows, 19_980, 19_985, 19_975, 19_978, gap_min=65)   # the daily halt
        bar(rows, 19_978, 19_982, 19_975, 19_980)
        res, trades = run(rows, long_on=False)
        assert trades == [] and res.counts["skipped_gap"] == 1


class TestDailyFilter:
    def _two_sessions(self, prev_session_red: bool, this_session_red: bool):
        start = _ms("2026-03-02 15:20")                    # session 2026-03-02
        rows = flat(20, start=start)
        rows[-1]["close"] = 19_990.0 if prev_session_red else 20_010.0
        rows[-1]["high"] = max(rows[-1]["high"], rows[-1]["close"])
        rows[-1]["low"] = min(rows[-1]["low"], rows[-1]["close"])
        # 18:00 reopen: session 2026-03-03
        bar(rows, 20_000, 20_005, 19_995, 20_000, gap_min=65)
        bar(rows, 20_000, 20_005, 19_995, 20_000)
        _star(rows)
        bar(rows, 19_990, 19_995, 19_985, 19_986)
        last = 19_900.0 if this_session_red else 20_100.0
        bar(rows, 19_986, max(19_990, last), min(19_980, last), last)
        return rows

    def test_previous_session_red_allows_the_short(self):
        _, trades = run(self._two_sessions(True, False), long_on=False, daily_filter=True)
        assert len(trades) == 1

    def test_previous_session_green_blocks_it(self):
        _, trades = run(self._two_sessions(False, True), long_on=False, daily_filter=True)
        assert trades == []

    def test_no_lookahead_into_the_current_session(self):
        """Every candle sees the PREVIOUS session's colour, never its own session's,
        which isn't finished yet when the candle trades."""
        from src.strategies.futures.mnq_hybrid import prepare_bars
        for prev_red, this_red in ((True, False), (False, True)):
            b = prepare_bars(pd.DataFrame(self._two_sessions(prev_red, this_red)),
                             MNQHybridConfig())
            second = b[b.session == b.session.max()]
            assert second.prev_red.all() == prev_red
            assert (second.prev_red != this_red).all()


# ═══════════════════════════════════════════════════════════════════════════
# SESSION AND DATA
# ═══════════════════════════════════════════════════════════════════════════

class TestOpeningCandle:
    """11:14: 'He never trades the opening candle.' 09:30 New York assumed."""

    def _into_the_open(self):
        rows = flat(14, start=_ms("2026-03-03 08:20"))      # last flat candle: 09:25
        bar(rows, 20_000, 20_002, 19_900, 19_950)           # the 09:30 candle crashes the band
        bar(rows, 19_950, 19_955, 19_945, 19_952)
        return rows

    def test_no_limit_is_live_during_the_opening_candle(self):
        _, trades = run(self._into_the_open(), short_on=False)
        assert all(t.entry_ts != _ms("2026-03-03 09:30") for t in trades)

    def test_skip_open_off_would_have_taken_it(self):
        _, trades = run(self._into_the_open(), short_on=False, skip_open=False)
        assert trades and trades[0].entry_ts == _ms("2026-03-03 09:30")

    def test_a_star_on_the_opening_candle_is_ignored(self):
        rows = flat(14, start=_ms("2026-03-03 08:20"))
        _star(rows)                                          # the 09:30 candle
        bar(rows, 19_990, 19_995, 19_985, 19_986)
        bar(rows, 19_980, 19_985, 19_975, 19_978)
        res, trades = run(rows, long_on=False)
        assert trades == [] and res.counts["skipped_opening"] >= 1


class TestRolls:
    def test_position_closed_before_the_roll(self):
        rows = flat(20)
        for r in rows:
            r["instrument_id"] = 1
        bar(rows, 19_980, 19_985, 19_969, 19_975, instrument_id=1)   # long fills
        bar(rows, 20_100, 20_110, 20_095, 20_105, instrument_id=2)   # new contract, +125
        _, (t,) = run(rows)
        assert t.exit_reason == "roll" and t.exit_price == 19_975.0
        assert t.exit_ts == rows[20]["ts"]


class TestDataPrep:
    def test_seconds_timestamps_are_read_as_seconds(self):
        df = normalise(pd.DataFrame({"time": [1_700_000_000, 1_700_000_300],
                                     "open": 1, "high": 1, "low": 1, "close": 1}))
        assert df.ts.tolist() == [1_700_000_000_000, 1_700_000_300_000]

    def test_timezone_aware_datetimes_as_databento_returns_them(self):
        ts = pd.to_datetime(["2026-03-03 15:00", "2026-03-03 15:01"]).tz_localize("UTC")
        df = normalise(pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}))
        assert df.ts.tolist() == [_ms("2026-03-03 10:00"), _ms("2026-03-03 10:01")]

    def test_naive_datetime_strings_are_utc(self):
        df = normalise(pd.DataFrame({"timestamp": ["2026-03-03 15:00:00"],
                                     "open": 1, "high": 1, "low": 1, "close": 1}))
        assert df.ts.tolist() == [_ms("2026-03-03 10:00")]

    def test_resample_is_left_labelled(self):
        start = _ms("2026-03-03 09:30")
        m1 = pd.DataFrame([{"ts": start + k * 60_000, "open": 100 + k, "high": 101 + k,
                            "low": 99 + k, "close": 100.5 + k} for k in range(10)])
        b5 = resample_1m_to_5m(m1)
        assert b5.ts.tolist() == [start, start + FIVE_MIN_MS]
        first = b5.iloc[0]
        assert (first.open, first.high, first.low, first.close) == (100, 105, 99, 104.5)


class TestAnalysis:
    def test_limit_breach_counts_costs(self):
        """Sized to exactly $480 of stop risk; slippage and fees push the loss
        past the $500 limit. That's why the sizing note says to leave margin."""
        rows = flat(20)
        bar(rows, 19_980, 19_982, 19_950, 19_952)
        _, retail = run(rows, fee=RETAIL)
        _, zero = run(rows, fee=ZERO)
        limit = MNQHybridConfig().risk_limit_usd
        assert module_stats(zero, limit)["limit_breaches"] == 0          # -$480
        assert module_stats(retail, limit)["limit_breaches"] == 1        # -$507.84

    def test_equity_ends_at_start_plus_pnl(self):
        rows = flat(20)
        bar(rows, 19_980, 19_985, 19_969, 19_975)
        bar(rows, 19_976, 19_990, 19_970, 19_988)
        res, trades = run(rows, fee=RETAIL)
        assert len(res.equity) == len(rows)
        assert res.equity.iloc[-1] == pytest.approx(100_000 + sum(t.pnl for t in trades))


class TestBacktestScript:
    """scripts/mnq_backtest.py end to end, on a synthetic week of 1-minute bars."""

    def test_daily_equity_is_in_unix_ms(self):
        """pandas 3 keeps ms resolution, so astype(int64) // 1e6 gave 1970 and Sharpe 0."""
        from scripts.mnq_backtest import daily_equity
        idx = [_ms("2026-03-03 10:00"), _ms("2026-03-04 10:00"), _ms("2026-03-05 10:00")]
        out = daily_equity(pd.Series([100_100.0, 99_900.0, 100_300.0], index=idx), 100_000)
        assert list(out.values) == [100_000.0, 100_100.0, 99_900.0, 100_300.0]
        assert out.index[0] == _ms("2026-03-02 00:00")
        assert out.index[1] == _ms("2026-03-03 00:00")

    def test_main_stores_runs_and_writes_the_summary(self, tmp_path, monkeypatch):
        import json

        import scripts.mnq_backtest as mb
        rng = np.random.default_rng(7)
        days = pd.bdate_range("2026-03-02", periods=5)
        ts = np.concatenate([
            pd.date_range(f"{d:%Y-%m-%d} 09:00", f"{d:%Y-%m-%d} 15:59", freq="1min", tz=TZ).to_numpy()
            for d in days])
        close = 20_000 + np.cumsum(rng.normal(0, 4, len(ts))).round(2)
        open_ = np.r_[close[0], close[:-1]]
        spread = np.abs(rng.normal(0, 2, len(ts)))
        df = pd.DataFrame({"ts": pd.DatetimeIndex(ts).tz_convert("UTC"), "open": open_,
                           "high": np.maximum(open_, close) + spread,
                           "low": np.minimum(open_, close) - spread, "close": close})
        data = tmp_path / "mnq"
        data.mkdir()
        normalise(df).to_parquet(data / "mnq_1m.parquet", index=False)
        monkeypatch.setattr(mb, "DATA_DIR", data)

        summary = mb.main(["--fee-profile", "cme_mnq_retail", "--no-sweep",
                           "--reports", str(tmp_path / "reports"), "--db", str(tmp_path / "t.db")])
        assert [r["key"] for r in summary["runs"]] == [k for k, _, _ in mb.RUNS]
        assert summary["data"]["used_1m"] and summary["data"]["sessions"] == 5
        assert (tmp_path / "reports" / "mnq_hybrid_summary.md").exists()
        for r in summary["runs"]:
            if r.get("run_id"):
                stored = json.loads((tmp_path / "reports" / f"{r['run_id']}.json").read_text())
                metrics = stored.get("metrics", stored)
                assert metrics["initial_capital"] == 100_000
