#!/usr/bin/env python3
"""Broker decision analyzer — runs weekly after the Monday sampler.

Consumes all data/ctrader_spread_samples_XAUUSD_*.csv, isolates the
London/NY overlap-window ticks (13:00-16:00 UTC), and decides:

    STAY              — avg overlap spread ≤ 0.4 std-pips, backtest is right
    RECALIBRATE       — 0.4 < avg ≤ 0.7 std-pips, keep broker but bump
                        ic_markets_ctrader_xauusd_normal base_spread_pips
    CONSIDER_SWITCH   — 0.7 < avg ≤ 1.0 std-pips, spread is systematically
                        wider than backtest; triage whether to switch broker
    RECOMMEND_SWITCH  — avg > 1.0 std-pips, IC Markets is meaningfully more
                        expensive than claimed; compare with Tickmill /
                        Pepperstone in a side-by-side test

Data sufficiency gate:
    - At least N_WEEKS distinct Mondays of samples (default 3)
    - At least N_OVERLAP_TICKS in the overlap window (default 5000)

If insufficient, writes "DATA_INSUFFICIENT" to the report and exits 2.
If sufficient, writes the decision + rationale and exits 0 (no-action
verdicts STAY/RECALIBRATE) or 1 (action-needed verdicts
CONSIDER_SWITCH/RECOMMEND_SWITCH).

The exit code is the hook a CronCreate scheduled agent uses to decide
whether to send Prince a push notification.

Outputs:
    data/broker_decision_report.md  — Prince-facing markdown report
    data/broker_decision_status.json — machine-readable status for the
                                       scheduler
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median


# Tunables (can be overridden via CLI)
N_WEEKS_MIN = 3
N_OVERLAP_TICKS_MIN = 5000

# Overlap window in UTC hours (London/NY peak)
OVERLAP_START_UTC = 13
OVERLAP_END_UTC = 16   # half-open

# Decision thresholds (average overlap spread in standard pips, where 1 pip = $0.10/oz)
THRESH_STAY = 0.4
THRESH_RECALIBRATE = 0.7
THRESH_CONSIDER_SWITCH = 1.0

# Standard pip size for XAUUSD (in our config)
PIP_SIZE_STD = 0.10

# Static broker comparison — values from memory project_gold_blockers.md.
# These are the alternatives to surface when a switch is being considered.
BROKER_COMPARISON = [
    {
        "broker": "IC Markets Raw cTrader",
        "comm_rt_per_lot": 29.17,   # at $4865/oz
        "spread_rt_per_lot": None,  # filled from observed
        "regulator": "FSA Seychelles",
        "api": "native Python (ctrader-open-api)",
        "india": True,
        "notes": "Current. Already live.",
    },
    {
        "broker": "IC Markets Raw MT4",
        "comm_rt_per_lot": 7.00,
        "spread_rt_per_lot": 6.00,
        "regulator": "FSA Seychelles",
        "api": "MT4 bridge only (no macOS)",
        "india": True,
        "notes": "Cheapest commission but MT4 rejected in BLUEPRINT — no macOS support.",
    },
    {
        "broker": "Tickmill Raw",
        "comm_rt_per_lot": 7.20,
        "spread_rt_per_lot": 6.00,
        "regulator": "FSA Seychelles",
        "api": "FIX (weeks to integrate)",
        "india": True,
        "notes": "Saves ~$22/lot on commission. FIX integration is ~2-4 weeks.",
    },
    {
        "broker": "Pepperstone Razor",
        "comm_rt_per_lot": 8.70,
        "spread_rt_per_lot": 6.00,
        "regulator": "ASIC / FCA",
        "api": "cTrader Open API OR MT4",
        "india": True,
        "notes": "Similar to Tickmill but stronger regulators. Drop-in cTrader replacement.",
    },
    {
        "broker": "XM Ultra Low",
        "comm_rt_per_lot": 0.00,
        "spread_rt_per_lot": 50.00,
        "regulator": "FSA Seychelles",
        "api": "none",
        "india": True,
        "notes": "REJECTED per memory: market-maker dealing desk, 10-100× slower execution, real spread 1.6 pips not marketed 0.16.",
    },
]


@dataclass
class AnalysisResult:
    total_ticks: int
    overlap_ticks: int
    unique_weeks: int
    date_range: tuple[str, str]
    sufficient: bool
    insufficient_reason: str
    avg_spread_overlap_usd: float
    avg_spread_overlap_pips_std: float
    p50_overlap_pips_std: float
    p90_overlap_pips_std: float
    decision: str
    rationale: str


def _load_samples(paths: list[str]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        try:
            with open(p) as f:
                r = csv.DictReader(f)
                for row in r:
                    rows.append({
                        "ts_unix_ms": int(row["ts_unix_ms"]),
                        "spread": float(row["spread"]),
                    })
        except Exception as e:
            print(f"WARN: skipping {p}: {e}", file=sys.stderr)
    return rows


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def _monday_iso_week(ts_ms: int) -> str:
    """Return the Monday (ISO format) that a timestamp falls into that week.
    Used to count distinct weeks of data.
    """
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    # isocalendar: (year, week, weekday). Weekday 1 = Monday.
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def analyze(
    sample_paths: list[str],
    *,
    n_weeks_min: int = N_WEEKS_MIN,
    n_overlap_ticks_min: int = N_OVERLAP_TICKS_MIN,
) -> AnalysisResult:
    rows = _load_samples(sample_paths)
    total_ticks = len(rows)
    if not rows:
        return AnalysisResult(
            total_ticks=0, overlap_ticks=0, unique_weeks=0,
            date_range=("", ""),
            sufficient=False,
            insufficient_reason="No sample CSVs found",
            avg_spread_overlap_usd=0.0, avg_spread_overlap_pips_std=0.0,
            p50_overlap_pips_std=0.0, p90_overlap_pips_std=0.0,
            decision="DATA_INSUFFICIENT",
            rationale="Run the Monday sampler at least once.",
        )

    # Bucket to overlap-only
    overlap_rows = []
    weeks = set()
    for r in rows:
        dt = datetime.fromtimestamp(r["ts_unix_ms"] / 1000.0, tz=timezone.utc)
        if OVERLAP_START_UTC <= dt.hour < OVERLAP_END_UTC:
            overlap_rows.append(r)
            weeks.add(_monday_iso_week(r["ts_unix_ms"]))

    overlap_spreads_usd = [r["spread"] for r in overlap_rows]
    overlap_spreads_pips = [s / PIP_SIZE_STD for s in overlap_spreads_usd]

    rows.sort(key=lambda r: r["ts_unix_ms"])
    first_iso = datetime.fromtimestamp(rows[0]["ts_unix_ms"] / 1000.0, tz=timezone.utc).isoformat()
    last_iso = datetime.fromtimestamp(rows[-1]["ts_unix_ms"] / 1000.0, tz=timezone.utc).isoformat()

    insuff_reasons = []
    if len(weeks) < n_weeks_min:
        insuff_reasons.append(f"only {len(weeks)} distinct weeks (need ≥ {n_weeks_min})")
    if len(overlap_rows) < n_overlap_ticks_min:
        insuff_reasons.append(f"only {len(overlap_rows)} overlap-window ticks (need ≥ {n_overlap_ticks_min})")

    sufficient = not insuff_reasons

    if not sufficient:
        return AnalysisResult(
            total_ticks=total_ticks,
            overlap_ticks=len(overlap_rows),
            unique_weeks=len(weeks),
            date_range=(first_iso, last_iso),
            sufficient=False,
            insufficient_reason="; ".join(insuff_reasons),
            avg_spread_overlap_usd=mean(overlap_spreads_usd) if overlap_spreads_usd else 0.0,
            avg_spread_overlap_pips_std=mean(overlap_spreads_pips) if overlap_spreads_pips else 0.0,
            p50_overlap_pips_std=_percentile(overlap_spreads_pips, 50),
            p90_overlap_pips_std=_percentile(overlap_spreads_pips, 90),
            decision="DATA_INSUFFICIENT",
            rationale=f"Insufficient data: {'; '.join(insuff_reasons)}. The weekly Monday sampler runs at 18:30 IST each Monday; come back next week.",
        )

    # Sufficient — compute statistics and decide
    avg_usd = mean(overlap_spreads_usd)
    avg_pips = mean(overlap_spreads_pips)
    p50 = _percentile(overlap_spreads_pips, 50)
    p90 = _percentile(overlap_spreads_pips, 90)

    if avg_pips <= THRESH_STAY:
        decision = "STAY"
        rationale = (
            f"Average overlap-window spread {avg_pips:.2f} std-pips is within "
            f"backtest assumption (0.30 pips). No action needed — backtest "
            f"costs are honest."
        )
    elif avg_pips <= THRESH_RECALIBRATE:
        recommended = round(avg_pips, 2)
        decision = "RECALIBRATE"
        rationale = (
            f"Average overlap-window spread {avg_pips:.2f} std-pips is moderately "
            f"above backtest assumption (0.30 pips). Recommend updating the "
            f"`base_spread_pips` in config/brokers/ic_markets_ctrader.toml "
            f"(`[instruments.xauusd_metals.scenarios.normal.spread]`) from 0.30 → "
            f"{recommended}, then re-running walk-forward on donchian_gold + "
            f"vol_momentum_gold to confirm the deployable bar still passes. "
            f"NO broker change needed."
        )
    elif avg_pips <= THRESH_CONSIDER_SWITCH:
        decision = "CONSIDER_SWITCH"
        rationale = (
            f"Average overlap-window spread {avg_pips:.2f} std-pips is "
            f"meaningfully above backtest assumption (0.30 pips). IC Markets "
            f"cTrader spread appears systematically wider than the marketed "
            f"0.09-0.30 pips. Triage options: (a) ask IC Markets to investigate "
            f"their LP mix on your account, (b) open a Pepperstone Razor demo "
            f"and run a parallel 2-week sampler there, (c) update the backtest "
            f"profile to {avg_pips:.2f} pips and accept the higher cost."
        )
    else:
        decision = "RECOMMEND_SWITCH"
        rationale = (
            f"Average overlap-window spread {avg_pips:.2f} std-pips is "
            f"3×+ the backtest assumption. This is not a calibration error — "
            f"IC Markets is genuinely more expensive than the alternatives at "
            f"your current account tier. Recommend opening a Pepperstone Razor "
            f"or Tickmill Raw demo and running the weekly sampler there for "
            f"2-3 weeks before making the switch call. Do NOT consider XM — "
            f"rejected per project memory for market-maker conflict + no API."
        )

    return AnalysisResult(
        total_ticks=total_ticks,
        overlap_ticks=len(overlap_rows),
        unique_weeks=len(weeks),
        date_range=(first_iso, last_iso),
        sufficient=True,
        insufficient_reason="",
        avg_spread_overlap_usd=avg_usd,
        avg_spread_overlap_pips_std=avg_pips,
        p50_overlap_pips_std=p50,
        p90_overlap_pips_std=p90,
        decision=decision,
        rationale=rationale,
    )


def _emit_markdown(result: AnalysisResult, output_path: Path) -> None:
    mid_px = 4865.0  # rough reference for cost translation
    lot_oz = 100.0
    # Compute live total cost for context
    spread_rt_usd = result.avg_spread_overlap_usd * lot_oz * 2 if result.sufficient else 0.0
    commission_rt_usd = (mid_px * lot_oz / 100_000) * 3.0 * 2
    live_total = spread_rt_usd + commission_rt_usd

    # Fill IC Markets Raw cTrader spread from observed (for comparison table)
    bc = [dict(b) for b in BROKER_COMPARISON]
    for b in bc:
        if b["broker"] == "IC Markets Raw cTrader":
            b["spread_rt_per_lot"] = spread_rt_usd if result.sufficient else None

    now_iso = datetime.now(timezone.utc).isoformat()

    lines = []
    lines.append("# Broker Decision Report (XAUUSD)")
    lines.append("")
    lines.append(f"**Generated:** {now_iso}")
    lines.append(f"**Data source:** `data/ctrader_spread_samples_XAUUSD_*.csv`")
    lines.append("")
    lines.append("## Decision")
    lines.append("")
    lines.append(f"### 🎯 **{result.decision}**")
    lines.append("")
    lines.append(f"{result.rationale}")
    lines.append("")
    lines.append("## Data summary")
    lines.append("")
    lines.append(f"- **Total ticks captured:** {result.total_ticks:,}")
    lines.append(f"- **Overlap-window ticks (13:00-16:00 UTC):** {result.overlap_ticks:,}")
    lines.append(f"- **Distinct weeks of data:** {result.unique_weeks}")
    if result.sufficient:
        lines.append(f"- **Data range:** {result.date_range[0]} → {result.date_range[1]}")
        lines.append("")
        lines.append("### Spread distribution (overlap-window only)")
        lines.append("")
        lines.append(f"- **Average:** ${result.avg_spread_overlap_usd:.4f} per oz = **{result.avg_spread_overlap_pips_std:.2f} std-pips**")
        lines.append(f"- **p50 / median:** {result.p50_overlap_pips_std:.2f} std-pips")
        lines.append(f"- **p90:** {result.p90_overlap_pips_std:.2f} std-pips")
        lines.append("")
        lines.append(f"### Cost at $4865/oz, 1 standard lot (100 oz)")
        lines.append("")
        lines.append(f"| Component | USD / lot / round-trip |")
        lines.append(f"|---|---:|")
        lines.append(f"| Spread (observed) | ${spread_rt_usd:.2f} |")
        lines.append(f"| Commission (IC Markets published) | ${commission_rt_usd:.2f} |")
        lines.append(f"| **Total** | **${live_total:.2f}** |")
        lines.append(f"| Backtest assumption | $35.17 |")
        bt_total = 35.17
        diff_pct = (live_total - bt_total) / bt_total * 100
        lines.append(f"| **Δ vs backtest** | **{diff_pct:+.1f}%** |")
    else:
        lines.append("")
        lines.append(f"### ⏳ Not yet enough data")
        lines.append("")
        lines.append(f"**Reason:** {result.insufficient_reason}")
        lines.append("")
        lines.append(f"The weekly Monday sampler at 18:30 IST captures 240 min of ticks during the London/NY overlap. This analyzer will emit a real decision once it has ≥ {N_WEEKS_MIN} weeks of data AND ≥ {N_OVERLAP_TICKS_MIN:,} overlap-window ticks.")
        lines.append("")

    lines.append("")
    lines.append("## Broker comparison (for context)")
    lines.append("")
    lines.append(f"| Broker | Comm RT | Spread RT | Total RT | Regulator | API | India | Notes |")
    lines.append(f"|---|---:|---:|---:|---|---|---|---|")
    for b in bc:
        comm = f"${b['comm_rt_per_lot']:.2f}"
        if b["spread_rt_per_lot"] is None:
            spread = "—"
            total = "—"
        else:
            spread = f"${b['spread_rt_per_lot']:.2f}"
            total = f"${b['comm_rt_per_lot'] + b['spread_rt_per_lot']:.2f}"
        india = "✅" if b["india"] else "❌"
        lines.append(f"| {b['broker']} | {comm} | {spread} | {total} | {b['regulator']} | {b['api']} | {india} | {b['notes']} |")

    lines.append("")
    lines.append("## Next action")
    lines.append("")
    if result.decision == "DATA_INSUFFICIENT":
        lines.append("- Wait for more Monday samples; this analyzer will re-run weekly.")
    elif result.decision == "STAY":
        lines.append("- ✅ No action. Backtest costs match live observation within 10%.")
    elif result.decision == "RECALIBRATE":
        recommended = round(result.avg_spread_overlap_pips_std, 2)
        lines.append(f"- Edit `config/brokers/ic_markets_ctrader.toml` → `[instruments.xauusd_metals.scenarios.normal.spread]` → `base_spread_pips = {recommended}`")
        lines.append("- Re-run walk-forward: `python3 -m scripts.backtest validate donchian_gold --tier standard`")
        lines.append("- Re-run: `python3 -m scripts.backtest validate vol_momentum_gold --tier standard`")
        lines.append("- Confirm both still pass the deployable bar.")
    elif result.decision == "CONSIDER_SWITCH":
        lines.append("- Open a Pepperstone Razor demo account (ASIC, cTrader-compatible).")
        lines.append("- Copy the spread sampler over and run for 2-3 weeks in parallel.")
        lines.append("- Meanwhile, update IC Markets backtest profile to observed spread.")
    elif result.decision == "RECOMMEND_SWITCH":
        lines.append("- Stop recommending IC Markets in future projects.")
        lines.append("- Open Pepperstone Razor + Tickmill Raw demos in parallel.")
        lines.append("- Run weekly sampler on both for 3 weeks.")
        lines.append("- After 3 weeks, compare all three + pick the winner.")
        lines.append("- NOT XM under any circumstance (memory-blocked).")

    lines.append("")
    output_path.write_text("\n".join(lines) + "\n")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n-weeks-min", type=int, default=N_WEEKS_MIN)
    p.add_argument("--n-overlap-ticks-min", type=int, default=N_OVERLAP_TICKS_MIN)
    p.add_argument("--output", type=str, default="data/broker_decision_report.md")
    p.add_argument("--status", type=str, default="data/broker_decision_status.json")
    args = p.parse_args()

    paths = sorted(glob.glob("data/ctrader_spread_samples_XAUUSD_*.csv"))
    result = analyze(
        paths,
        n_weeks_min=args.n_weeks_min,
        n_overlap_ticks_min=args.n_overlap_ticks_min,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _emit_markdown(result, output_path)

    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sufficient": result.sufficient,
        "decision": result.decision,
        "total_ticks": result.total_ticks,
        "overlap_ticks": result.overlap_ticks,
        "unique_weeks": result.unique_weeks,
        "avg_overlap_spread_pips_std": result.avg_spread_overlap_pips_std,
        "report_path": str(output_path.absolute()),
    }
    Path(args.status).write_text(json.dumps(status, indent=2))

    print(f"Decision: {result.decision}")
    print(f"Report:   {output_path}")
    print(f"Status:   {args.status}")

    # Exit codes drive the scheduler's notification logic:
    #   0 = no action needed (STAY / RECALIBRATE can be handled async)
    #   1 = action needed (CONSIDER_SWITCH / RECOMMEND_SWITCH → page Prince)
    #   2 = insufficient data (silent, don't notify)
    if result.decision == "DATA_INSUFFICIENT":
        return 2
    if result.decision in ("CONSIDER_SWITCH", "RECOMMEND_SWITCH"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
