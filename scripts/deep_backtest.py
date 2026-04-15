#!/usr/bin/env python3
"""deep_backtest — CLI for the generic deep backtest pipeline.

Runs the full high-accuracy multi-phase debugging process (matrix + sanity
checks + auto-triggered leverage validation + walk-forward OOS + HTML/PDF
report) that was built for SWIFT (tasks #109/#110/#111).

USAGE — interactive TUI (preferred, default when run in a terminal):

    PYTHONPATH=. python3 scripts/deep_backtest.py <strategy>

    The CLI walks you through multi-select prompts for every dimension
    (windows, timeframes, fees, leverages, leverage modes, walk-forward)
    using questionary tick-boxes. Default behavior when invoked without
    the dimension flags.

USAGE — non-interactive (scripting / CI):

    # All dimensions as CLI flags:
    PYTHONPATH=. python3 scripts/deep_backtest.py donchian_gold \\
        --non-interactive \\
        --timeframes 1h --windows 30,90,365 --leverages 10,20 \\
        --fees ic_markets_mt4_xauusd_normal \\
        --leverage-mode risk_scaled --baseline-leverage 10

    # Quick preset (smoke test):
    PYTHONPATH=. python3 scripts/deep_backtest.py swift_alma --quick

Leverage modes (task #114):
    margin_capped    — default, current behavior (L gates margin only)
    invariant        — Pine-style P&L-invariant runs
    vol_targeted     — strategy's own vol-scaled sizing
    risk_scaled      — risk_pct = base × (L / baseline_leverage)
    kelly_fractional — risk_pct = kelly_fraction × f* (requires priors)

Output: `reports/deep_backtest_<strategy>_<mode>_<timestamp>/` with
    index.html, report.pdf, matrix.csv, summary.json, heatmaps/
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.backtest.deep_backtest import (  # noqa: E402
    DeepBacktestConfig,
    LeverageMode,
    run_deep_backtest,
)
from src.backtest.deep_backtest_interactive import (  # noqa: E402
    collect_config_interactive,
)


def _parse_list(s: str, cast=str) -> list:
    return [cast(x.strip()) for x in s.split(",") if x.strip()]


def _parse_kv(s: str) -> dict:
    """Parse 'k1=v1,k2=v2' into a dict. Values are auto-cast (int → float → str)."""
    out = {}
    for pair in s.split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        try:
            if "." in v:
                out[k] = float(v)
            else:
                out[k] = int(v)
        except ValueError:
            if v.lower() in ("true", "false"):
                out[k] = v.lower() == "true"
            else:
                out[k] = v
    return out


def _parse_param_grid(s: str) -> dict | None:
    """Parse 'param=v1|v2|v3,param2=a|b' into a dict of lists for --wf-retune."""
    if not s:
        return None
    grid = {}
    for pair in s.split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        vals = []
        for raw in v.split("|"):
            raw = raw.strip()
            try:
                vals.append(float(raw) if "." in raw else int(raw))
            except ValueError:
                vals.append(raw)
        grid[k.strip()] = vals
    return grid


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generic deep backtest pipeline (SWIFT-style multi-phase validation)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("strategy",
                   help="Strategy registry key (e.g. 'swift_alma') or dotted path "
                        "'module.path:ClassName'")
    p.add_argument("--strategy-params", default="",
                   help="Extra strategy kwargs as 'k=v,k=v' (override defaults)")
    p.add_argument("--symbol", default="XAUUSD")

    # Matrix dimensions — default None means "prompt in TUI"
    p.add_argument("--timeframes", default=None,
                   help="Comma-separated timeframes (1m/5m/15m/30m/1h/4h/1d). "
                        "If omitted in interactive mode, prompts via TUI.")
    p.add_argument("--windows", default=None,
                   help="Comma-separated window sizes in days. "
                        "If omitted in interactive mode, prompts via TUI.")
    p.add_argument("--leverages", default=None,
                   help="Comma-separated leverages. Same TUI behavior.")
    p.add_argument("--fees", default=None,
                   help="Comma-separated fee profile names. Same TUI behavior.")

    p.add_argument("--initial-cash", type=float, default=10_000.0)
    p.add_argument("--warmup-bars", type=int, default=250)

    # Walk-forward
    p.add_argument("--no-wf", action="store_true", help="Skip walk-forward phase")
    p.add_argument("--wf-fold-days", type=int, default=90)
    p.add_argument("--wf-n-folds", type=int, default=7)
    p.add_argument("--wf-gate-calmar", type=float, default=0.5)
    p.add_argument("--wf-retune", action="store_true",
                   help="Grid-search on train window per fold (requires --wf-param-grid)")
    p.add_argument("--wf-param-grid", default="",
                   help="Grid as 'param=v1|v2|v3,param2=v1|v2' for --wf-retune")
    p.add_argument("--wf-train-days", type=int, default=90)
    p.add_argument("--wf-tf", default=None, help="Override WF timeframe (default: matrix best cell)")
    p.add_argument("--wf-fee", default=None, help="Override WF fee profile (default: matrix best cell)")
    p.add_argument("--wf-leverage", type=float, default=10.0)

    # Leverage validation
    p.add_argument("--no-leverage-validation", action="store_true",
                   help="Skip auto leverage deep-dive even if invariance detected")

    # Leverage mode (task #114)
    p.add_argument("--leverage-mode", default=None,
                   choices=["invariant", "margin_capped", "vol_targeted",
                            "risk_scaled", "kelly_fractional"],
                   help="Leverage mode (skips TUI prompt if set)")
    p.add_argument("--baseline-leverage", type=float, default=10.0,
                   help="RISK_SCALED only: reference leverage for scaling")
    p.add_argument("--kelly-fraction", type=float, default=0.5,
                   help="KELLY_FRACTIONAL: 0.5=half-Kelly (default), 0.25=quarter")
    p.add_argument("--kelly-win-rate", type=float, default=None,
                   help="KELLY_FRACTIONAL: historical OOS win rate [0, 1]")
    p.add_argument("--kelly-payoff-ratio", type=float, default=None,
                   help="KELLY_FRACTIONAL: avg_win / avg_loss")
    p.add_argument("--risk-pct-param", default="max_risk_per_trade",
                   help="Strategy kwarg that holds risk_pct (default: max_risk_per_trade)")

    # Strategy storage (task #120)
    p.add_argument("--version-slug", default=None,
                   help="Explicit slug for the strategy_versions row "
                        "(default: auto-generated as {mode}_L{baseline}_{tf})")

    # Interactive TUI
    p.add_argument("--non-interactive", "-y", action="store_true",
                   help="Skip all interactive prompts, use defaults for unspecified dimensions")

    # Output
    p.add_argument("--out-dir", default=None,
                   help="Override report output directory")
    p.add_argument("--no-html", action="store_true")
    p.add_argument("--no-pdf", action="store_true")
    p.add_argument("--no-heatmaps", action="store_true")
    p.add_argument("--quiet", action="store_true", help="Suppress progress output")

    # Presets
    p.add_argument("--quick", action="store_true",
                   help="Smoke-test preset: 2 windows × 2 TFs × 2 leverages × 2 fees + 3 WF folds")
    p.add_argument("--full", action="store_true",
                   help="Full SWIFT preset: 4 windows × 4 TFs × 5 leverages × 3 fees + 7 WF folds")
    return p


def _build_fallback_config(args: argparse.Namespace) -> DeepBacktestConfig:
    """Build a DeepBacktestConfig from CLI args only. The interactive TUI
    may override some fields via its own prompts; this is the 'what to use
    when the user skips the TUI' fallback."""
    # Quick preset short-circuits
    if args.quick:
        timeframes = ["5m", "15m"]
        windows = [30, 90]
        leverages = [1.0, 100.0]
        fees = ["ic_markets_mt4_xauusd_normal", "ic_markets_ctrader_xauusd_normal"]
        wf_n_folds = 3
    else:
        timeframes = _parse_list(args.timeframes) if args.timeframes else ["1h"]
        windows = _parse_list(args.windows, int) if args.windows else [30, 90, 180, 365]
        leverages = _parse_list(args.leverages, float) if args.leverages else [10.0]
        fees = (_parse_list(args.fees) if args.fees
                else ["ic_markets_mt4_xauusd_normal"])
        wf_n_folds = args.wf_n_folds

    leverage_mode = (LeverageMode(args.leverage_mode) if args.leverage_mode
                     else LeverageMode.MARGIN_CAPPED)

    return DeepBacktestConfig(
        strategy=args.strategy,
        strategy_params=_parse_kv(args.strategy_params) if args.strategy_params else {},
        symbol=args.symbol,
        timeframes=timeframes,
        window_days=windows,
        leverages=leverages,
        fee_profiles=fees,
        initial_cash=args.initial_cash,
        warmup_bars=args.warmup_bars,
        wf_enabled=not args.no_wf,
        wf_fold_days=args.wf_fold_days,
        wf_n_folds=wf_n_folds,
        wf_gate_calmar=args.wf_gate_calmar,
        wf_retune=args.wf_retune,
        wf_param_grid=_parse_param_grid(args.wf_param_grid),
        wf_train_days=args.wf_train_days,
        wf_timeframe=args.wf_tf,
        wf_fee_profile=args.wf_fee,
        wf_leverage=args.wf_leverage,
        leverage_validation_enabled=not args.no_leverage_validation,
        leverage_mode=leverage_mode,
        baseline_leverage=args.baseline_leverage,
        kelly_fraction=args.kelly_fraction,
        kelly_win_rate=args.kelly_win_rate,
        kelly_payoff_ratio=args.kelly_payoff_ratio,
        risk_pct_param_name=args.risk_pct_param,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        generate_html=not args.no_html,
        generate_pdf=not args.no_pdf,
        generate_heatmaps=not args.no_heatmaps,
        progress=not args.quiet,
        version_slug=getattr(args, "version_slug", None),
    )


