"""Interactive TUI for deep_backtest configuration collection (task #114).

Presents multi-select tick-box prompts for each configuration dimension
(windows, timeframes, fees, leverages, leverage modes) using `questionary`.
Falls back gracefully to CLI-flag behavior when:
    - questionary is not installed
    - stdout is not a TTY (piped / CI / subprocess)
    - user passes --non-interactive / -y
    - a dimension is already specified via CLI flags (that dimension is
      locked in and its prompt is skipped)

Public API:
    collect_config_interactive(strategy_name, cli_args, fallback_config)
        → (DeepBacktestConfig, list[LeverageMode])

The caller iterates the returned list of modes and runs the pipeline once
per mode. Multi-mode runs write to separate report directories.

See `feedback_deep_backtest_tui.md` memory + task #114 plan for the UX
spec Prince approved on 2026-04-15.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import Any

try:
    import questionary
    from questionary import Choice
    _HAS_QUESTIONARY = True
except ImportError:
    _HAS_QUESTIONARY = False
    questionary = None  # type: ignore
    Choice = None  # type: ignore

from src.backtest.deep_backtest import (
    DeepBacktestConfig,
    LeverageMode,
    _check_window_availability,
)


# ── Dimension catalogs ──────────────────────────────────────────────────
#
# Order in each list is the UI display order. Labels are human-readable;
# values are what deep_backtest expects in the config.

WINDOWS_CATALOG: list[tuple[int, str]] = [
    (30, "1 month"),
    (90, "3 months"),
    (180, "6 months"),
    (365, "1 year"),
    (730, "2 years"),
    (1460, "4 years"),
]

TIMEFRAMES_CATALOG: list[tuple[str, str]] = [
    ("1m", "1 minute"),
    ("5m", "5 minutes"),
    ("15m", "15 minutes"),
    ("30m", "30 minutes"),
    ("1h", "1 hour"),
    ("4h", "4 hours"),
    ("1d", "1 day"),
]

FEES_CATALOG: list[tuple[str, str]] = [
    ("pine_zero_cost", "No fees (Pine-faithful)"),
    ("ic_markets_mt4_xauusd_normal", "IC Markets MT4 (fixed per-lot)"),
    ("ic_markets_ctrader_xauusd_normal", "IC Markets cTrader (volume-based)"),
]

LEVERAGES_CATALOG: list[int] = [1, 5, 10, 25, 50, 100, 200, 400, 500, 1000]

LEVERAGE_MODES_CATALOG: list[tuple[LeverageMode, str]] = [
    (LeverageMode.MARGIN_CAPPED,
     "MARGIN_CAPPED (default — notional fixed, L gates margin)"),
    (LeverageMode.INVARIANT,
     "INVARIANT (P&L identical across leverages — Pine-style)"),
    (LeverageMode.RISK_SCALED,
     "RISK_SCALED (NEW — linearly amplify returns with leverage)"),
    (LeverageMode.KELLY_FRACTIONAL,
     "KELLY_FRACTIONAL (NEW — formula-driven, needs OOS stats)"),
    (LeverageMode.VOL_TARGETED,
     "VOL_TARGETED (vol-scaled position sizing)"),
]


# ── Single source of truth for help text (task #141) ────────────────────
#
# These dicts feed BOTH the Streamlit `help=` kwargs on page 7 AND the
# standalone Glossary page. Edit here, changes propagate everywhere.
# The Glossary page renders these at runtime rather than reading a
# pre-written markdown file so definitions never drift.

LEVERAGE_MODE_HELP: dict[str, str] = {
    "margin_capped": (
        "**Default mode.** Position is sized from `max_risk_per_trade` "
        "normally; leverage only caps the max margin usage. `risk_pct` "
        "stays fixed regardless of L. Use when you want the strategy's "
        "native sizing to dominate and leverage to be a pure safety cap."
    ),
    "invariant": (
        "**Pine-style.** `risk_pct` is clamped to `sl_pct` so "
        "`notional == equity`. P&L is **identical** at every leverage — "
        "leverage is literally ignored. Used for Pine Script parity "
        "testing and as a baseline for leverage-mode comparisons."
    ),
    "vol_targeted": (
        "**Volatility-scaled.** Scales base risk by `target_vol / "
        "realized_vol` with a `[0.5, 2.0]` clamp. Dampens sizing in "
        "high-vol regimes, boosts it in calm regimes. Good for "
        "strategies whose edge decays in chop."
    ),
    "risk_scaled": (
        "**Linear amp.** `risk_pct = base × (L / baseline_leverage)`. "
        "At 2× baseline you get 2× risk → ~2× P&L (linear). Use when "
        "you've validated the strategy's alpha and want to dial up "
        "exposure. Flagged by Phase 2.5 if linearity breaks."
    ),
    "kelly_fractional": (
        "**Edge-scaled.** `risk_pct = min(0.25, kelly_fraction × f*)` "
        "where `f* = (b·p − q)/b` is the Kelly optimal from "
        "(win_rate, payoff_ratio) priors. Half-Kelly (fraction=0.5) is "
        "the industry-standard conservative setting."
    ),
}


TECHNICAL_TERMS: dict[str, str] = {
    "window": (
        "A historical time slice. `1mo = 30 days`, `1y = 365 days`, etc. "
        "Each window becomes one matrix cell per (TF × leverage × fee × "
        "mode) combo. Pick multiple to test regime robustness."
    ),
    "timeframe": (
        "Candle aggregation level — `1m` = 1-minute bars, `1h` = 1-hour "
        "bars. Smaller TFs = more bars = more potential trades but more "
        "noise and higher commission drag."
    ),
    "leverage": (
        "Multiplier on position notional vs equity. `L=10` means $10k "
        "equity can open a $100k position. **Per-mode behavior varies** — "
        "hover over each mode in the selector for the exact formula."
    ),
    "baseline_leverage": (
        "Reference point for RISK_SCALED mode. `risk_pct` scales linearly "
        "from the baseline — at L=baseline you get the strategy's stored "
        "`max_risk_per_trade`; at 2×baseline you get 2× that. Ignored by "
        "all other modes."
    ),
    "walk_forward": (
        "Out-of-sample validation. Splits the data into rolling "
        "`(train, test)` folds and reports OOS metrics per fold plus a "
        "continuous run. The **continuous WF Calmar** is the single most "
        "important gate — a strategy that passes in-sample but fails "
        "continuous WF is a curve-fit."
    ),
    "wf_retune": (
        "Per-fold parameter grid search. Uses the strategy's "
        "`wf_param_grid` attribute to re-optimize on each training fold "
        "before evaluating OOS. Without this, WF uses fixed params "
        "(the 'honest Pine-parity' baseline)."
    ),
    "matrix": (
        "Grid of backtest cells = **window × timeframe × leverage × "
        "fee × mode**. A 2-window × 2-TF × 3-leverage × 2-fee × 5-mode "
        "run = 120 cells. Runtime scales linearly with cell count."
    ),
    "verdict": (
        "Final judgment from the 6-phase pipeline. Values: "
        "**DEPLOYABLE** (ship), **NEEDS_WF** (rerun with walk-forward), "
        "**RESEARCH_ONLY** (keep studying), **FAILED** (broken — gate fail)."
    ),
    "sane_cell": (
        "A cell that passes basic sanity gates: **trades≥30, DD<60%, "
        "Calmar>0.2**. Insane cells with +340% on 4 trades get flagged "
        "as vanity traps and rendered with a ⚠ badge in the UI."
    ),
    "facets": (
        "Per-(window, fee) best-cell cache on each version row. "
        "Maintained at write-time by `record_deep_backtest_result` "
        "under keep-best semantics (sane-first → WF Calmar → return)."
    ),
    "kelly_win_rate": (
        "Probability-of-win prior for the Kelly Criterion formula. "
        "Only used when `leverage_mode=kelly_fractional`. Value in "
        "`[0, 1]`. Estimated from walk-forward OOS wins."
    ),
    "kelly_payoff_ratio": (
        "Average win / average loss ratio. The `b` in Kelly's "
        "`f* = (bp - q)/b`. Estimated from walk-forward OOS trade "
        "P&L distribution. Only used in kelly_fractional mode."
    ),
    "kelly_fraction": (
        "Multiplier on the raw Kelly optimal `f*`. `0.5` = half-Kelly "
        "(industry standard conservative), `1.0` = full Kelly (risky), "
        "`0.25` = quarter-Kelly (very conservative)."
    ),
    "fee_profile": (
        "Broker × platform × instrument × scenario cost model. "
        "E.g., `ic_markets_mt4_xauusd_normal` = MT4 fixed-per-lot "
        "commission on XAUUSD under normal spreads."
    ),
    "broker": (
        "Which broker's cost model to use. IC Markets is currently "
        "the only real broker in the registry; Pine validation is a "
        "zero-cost synthetic for TradingView parity testing."
    ),
    "platform": (
        "Trading platform variant under a broker. For IC Markets, "
        "`mt4` = MetaTrader 4 (fixed-per-lot commission $3.50/side), "
        "`ctrader` = cTrader (volume-based, higher for gold)."
    ),
    "scenario": (
        "Cost regime. `normal` = default spreads/commission, "
        "`news_active` = widened spreads around news, `stress` = 2× "
        "spreads/slip for worst-case testing, `pine_faithful` = zero "
        "cost for TV parity."
    ),
    "version_slug": (
        "DB key for a strategy variant. Format: "
        "`{leverage_mode}_L{int(baseline_leverage)}_{timeframe}` "
        "(e.g., `risk_scaled_L15_1h`). Same triplet → same row, "
        "upsert under keep-best on re-run."
    ),
    "strategy_params": (
        "Override strategy-class `__init__` kwargs. Format: "
        "`key1=value1,key2=value2`. Applied before the strategy is "
        "instantiated. Use to sweep a single param without editing "
        "the class source."
    ),
    "initial_cash": (
        "Starting equity for the backtest. Affects minimum position "
        "size (a $1k account can't open 1-lot gold at L=1) but NOT "
        "percentage returns. Default $10k matches IC Markets' "
        "cTrader demo minimum deposit."
    ),
    "symbol": (
        "Ticker symbol. Deep backtest is currently XAUUSD-only — "
        "other symbols will fall through to a NotImplementedError in "
        "`_load_timeframe`. Extending to FX/crypto is a future task."
    ),
}


# ── TTY / availability guards ───────────────────────────────────────────


def interactive_available(cli_args: Any) -> bool:
    """Returns True if the TUI should run, False if we should fall back."""
    if not _HAS_QUESTIONARY:
        return False
    if not sys.stdout.isatty():
        return False
    if getattr(cli_args, "non_interactive", False):
        return False
    return True


def _parse_csv_list(s: str, cast=str) -> list:
    return [cast(x.strip()) for x in s.split(",") if x.strip()]


# ── Main entry ──────────────────────────────────────────────────────────


def collect_config_interactive(
    strategy_name: str,
    cli_args: Any,
    fallback_config: DeepBacktestConfig,
) -> tuple[DeepBacktestConfig, list[LeverageMode]]:
    """Prompt the user for dimension selections.

    Returns (config, modes_to_run). If the user picks multiple modes, the
    caller runs the pipeline once per mode. If only one mode is picked (or
    a mode was specified via CLI flag), the returned list has one element.

    `fallback_config` is used verbatim if the TUI isn't available or if the
    user hits Ctrl+C during prompts.

    CLI flag precedence: any dimension already specified via flags is locked
    in (that prompt is skipped). --non-interactive / -y skips ALL prompts.
    """
    if not interactive_available(cli_args):
        reason = (
            "questionary not installed" if not _HAS_QUESTIONARY
            else "stdout is not a TTY" if not sys.stdout.isatty()
            else "--non-interactive flag set"
        )
        print(f"[deep_backtest] interactive TUI disabled ({reason}); "
              f"using CLI/default config")
        return fallback_config, [fallback_config.leverage_mode]

    print(f"\n[deep_backtest] interactive config for strategy: {strategy_name}")
    print("    (use space to toggle, arrows to navigate, enter to confirm)\n")

    # 1. Windows
    if getattr(cli_args, "windows", None):
        windows = _parse_csv_list(cli_args.windows, int)
        print(f"  Windows (from CLI): {windows}")
    else:
        choices = []
        for days, label in WINDOWS_CATALOG:
            available, reason = _check_window_availability(days, "1h", "XAUUSD")
            display = f"{label} ({days}d)"
            if not available:
                display += f"  [NOT AVAILABLE — {reason}]"
            choices.append(Choice(
                title=display,
                value=days,
                checked=available and days in (30, 90, 365),  # sensible defaults
                disabled=None if available else "not available",
            ))
        windows = questionary.checkbox(
            "Select time durations (windows):",
            choices=choices,
        ).ask()
        if not windows:
            print("No windows selected; aborting.")
            sys.exit(0)

    # 2. Timeframes
    if getattr(cli_args, "timeframes", None):
        timeframes = _parse_csv_list(cli_args.timeframes, str)
        print(f"  Timeframes (from CLI): {timeframes}")
    else:
        choices = [
            Choice(
                title=f"{tf} — {label}",
                value=tf,
                checked=(tf == "1h"),  # default to 1h only
            )
            for tf, label in TIMEFRAMES_CATALOG
        ]
        timeframes = questionary.checkbox(
            "Select candle timeframes:",
            choices=choices,
        ).ask()
        if not timeframes:
            print("No timeframes selected; aborting.")
            sys.exit(0)

    # 3. Fee profiles
    if getattr(cli_args, "fees", None):
        fees = _parse_csv_list(cli_args.fees, str)
        print(f"  Fees (from CLI): {fees}")
    else:
        choices = [
            Choice(title=label, value=key, checked=True)
            for key, label in FEES_CATALOG
        ]
        fees = questionary.checkbox(
            "Select fee profiles:",
            choices=choices,
        ).ask()
        if not fees:
            print("No fees selected; aborting.")
            sys.exit(0)

    # 4. Leverages
    if getattr(cli_args, "leverages", None):
        leverages = _parse_csv_list(cli_args.leverages, float)
        print(f"  Leverages (from CLI): {leverages}")
    else:
        choices = [
            Choice(
                title=f"{lev}x",
                value=float(lev),
                checked=(lev == 10),  # default to 10x only
            )
            for lev in LEVERAGES_CATALOG
        ]
        leverages = questionary.checkbox(
            "Select leverages:",
            choices=choices,
        ).ask()
        if not leverages:
            print("No leverages selected; aborting.")
            sys.exit(0)

    # 5. Leverage modes (multi-select — runs once per mode)
    if getattr(cli_args, "leverage_mode", None):
        modes = [LeverageMode(cli_args.leverage_mode)]
        print(f"  Leverage mode (from CLI): {modes[0].value}")
    else:
        choices = [
            Choice(
                title=label,
                value=mode,
                checked=(mode == LeverageMode.MARGIN_CAPPED),  # default
            )
            for mode, label in LEVERAGE_MODES_CATALOG
        ]
        modes = questionary.checkbox(
            "Select leverage modes (backtest runs once per selected mode):",
            choices=choices,
        ).ask()
        if not modes:
            print("No leverage modes selected; aborting.")
            sys.exit(0)

    # 6. Walk-forward
    if getattr(cli_args, "no_wf", False):
        wf_enabled = False
        wf_retune = False
    else:
        wf_enabled = questionary.confirm(
            "Run walk-forward validation?",
            default=True,
        ).ask()
        wf_retune = False
        if wf_enabled:
            wf_retune = questionary.confirm(
                "Retune strategy params per fold? "
                "(slower but more honest — requires --wf-param-grid for gold strategies)",
                default=False,
            ).ask()

    # 7. Confirmation
    n_cells_per_mode = len(windows) * len(timeframes) * len(fees) * len(leverages)
    n_total = n_cells_per_mode * len(modes)
    est_seconds = n_total * 3  # rough ~3s per cell average
    if wf_enabled:
        est_seconds += 30 * len(modes)  # WF overhead
    est_min, est_sec = divmod(int(est_seconds), 60)
    wf_label = " + walk-forward" if wf_enabled else ""
    proceed = questionary.confirm(
        f"Run {n_total} cells ({len(modes)} mode(s) × {n_cells_per_mode} cells/mode)"
        f"{wf_label} — estimated {est_min}m {est_sec}s?",
        default=True,
    ).ask()
    if not proceed:
        print("Aborted.")
        sys.exit(0)

    # Build the config
    config = replace(
        fallback_config,
        window_days=sorted(windows),
        timeframes=list(timeframes),
        fee_profiles=list(fees),
        leverages=sorted(float(lev) for lev in leverages),
        leverage_mode=modes[0],  # primary; caller iterates over the full list
        wf_enabled=wf_enabled,
        wf_retune=wf_retune,
    )
    return config, modes
