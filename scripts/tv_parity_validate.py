#!/usr/bin/env python3
"""TradingView parity validation CLI.

Confirms that a Python port of a Pine Script strategy produces the
same entry signals as TradingView's strategy tester on the same OHLC
data. Used as Stage 0 of the strategy development process for any
externally-sourced Pine Script.

Usage:
    PYTHONPATH=. python3 scripts/tv_parity_validate.py \\
        --strategy-module src.strategies.trend_following.swift_alma \\
        --strategy-class SwiftAlmaStrategy \\
        --config-json '{"alt_tf_multiplier": 8, "alma_length": 2, "alma_sigma": 5}' \\
        --tv-chart-csv "~/Downloads/VANTAGE_XAUUSD, 5.csv" \\
        --tv-trades-csv "~/Downloads/SWIFTALGO_VANTAGE_XAUUSD_2026-04-14.csv" \\
        --alt-tf-min 40 \\
        --initial-equity 1000000 \\
        --position-pct 0.10

Exit codes:
    0  PASS (≥ 99.0% bar-by-bar match)
    2  FAIL (< 99.0% match)
    1  Usage / error
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.backtest.tv_parity import (  # noqa: E402
    bar_by_bar_match,
    detect_alt_anchor_segments,
    detect_chart_tz_from_csv,
    extract_pine_config_from_xlsx,
    format_parity_report,
    load_tv_chart_csv,
    load_tv_trades_csv,
    load_tv_trades_xlsx,
    simulate_reversal_strategy,
    trade_by_trade_match,
)


PASS_THRESHOLD_PCT = 99.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TradingView parity validation for Pine Script ports")
    p.add_argument("--strategy-module", required=True,
                   help="Python module path, e.g. src.strategies.trend_following.swift_alma")
    p.add_argument("--strategy-class", required=True,
                   help="Class name inside the module, e.g. SwiftAlmaStrategy")
    p.add_argument("--config-json", default="{}",
                   help="JSON string with strategy-specific config kwargs")
    p.add_argument("--tv-chart-csv", required=True,
                   help="Path to TV chart data export CSV (OHLC + indicator columns)")
    p.add_argument("--tv-trades-csv", default=None,
                   help="Optional path to TV strategy trades export (.csv OR .xlsx)")
    p.add_argument("--alt-tf-min", type=int, default=None,
                   help="Alt-TF bar duration in minutes. If omitted and an xlsx "
                        "trades file is provided, auto-extracted from the xlsx "
                        "Properties sheet (Pine intRes × Pine res). Falls back to 40.")
    p.add_argument("--tv-trades-tz", default=None,
                   help="Timezone for the xlsx trades file (e.g. 'Asia/Kolkata' "
                        "for IST). Default: auto-detect from chart CSV ISO offset.")
    p.add_argument("--initial-equity", type=float, default=1_000_000.0,
                   help="Starting equity for P&L sim (default: 1M matching Pine default)")
    p.add_argument("--position-pct", type=float, default=0.10,
                   help="Fixed % of equity per trade (default: 0.10 matching Pine default_qty_value=10)")
    p.add_argument("--out-dir", default="reports/tv_parity",
                   help="Where to write the Markdown report")
    return p.parse_args()


def load_strategy_class(module_path: str, class_name: str):
    """Dynamically import a strategy class and verify it supports parity validation."""
    try:
        mod = importlib.import_module(module_path)
    except ImportError as e:
        print(f"ERROR: could not import module {module_path}: {e}", file=sys.stderr)
        sys.exit(1)

    cls = getattr(mod, class_name, None)
    if cls is None:
        print(f"ERROR: class {class_name} not found in {module_path}", file=sys.stderr)
        sys.exit(1)

    if not hasattr(cls, "detect_signals_lookahead"):
        print(
            f"ERROR: {class_name} does not expose detect_signals_lookahead classmethod.\n"
            f"Pine ports must implement this classmethod to be validatable.\n"
            f"See src/strategies/trend_following/swift_alma.py as a reference.",
            file=sys.stderr,
        )
        sys.exit(1)

    return cls


def main() -> int:
    args = parse_args()

    print("=" * 76)
    print("TradingView Parity Validation — Stage 0")
    print("=" * 76)
    print(f"Strategy:  {args.strategy_module} :: {args.strategy_class}")

    # 1. Load strategy class
    strategy_cls = load_strategy_class(args.strategy_module, args.strategy_class)

    config = {}
    if args.config_json and args.config_json != "{}":
        try:
            config = json.loads(args.config_json)
        except json.JSONDecodeError as e:
            print(f"ERROR: invalid --config-json: {e}", file=sys.stderr)
            return 1
    print(f"Config:    {config}")

    # 2. Load TV chart data
    tv_chart_path = Path(args.tv_chart_csv).expanduser()
    if not tv_chart_path.exists():
        print(f"ERROR: TV chart CSV not found at {tv_chart_path}", file=sys.stderr)
        return 1
    print(f"Chart CSV: {tv_chart_path}")
    tv_chart_df = load_tv_chart_csv(tv_chart_path)
    print(f"           {len(tv_chart_df)} bars, {tv_chart_df['dt'].iloc[0]} → {tv_chart_df['dt'].iloc[-1]}")

    # 2b. Auto-detect chart display tz (used to interpret naive xlsx times)
    chart_tz = detect_chart_tz_from_csv(tv_chart_path)
    if chart_tz is not None:
        print(f"Chart TZ:  {chart_tz} (detected from ISO 8601 offset)")
    else:
        chart_tz = "UTC"

    tv_trades = None
    tv_trades_path_str = None
    pine_cfg: dict = {}
    if args.tv_trades_csv:
        tv_trades_path = Path(args.tv_trades_csv).expanduser()
        if not tv_trades_path.exists():
            print(f"WARNING: TV trades file not found at {tv_trades_path}, skipping trade-level match", file=sys.stderr)
        else:
            ext = tv_trades_path.suffix.lower()
            tz_to_use = args.tv_trades_tz or chart_tz
            if ext == ".xlsx":
                tv_trades = load_tv_trades_xlsx(tv_trades_path, tz=tz_to_use)
                pine_cfg = extract_pine_config_from_xlsx(tv_trades_path)
                print(f"Trades XLS:{tv_trades_path}")
                print(f"           {len(tv_trades)} TV trades (legs collapsed) in tz={tz_to_use}")
                if pine_cfg:
                    chart_tf = pine_cfg.get('chart_tf_min')
                    pine_res = pine_cfg.get('pine_res_min')
                    alt_tf = pine_cfg.get('alt_tf_min')
                    mult = (alt_tf // chart_tf) if (alt_tf and chart_tf) else None
                    print(f"Pine cfg:  symbol={pine_cfg.get('symbol')} | "
                          f"chart={pine_cfg.get('timeframe')} | "
                          f"alt_tf_min={alt_tf} (=chart {chart_tf}min × mult {mult}; "
                          f"Pine res input was {pine_res}min — IGNORED at runtime)")
                    print(f"           ALMA(len={pine_cfg.get('alma_length')}, "
                          f"offset={pine_cfg.get('alma_offset')}, "
                          f"sigma={pine_cfg.get('alma_sigma')}) | "
                          f"SL={pine_cfg.get('sl_pct')}% | "
                          f"TP1/2/3={pine_cfg.get('tp1_pct')}/{pine_cfg.get('tp2_pct')}/{pine_cfg.get('tp3_pct')}")
            else:
                tv_trades = load_tv_trades_csv(tv_trades_path)
                print(f"Trades CSV:{tv_trades_path}")
                print(f"           {len(tv_trades)} TV trades")
            tv_trades_path_str = str(tv_trades_path)

    # 2c. Resolve alt-TF: CLI override → Pine cfg from xlsx → default 40
    if args.alt_tf_min is not None:
        alt_tf_min = args.alt_tf_min
        print(f"Alt-TF:    {alt_tf_min} min (from --alt-tf-min)")
    elif pine_cfg.get("alt_tf_min"):
        alt_tf_min = int(pine_cfg["alt_tf_min"])
        print(f"Alt-TF:    {alt_tf_min} min (auto-extracted from xlsx Properties)")
    else:
        alt_tf_min = 40
        print(f"Alt-TF:    {alt_tf_min} min (fallback default)")

    # 2d. Auto-merge Pine config into strategy config if not explicitly overridden
    if pine_cfg:
        # alt_tf_multiplier expressed in CHART bars (chart TF × this = alt TF)
        chart_tf_min = pine_cfg.get("chart_tf_min")
        if chart_tf_min and alt_tf_min and chart_tf_min > 0:
            auto_alt_mult = alt_tf_min // chart_tf_min
            config.setdefault("alt_tf_multiplier", auto_alt_mult)
            config.setdefault("timeframe_minutes", chart_tf_min)
        for src_key, dst_key in [
            ("alma_length", "alma_length"),
            ("alma_offset", "alma_offset"),
            ("alma_sigma", "alma_sigma"),
        ]:
            if pine_cfg.get(src_key) is not None:
                config.setdefault(dst_key, pine_cfg[src_key])
        print(f"Auto-cfg:  {config}")

    # 3. Detect alt-TF anchor segments (DST-aware)
    print()
    print("Detecting alt-TF anchor segments...")
    segments = detect_alt_anchor_segments(tv_chart_df, alt_tf_min=alt_tf_min)
    import pandas as pd
    for seg_ts, anchor in segments:
        dt = pd.Timestamp(seg_ts, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")
        print(f"  from {dt} UTC: anchor = +{anchor} min")

    # 4. Run the strategy's classmethod to detect signals
    print()
    print(f"Calling {args.strategy_class}.detect_signals_lookahead(...)")
    my_df = strategy_cls.detect_signals_lookahead(
        tv_chart_df,
        anchor_segments=segments,
        **config,
    )
    my_longs = int(my_df["le_trigger"].sum())
    my_shorts = int(my_df["se_trigger"].sum())
    print(f"  My le_triggers: {my_longs}")
    print(f"  My se_triggers: {my_shorts}")

    # 5. Bar-by-bar match
    print()
    print("── Bar-by-bar match ──")
    bar_stats = bar_by_bar_match(tv_chart_df, my_df)
    print(f"  TV Longs:  {bar_stats['tv_longs']}")
    print(f"  TV Shorts: {bar_stats['tv_shorts']}")
    print(f"  LONG match:  {bar_stats['long_match']} / {bar_stats['tv_longs']}")
    print(f"  SHORT match: {bar_stats['short_match']} / {bar_stats['tv_shorts']}")
    print(f"  TOTAL:       {bar_stats['total_match']} / {bar_stats['total_tv']} = {bar_stats['total_match_pct']:.2f}%")

    # 6. Optional: trade-by-trade match if TV trades CSV provided
    trade_stats = None
    if tv_trades is not None:
        print()
        print("── Trade-by-trade match ──")
        my_trades = simulate_reversal_strategy(
            my_df,
            initial_equity=args.initial_equity,
            position_pct=args.position_pct,
        )
        print(f"  My simulated trades: {len(my_trades)}")
        trade_stats = trade_by_trade_match(tv_trades, my_trades)
        print(f"  Matched: {trade_stats['matched']} / {trade_stats['tv_total']} = {trade_stats['tv_match_pct']:.2f}%")
        print(f"  Median time diff: {trade_stats['median_time_diff_min']:.1f} min")
        print(f"  Median entry price diff: {trade_stats['median_entry_price_diff_pct']:.4f}%")
        print(f"  P&L sign agreement: {trade_stats['pnl_sign_agreement_pct']:.2f}%")

    # 7. Write report + print verdict
    print()
    print("=" * 76)
    pct = bar_stats["total_match_pct"]
    if pct >= PASS_THRESHOLD_PCT:
        verdict = "PASS"
        print(f"✅ VERDICT: PORT CONFIRMED — {pct:.2f}% equivalent to Pine Script on same data.")
        print("   The strategy is safe to proceed to Stage 1+ of development.")
    else:
        verdict = "FAIL"
        print(f"❌ VERDICT: PORT INCORRECT — only {pct:.2f}% match.")
        print("   Do NOT proceed to Stage 1 until the gap is resolved.")

    report_md = format_parity_report(
        strategy_name=f"{args.strategy_module}::{args.strategy_class}",
        tv_chart_csv=str(tv_chart_path),
        tv_trades_csv=tv_trades_path_str,
        bar_stats=bar_stats,
        trade_stats=trade_stats,
        anchor_segments=segments,
    )

    out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.strategy_class}_parity.md"
    out_path.write_text(report_md)
    print(f"\nReport: {out_path}")

    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
