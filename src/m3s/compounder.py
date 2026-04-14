"""M3S Compounder — HWM-gated, vol-targeted, mode-aware position-size scalar.

Produces the portfolio-level `risk_scalar` multiplied into every signal's
`risk_pct` before it reaches the risk server, and manages the `CompoundState`
(base_equity + HWM) that advances at weekly/daily/per-trade close.

## Scalar composition (every signal)

    scalar = vol_target_scalar
           × mode_pace_scalar     (rolling Sharpe dial, clamped per mode)
           × cvar_scalar          (Tier 1 #5 — fat-tail protection)
           × dd_scalar            (mode DD freeze/halt)

Each factor is in [0, some_cap]. DD halt forces the whole thing to 0.
Final scalar is clamped to [0.0, 1.5] as a hard safety rail — M3S can
shrink a signal, can double it at most 1.5x in extreme GROWTH mode.

## Base advancement (scheduled tick)

    on weekly/daily/per-trade close:
        if drawdown <= freeze_threshold:
            if equity > state.hwm:
                state.hwm        = equity   # HWM ratchet
                state.base_equity = equity  # compound
        else:
            # frozen — no compounding during drawdown
            pass

Per-trade compounding is gated behind `compound_cadence == "per_trade"` and
the `compound_every_n_trades` counter (CUSTOM mode only, enforced by the
mode loader's safety rails).

## CVaR tail-risk scaling (Tier 1 #5)

Vol targeting assumes Gaussian returns. Crypto doesn't — fat left tails
during deleveraging cascades. CVaR_95 (average of worst 5% of daily
returns over a rolling 60-day window) is a direct measure of the left
tail. When CVaR deteriorates (more negative) relative to its baseline,
the compounder auto-de-levers.

    target_cvar = -0.02  # expect worst 5% days to average -2% daily
    realized_cvar_95 = avg(sorted(daily_returns)[:ceil(0.05 * n)])
    cvar_scalar = clamp(target_cvar / realized_cvar_95, 0.3, 1.2)

If realized_cvar_95 is less negative than target (tails calm), scalar >1.
If realized_cvar_95 is more negative than target (tails fat), scalar <1.
Direction handling carefully: we want scalar small when realized is bad,
so the ratio reverses sign-wise — see `_compute_cvar_scalar` below.

## Dependencies

- `PortfolioTracker` (sub-phase 0.2) — reads snapshot for rolling metrics
  and the full trade history for CVaR estimation.
- `ModeConfig` (sub-phase 0.1) — mode-specific dials (all 4 modes).

No dependency on allocator/scheduler/hooks — those consume the compounder.
"""

from __future__ import annotations

import enum
import math
import time
from dataclasses import dataclass, field

from src.m3s.modes import M3SMode, ModeConfig
from src.m3s.portfolio import PortfolioTracker, _ClosedTrade
from src.m3s.types import CompoundState, PortfolioSnapshot
from src.utils.logger import get_logger

log = get_logger("m3s.compounder")


# ── Constants ──────────────────────────────────────────────────────────


_MS_PER_DAY = 86_400_000
_CRYPTO_ANNUALIZATION = math.sqrt(365.0)

# Hard global safety rails on the final scalar. Even in GROWTH mode with
# a roaring Sharpe, we never upscale a signal beyond 1.5x its original
# risk_pct. Conservative max enforced by mode ceilings is smaller still.
_SCALAR_HARD_FLOOR = 0.0
_SCALAR_HARD_CEILING = 1.5

# CVaR scaling bounds. Tighter than the overall scalar because CVaR is
# one factor among many.
_CVAR_SCALAR_FLOOR = 0.3
_CVAR_SCALAR_CEILING = 1.2
_CVAR_CONFIDENCE = 0.95          # 95% CVaR
_CVAR_TARGET_DAILY = -0.02       # expected worst-5% avg daily return
_CVAR_WINDOW_DAYS = 60

# Vol targeting bounds — matches the v1 plan § 8.
_VOL_SCALAR_FLOOR = 0.5
_VOL_SCALAR_CEILING = 1.5


# ── Trade close event for per-trade compounding ───────────────────────


@dataclass(frozen=True)
class TradeCloseEvent:
    """Minimal info the compounder needs when a trade closes."""
    ts_ms: int
    strategy: str
    pnl: float


# ── Compound tick outcome ──────────────────────────────────────────────


