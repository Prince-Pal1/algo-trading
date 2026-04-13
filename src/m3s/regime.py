"""M3S Regime Detector + Auto Mode Switching — Tier 1 #1.

The single biggest Tier 1 upgrade. Your strategies have very different regime
preferences (mean reversion loves chop, breakout loves trends, momentum loves
volatility). A static allocation gives equal weight always. Regime-aware
allocation *shifts weight toward whichever mode matches the current market*.

## Regime classification

Classifier inputs (all annualized where meaningful):

- `btc_realized_vol_annual` — BTC 30-day realized volatility (annualized).
  Low <20%, normal 20–40%, high 40–60%, crisis >60%.
- `btc_adx_14` — 14-period ADX for BTC, 0-100. >25 = trending, <20 = chop.
- `portfolio_pairwise_max_corr` — maximum pairwise strategy correlation.
  Spikes near 1.0 in crisis (all strategies lose simultaneously).
- `vol_median_annual` — long-run median realized vol for the asset, used as
  a denominator when assessing "how unusual is current vol."

Classification rules (evaluated top-down, first-match wins):

    crisis         = portfolio_max_corr ≥ 0.85 OR btc_vol ≥ 2 × median
    high_vol       = btc_vol ≥ 1.5 × median AND btc_adx < 25
    low_vol_trend  = btc_vol ≤ 0.8 × median AND btc_adx ≥ 25
    normal         = (everything else)

Each regime maps to an M3S mode (configurable via `AutoModeSwitcher`):

    low_vol_trend → GROWTH       (good risk/reward — compound faster)
    normal        → STANDARD     (default)
    high_vol      → CONSERVATIVE (choppy, reduce size)
    crisis        → CONSERVATIVE (preserve capital; caller usually freezes too)

## Auto mode switcher

Proposes and applies mode transitions on `M3S.set_mode()` with two safety
rails:

- **Cooldown.** After a mode change, no further changes are allowed for
  `cooldown_hours`. Prevents whipsawing across regime boundaries.
- **Manual override.** If Prince manually set the mode via CLI, auto-switching
  is disabled for `manual_override_hours`. The scheduler respects the human.

The switcher does not promote from CONSERVATIVE→STANDARD/GROWTH automatically.
Re-upping after a drawdown is the most emotional decision and is always
manual — matches the v1 plan §7 rule.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.m3s.modes import MODE_PRESETS, M3SMode
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.m3s.hooks import M3S

log = get_logger("m3s.regime")


_MS_PER_HOUR = 3_600_000


# ══════════════════════════════════════════════════════════════════════
# Regime types
# ══════════════════════════════════════════════════════════════════════


class Regime(str, enum.Enum):
    LOW_VOL_TREND = "low_vol_trend"
    NORMAL = "normal"
    HIGH_VOL = "high_vol"
    CRISIS = "crisis"


@dataclass(frozen=True)
class RegimeInputs:
    """Point-in-time observable features for regime classification."""
    btc_realized_vol_annual: float       # e.g., 0.45 = 45% annualized
    btc_adx_14: float                    # 0-100
    portfolio_pairwise_max_corr: float   # -1..1
    vol_median_annual: float = 0.35      # long-run benchmark


@dataclass(frozen=True)
class RegimeClassification:
    regime: Regime
    reasoning: str
    inputs: RegimeInputs


# ══════════════════════════════════════════════════════════════════════
# Classifier
# ══════════════════════════════════════════════════════════════════════


class RegimeClassifier:
    """Pure rule-based classifier. Stateless — safe to call concurrently."""

    def __init__(
        self,
        *,
        crisis_corr_threshold: float = 0.85,
        crisis_vol_ratio: float = 2.0,
        high_vol_ratio: float = 1.5,
        low_vol_ratio: float = 0.8,
        trend_adx_threshold: float = 25.0,
        chop_adx_threshold: float = 20.0,
    ) -> None:
        if not (0.0 < crisis_corr_threshold <= 1.0):
            raise ValueError(
                f"crisis_corr_threshold must be in (0, 1], got {crisis_corr_threshold}"
            )
        if crisis_vol_ratio <= 1.0:
            raise ValueError(f"crisis_vol_ratio must be > 1, got {crisis_vol_ratio}")
        if high_vol_ratio <= 1.0 or high_vol_ratio >= crisis_vol_ratio:
            raise ValueError(
                f"high_vol_ratio must satisfy 1 < x < {crisis_vol_ratio}, got {high_vol_ratio}"
            )
        if low_vol_ratio >= 1.0 or low_vol_ratio <= 0.0:
            raise ValueError(
                f"low_vol_ratio must be in (0, 1), got {low_vol_ratio}"
            )
        if trend_adx_threshold <= chop_adx_threshold:
            raise ValueError(
                f"trend_adx_threshold ({trend_adx_threshold}) must be > "
                f"chop_adx_threshold ({chop_adx_threshold})"
            )

        self._crisis_corr = crisis_corr_threshold
        self._crisis_vol_ratio = crisis_vol_ratio
        self._high_vol_ratio = high_vol_ratio
        self._low_vol_ratio = low_vol_ratio
        self._trend_adx = trend_adx_threshold
        self._chop_adx = chop_adx_threshold

    def classify(self, inputs: RegimeInputs) -> RegimeClassification:
        """Apply the classification rules top-down, first-match wins."""
        median_vol = max(inputs.vol_median_annual, 1e-6)
        vol_ratio = inputs.btc_realized_vol_annual / median_vol

        # (1) Crisis — correlation spike OR 2× normal vol.
        if inputs.portfolio_pairwise_max_corr >= self._crisis_corr:
            return RegimeClassification(
                regime=Regime.CRISIS,
                reasoning=(
                    f"crisis: max_corr={inputs.portfolio_pairwise_max_corr:.2f} "
                    f">= {self._crisis_corr:.2f}"
                ),
                inputs=inputs,
            )
        if vol_ratio >= self._crisis_vol_ratio:
            return RegimeClassification(
                regime=Regime.CRISIS,
                reasoning=(
                    f"crisis: vol_ratio={vol_ratio:.2f} >= {self._crisis_vol_ratio:.2f}"
                ),
                inputs=inputs,
            )

        # (2) High vol chop.
        if vol_ratio >= self._high_vol_ratio and inputs.btc_adx_14 < self._trend_adx:
            return RegimeClassification(
                regime=Regime.HIGH_VOL,
                reasoning=(
                    f"high_vol: vol_ratio={vol_ratio:.2f}>={self._high_vol_ratio:.2f}, "
                    f"adx={inputs.btc_adx_14:.1f}<{self._trend_adx:.1f}"
                ),
                inputs=inputs,
            )

        # (3) Low-vol trending environment.
        if vol_ratio <= self._low_vol_ratio and inputs.btc_adx_14 >= self._trend_adx:
            return RegimeClassification(
                regime=Regime.LOW_VOL_TREND,
                reasoning=(
                    f"low_vol_trend: vol_ratio={vol_ratio:.2f}<={self._low_vol_ratio:.2f}, "
                    f"adx={inputs.btc_adx_14:.1f}>={self._trend_adx:.1f}"
                ),
                inputs=inputs,
            )

        # (4) Default: normal.
        return RegimeClassification(
            regime=Regime.NORMAL,
            reasoning=(
                f"normal: vol_ratio={vol_ratio:.2f}, adx={inputs.btc_adx_14:.1f}, "
                f"max_corr={inputs.portfolio_pairwise_max_corr:.2f}"
            ),
            inputs=inputs,
        )


# ══════════════════════════════════════════════════════════════════════
# Auto mode switcher
# ══════════════════════════════════════════════════════════════════════


_DEFAULT_MODE_MAP: dict[Regime, M3SMode] = {
    Regime.LOW_VOL_TREND: M3SMode.GROWTH,
    Regime.NORMAL: M3SMode.STANDARD,
    Regime.HIGH_VOL: M3SMode.CONSERVATIVE,
    Regime.CRISIS: M3SMode.CONSERVATIVE,
}


@dataclass(frozen=True)
class ModeTransition:
    """One proposed (or applied) mode change."""
    from_mode: M3SMode
    to_mode: M3SMode
    regime: Regime
    reasoning: str
    applied: bool
    ts_ms: int


class AutoModeSwitcher:
    """Orchestrator that pushes regime classifications into mode changes on M3S.

    Usage (from the scheduler in sub-phase 0.6):
        switcher = AutoModeSwitcher(m3s=m3s, classifier=RegimeClassifier())
        # on every scheduled tick, before rebalance:
        switcher.maybe_switch(inputs=regime_inputs, now_ms=...)
    """

    def __init__(
        self,
        *,
        m3s: "M3S",
        classifier: RegimeClassifier | None = None,
        mode_map: dict[Regime, M3SMode] | None = None,
        cooldown_hours: int = 12,
        manual_override_hours: int = 24,
        promote_up_enabled: bool = False,
    ) -> None:
        self._m3s = m3s
        self._classifier = classifier or RegimeClassifier()
        self._mode_map = dict(mode_map or _DEFAULT_MODE_MAP)
        self._cooldown_ms = int(cooldown_hours) * _MS_PER_HOUR
        self._manual_override_ms = int(manual_override_hours) * _MS_PER_HOUR
        self._promote_up_enabled = bool(promote_up_enabled)

        self._last_auto_change_ts_ms: int | None = None
        self._last_manual_change_ts_ms: int | None = None
        self._history: list[ModeTransition] = []

    # ── Public API ──────────────────────────────────────────────────

    def record_manual_change(self, ts_ms: int) -> None:
        """Called by CLI when Prince manually sets a mode.

        Disables auto-switching for `manual_override_hours`.
        """
        self._last_manual_change_ts_ms = ts_ms
        log.info(
            "m3s.regime.manual_change_recorded",
            ts_ms=ts_ms,
            override_until=ts_ms + self._manual_override_ms,
        )

    def history(self) -> list[ModeTransition]:
        return list(self._history)

    def maybe_switch(
        self,
        *,
        inputs: RegimeInputs,
        now_ms: int,
    ) -> ModeTransition | None:
        """Classify regime and switch mode if needed. Returns a
        ModeTransition if a change happened (applied=True) or was proposed
        but blocked by a gate (applied=False), or None if the current mode
        is already correct.
        """
        classification = self._classifier.classify(inputs)
        target_mode = self._mode_map.get(classification.regime, M3SMode.STANDARD)
        current_mode = self._m3s.mode.name

        if current_mode == target_mode:
            return None

        # Gate 1: cooldown — no back-to-back auto changes.
        if (
            self._last_auto_change_ts_ms is not None
            and (now_ms - self._last_auto_change_ts_ms) < self._cooldown_ms
        ):
            transition = ModeTransition(
                from_mode=current_mode,
                to_mode=target_mode,
                regime=classification.regime,
                reasoning=f"cooldown: {classification.reasoning}",
                applied=False,
                ts_ms=now_ms,
            )
            self._history.append(transition)
            log.info("m3s.regime.cooldown_block", **transition.__dict__)
            return transition

        # Gate 2: manual override — respect human command.
        if (
            self._last_manual_change_ts_ms is not None
            and (now_ms - self._last_manual_change_ts_ms) < self._manual_override_ms
        ):
            transition = ModeTransition(
                from_mode=current_mode,
                to_mode=target_mode,
                regime=classification.regime,
                reasoning=f"manual_override: {classification.reasoning}",
                applied=False,
                ts_ms=now_ms,
            )
            self._history.append(transition)
            return transition

        # Gate 3: promote-up disabled (CONSERVATIVE → STANDARD/GROWTH is manual only
        # unless the user flips `promote_up_enabled`).
        if not self._promote_up_enabled and self._is_promotion(current_mode, target_mode):
            transition = ModeTransition(
                from_mode=current_mode,
                to_mode=target_mode,
                regime=classification.regime,
                reasoning=(
                    f"promote_up_disabled: {classification.reasoning}; "
                    f"{current_mode.value} → {target_mode.value} requires manual override"
                ),
                applied=False,
                ts_ms=now_ms,
            )
            self._history.append(transition)
            log.info("m3s.regime.promote_blocked", **transition.__dict__)
            return transition

        # Apply the transition.
        self._m3s.set_mode(MODE_PRESETS[target_mode])
        self._last_auto_change_ts_ms = now_ms
        transition = ModeTransition(
            from_mode=current_mode,
            to_mode=target_mode,
            regime=classification.regime,
            reasoning=classification.reasoning,
            applied=True,
            ts_ms=now_ms,
        )
        self._history.append(transition)
        log.info("m3s.regime.mode_changed", **transition.__dict__)
        return transition

    # ── Internal ────────────────────────────────────────────────────

    def _is_promotion(self, current: M3SMode, target: M3SMode) -> bool:
        """Is target a more-aggressive mode than current?"""
        aggression = {
            M3SMode.CONSERVATIVE: 0,
            M3SMode.STANDARD: 1,
            M3SMode.GROWTH: 2,
            M3SMode.CUSTOM: 3,   # user-chosen — treat as "above growth"
        }
        return aggression.get(target, 0) > aggression.get(current, 0)
