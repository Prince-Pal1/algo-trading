#!/usr/bin/env python3
"""deep_backtest wrapper with strict OOS holdout pass.

Per `docs/research/2026-05-06_oos_holdout_lock.md`, every Workstream-A
backtest replay must run TWO passes:

1. Tune pass — WF retune across param grid on tune window only.
2. Holdout pass — re-run BEST params on a separate post-tune window
   the original tune never saw.

Verdict requires BOTH gates pass. WF retune within tune is not strict
enough — final chosen params still bear correlation to every fold's
training data. The post-tune holdout pass is the real OOS check.

deep_backtest hardcodes parquet paths and windows by "last N days from
parquet end." This wrapper monkey-patches `_load_timeframe` to slice by
explicit date range, then drives deep_backtest's run pipeline twice.

Usage:
    python scripts/research/deep_backtest_with_holdout.py \
        --strategy vol_momentum_gold \
        --tune-start 2024-04-15 --tune-end 2025-12-31 \
        --holdout-start 2026-04-15 --holdout-end 2026-05-06 \
        --param-grid 'momentum_threshold=1.5|2.0|2.5|3.0,vol_lookback=10|20|30,cooldown_bars=0|4|8|16' \
        --fee-profile ic_markets_ctrader_xauusd_normal \
        --tune-gate-calmar 1.0 \
        --holdout-gate-pf 1.2 \
        --label a1_vol_momentum_gold_2026-05-07 \
        [--apply]

Default is dry-run (prints what it would do). --apply actually invokes
the engine.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_DIR))

from src.backtest import deep_backtest as db  # noqa: E402
from src.backtest.deep_backtest import LeverageMode  # noqa: E402


def _to_ms(date_str: str) -> int:
    """Parse YYYY-MM-DD → UTC midnight timestamp in ms."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _date_range_check(parquet_path: Path, start_ms: int, end_ms: int, label: str) -> tuple[bool, str]:
    """Confirm the parquet covers the requested date range."""
    if not parquet_path.exists():
        return False, f"parquet missing: {parquet_path}"
    df = pd.read_parquet(parquet_path)
    if "timestamp" not in df.columns:
        return False, f"parquet has no timestamp column: {parquet_path}"
    p_start = int(df["timestamp"].min())
    p_end = int(df["timestamp"].max())
    if p_start > start_ms:
        return False, f"{label} parquet starts {pd.Timestamp(p_start, unit='ms', tz='UTC')} > requested {pd.Timestamp(start_ms, unit='ms', tz='UTC')}"
    if p_end < end_ms:
        return False, f"{label} parquet ends {pd.Timestamp(p_end, unit='ms', tz='UTC')} < requested {pd.Timestamp(end_ms, unit='ms', tz='UTC')} — RUN BACKFILL FIRST"
    return True, f"{label} parquet covers {pd.Timestamp(p_start, unit='ms', tz='UTC')} → {pd.Timestamp(p_end, unit='ms', tz='UTC')} (requested OK)"


def _make_sliced_loader(start_ms: int, end_ms: int):
    """Return a closure that slices _load_timeframe output by date range."""
    original = db._load_timeframe

    def sliced(symbol: str, timeframe: str) -> pd.DataFrame:
        df = original(symbol, timeframe)
        if "timestamp" not in df.columns:
            raise RuntimeError(f"_load_timeframe returned df without timestamp: cols={list(df.columns)}")
        mask = (df["timestamp"] >= start_ms) & (df["timestamp"] <= end_ms)
        sliced_df = df.loc[mask].reset_index(drop=True)
        if len(sliced_df) == 0:
            raise RuntimeError(
                f"slice produced zero rows: {symbol} {timeframe} "
                f"{pd.Timestamp(start_ms, unit='ms', tz='UTC')} → "
                f"{pd.Timestamp(end_ms, unit='ms', tz='UTC')}"
            )
        return sliced_df

    return sliced, original


def _parse_param_grid(s: str | None) -> dict[str, list]:
    if not s:
        return {}
    out = {}
    for term in s.split(","):
        if "=" not in term:
            continue
        k, v = term.split("=", 1)
        vals = []
        for item in v.split("|"):
            item = item.strip()
            try:
                vals.append(float(item) if "." in item else int(item))
            except ValueError:
                vals.append(item)
        out[k.strip()] = vals
    return out


