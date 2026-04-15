#!/usr/bin/env python3
"""deep_backtest — CLI for the generic deep backtest pipeline.

Runs the full high-accuracy multi-phase debugging process (matrix + sanity
checks + auto-triggered leverage validation + walk-forward OOS + HTML/PDF
report) that was built for SWIFT (tasks #109/#110/#111). Invoke as:

    PYTHONPATH=. python3 scripts/deep_backtest.py <strategy>

Examples:

    # Reproduce the SWIFT full process:
    PYTHONPATH=. python3 scripts/deep_backtest.py swift_alma

    # Quick smoke test (fewer cells, faster):
    PYTHONPATH=. python3 scripts/deep_backtest.py swift_alma --quick

    # Run against donchian_gold with walk-forward retuning:
    PYTHONPATH=. python3 scripts/deep_backtest.py donchian_gold --wf-retune

    # Custom grid:
    PYTHONPATH=. python3 scripts/deep_backtest.py swift_alma \\
        --timeframes 15m,1h --windows 90,365 --leverages 1,100 \\
        --fees ic_markets_mt4_xauusd_normal

    # Strategy not in registry — use dotted path:
    PYTHONPATH=. python3 scripts/deep_backtest.py \\
        src.strategies.custom:MyStrategy --strategy-params 'length=20'

Output is written to `reports/deep_backtest_<strategy>_<timestamp>/`:
    ├── index.html             # primary report
    ├── report.pdf             # landscape A4 PDF
    ├── matrix.csv             # all cells
    ├── summary.json           # structured result for downstream tools
    └── heatmaps/              # PNG heatmaps per (window, fee)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.backtest.deep_backtest import (  # noqa: E402
    DeepBacktestConfig,
    run_deep_backtest,
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
        # Type guess
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


def main() -> int:
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
    p.add_argument("--timeframes", default="1m,5m,15m,1h",
                   help="Comma-separated timeframes")
    p.add_argument("--windows", default="30,90,180,365",
                   help="Comma-separated window sizes in days")
    p.add_argument("--leverages", default="1,50,100,500,1000",
                   help="Comma-separated leverages")
    p.add_argument("--fees",
                   default="pine_zero_cost,ic_markets_mt4_xauusd_normal,ic_markets_ctrader_xauusd_normal",
                   help="Comma-separated fee profile names")
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
                   help="Full SWIFT preset: 4 windows × 4 TFs × 5 leverages × 3 fees + 7 WF folds (default)")

    args = p.parse_args()

    # Parse --wf-param-grid special syntax: "k=v1|v2|v3,k2=a|b"
    wf_param_grid = None
    if args.wf_param_grid:
        wf_param_grid = {}
        for pair in args.wf_param_grid.split(","):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            vals = []
            for raw in v.split("|"):
                raw = raw.strip()
                try:
                    if "." in raw:
                        vals.append(float(raw))
                    else:
                        vals.append(int(raw))
                except ValueError:
                    vals.append(raw)
            wf_param_grid[k.strip()] = vals

    # Quick preset overrides
    if args.quick:
        timeframes = ["5m", "15m"]
        windows = [30, 90]
        leverages = [1.0, 100.0]
        fees = ["ic_markets_mt4_xauusd_normal", "ic_markets_ctrader_xauusd_normal"]
        wf_n_folds = 3
    else:
        timeframes = _parse_list(args.timeframes)
        windows = _parse_list(args.windows, int)
        leverages = _parse_list(args.leverages, float)
        fees = _parse_list(args.fees)
        wf_n_folds = args.wf_n_folds

    config = DeepBacktestConfig(
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
        wf_param_grid=wf_param_grid,
        wf_train_days=args.wf_train_days,
        wf_timeframe=args.wf_tf,
        wf_fee_profile=args.wf_fee,
        wf_leverage=args.wf_leverage,
        leverage_validation_enabled=not args.no_leverage_validation,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        generate_html=not args.no_html,
        generate_pdf=not args.no_pdf,
        generate_heatmaps=not args.no_heatmaps,
        progress=not args.quiet,
    )

    result = run_deep_backtest(config)

    # Exit code reflects verdict — useful for CI integration
    verdict_exit = {
        "DEPLOYABLE": 0,
        "RESEARCH_ONLY": 1,
        "NEEDS_WF": 1,
        "FAILED": 2,
    }
    return verdict_exit.get(result.verdict, 0)


if __name__ == "__main__":
    sys.exit(main())