class CompoundTickReason(str, enum.Enum):
    """Why the last compound base update did or did not move."""
    ADVANCED = "advanced"              # base moved up with HWM
    FROZEN_DD = "frozen_drawdown"       # DD above freeze → no advance
    BELOW_HWM = "below_hwm"             # equity < existing HWM → no advance
    WAITING_N_TRADES = "waiting_n_trades"  # per-trade cadence, counter not met
    OFF_CADENCE = "off_cadence"         # cadence mismatch


@dataclass(frozen=True)
class CompoundTickResult:
    reason: CompoundTickReason
    new_base: float
    new_hwm: float
    ts_ms: int


# ── Compounder ─────────────────────────────────────────────────────────


class Compounder:
    """Portfolio-level position-size scalar + HWM-gated base advancement."""

    def __init__(
        self,
        *,
        mode: ModeConfig,
        tracker: PortfolioTracker,
        initial_equity: float | None = None,
        cvar_enabled: bool = True,
    ) -> None:
        self._mode = mode
        self._tracker = tracker
        self._cvar_enabled = bool(cvar_enabled)

        eq = float(initial_equity if initial_equity is not None else tracker.equity)
        now_ms = int(time.time() * 1000)
        self._state = CompoundState(
            base_equity=eq,
            hwm=eq,
            last_updated_ts_ms=now_ms,
            mode=mode.name.value,
        )

        # Per-trade cadence counter: per-strategy, resets after N trades.
        self._trades_since_compound: dict[str, int] = {}

    # ── Public read accessors ───────────────────────────────────────

    @property
    def state(self) -> CompoundState:
        return self._state

    @property
    def mode(self) -> ModeConfig:
        return self._mode

    def set_mode(self, mode: ModeConfig) -> None:
        """Mode transition (e.g., auto-demote). Does NOT touch base/HWM."""
        log.info(
            "m3s.compounder.mode_transition",
            old=self._state.mode,
            new=mode.name.value,
        )
        self._mode = mode
        self._state.mode = mode.name.value

    # ── Leverage picker (G.0c.3 — regime-aware target leverage) ─────

    def target_leverage(
        self,
        leverage_range: tuple[float, float],
        snapshot: PortfolioSnapshot,
    ) -> float:
        """Return a target effective leverage within `leverage_range` based on
        current portfolio regime (mode + drawdown + HWM distance).

        Strategies declare their leverage_range; M3S picks the actual level
        per regime. This is the G.0c.3 leverage-first hook. Strategies that
        want leverage should call this method at signal time to get a
        regime-appropriate value, then set `signal.leverage` before emitting.

        Decision heuristic (intentionally simple; more sophisticated regime
        rules can be added without changing the interface):
          - CONSERVATIVE mode → always min of range
          - STANDARD mode    → middle of range when drawdown < 2%, min otherwise
          - GROWTH mode      → max of range when drawdown < 5%, middle when
                               < 10%, min when deeper
          - CUSTOM mode      → middle of range (CUSTOM has its own rules,
                               users can override by setting signal.leverage
                               explicitly)

        Caps:
          - NEVER exceeds max(leverage_range) no matter what mode says
          - NEVER below min(leverage_range) for live signals
          - If range is (1, 1) (default crypto), always returns 1.0
        """
        lo, hi = float(leverage_range[0]), float(leverage_range[1])
        if lo < 1.0:
            lo = 1.0
        if hi < lo:
            hi = lo
        if lo >= hi:
            # Single-point range — just return it
            return lo

        dd = float(snapshot.drawdown_pct)
        mode_name = self._mode.name.value

        if mode_name == "CONSERVATIVE":
            return lo
        if mode_name == "STANDARD":
            return lo + (hi - lo) * 0.5 if dd < 0.02 else lo
        if mode_name == "GROWTH":
            if dd < 0.05:
                return hi
            if dd < 0.10:
                return lo + (hi - lo) * 0.5
            return lo
        # CUSTOM or unknown — conservative middle
        return lo + (hi - lo) * 0.5

    # ── Scalar composition (called on every signal) ─────────────────

    def risk_scalar(
        self,
        snapshot: PortfolioSnapshot,
        leverage: float = 1.0,
    ) -> float:
        """Composition scalar for Signal.risk_pct. At leverage==1.0 uses the
        legacy product (bit-exact with pre-G.2b); at leverage>1 uses geometric
        mean + explicit leverage damping so factors don't fight at high L.
        """
        dd_scalar = self._dd_scalar(snapshot.drawdown_pct)
        if dd_scalar == 0.0:
            return 0.0

        vol_scalar = self._vol_target_scalar(snapshot)
        pace_scalar = self._mode_pace_scalar(snapshot)
        cvar_scalar = self._cvar_scalar(snapshot) if self._cvar_enabled else 1.0

        if leverage <= 1.0:
            combined = vol_scalar * pace_scalar * cvar_scalar * dd_scalar
        else:
            product = vol_scalar * pace_scalar * cvar_scalar
            risk_geomean = product ** (1.0 / 3.0) if product > 0 else 0.0
            stress = 1.0 - min(vol_scalar, pace_scalar, cvar_scalar)
            leverage_damping = math.exp(-stress * math.log1p(leverage) * 0.1)
            combined = risk_geomean * leverage_damping * dd_scalar

        return max(_SCALAR_HARD_FLOOR, min(_SCALAR_HARD_CEILING, combined))

    # ── Base advancement (called on scheduled tick or trade close) ──

    def update_base(
        self,
        snapshot: PortfolioSnapshot,
        *,
        trigger: str = "scheduled",
    ) -> CompoundTickResult:
        """Advance compound state if HWM gate allows.

        `trigger` is one of "scheduled" (weekly/daily close) or "trade_close"
        (per-trade cadence only). Per-trade calls are additionally gated by
        `compound_every_n_trades` via `on_trade_close` — this method assumes
        any counter gating has already happened.
        """
        ts = snapshot.ts_ms

        # (1) DD freeze — base doesn't advance.
        if snapshot.drawdown_pct > self._mode.dd_freeze_threshold:
            return CompoundTickResult(
                reason=CompoundTickReason.FROZEN_DD,
                new_base=self._state.base_equity,
                new_hwm=self._state.hwm,
                ts_ms=ts,
            )

        # (2) HWM gate — equity must exceed existing HWM.
        if snapshot.equity <= self._state.hwm:
            return CompoundTickResult(
                reason=CompoundTickReason.BELOW_HWM,
                new_base=self._state.base_equity,
                new_hwm=self._state.hwm,
                ts_ms=ts,
            )

        # (3) Advance both.
        self._state.hwm = snapshot.equity
        self._state.base_equity = snapshot.equity
        self._state.last_updated_ts_ms = ts
        log.info(
            "m3s.compounder.advanced",
            new_base=self._state.base_equity,
            new_hwm=self._state.hwm,
            trigger=trigger,
            mode=self._state.mode,
        )
        return CompoundTickResult(
            reason=CompoundTickReason.ADVANCED,
            new_base=self._state.base_equity,
            new_hwm=self._state.hwm,
            ts_ms=ts,
        )

    def on_trade_close(
        self,
        event: TradeCloseEvent,
        snapshot: PortfolioSnapshot,
    ) -> CompoundTickResult | None:
        """Per-trade compounding hook.

        Only relevant when `compound_cadence == "per_trade"` (CUSTOM mode
        only, per safety rail). Returns None when the cadence is not
        per-trade (caller should use scheduled `update_base` instead).
        """
        if self._mode.compound_cadence != "per_trade":
            return CompoundTickResult(
                reason=CompoundTickReason.OFF_CADENCE,
                new_base=self._state.base_equity,
                new_hwm=self._state.hwm,
                ts_ms=event.ts_ms,
            )

        counter = self._trades_since_compound.get(event.strategy, 0) + 1
        if counter < self._mode.compound_every_n_trades:
            self._trades_since_compound[event.strategy] = counter
            return CompoundTickResult(
                reason=CompoundTickReason.WAITING_N_TRADES,
                new_base=self._state.base_equity,
                new_hwm=self._state.hwm,
                ts_ms=event.ts_ms,
            )

        # Reset counter, advance base.
        self._trades_since_compound[event.strategy] = 0
        return self.update_base(snapshot, trigger="trade_close")

    # ── Internal: scalar factors ─────────────────────────────────────

    def _dd_scalar(self, dd_pct: float) -> float:
        if dd_pct >= self._mode.dd_halt_threshold:
            return 0.0
        if dd_pct > self._mode.dd_freeze_threshold:
            return 0.5
        return 1.0

    def _vol_target_scalar(self, snapshot: PortfolioSnapshot) -> float:
        """Ratio of target vol to realized portfolio vol, clamped."""
        realized_vol = self._portfolio_realized_vol(snapshot)
        if realized_vol <= 0.0:
            return 1.0
        raw = self._mode.vol_target_annual / realized_vol
        return max(_VOL_SCALAR_FLOOR, min(_VOL_SCALAR_CEILING, raw))

    def _mode_pace_scalar(self, snapshot: PortfolioSnapshot) -> float:
        """Rolling-Sharpe pace dial clamped to mode's floor/ceiling.

        Uses the portfolio average rolling Sharpe — if there are no
        per-strategy snapshots, returns 1.0 (neutral).
        """
        strategies = snapshot.per_strategy
        if not strategies:
            return 1.0

        sharpes = [s.rolling_sharpe_30d for s in strategies.values()]
        avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0.0

        # Target Sharpe for full pace ~= midpoint of "decent" range.
        target_sharpe = 1.5
        raw = avg_sharpe / target_sharpe if target_sharpe > 0 else 0.0

        # Clamp to mode's floor/ceiling.
        return max(
            self._mode.compound_pace_floor,
            min(self._mode.compound_pace_ceiling, raw),
        )

    def _cvar_scalar(self, snapshot: PortfolioSnapshot) -> float:
        """CVaR-based tail-risk scalar.

        Computes realized CVaR_95 from the tracker's trade history over
        `_CVAR_WINDOW_DAYS`, compares to `_CVAR_TARGET_DAILY`, and returns
        the ratio clamped to [_CVAR_SCALAR_FLOOR, _CVAR_SCALAR_CEILING].

        Direction:
        - realized ≈ target (e.g., -2%) → scalar ≈ 1
        - realized MORE negative than target (-4%, fat tail) → scalar < 1 (de-lever)
        - realized LESS negative than target (-1%, calm tail) → scalar > 1 (up-lever)
        - realized positive (no losing-tail days at all in window) → scalar = ceiling
        - insufficient data or CVaR disabled → 1.0 (neutral)
        """
        if not self._cvar_enabled:
            return 1.0
        realized_cvar = self._compute_realized_cvar(snapshot.ts_ms)
        if realized_cvar is None:
            return 1.0

        # Positive or zero realized CVaR means the worst 5% of days were
        # still non-negative — calm tails. Up-lever to the ceiling.
        if realized_cvar >= 0.0:
            return _CVAR_SCALAR_CEILING

        # Both target and realized are negative here.
        raw = _CVAR_TARGET_DAILY / realized_cvar
        return max(_CVAR_SCALAR_FLOOR, min(_CVAR_SCALAR_CEILING, raw))

    def _compute_realized_cvar(self, now_ms: int) -> float | None:
        """Compute the 95% CVaR of daily portfolio returns over the window.

        Returns None if fewer than 10 non-zero days in the window (too
        little data — neutral scalar).
        """
        if self._tracker.equity <= 0:
            return None

        cutoff_ms = now_ms - _CVAR_WINDOW_DAYS * _MS_PER_DAY
        # Aggregate ALL strategies' trades into portfolio daily returns.
        daily_pnl: dict[int, float] = {}
        for name in self._tracker.strategy_names():
            st = self._tracker._strategies[name]
            for tr in st.trades:
                if tr.ts_ms < cutoff_ms:
                    continue
                day = tr.ts_ms // _MS_PER_DAY
                daily_pnl[day] = daily_pnl.get(day, 0.0) + tr.pnl

        if len(daily_pnl) < 10:
            return None

        returns = sorted(
            (pnl / self._tracker.equity for pnl in daily_pnl.values())
        )
        # CVaR_95 = average of worst 5% of returns.
        k = max(1, int(math.ceil(len(returns) * (1.0 - _CVAR_CONFIDENCE))))
        worst = returns[:k]
        cvar = sum(worst) / len(worst)
        return cvar

    def _portfolio_realized_vol(self, snapshot: PortfolioSnapshot) -> float:
        """Aggregate per-strategy realized vol into a portfolio estimate.

        Assumes zero cross-correlation (upper bound on diversification
        benefit — conservative for vol targeting: produces a lower
        aggregate vol, which produces a higher scalar, which the clamp
        caps. We use per-strategy vol as a proxy for now; the allocator
        will refine this in sub-phase 0.4 using the full covariance matrix.)

        If no strategies yet, returns the mode's target (neutral scalar=1).
        """
        vols = [
            s.realized_vol_30d for s in snapshot.per_strategy.values()
            if s.realized_vol_30d > 0
        ]
        if not vols:
            return self._mode.vol_target_annual  # neutral
        # Simple RMS of per-strategy vols as portfolio proxy.
        rms = math.sqrt(sum(v * v for v in vols) / len(vols))
        return rms