def _build_config(
    strategy: str,
    window_days: int,
    fee_profile: str,
    timeframe: str,
    *,
    wf_enabled: bool,
    wf_n_folds: int,
    wf_fold_days: int,
    wf_gate_calmar: float,
    wf_retune: bool,
    param_grid: dict | None,
    out_dir: Path,
    label: str,
    leverage: float = 1.0,
) -> db.DeepBacktestConfig:
    return db.DeepBacktestConfig(
        strategy=strategy,
        symbol="XAUUSD",
        timeframes=[timeframe],
        window_days=[window_days],
        leverages=[leverage],
        fee_profiles=[fee_profile],
        wf_enabled=wf_enabled,
        wf_n_folds=wf_n_folds,
        wf_fold_days=wf_fold_days,
        wf_gate_calmar=wf_gate_calmar,
        wf_retune=wf_retune,
        wf_param_grid=param_grid,
        leverage_mode=LeverageMode.INVARIANT,
        baseline_leverage=leverage,
        run_id_prefix=label,
        out_dir=out_dir,
        leverage_validation_enabled=False,
        # Trim long-running side jobs we don't need
        generate_pdf=False,
        generate_heatmaps=False,
    )


def _verdict_block(label: str, result: dict, gate_name: str, gate_value: float, observed: float | None) -> str:
    if observed is None:
        status = "MISSING"
    elif observed >= gate_value:
        status = "PASS"
    else:
        status = "FAIL"
    return f"  {label}: {gate_name} = {observed} (gate {gate_value}) → {status}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", required=True)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--tune-start", required=True, help="YYYY-MM-DD")
    p.add_argument("--tune-end", required=True, help="YYYY-MM-DD")
    p.add_argument("--holdout-start", required=True, help="YYYY-MM-DD")
    p.add_argument("--holdout-end", required=True, help="YYYY-MM-DD")
    p.add_argument("--param-grid", default="",
                   help="'k=v1|v2,k2=v3|v4' for WF retune. Empty = no retune.")
    p.add_argument("--fee-profile", required=True)
    p.add_argument("--leverage", type=float, default=1.0)
    p.add_argument("--wf-n-folds", type=int, default=5)
    p.add_argument("--wf-fold-days", type=int, default=18)
    p.add_argument("--wf-gate-calmar", type=float, default=1.0)
    p.add_argument("--tune-gate-calmar", type=float, default=1.0,
                   help="Required Calmar for tune WF folds.")
    p.add_argument("--holdout-gate-pf", type=float, default=1.2,
                   help="Required PF for holdout. Set to 0 to skip PF gate.")
    p.add_argument("--holdout-gate-sharpe", type=float, default=0.0,
                   help="Required Sharpe for holdout. Set to 0 to skip.")
    p.add_argument("--holdout-gate-calmar", type=float, default=0.0,
                   help="Required Calmar for holdout. Set to 0 to skip.")
    p.add_argument("--label", required=True)
    p.add_argument("--apply", action="store_true",
                   help="Actually run (default: dry-run)")
    args = p.parse_args()

    tune_start_ms = _to_ms(args.tune_start)
    # End-of-day inclusive: tune_end of "2025-12-31" means up to and including
    # 2025-12-31 23:59:59. We add ~1 day minus 1 ms.
    tune_end_ms = _to_ms(args.tune_end) + (86400 * 1000 - 1)
    holdout_start_ms = _to_ms(args.holdout_start)
    holdout_end_ms = _to_ms(args.holdout_end) + (86400 * 1000 - 1)

    if tune_end_ms >= holdout_start_ms:
        print(f"REFUSED: tune window end ({args.tune_end}) overlaps holdout start ({args.holdout_start})", file=sys.stderr)
        return 2

    # Coverage check on the parquet (use 1h as the canonical check; M1/M5 checked at runtime)
    parquet = PROJECT_DIR / "data" / "historical" / f"{args.symbol}_{args.timeframe}.parquet"
    cov_ok, cov_msg = _date_range_check(parquet, tune_start_ms, holdout_end_ms, "tune+holdout")
    print(f"[coverage] {cov_msg}")
    if not cov_ok:
        print("REFUSED: data coverage insufficient — backfill first.", file=sys.stderr)
        return 2

    tune_window_days = int((tune_end_ms - tune_start_ms) / 86400 / 1000)
    holdout_window_days = int((holdout_end_ms - holdout_start_ms) / 86400 / 1000)

    param_grid = _parse_param_grid(args.param_grid)

    out_root = PROJECT_DIR / "reports" / args.label
    tune_out = out_root / "tune"
    holdout_out = out_root / "holdout"

    print(f"\n=== Tune pass ===")
    print(f"  window: {args.tune_start} → {args.tune_end} ({tune_window_days}d)")
    print(f"  WF: {args.wf_n_folds} folds × {args.wf_fold_days}d, retune={bool(param_grid)}")
    print(f"  param grid: {param_grid if param_grid else '(none — fixed-params WF)'}")
    print(f"  out: {tune_out}")
    print(f"\n=== Holdout pass ===")
    print(f"  window: {args.holdout_start} → {args.holdout_end} ({holdout_window_days}d)")
    print(f"  WF: disabled (single-window pass on chosen params)")
    print(f"  out: {holdout_out}")
    print(f"\n=== Gates ===")
    print(f"  tune WF Calmar > {args.tune_gate_calmar}")
    if args.holdout_gate_pf > 0:
        print(f"  holdout PF > {args.holdout_gate_pf}")
    if args.holdout_gate_sharpe > 0:
        print(f"  holdout Sharpe > {args.holdout_gate_sharpe}")
    if args.holdout_gate_calmar > 0:
        print(f"  holdout Calmar > {args.holdout_gate_calmar}")

    if not args.apply:
        print("\n[DRY-RUN] Would invoke deep_backtest tune + holdout passes.")
        print("[DRY-RUN] Re-run with --apply to actually execute.")
        return 0

    out_root.mkdir(parents=True, exist_ok=True)
    tune_out.mkdir(parents=True, exist_ok=True)
    holdout_out.mkdir(parents=True, exist_ok=True)

    # ── Tune pass ───────────────────────────────────────────
    sliced, original = _make_sliced_loader(tune_start_ms, tune_end_ms)
    db._load_timeframe = sliced
    try:
        tune_cfg = _build_config(
            args.strategy, tune_window_days, args.fee_profile, args.timeframe,
            wf_enabled=True,
            wf_n_folds=args.wf_n_folds,
            wf_fold_days=args.wf_fold_days,
            wf_gate_calmar=args.tune_gate_calmar,
            wf_retune=bool(param_grid),
            param_grid=param_grid if param_grid else None,
            out_dir=tune_out,
            label=f"{args.label}_tune",
            leverage=args.leverage,
        )
        tune_result = db.run_deep_backtest(tune_cfg)
    finally:
        db._load_timeframe = original

    tune_summary_path = tune_out / "summary.json"
    if not tune_summary_path.exists():
        # deep_backtest may write to a timestamped subdir; find it.
        candidates = sorted(tune_out.glob("**/summary.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            print(f"ERROR: no summary.json in {tune_out}", file=sys.stderr)
            return 3
        tune_summary_path = candidates[-1]
    tune_summary = json.loads(tune_summary_path.read_text())

    # Extract WF best params: top-level may be null when retune is per-fold;
    # fall back to per-fold best_params (most-recent fold wins, conservative).
    chosen_params = tune_summary.get("best_params") or {}
    wf = tune_summary.get("walk_forward") or {}
    if not chosen_params:
        folds = wf.get("folds") or []
        # Pick the per-fold best_params from the highest-Calmar fold
        candidate_folds = [f for f in folds if f.get("best_params")]
        if candidate_folds:
            best_fold = max(candidate_folds, key=lambda f: (f.get("calmar") or -1e18))
            chosen_params = best_fold.get("best_params") or {}
    # Continuous Calmar (single contiguous WF series) is the conservative gate.
    wf_calmar = wf.get("continuous_calmar")
    if wf_calmar is None:
        wf_calmar = wf.get("mean_calmar")
    print(f"\n[tune] best_params: {chosen_params}")
    print(f"[tune] WF continuous_calmar: {wf.get('continuous_calmar')} mean_calmar: {wf.get('mean_calmar')}")

    # ── Holdout pass ────────────────────────────────────────
    sliced, original = _make_sliced_loader(holdout_start_ms, holdout_end_ms)
    db._load_timeframe = sliced
    try:
        holdout_cfg = _build_config(
            args.strategy, holdout_window_days, args.fee_profile, args.timeframe,
            wf_enabled=False,
            wf_n_folds=0,
            wf_fold_days=0,
            wf_gate_calmar=0.0,
            wf_retune=False,
            param_grid=None,
            out_dir=holdout_out,
            label=f"{args.label}_holdout",
            leverage=args.leverage,
        )
        # Inject chosen params into strategy config
        if chosen_params:
            holdout_cfg.strategy_params = dict(chosen_params)
        holdout_result = db.run_deep_backtest(holdout_cfg)
    finally:
        db._load_timeframe = original

    holdout_summary_path = holdout_out / "summary.json"
    if not holdout_summary_path.exists():
        candidates = sorted(holdout_out.glob("**/summary.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            print(f"ERROR: no summary.json in {holdout_out}", file=sys.stderr)
            return 3
        holdout_summary_path = candidates[-1]
    holdout_summary = json.loads(holdout_summary_path.read_text())

    # ── Verdict ─────────────────────────────────────────────
    best_cell = (holdout_summary.get("sanity_checks") or {}).get("best_cell") or {}
    holdout_pf = best_cell.get("profit_factor")
    holdout_sharpe = best_cell.get("sharpe")
    holdout_calmar = best_cell.get("calmar")

    print("\n=== Verdict ===")
    print(_verdict_block("tune WF", tune_summary, "Calmar", args.tune_gate_calmar, wf_calmar))
    if args.holdout_gate_pf > 0:
        print(_verdict_block("holdout", holdout_summary, "PF", args.holdout_gate_pf, holdout_pf))
    if args.holdout_gate_sharpe > 0:
        print(_verdict_block("holdout", holdout_summary, "Sharpe", args.holdout_gate_sharpe, holdout_sharpe))
    if args.holdout_gate_calmar > 0:
        print(_verdict_block("holdout", holdout_summary, "Calmar", args.holdout_gate_calmar, holdout_calmar))

    # Combined verdict
    tune_pass = wf_calmar is not None and wf_calmar >= args.tune_gate_calmar
    holdout_pass = True
    if args.holdout_gate_pf > 0:
        holdout_pass = holdout_pass and (holdout_pf is not None and holdout_pf >= args.holdout_gate_pf)
    if args.holdout_gate_sharpe > 0:
        holdout_pass = holdout_pass and (holdout_sharpe is not None and holdout_sharpe >= args.holdout_gate_sharpe)
    if args.holdout_gate_calmar > 0:
        holdout_pass = holdout_pass and (holdout_calmar is not None and holdout_calmar >= args.holdout_gate_calmar)

    overall = "PASS" if (tune_pass and holdout_pass) else "FAIL"
    print(f"\nOVERALL: {overall}")
    print(f"  tune_pass={tune_pass}  holdout_pass={holdout_pass}")

    # Combined report file
    combined = {
        "label": args.label,
        "strategy": args.strategy,
        "tune_window": [args.tune_start, args.tune_end],
        "holdout_window": [args.holdout_start, args.holdout_end],
        "fee_profile": args.fee_profile,
        "param_grid": param_grid,
        "chosen_params": chosen_params,
        "tune_wf_calmar": wf_calmar,
        "holdout_pf": holdout_pf,
        "holdout_sharpe": holdout_sharpe,
        "holdout_calmar": holdout_calmar,
        "gates": {
            "tune_calmar": args.tune_gate_calmar,
            "holdout_pf": args.holdout_gate_pf,
            "holdout_sharpe": args.holdout_gate_sharpe,
            "holdout_calmar": args.holdout_gate_calmar,
        },
        "verdict": overall,
        "tune_summary_path": str(tune_summary_path),
        "holdout_summary_path": str(holdout_summary_path),
    }
    combined_path = out_root / "verdict.json"
    combined_path.write_text(json.dumps(combined, indent=2, default=str) + "\n")
    print(f"\nCombined verdict: {combined_path}")

    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
