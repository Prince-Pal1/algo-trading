#!/usr/bin/env python3
"""Train meta-label classifiers for one or all strategies.

Reads audit rows from data/trades.db, trains LightGBM (or LR fallback),
serializes the fitted model to data/models/meta_label/, and prints a
metrics report.

Usage:
    python3 scripts/train_meta_classifier.py --all
    python3 scripts/train_meta_classifier.py --strategy vol_momentum
    python3 scripts/train_meta_classifier.py --all --run-prefix harvest_
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.m3s.signal_filter.train import fit_meta_classifier
from src.strategies.router import STRATEGY_REGISTRY
from src.utils.config import get_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=None, help="Train one strategy")
    parser.add_argument("--all", action="store_true", help="Train all enabled strategies")
    parser.add_argument("--run-prefix", default=None,
                        help="Only use audit rows with run_id starting with this prefix")
    parser.add_argument("--db-path", default="data/trades.db")
    args = parser.parse_args()

    if args.all:
        cfg = get_config()
        strategies = [
            name for name, strat in cfg.strategies.items()
            if strat.get("enabled", False) and name in STRATEGY_REGISTRY
        ]
    elif args.strategy:
        strategies = [args.strategy]
    else:
        print("ERROR: pass --all or --strategy NAME", file=sys.stderr)
        return 1

    print(f"Training meta classifiers for: {strategies}")
    if args.run_prefix:
        print(f"Run ID prefix filter: {args.run_prefix}")
    print()

    results = []
    for name in strategies:
        print(f"─── {name} ───")
        try:
            result = fit_meta_classifier(
                name, db_path=args.db_path, run_id_prefix=args.run_prefix,
            )
        except Exception as e:
            print(f"  ❌ FAILED: {e}")
            continue

        print(f"  model_type:        {result.model_type}")
        print(f"  n_samples:         {result.n_samples}")
        print(f"  n_features:        {result.n_features}")
        print(f"  train/test split:  {result.n_train}/{result.n_test}")
        print(f"  mean_y (base rate): {result.mean_y:.3f}")
        print(f"  AUC:               {result.auc:.4f}")
        print(f"  Brier score:       {result.brier:.4f}")
        print(f"  calibration slope: {result.calibration_slope:.3f}")
        if result.model_path:
            print(f"  model path:        {result.model_path}")
        if result.notes:
            print(f"  notes:             {result.notes}")
        if result.feature_importance:
            top = sorted(result.feature_importance.items(), key=lambda x: -x[1])[:5]
            print(f"  top 5 features:")
            for key, imp in top:
                print(f"    {key}: {imp:.4f}")
        print()
        results.append(result)

    # Summary
    print("═══ TRAINING SUMMARY ═══")
    for r in results:
        gate_mark = "✅" if r.auc >= 0.55 else ("⚠" if r.auc >= 0.5 else "❌")
        print(f"  {gate_mark} {r.strategy}: AUC={r.auc:.3f}, "
              f"Brier={r.brier:.3f}, n={r.n_samples}, type={r.model_type}")

    any_shipped = any(r.model_path for r in results)
    return 0 if any_shipped else 2


if __name__ == "__main__":
    sys.exit(main())