def main() -> int:
    p = _build_argparser()
    args = p.parse_args()

    fallback = _build_fallback_config(args)

    # Decide: interactive TUI or non-interactive?
    # The TUI itself checks isatty / questionary availability / --non-interactive
    # flag and returns the fallback verbatim when skipping prompts.
    config, modes = collect_config_interactive(
        strategy_name=args.strategy,
        cli_args=args,
        fallback_config=fallback,
    )

    # Run the pipeline once per selected leverage mode. If only one mode is
    # picked (or specified via --leverage-mode), this loop runs once.
    results = []
    exit_codes = []
    verdict_exit = {
        "DEPLOYABLE": 0,
        "RESEARCH_ONLY": 1,
        "NEEDS_WF": 1,
        "FAILED": 2,
    }

    for i, mode in enumerate(modes):
        per_mode_config = replace(config, leverage_mode=mode)

        # Separate output dir per mode to avoid overwriting when running
        # multiple modes in one invocation
        if len(modes) > 1:
            if per_mode_config.out_dir is not None:
                per_mode_config = replace(
                    per_mode_config,
                    out_dir=per_mode_config.out_dir.with_name(
                        f"{per_mode_config.out_dir.name}_{mode.value}"
                    ),
                )
            # else: run_deep_backtest generates a timestamped path that
            # already embeds the strategy name; we'll inject mode after it
            # auto-generates, which we can't easily do — accept the collision
            # risk here since multi-mode with default out_dir is uncommon

        if len(modes) > 1:
            print(f"\n{'#' * 80}\n# Mode {i + 1}/{len(modes)}: {mode.value}\n{'#' * 80}\n")

        result = run_deep_backtest(per_mode_config)
        results.append((mode, result))
        exit_codes.append(verdict_exit.get(result.verdict, 0))

    # Multi-mode summary
    if len(modes) > 1:
        print("\n" + "=" * 80)
        print("MULTI-MODE SUMMARY")
        print("=" * 80)
        for mode, result in results:
            print(f"  {mode.value:18s} → {result.verdict:15s} "
                  f"elapsed {result.elapsed_seconds:5.1f}s  →  {result.report_dir}")
        print()

    # Exit code: worst verdict across all modes
    return max(exit_codes) if exit_codes else 0


if __name__ == "__main__":
    sys.exit(main())
