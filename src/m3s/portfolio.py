"""M3S Portfolio Tracker — live equity / HWM / drawdown + per-strategy rolling stats.

The tracker is the read-model for Allocator (sub-phase 0.4) and Compounder
(sub-phase 0.3). It consumes trade closes and per-bar exposure updates and
produces a `PortfolioSnapshot` on demand (snapshot is frozen).

State model:
- **Equity / HWM / drawdown** — updated on every trade close (the only PnL
  source). Equity moves down with losses too; HWM only ratchets up.
- **Per-strategy trade log** — list of (ts_ms, pnl, symbol) tuples. Rolling
  stats (Sharpe, realized vol, pnl_30d, n_trades_30d) are computed on demand
  at snapshot time so the tracker doesn't re-run expensive math on every fill.
- **Per-strategy exposure timeline** — list of (ts_ms, symbol, sign). Used
  to compute pairwise signal correlation at snapshot time, since signal-level
  correlation is the right metric for same-market strategies (not PnL
  correlation — see m3s_plan_v1.md §1 D).

Rolling-stat conventions:
- Annualization factor is now parameterized via `periods_per_year` on the
  tracker (default 365 = crypto 24/7; pass 252 for forex 24/5). This is
  the G.0 refactor from the gold plan. Existing call sites using the default
  see identical behavior.
- Sparse-signal strategies produce degenerate per-trade Sharpe (bb_rsi_mr_opt
  often fires 1 trade/month). We daily-resample trade PnLs over the window,
  zero-fill empty days, then compute mean/std of daily returns. This matches
  the backtest metrics pattern (ARCHITECTURE.md Known Gotchas 2026-04-11).
- "Returns" are `daily_pnl / equity_ref` where equity_ref is the tracker's
  current equity. This is an approximation — proper per-strategy returns
  would need per-strategy capital allocations, which is allocator's job
  (sub-phase 0.4). Good enough for snapshot-time display + edge-decay input.

This module has no dependency on the allocator, compounder, or scheduler.
It is consumed via hooks in sub-phase 0.5.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from src.m3s.types import PortfolioSnapshot, StrategySnapshot
from src.utils.logger import get_logger

log = get_logger("m3s.portfolio")


# ── Internal record types ──────────────────────────────────────────────


@dataclass(frozen=True)
class _ClosedTrade:
    """One closed trade. Internal — not exposed via types.py."""
    ts_ms: int
    strategy: str
    symbol: str
    pnl: float


@dataclass(frozen=True)
class _ExposurePoint:
    """One per-bar exposure reading. Internal."""
    ts_ms: int
    strategy: str
    symbol: str
    sign: int  # -1 short, 0 flat, +1 long


@dataclass
class _PerStrategyState:
    trades: list[_ClosedTrade] = field(default_factory=list)
    exposure: list[_ExposurePoint] = field(default_factory=list)


# ── Constants ──────────────────────────────────────────────────────────


_MS_PER_DAY = 86_400_000
_CRYPTO_ANNUALIZATION = math.sqrt(365.0)  # Legacy — use self._annualization instead
_DEFAULT_PERIODS_PER_YEAR = 365.0         # Crypto 24/7 default (G.0 backward-compat)
_FOREX_PERIODS_PER_YEAR = 252.0           # Forex 24/5 (for XAUUSD, FX majors)
_DEFAULT_ROLLING_DAYS = 30
_SIGNAL_CORR_WINDOW_BARS = 720  # 30d × 1h — matches m3s_plan_v1 §10


# ── PortfolioTracker ───────────────────────────────────────────────────


class PortfolioTracker:
    """Live portfolio state for M3S.

    Usage:
        pt = PortfolioTracker(initial_equity=10_000.0)
        pt.on_trade_close("bb_rsi_mr_opt", pnl=120.0, symbol="BTCUSDT", ts_ms=...)
        pt.on_bar("bb_rsi_mr_opt", "BTCUSDT", exposure=1, ts_ms=...)
        snap = pt.snapshot(now_ms=...)
    """

    def __init__(
        self,
        *,
        initial_equity: float = 10_000.0,
        rolling_window_days: int = _DEFAULT_ROLLING_DAYS,
        signal_corr_window_bars: int = _SIGNAL_CORR_WINDOW_BARS,
        periods_per_year: float = _DEFAULT_PERIODS_PER_YEAR,
    ) -> None:
        """Construct a portfolio tracker.

        Args:
            periods_per_year: annualization denominator. Default 365 for
                crypto 24/7. Pass 252 for forex 24/5 (gold, FX majors).
                This is the G.0 gold-plan parameterization — backward
                compatible, existing call sites see identical behavior.
        """
        self._equity: float = float(initial_equity)
        self._hwm: float = float(initial_equity)
        self._last_equity_ts_ms: int = 0

        self._strategies: dict[str, _PerStrategyState] = {}
        self._rolling_window_days: int = int(rolling_window_days)
        self._signal_corr_window_bars: int = int(signal_corr_window_bars)
        self._periods_per_year: float = float(periods_per_year)
        self._annualization: float = math.sqrt(self._periods_per_year)

    # ── Mutators ─────────────────────────────────────────────────────

    def on_trade_close(
        self,
        strategy: str,
        pnl: float,
        symbol: str,
        ts_ms: int,
    ) -> None:
        """Record a closed trade and update equity/HWM.

        Equity moves by `pnl` (may be negative). HWM ratchets up only.
        Drawdown is computed at snapshot time from the current ratio.
        """
        st = self._strategies.setdefault(strategy, _PerStrategyState())
        st.trades.append(_ClosedTrade(ts_ms=ts_ms, strategy=strategy, symbol=symbol, pnl=float(pnl)))
        self._equity += float(pnl)
        if self._equity > self._hwm:
            self._hwm = self._equity
        self._last_equity_ts_ms = ts_ms

    def on_bar(
        self,
        strategy: str,
        symbol: str,
        exposure: int,
        ts_ms: int,
    ) -> None:
        """Record per-bar exposure sign for a strategy on a symbol.

        `exposure` must be -1, 0, or +1. The tracker keeps a rolling tail
        of points for signal-correlation computation — older points beyond
        `signal_corr_window_bars` are trimmed lazily at snapshot time.
        """
        if exposure not in (-1, 0, 1):
            raise ValueError(f"exposure must be -1/0/1, got {exposure}")
        st = self._strategies.setdefault(strategy, _PerStrategyState())
        st.exposure.append(
            _ExposurePoint(ts_ms=ts_ms, strategy=strategy, symbol=symbol, sign=int(exposure))
        )

    def update_equity_from_executor(self, new_equity: float, ts_ms: int) -> None:
        """Force-sync equity from an external source (e.g., PaperExecutor).

        Used when the M3S tracker is wired into a running engine and must
        reconcile with the authoritative equity number. Does NOT touch the
        per-strategy trade log — those come from on_trade_close.
        """
        self._equity = float(new_equity)
        if self._equity > self._hwm:
            self._hwm = self._equity
        self._last_equity_ts_ms = ts_ms

    # ── Read accessors ───────────────────────────────────────────────

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def hwm(self) -> float:
        return self._hwm

    @property
    def drawdown_pct(self) -> float:
        if self._hwm <= 0:
            return 0.0
        return max(0.0, 1.0 - (self._equity / self._hwm))

    def strategy_names(self) -> list[str]:
        return sorted(self._strategies.keys())

    # ── Snapshot ─────────────────────────────────────────────────────

    def snapshot(self, now_ms: int | None = None) -> PortfolioSnapshot:
        """Produce a point-in-time frozen snapshot of the portfolio.

        `now_ms` is the anchor for rolling windows. If None, uses the
        last ingested trade/bar timestamp (or 0 if no activity yet).
        """
        if now_ms is None:
            now_ms = self._last_equity_ts_ms

        per_strategy: dict[str, StrategySnapshot] = {}
        for name in self.strategy_names():
            per_strategy[name] = self._compute_strategy_snapshot(name, now_ms)

        signal_corr = self._compute_signal_corr(now_ms)

        return PortfolioSnapshot(
            ts_ms=int(now_ms),
            equity=float(self._equity),
            hwm=float(self._hwm),
            drawdown_pct=float(self.drawdown_pct),
            per_strategy=per_strategy,
            signal_corr=signal_corr,
        )

    # ── Per-strategy stat computation ────────────────────────────────

    def _compute_strategy_snapshot(self, name: str, now_ms: int) -> StrategySnapshot:
        st = self._strategies[name]
        trades = st.trades

        if not trades:
            return StrategySnapshot(
                name=name,
                n_trades_30d=0,
                rolling_sharpe_30d=0.0,
                realized_vol_30d=0.0,
                pnl_30d=0.0,
                lifetime_sharpe=0.0,
                lifetime_winrate=0.0,
            )

        window_ms = self._rolling_window_days * _MS_PER_DAY
        cutoff = now_ms - window_ms
        recent = [t for t in trades if t.ts_ms >= cutoff]

        pnl_30d = sum(t.pnl for t in recent)
        n_30d = len(recent)

        rolling_sharpe, realized_vol = self._daily_resampled_stats(
            recent, now_ms=now_ms, window_days=self._rolling_window_days
        )

        lifetime_sharpe, lifetime_winrate = self._lifetime_stats(trades)

        return StrategySnapshot(
            name=name,
            n_trades_30d=n_30d,
            rolling_sharpe_30d=rolling_sharpe,
            realized_vol_30d=realized_vol,
            pnl_30d=pnl_30d,
            lifetime_sharpe=lifetime_sharpe,
            lifetime_winrate=lifetime_winrate,
        )

    def _daily_resampled_stats(
        self,
        trades: list[_ClosedTrade],
        *,
        now_ms: int,
        window_days: int,
    ) -> tuple[float, float]:
        """Return (annualized_sharpe, annualized_realized_vol) over `window_days`.

        Trades are bucketed into days, zero-filled, converted to daily
        returns on the current equity base, then stats are computed.
        """
        if not trades or self._equity <= 0:
            return 0.0, 0.0

        # Build daily PnL buckets keyed by day index since epoch.
        daily_pnl: dict[int, float] = {}
        for t in trades:
            day = t.ts_ms // _MS_PER_DAY
            daily_pnl[day] = daily_pnl.get(day, 0.0) + t.pnl

        today = now_ms // _MS_PER_DAY
        returns: list[float] = []
        for i in range(window_days):
            day = today - i
            pnl = daily_pnl.get(day, 0.0)
            returns.append(pnl / self._equity)

        if len(returns) < 2:
            return 0.0, 0.0

        mean_r = sum(returns) / len(returns)
        # Population stdev is correct here: we're describing the sample,
        # not inferring a broader distribution.
        var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        std_r = math.sqrt(var)

        if std_r == 0.0:
            return 0.0, 0.0

        sharpe = (mean_r / std_r) * self._annualization
        vol_annualized = std_r * self._annualization
        return sharpe, vol_annualized

    def _lifetime_stats(self, trades: list[_ClosedTrade]) -> tuple[float, float]:
        """Return (lifetime_sharpe, lifetime_winrate)."""
        if len(trades) < 2:
            # Winrate still meaningful for 1 trade; Sharpe not.
            wins = sum(1 for t in trades if t.pnl > 0)
            return 0.0, (wins / len(trades)) if trades else 0.0

        pnls = [t.pnl for t in trades]
        n = len(pnls)
        mean_p = sum(pnls) / n
        var = sum((p - mean_p) ** 2 for p in pnls) / n
        std_p = math.sqrt(var)

        wins = sum(1 for p in pnls if p > 0)
        winrate = wins / n

        if std_p == 0.0:
            return 0.0, winrate

        # Per-trade Sharpe annualized by assumed 1 trade/day ≈ sqrt(periods_per_year).
        # This is a rough lifetime benchmark — not what the allocator uses
        # for live sizing. For that, see rolling_sharpe_30d above.
        lifetime_sharpe = (mean_p / std_p) * self._annualization
        return lifetime_sharpe, winrate

    # ── Signal correlation ───────────────────────────────────────────

    def _compute_signal_corr(self, now_ms: int) -> dict[str, float]:
        """Pairwise Pearson correlation of exposure series within the window.

        Output keys are "sorted_a|sorted_b" (alphabetical) so a given pair
        appears exactly once. Only strategies with at least 2 exposure points
        in the window contribute.
        """
        names = self.strategy_names()
        if len(names) < 2:
            return {}

        # Build aligned exposure series per strategy: list of (ts_bucket, sign)
        # bucketed by second-resolution timestamps within the last N bars of
        # activity. We use the raw exposure timeline trimmed to the window.
        series: dict[str, list[tuple[int, int]]] = {}
        window_bars = self._signal_corr_window_bars
        for name in names:
            points = self._strategies[name].exposure
            if not points:
                continue
            # Trim to the last `window_bars` points (cheap, consistent).
            tail = points[-window_bars:]
            series[name] = [(p.ts_ms, p.sign) for p in tail]

        out: dict[str, float] = {}
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if a not in series or b not in series:
                    continue
                corr = _pearson_by_ts(series[a], series[b])
                if corr is None:
                    continue
                key = f"{a}|{b}"  # names already sorted
                out[key] = corr
        return out


# ── Helpers (module-private) ───────────────────────────────────────────


def _pearson_by_ts(
    a: list[tuple[int, int]],
    b: list[tuple[int, int]],
) -> float | None:
    """Pearson correlation of two exposure series aligned on timestamp.

    Only overlapping timestamps contribute. Returns None if fewer than 2
    overlapping points or either side has zero variance.
    """
    # Align by exact timestamp.
    map_a = dict(a)
    map_b = dict(b)
    common_ts = sorted(set(map_a.keys()) & set(map_b.keys()))
    if len(common_ts) < 2:
        return None

    xs = [map_a[t] for t in common_ts]
    ys = [map_b[t] for t in common_ts]

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    cov = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n)) / n
    var_x = sum((xs[i] - mean_x) ** 2 for i in range(n)) / n
    var_y = sum((ys[i] - mean_y) ** 2 for i in range(n)) / n

    if var_x == 0.0 or var_y == 0.0:
        return None

    return cov / math.sqrt(var_x * var_y)
