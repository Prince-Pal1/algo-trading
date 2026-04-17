"""Leveraged backtest engine — Phase G.2a.4 of the gold trading plan.

**This is a NEW engine, completely separate from `src/backtest/engine.py`.**
The existing `BacktestEngine` stays 100% untouched — all crypto backtests
continue to use it, TV-bit-exact indicator math is locked down by existing
tests, and M3S training pipeline sees zero change.

The new `LeveragedBacktestEngine`:
- Uses `Book` (src/backtest/book.py) for broker-accurate margin accounting
- Uses `ICMarketsMetalFeeModel` (src/backtest/costs.py) for realistic
  spread + commission
- Uses `BrownianBridgeModel` (src/backtest/path.py) for intrabar SL/TP
  resolution and worst-case excursion checks
- Supports TWO sub-books (institutional + aggressive) via the same Book
- Tracks leverage-aware equity curve (cash + floating P&L, with margin
  level at bar close for reporting)

Key behavioral differences vs the old engine:
1. Positions track LEVERAGE. A position at leverage L consumes
   `notional / L` margin instead of the full notional.
2. Broker stop-out fires when account margin_level drops to 50%, not
   when an individual position loses 100% of its margin.
3. Strategy-set SL is honored FIRST (checked intrabar via BrownianBridge),
   broker stop-out is the LAST line of defense.
4. Spread + commission use the real IC Markets cTrader schema, not a
   flat 0.04% approximation.
5. Intrabar path is reconstructed via Brownian bridge — both-hit
   (SL and TP inside the bar) is resolved deterministically.

Usage:
    from src.backtest.leveraged_engine import LeveragedBacktestEngine

    engine = LeveragedBacktestEngine(
        initial_institutional_cash=7_000.0,
        initial_aggressive_cash=3_000.0,
    )
    result = engine.run(
        strategy=my_strategy,
        data=xauusd_1h_parquet,
        symbol="XAUUSD",
        timeframe="1h",
        leverage=25.0,  # caller-supplied; strategy picks or config defaults
    )
    print(result.metrics)  # total_return_pct, sharpe, stop_outs, max_dd_pct

The engine does NOT integrate with:
- The old BacktestEngine's audit hooks (Phase 0 signal_audit) — these
  exist for the crypto meta-labeling pipeline and don't apply to the
  separate gold bucket.
- The ZMQ RiskManager — for MVP, the LeveragedBacktestEngine respects
  only the in-book margin rules. G.2c ships the inline RiskManager
  integration.
- M3S Compounder's risk_scalar blend — G.2b ships the
  request_leverage API that integrates M3S policy into the leveraged
  engine's position sizing.

Those integrations are explicit next steps. The MVP is: can we honestly
simulate a leveraged position over a price series, with margin accounting,
intrabar SL/TP, and broker stop-out?
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.backtest.book import (
    SUB_BOOK_AGGRESSIVE,
    SUB_BOOK_INSTITUTIONAL,
    Book,
    LeveragedPosition,
)
from src.backtest.costs import ICMarketsMetalFeeModel, ZeroCostFeeModel
from src.backtest.path import Bar, BrownianBridgeModel, M1PathModel, check_sl_tp_hits
from src.data.feature_engine import _compute_indicators
from src.m3s.hooks import M3S
from src.strategies.base import BaseStrategy
from src.utils.instruments import get_instrument
from src.utils.logger import get_logger
from src.utils.types import LeverageReasonCode, SignalAction

log = get_logger("leveraged_backtest")


# ── Trade record ─────────────────────────────────────────────────────────


@dataclass
class LeveragedTrade:
    """A closed trade with leverage-aware fields.

    Separate from the regular `Trade` dataclass in engine.py so the old
    engine stays untouched. Extra fields: leverage, margin_used, spread_cost,
    sub_book, stop_out_flag.
    """
    entry_idx: int
    exit_idx: int
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    leverage: float
    margin_used: float
    pnl: float
    pnl_pct: float
    spread_cost: float
    commission: float
    total_cost: float
    realized_pnl: float
    exit_reason: str                 # "signal" | "stop_loss" | "take_profit" | "broker_stop_out"
    sub_book: str
    strategy_name: str


@dataclass
class LeveragedBacktestResult:
    """Output of a leveraged backtest run.

    Tracks two equity curves (institutional + aggressive) plus the combined
    curve. Metrics include both books' individual Sharpe/DD/return plus
    the combined numbers.
    """
    trades: list[LeveragedTrade] = field(default_factory=list)
    equity_curve_total: list[float] = field(default_factory=list)
    equity_curve_institutional: list[float] = field(default_factory=list)
    equity_curve_aggressive: list[float] = field(default_factory=list)
    margin_level_curve: list[float] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    total_candles: int = 0
    broker_stop_out_count: int = 0
    open_rejected_count: int = 0  # task #115: positions rejected by margin gate
    final_institutional_equity: float = 0.0
    final_aggressive_equity: float = 0.0


# ── Engine ───────────────────────────────────────────────────────────────


class LeveragedBacktestEngine:
    """Event-driven leveraged backtest engine — separate from BacktestEngine.

    Uses Book + costs + path modules to simulate a leveraged gold book
    honestly. Existing crypto BacktestEngine is untouched and still handles
    all crypto backtests.
    """

    def __init__(
        self,
        *,
        initial_institutional_cash: float = 7_000.0,
        initial_aggressive_cash: float = 3_000.0,
        fee_model: ICMarketsMetalFeeModel | ZeroCostFeeModel | None = None,
        path_model: BrownianBridgeModel | M1PathModel | None = None,
        m3s: M3S | None = None,
        run_id: str = "leveraged_default",
    ) -> None:
        """Construct the leveraged engine.

        Args:
            initial_institutional_cash: starting cash for the institutional
                (Tiers 1-4) sub-book
            initial_aggressive_cash: starting cash for the aggressive
                (Tier 5) sub-book. Default 3000/7000 split matches the
                plan's 30/70 initial suggestion.
            fee_model: IC Markets cost model. Default IC Markets Raw cTrader.
            path_model: intrabar path reconstruction model. Default
                Brownian bridge seeded by run_id. Pass an `M1PathModel`
                (with M1 ground-truth data) for tick-accuracy SL/TP
                ordering on M5/H1 backtests — recommended for any
                scalping strategy with SL distance < 2 × ATR.
            m3s: optional M3S facade. When provided, every LONG/SHORT signal
                calls `m3s.request_leverage(strategy, conviction, range)`
                and uses the granted leverage. Without an M3S instance, the
                engine falls back to the `leverage` kwarg of `run()`.
            run_id: deterministic seed tag for reproducibility
        """
        self._initial_institutional_cash = float(initial_institutional_cash)
        self._initial_aggressive_cash = float(initial_aggressive_cash)
        # If no fee_model passed, route through FeeManager using the active
        # broker + XAUUSD normal scenario. This is still honest (named profile
        # via the registry) whereas the old default ICMarketsMetalFeeModel()
        # was the 90× slippage bug source (see feedback_select_fee_profile_first).
        # Research callers should still pass an explicit fee_model for full
        # traceability.
        if fee_model is None:
            try:
                from src.fees import FeeManager
                fee_model = FeeManager.resolve(symbol="XAUUSD", style="swing", scenario="normal")
            except Exception:
                # Last resort: bare default (preserves non-breakingness if
                # FeeManager/broker registry is not yet installed)
                fee_model = ICMarketsMetalFeeModel()
        self._fee_model = fee_model
        self._path_model = path_model or BrownianBridgeModel(run_id=run_id)
        self._m3s = m3s
        self._run_id = run_id
        # Task #115: counter for positions rejected by book.open_position's
        # margin check. Incremented in _handle_signal's ValueError handler.
        # Surfaced in LeveragedBacktestResult.open_rejected_count so
        # deep_backtest can detect margin starvation at high leverage.
        self._open_rejected_count: int = 0

    def run(
        self,
        strategy: BaseStrategy,
        data: pd.DataFrame,
        *,
        symbol: str = "XAUUSD",
        timeframe: str = "1h",
        leverage: float = 10.0,
        sub_book: str = SUB_BOOK_INSTITUTIONAL,
        indicators: list[str] | None = None,
    ) -> LeveragedBacktestResult:
        return self.run_multi(
            strategy_routes=[(strategy, sub_book, leverage)],
            data=data,
            symbol=symbol,
            timeframe=timeframe,
            indicators=indicators,
        )

    def run_multi(
        self,
        *,
        strategy_routes: list[tuple[BaseStrategy, str, float]],
        data: pd.DataFrame,
        symbol: str = "XAUUSD",
        timeframe: str = "1h",
        indicators: list[str] | None = None,
    ) -> LeveragedBacktestResult:
        if indicators is None:
            indicators = [
                "ema_9", "ema_21", "sma_50", "rsi_14", "bbands_20",
                "macd", "atr_14", "adx_14", "donchian_20",
            ]

        n = len(data)
        if n < 50:
            log.warning("leveraged_insufficient_data", rows=n, min_required=50)
            return LeveragedBacktestResult(total_candles=n)

        df = data.copy()
        df = _compute_indicators(df, indicators)

        inst = get_instrument(symbol)
        log.info(
            "leveraged_backtest_start",
            symbol=symbol,
            timeframe=timeframe,
            candles=n,
            institutional_cash=self._initial_institutional_cash,
            aggressive_cash=self._initial_aggressive_cash,
            strategies=[s.name for s, _, _ in strategy_routes],
        )

        book = Book.new(
            institutional_cash=self._initial_institutional_cash,
            aggressive_cash=self._initial_aggressive_cash,
        )

        trades: list[LeveragedTrade] = []
        equity_total: list[float] = []
        equity_institutional: list[float] = []
        equity_aggressive: list[float] = []
        margin_level_curve: list[float] = []

        for i in range(n):
            row = df.iloc[i]
            bar = Bar(
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0) or 0.0),
                ts_ms=int(row.get("timestamp", 0) or 0),
            )
            mark_price = bar.close

            # Step 1: intrabar SL/TP on existing positions (strategy_name comes from the position)
            self._apply_intrabar_sl_tp(book, bar, i, trades, strategy_name=None)

            # Step 2: broker stop-out per sub-book using worst-case intrabar marks
            worst_marks_inst = self._worst_case_marks(
                book.institutional.positions, bar, self._path_model
            )
            worst_marks_aggr = self._worst_case_marks(
                book.aggressive.positions, bar, self._path_model
            )
            merged_marks = {**worst_marks_inst, **worst_marks_aggr}
            for sb_name in (SUB_BOOK_INSTITUTIONAL, SUB_BOOK_AGGRESSIVE):
                for t in book.execute_stop_outs(merged_marks, sb_name):
                    trades.append(self._to_trade(
                        t, entry_idx=i, exit_idx=i,
                        exit_reason="broker_stop_out",
                        strategy_name=t.get("strategy_name", ""),
                    ))

            # Step 3: call each strategy, route its signal to the declared sub-book.
            # When the path model is an M1PathModel, attach the current bar's
            # M1 sub-bars to the row so scalper strategies can query intrabar
            # velocity / microstructure during signal generation. Strategies
            # that don't care read nothing and pay no cost.
            if isinstance(self._path_model, M1PathModel):
                sub_bars_for_row = self._path_model._sub_bars_for(int(bar.ts_ms))
                if sub_bars_for_row:
                    row = row.copy()
                    row["intrabar_sub_bars"] = sub_bars_for_row
            for strategy, sub_book, leverage in strategy_routes:
                signal = strategy.process(symbol, timeframe, row)
                if signal is not None:
                    self._handle_signal(
                        signal=signal,
                        bar=bar,
                        bar_idx=i,
                        book=book,
                        leverage=leverage,
                        sub_book=sub_book,
                        inst=inst,
                        trades=trades,
                        strategy_name=strategy.name,
                    )

            # Step 4: equity curves + peak tracker update
            all_marks = {pos.id: mark_price for pos in book.all_positions()}
            book.update_peaks(all_marks)
            inst_eq = book.institutional.equity(all_marks)
            aggr_eq = book.aggressive.equity(all_marks)
            total_eq = inst_eq + aggr_eq
            equity_institutional.append(inst_eq)
            equity_aggressive.append(aggr_eq)
            equity_total.append(total_eq)
            total_used = book.total_used_margin()
            ml = (total_eq / total_used) if total_used > 0 else float("inf")
            margin_level_curve.append(ml if ml != float("inf") else 0.0)

        # Final close of any still-open positions at last-bar close
        for pos in list(book.all_positions()):
            exit_price = float(df.iloc[-1]["close"])
            fee_side = "SELL" if pos.side == "LONG" else "BUY"
            fill = self._fee_model.fill_price(
                side=fee_side,
                reference_price=exit_price,
                atr=self._get_atr(df, -1),
                ts_ms=int(df.iloc[-1].get("timestamp", 0) or 0),
            )
            commission = self._fee_model.commission_usd(
                quantity_units=pos.quantity,
                reference_price=fill,
            )
            pos_strategy = pos.strategy_name
            closed = book.close_position(pos.id, exit_price=fill, commission=commission)
            trades.append(self._to_trade(
                closed, entry_idx=pos.entry_idx, exit_idx=n - 1,
                exit_reason="signal", strategy_name=pos_strategy,
            ))

        # ── Compute metrics ──
        final_inst = self._initial_institutional_cash + book.institutional.realized_pnl_total
        final_aggr = self._initial_aggressive_cash + book.aggressive.realized_pnl_total
        total_return_pct = (
            (final_inst + final_aggr - (self._initial_institutional_cash + self._initial_aggressive_cash))
            / (self._initial_institutional_cash + self._initial_aggressive_cash) * 100.0
        )

        # Max drawdown from the combined equity curve
        max_dd_pct = self._compute_max_dd_pct(equity_total)

        total_stop_outs = (
            book.institutional.stop_out_count + book.aggressive.stop_out_count
        )

        metrics = {
            "total_return_pct": total_return_pct,
            "institutional_return_pct": (
                (final_inst - self._initial_institutional_cash)
                / self._initial_institutional_cash * 100.0
                if self._initial_institutional_cash > 0 else 0.0
            ),
            "aggressive_return_pct": (
                (final_aggr - self._initial_aggressive_cash)
                / self._initial_aggressive_cash * 100.0
                if self._initial_aggressive_cash > 0 else 0.0
            ),
            "max_dd_pct": max_dd_pct,
            "total_trades": len(trades),
            "broker_stop_outs": total_stop_outs,
            "final_institutional_equity": final_inst,
            "final_aggressive_equity": final_aggr,
            "final_total_equity": final_inst + final_aggr,
        }

        log.info(
            "leveraged_backtest_complete",
            trades=len(trades),
            return_pct=round(total_return_pct, 2),
            max_dd_pct=round(max_dd_pct, 2),
            broker_stop_outs=total_stop_outs,
        )

        return LeveragedBacktestResult(
            trades=trades,
            equity_curve_total=equity_total,
            equity_curve_institutional=equity_institutional,
            equity_curve_aggressive=equity_aggressive,
            margin_level_curve=margin_level_curve,
            metrics=metrics,
            total_candles=n,
            broker_stop_out_count=total_stop_outs,
            open_rejected_count=self._open_rejected_count,  # task #115
            final_institutional_equity=final_inst,
            final_aggressive_equity=final_aggr,
        )

    # ── Private helpers ──────────────────────────────────────────────────

    def _apply_intrabar_sl_tp(
        self,
        book: Book,
        bar: Bar,
        bar_idx: int,
        trades: list[LeveragedTrade],
        strategy_name: str | None = None,
    ) -> list[int]:
        closed_ids: list[int] = []
        for pos in list(book.all_positions()):
            hit_label, hit_price = check_sl_tp_hits(
                bar=bar,
                side=pos.side,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                path_model=self._path_model,
                bar_idx=bar_idx,
            )
            if hit_label is None or hit_price is None:
                continue

            fee_side = "SELL" if pos.side == "LONG" else "BUY"
            fill_price = self._fee_model.fill_price(
                side=fee_side,
                reference_price=hit_price,
                atr=0.0,
                ts_ms=bar.ts_ms,
            )
            commission = self._fee_model.commission_usd(
                quantity_units=pos.quantity,
                reference_price=fill_price,
            )
            pos_strategy = pos.strategy_name
            pos_entry_idx = pos.entry_idx
            closed = book.close_position(
                pos.id, exit_price=fill_price, commission=commission,
            )
            trades.append(self._to_trade(
                closed, entry_idx=pos_entry_idx, exit_idx=bar_idx,
                exit_reason="stop_loss" if hit_label == "sl" else "take_profit",
                strategy_name=strategy_name or pos_strategy,
            ))
            closed_ids.append(pos.id)
        return closed_ids

    def _worst_case_marks(
        self,
        positions: dict[int, LeveragedPosition],
        bar: Bar,
        path_model: BrownianBridgeModel | M1PathModel,
    ) -> dict[int, float]:
        """Return worst-case intrabar mark prices per position.

        LONG positions are marked at bar.low (max adverse).
        SHORT positions are marked at bar.high (max adverse).

        Used for broker stop-out check: we want to catch cases where a
        position was stopped out mid-bar by worst-case excursion even if
        the bar closed favorable.
        """
        return {
            pos.id: path_model.worst_adverse_price(bar, pos.side)
            for pos in positions.values()
        }

    def _handle_signal(
        self,
        *,
        signal,
        bar: Bar,
        bar_idx: int,
        book: Book,
        leverage: float,
        sub_book: str,
        inst,
        trades: list[LeveragedTrade],
        strategy_name: str,
    ) -> None:
        """Open or close positions based on the strategy signal.

        For LONG/SHORT entries: open a new leveraged position with the
        strategy's risk_pct translated to quantity via the Instrument's
        contract_size and current equity.

        For CLOSE: close all positions in the chosen sub-book for this
        strategy. (MVP: simple policy, no position-level CLOSE targeting.)
        """
        action = signal.action

        if action == SignalAction.CLOSE:
            # Close all positions for this strategy in the chosen sub-book
            sb = book.sub_book(sub_book)
            pos_ids = [
                pid for pid, pos in sb.positions.items()
                if pos.strategy_name == strategy_name
            ]
            for pid in pos_ids:
                pos = sb.positions[pid]
                fee_side = "SELL" if pos.side == "LONG" else "BUY"
                fill_price = self._fee_model.fill_price(
                    side=fee_side,
                    reference_price=bar.close,
                    atr=0.0,
                    ts_ms=bar.ts_ms,
                )
                commission = self._fee_model.commission_usd(
                    quantity_units=pos.quantity,
                    reference_price=fill_price,
                )
                closed = book.close_position(
                    pid, exit_price=fill_price, commission=commission,
                )
                trades.append(self._to_trade(
                    closed, entry_idx=pos.entry_idx, exit_idx=bar_idx,
                    exit_reason="signal", strategy_name=strategy_name,
                ))
            return

        if action not in (SignalAction.LONG, SignalAction.SHORT):
            return  # HOLD or unknown — nothing to do

        # Only open if no existing position from this strategy in this sub-book
        # (MVP: one-at-a-time per strategy; hedged strategy will override this)
        sb = book.sub_book(sub_book)
        existing = [
            p for p in sb.positions.values() if p.strategy_name == strategy_name
        ]
        if existing:
            return  # already have a position; ignore new entry

        entry_reference = signal.entry_price or bar.close
        fee_side = "BUY" if action == SignalAction.LONG else "SELL"
        fill_price = self._fee_model.fill_price(
            side=fee_side,
            reference_price=entry_reference,
            atr=self._approximate_atr(bar),
            ts_ms=bar.ts_ms,
        )

        # Translate risk_pct to quantity via the Instrument metadata
        risk_pct = signal.risk_pct if signal.risk_pct is not None else 0.01
        sub_equity = sb.cash  # proxy for sub-book equity at open
        risk_amount_usd = sub_equity * risk_pct
        # Position size = risk_amount / stop_distance. We compute distance in
        # raw price units because the instrument's quote currency is USD/oz
        # (xauusd) — the contract_size conversion is handled at commission
        # time, not at sizing time.
        stop_distance_usd = 0.0
        if signal.stop_loss is not None:
            stop_distance_usd = abs(fill_price - signal.stop_loss)
        if stop_distance_usd <= 0:
            # No SL specified — size by a default 1% fallback stop distance
            stop_distance_usd = fill_price * 0.01

        quantity = risk_amount_usd / stop_distance_usd
        if quantity <= 0:
            return

        effective_leverage = self._resolve_leverage(
            signal=signal,
            default_leverage=leverage,
            book=book,
            strategy_name=strategy_name,
            bar_idx=bar_idx,
        )
        if effective_leverage is None or effective_leverage <= 0:
            # M3S declined the grant (zero headroom) — skip the open
            return

        try:
            pos = book.open_position(
                side="LONG" if action == SignalAction.LONG else "SHORT",
                entry_price=fill_price,
                quantity=quantity,
                leverage=effective_leverage,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                entry_ts_ms=bar.ts_ms,
                entry_idx=bar_idx,
                strategy_name=strategy_name,
                sub_book=sub_book,
            )
        except ValueError as e:
            # Task #115: count margin-gate rejections so deep_backtest can
            # surface margin-starvation as a CellResult.rejected_positions
            # field. Also emitted to structlog for debugging.
            self._open_rejected_count += 1
            log.warning("leveraged_open_rejected", reason=str(e),
                        strategy=strategy_name, bar_idx=bar_idx)
            return

    def _resolve_leverage(
        self,
        *,
        signal,
        default_leverage: float,
        book: Book,
        strategy_name: str,
        bar_idx: int,
    ) -> float | None:
        if self._m3s is None:
            if signal.leverage is not None and signal.leverage > 0:
                return float(signal.leverage)
            return float(default_leverage)

        declared_range = self._declared_range_for(signal, default_leverage)
        conviction = float(signal.confidence or 0.5)
        current_aggregate = self._compute_aggregate_leverage(book)

        grant = self._m3s.request_leverage(
            strategy_name=strategy_name,
            conviction=conviction,
            declared_range=declared_range,
            current_aggregate_leverage=current_aggregate,
            reason=f"signal_bar_{bar_idx}",
            ts_ms=int(signal.timestamp or 0) or None,
        )
        if grant.granted <= 0:
            log.info(
                "leveraged_signal_declined_by_m3s",
                strategy=strategy_name,
                reason=grant.reason.value,
                requested=grant.requested,
                aggregate_before=grant.aggregate_before,
            )
            return None
        return float(grant.granted)

    def _declared_range_for(self, signal, default_leverage: float) -> tuple[float, float]:
        if signal.leverage is not None and signal.leverage > 0:
            L = float(signal.leverage)
            return (L, L)
        return (1.0, max(1.0, float(default_leverage)))

    def _compute_aggregate_leverage(self, book: Book) -> float:
        marks = {pos.id: pos.entry_price for pos in book.all_positions()}
        equity = book.total_equity(marks)
        if equity <= 0:
            return float("inf")
        total_notional = sum(pos.notional() for pos in book.all_positions())
        return total_notional / equity

    def _to_trade(
        self,
        closed: dict,
        *,
        entry_idx: int,
        exit_idx: int,
        exit_reason: str,
        strategy_name: str,
    ) -> LeveragedTrade:
        """Convert a Book.close_position dict into a LeveragedTrade record."""
        pnl = float(closed["pnl"])
        commission = float(closed.get("commission", 0.0))
        realized = float(closed["realized_pnl"])
        entry_price = float(closed["entry_price"])
        quantity = float(closed["quantity"])
        # pnl_pct as a percentage of the margin used (leverage-aware)
        entry_margin = float(closed.get("entry_margin", 0.0))
        pnl_pct = (pnl / entry_margin * 100.0) if entry_margin > 0 else 0.0

        return LeveragedTrade(
            entry_idx=entry_idx,
            exit_idx=exit_idx,
            side=closed["side"],
            entry_price=entry_price,
            exit_price=float(closed["exit_price"]),
            quantity=quantity,
            leverage=float(closed["leverage"]),
            margin_used=entry_margin,
            pnl=pnl,
            pnl_pct=pnl_pct,
            spread_cost=0.0,  # spread is baked into fill_price; not itemized here
            commission=commission,
            total_cost=commission,  # spread is in entry/exit price, commission is separate
            realized_pnl=realized,
            exit_reason=exit_reason,
            sub_book=closed.get("sub_book", SUB_BOOK_INSTITUTIONAL),
            strategy_name=strategy_name,
        )

    def _compute_max_dd_pct(self, equity_curve: list[float]) -> float:
        """Max drawdown as a percentage from peak."""
        if not equity_curve:
            return 0.0
        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd * 100.0

    def _approximate_atr(self, bar: Bar) -> float:
        """Quick ATR proxy from a single bar (high - low). Used for
        slippage sizing when the real ATR indicator is not in scope.
        """
        return abs(bar.high - bar.low)

    def _get_atr(self, df: pd.DataFrame, idx: int) -> float:
        """Get ATR_14 from the dataframe at the given index, or 0 if missing."""
        try:
            val = df.iloc[idx].get("ATR_14")
            if val is None or (isinstance(val, float) and val != val):  # NaN check
                return 0.0
            return float(val)
        except (KeyError, IndexError):
            return 0.0
