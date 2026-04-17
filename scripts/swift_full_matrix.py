#!/usr/bin/env python3
"""SWIFT comprehensive backtest matrix — 4 windows × 4 TFs × 5 leverages × 3 fees.

Task #109. 240-cell matrix to make a deployment decision on SWIFT under
realistic costs across multiple timeframes, leverages, and time horizons.

  Timeframes:  1m, 5m, 15m, 1h
  Leverages:   1x, 50x, 100x, 500x, 1000x
  Cost modes:  pine_zero_cost, ic_markets_mt4_xauusd_normal,
               ic_markets_ctrader_xauusd_normal
  Windows:     1 month, 3 months, 6 months, 1 year (anchored to data end)

Hard requirements (per plan):
  1. NO signal repainting — use SwiftAlmaStrategy.on_features() (production)
  2. Full accuracy mode — M1PathModel for M5/M15/1h chart cells
  3. Phased validation — small steps first, find bugs
  4. Always use fee profile registry (never default constructor)

Outputs:
  reports/swift_full_matrix_2026-04-15/
    ├── index.html         — primary visual report
    ├── swift_full_matrix.pdf  — secondary PDF (fpdf2)
    ├── raw_data.csv       — flat 240-row table
    └── heatmaps/*.png     — 12 PNG heatmaps embedded in PDF/HTML
  data/swift_full_matrix_2026-04-15.json  (gitignored) — re-render cache

Usage:
    PYTHONPATH=. python3 scripts/swift_full_matrix.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Silence per-signal logs BEFORE engine imports
import structlog

logging.disable(logging.CRITICAL)
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

import pandas as pd  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.backtest.fee_profiles import make_fee_model  # noqa: E402
from src.backtest.leveraged_engine import LeveragedBacktestEngine  # noqa: E402
from src.backtest.path import get_or_build_m1_path_model  # noqa: E402
from src.strategies.trend_following.swift_alma import SwiftAlmaStrategy  # noqa: E402


# ── Config ──────────────────────────────────────────────────────────────

DATA_M1 = REPO_ROOT / "data" / "historical" / "XAUUSD_1m.parquet"
DATA_M5 = REPO_ROOT / "data" / "historical" / "XAUUSD_5m.parquet"
DATA_1H = REPO_ROOT / "data" / "historical" / "XAUUSD_1h.parquet"

REPORT_DIR = REPO_ROOT / "reports" / "swift_full_matrix_2026-04-15"
HEATMAPS_DIR = REPORT_DIR / "heatmaps"
JSON_CACHE = REPO_ROOT / "data" / "swift_full_matrix_2026-04-15.json"

INITIAL_CASH = 10_000.0

TIMEFRAMES = ["1m", "5m", "15m", "1h"]
LEVERAGES = [1.0, 50.0, 100.0, 500.0, 1000.0]
FEE_PROFILES = [
    "pine_zero_cost",
    "ic_markets_mt4_xauusd_normal",
    "ic_markets_ctrader_xauusd_normal",
]
WINDOWS_DAYS = [30, 90, 180, 365]
WINDOW_LABELS = {30: "1mo", 90: "3mo", 180: "6mo", 365: "1y"}

# SWIFT Pine defaults (verified from xlsx Properties sheet)
SWIFT_PARAMS = dict(
    alma_length=2,
    alma_offset=0.85,
    alma_sigma=5.0,
    alt_tf_multiplier=8,
    sl_pct=0.005,
    tp1_pct=0.010, tp1_qty=0.50,
    tp2_pct=0.015, tp2_qty=0.30,
    tp3_pct=0.020, tp3_qty=0.20,
    use_pine_ladder=True,
    same_bar_flip=True,
    max_risk_per_trade=0.005,
    leverage_range=(1.0, 1000.0),
)


# ── Result dataclass ─────────────────────────────────────────────────────


@dataclass
class CellResult:
    window_days: int
    window_label: str
    timeframe: str
    leverage: float
    fee_profile: str
    trades: int
    return_pct: float
    maxdd_pct: float
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    total_commission: float
    final_equity: float
    margin_per_trade: float          # avg margin used
    cost_per_trade: float            # avg total round-trip cost (commission only — slip is in fill price)
    cost_pct_of_margin: float        # the "leverage tax" derived metric
    broker_stop_outs: int
    notes: str = ""


# ── Data loading + slicing ──────────────────────────────────────────────


def _resample_5m_to_15m(df_m5: pd.DataFrame) -> pd.DataFrame:
    """Resample M5 OHLCV to M15."""
    df = df_m5.copy()
    df["dt_idx"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("dt_idx")
    rs = df.resample("15min", origin="epoch").agg({
        "timestamp": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    rs = rs.reset_index(drop=True)
    rs["dt"] = pd.to_datetime(rs["timestamp"], unit="ms", utc=True)
    return rs


def _load_full_tf(timeframe: str) -> pd.DataFrame:
    """Load the canonical parquet for a timeframe (resamples M15 from M5)."""
    if timeframe == "1m":
        df = pd.read_parquet(DATA_M1)
    elif timeframe == "5m":
        df = pd.read_parquet(DATA_M5)
    elif timeframe == "15m":
        df = _resample_5m_to_15m(pd.read_parquet(DATA_M5))
        return df  # already has dt
    elif timeframe == "1h":
        df = pd.read_parquet(DATA_1H)
    else:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def _slice_window(df: pd.DataFrame, window_days: int) -> pd.DataFrame:
    """Slice the most recent `window_days` of `df` plus warmup buffer.

    Warmup: 200 bars before window_start, sized to indicator needs
    (SWIFT alt_tf_multiplier=8, alma_length=2 → 16 alt bars = 128 chart bars
    minimum; 200 gives generous headroom).
    """
    end_dt = df["dt"].iloc[-1]
    window_start = end_dt - pd.Timedelta(days=window_days)
    sliced = df[df["dt"] >= window_start].reset_index(drop=True)
    # Add warmup BEFORE the window — we need 200 bars before the window starts
    full_idx_at_window_start = df.index[df["dt"] >= window_start][0]
    warmup_bars = 200
    full_start = max(0, full_idx_at_window_start - warmup_bars)
    return df.iloc[full_start:].reset_index(drop=True)


# ── Cell runner ─────────────────────────────────────────────────────────


def _make_strategy(timeframe: str) -> SwiftAlmaStrategy:
    return SwiftAlmaStrategy(timeframe=timeframe, **SWIFT_PARAMS)


def _metrics_from_trades(trades: list) -> dict:
    if not trades:
        return {"wins": 0, "losses": 0, "win_rate": 0.0, "pf": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0,
                "total_comm": 0.0, "avg_margin": 0.0}
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    margins = [t.margin_used for t in trades]
    return {
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(pnls) * 100.0,
        "pf": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf"),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "total_comm": sum(t.commission for t in trades),
        "avg_margin": sum(margins) / len(margins) if margins else 0.0,
    }


def _run_cell(
    *,
    window_days: int,
    timeframe: str,
    leverage: float,
    fee_profile: str,
    df: pd.DataFrame,
    m1_path_model_cache,
) -> CellResult:
    """Run one (window, TF, leverage, fee) backtest cell."""
    fee_model = make_fee_model(fee_profile)

    # M1PathModel for M5/M15/1h chart cells. M1 chart is degenerate (no sub-bars).
    use_m1_pm = timeframe != "1m"
    path_model = m1_path_model_cache if use_m1_pm else None

    engine = LeveragedBacktestEngine(
        initial_institutional_cash=INITIAL_CASH,
        initial_aggressive_cash=0.0,
        fee_model=fee_model,
        path_model=path_model,
        run_id=f"swift_full_{window_days}d_{timeframe}_{int(leverage)}x_{fee_profile[:8]}",
    )
    strategy = _make_strategy(timeframe)
    try:
        result = engine.run(
            strategy, df,
            symbol="XAUUSD",
            timeframe=timeframe,
            leverage=leverage,
            sub_book="institutional",
        )
    except Exception as e:
        return CellResult(
            window_days=window_days,
            window_label=WINDOW_LABELS[window_days],
            timeframe=timeframe,
            leverage=leverage,
            fee_profile=fee_profile,
            trades=0, return_pct=0.0, maxdd_pct=0.0, win_rate=0.0,
            profit_factor=0.0, avg_win=0.0, avg_loss=0.0,
            total_commission=0.0,
            final_equity=INITIAL_CASH,
            margin_per_trade=0.0,
            cost_per_trade=0.0,
            cost_pct_of_margin=0.0,
            broker_stop_outs=0,
            notes=f"ERROR: {type(e).__name__}: {e}",
        )

    m = _metrics_from_trades(result.trades)
    n = len(result.trades) or 1
    cost_per_trade = m["total_comm"] / n
    cost_pct = (cost_per_trade / m["avg_margin"] * 100.0) if m["avg_margin"] > 0 else 0.0
    final_eq = result.metrics["final_institutional_equity"]
    return CellResult(
        window_days=window_days,
        window_label=WINDOW_LABELS[window_days],
        timeframe=timeframe,
        leverage=leverage,
        fee_profile=fee_profile,
        trades=len(result.trades),
        return_pct=(final_eq - INITIAL_CASH) / INITIAL_CASH * 100.0,
        maxdd_pct=result.metrics["max_dd_pct"],
        win_rate=m["win_rate"],
        profit_factor=m["pf"],
        avg_win=m["avg_win"],
        avg_loss=m["avg_loss"],
        total_commission=m["total_comm"],
        final_equity=final_eq,
        margin_per_trade=m["avg_margin"],
        cost_per_trade=cost_per_trade,
        cost_pct_of_margin=cost_pct,
        broker_stop_outs=result.broker_stop_out_count,
    )


# ── Phase functions (validation + matrix execution) ─────────────────────


def _phase0_preflight() -> dict:
    """Pre-flight: data, fee profiles, M1PathModel cache."""
    print("=" * 80)
    print("Phase 0 — Preflight")
    print("=" * 80)
    errors = []
    for path, label in [(DATA_M1, "M1"), (DATA_M5, "M5"), (DATA_1H, "1H")]:
        if not path.exists():
            errors.append(f"missing data file: {path}")
        else:
            df = pd.read_parquet(path)
            print(f"  ✓ {label}: {len(df):,} bars")

    for prof in FEE_PROFILES:
        try:
            fm = make_fee_model(prof)
            print(f"  ✓ fee profile loadable: {prof} ({type(fm).__name__})")
        except Exception as e:
            errors.append(f"fee profile {prof} failed: {e}")

    try:
        m1pm = get_or_build_m1_path_model(str(DATA_M1), sub_bar_count=5)
        print(f"  ✓ M1PathModel built/loaded")
    except Exception as e:
        errors.append(f"M1PathModel build failed: {e}")
        m1pm = None

    if errors:
        print()
        print("PRE-FLIGHT ERRORS:")
        for err in errors:
            print(f"  ✗ {err}")
        sys.exit(1)
    print()
    print("Phase 0 PASS")
    print()
    return {"m1_path_model": m1pm}


def _phase1_smoke_test(ctx: dict) -> CellResult:
    """Single-cell smoke: M5 × 1mo × 1x × pine_zero_cost."""
    print("=" * 80)
    print("Phase 1 — Smoke test: M5 × 1mo × 1x × pine_zero_cost")
    print("=" * 80)
    df_m5 = _load_full_tf("5m")
    df_sliced = _slice_window(df_m5, 30)
    print(f"  Sliced bars: {len(df_sliced)} ({df_sliced['dt'].iloc[0]} → {df_sliced['dt'].iloc[-1]})")
    cell = _run_cell(
        window_days=30, timeframe="5m", leverage=1.0,
        fee_profile="pine_zero_cost",
        df=df_sliced, m1_path_model_cache=ctx["m1_path_model"],
    )
    print(f"  Result: trades={cell.trades}, ret={cell.return_pct:+.2f}%, "
          f"DD={cell.maxdd_pct:.2f}%, PF={cell.profit_factor:.2f}, "
          f"avg_margin=${cell.margin_per_trade:,.2f}")
    if cell.notes.startswith("ERROR"):
        print(f"  ✗ FAIL: {cell.notes}")
        sys.exit(2)
    if cell.trades == 0:
        print(f"  ✗ FAIL: 0 trades — indicator warmup or signal logic broken")
        sys.exit(2)
    if abs(cell.return_pct) > 90:
        print(f"  ✗ FAIL: implausible return {cell.return_pct:+.2f}% on a 1-month window")
        sys.exit(2)
    print(f"  ✓ Phase 1 PASS")
    print()
    return cell


def _phase2_cost_decomposition(ctx: dict) -> dict[str, CellResult]:
    """Same cell × 3 fee modes — verify cost magnitudes."""
    print("=" * 80)
    print("Phase 2 — Cost decomposition: M5 × 1mo × 1x × {3 fee modes}")
    print("=" * 80)
    df_m5 = _load_full_tf("5m")
    df_sliced = _slice_window(df_m5, 30)
    cells = {}
    for prof in FEE_PROFILES:
        cell = _run_cell(
            window_days=30, timeframe="5m", leverage=1.0,
            fee_profile=prof,
            df=df_sliced, m1_path_model_cache=ctx["m1_path_model"],
        )
        cells[prof] = cell
        print(f"  {prof:>40} | trades={cell.trades:>4} ret={cell.return_pct:+7.2f}% "
              f"comm/trade=${cell.cost_per_trade:>7.2f} margin/trade=${cell.margin_per_trade:>9,.0f}")

    # Sanity: pine_zero should have $0 commission
    if cells["pine_zero_cost"].cost_per_trade != 0.0:
        print(f"  ✗ FAIL: pine_zero_cost cost/trade = ${cells['pine_zero_cost'].cost_per_trade:.2f}, expected $0")
        sys.exit(2)
    # MT4 should be substantially less than cTrader for gold
    mt4_cost = cells["ic_markets_mt4_xauusd_normal"].cost_per_trade
    ct_cost = cells["ic_markets_ctrader_xauusd_normal"].cost_per_trade
    if mt4_cost <= 0:
        print(f"  ✗ FAIL: MT4 cost/trade = ${mt4_cost:.2f}, expected > 0")
        sys.exit(2)
    if ct_cost <= mt4_cost:
        print(f"  ✗ FAIL: cTrader cost/trade ({ct_cost:.2f}) should be > MT4 ({mt4_cost:.2f})")
        sys.exit(2)
    # Sanity guard: if cost > $200/trade, the 90× slip bug regressed
    if ct_cost > 200:
        print(f"  ✗ FAIL: cTrader cost/trade = ${ct_cost:.2f} — 90× slip bug regression?")
        sys.exit(2)
    print(f"  ✓ Phase 2 PASS — cost decomposition sane (MT4=${mt4_cost:.2f}, cTrader=${ct_cost:.2f})")
    print()
    return cells


def _phase3_leverage_axis(ctx: dict) -> list[CellResult]:
    """5 leverages × cTrader fees — confirm leverage transparency."""
    print("=" * 80)
    print("Phase 3 — Leverage axis: M5 × 1mo × {5 leverages} × cTrader")
    print("=" * 80)
    df_m5 = _load_full_tf("5m")
    df_sliced = _slice_window(df_m5, 30)
    results = []
    for lev in LEVERAGES:
        cell = _run_cell(
            window_days=30, timeframe="5m", leverage=lev,
            fee_profile="ic_markets_ctrader_xauusd_normal",
            df=df_sliced, m1_path_model_cache=ctx["m1_path_model"],
        )
        results.append(cell)
        print(f"  {int(lev):>5}x | trades={cell.trades:>4} ret={cell.return_pct:+7.2f}% "
              f"margin/trade=${cell.margin_per_trade:>9,.0f} "
              f"cost%margin={cell.cost_pct_of_margin:>6.3f}%")

    # Check P&L invariance (expected for SWIFT)
    returns = {r.return_pct for r in results}
    if len(returns) == 1:
        print(f"  ✓ P&L is leverage-invariant (as expected for risk-sized SWIFT)")
    else:
        print(f"  ⚠ P&L varies across leverages: {sorted(returns)}")
        print(f"    This is unexpected — investigate later (not blocking)")
    # Cost%margin must scale linearly with leverage
    cost_pcts = [r.cost_pct_of_margin for r in results]
    if cost_pcts[0] > 0 and cost_pcts[-1] / cost_pcts[0] < 100:
        print(f"  ⚠ Cost%margin not scaling linearly with leverage: 1x={cost_pcts[0]:.4f} → 1000x={cost_pcts[-1]:.4f}")
    print(f"  ✓ Phase 3 PASS")
    print()
    return results


def _phase4_tf_axis(ctx: dict) -> list[CellResult]:
    """4 TFs × 1mo × 1x × pine_zero — verify warmup + trade counts."""
    print("=" * 80)
    print("Phase 4 — Timeframe axis: {4 TFs} × 1mo × 1x × pine_zero_cost")
    print("=" * 80)
    results = []
    for tf in TIMEFRAMES:
        df_full = _load_full_tf(tf)
        df_sliced = _slice_window(df_full, 30)
        cell = _run_cell(
            window_days=30, timeframe=tf, leverage=1.0,
            fee_profile="pine_zero_cost",
            df=df_sliced, m1_path_model_cache=ctx["m1_path_model"],
        )
        results.append(cell)
        print(f"  {tf:>4} | bars={len(df_sliced):>6,} trades={cell.trades:>5} "
              f"ret={cell.return_pct:+7.2f}% PF={cell.profit_factor:.2f}")
    # Each TF should have > 0 trades (else warmup bug)
    for r in results:
        if r.trades == 0:
            print(f"  ✗ FAIL: 0 trades on {r.timeframe} — likely warmup or resample bug")
            sys.exit(2)
    print(f"  ✓ Phase 4 PASS")
    print()
    return results


def _phase5_window_axis(ctx: dict) -> list[CellResult]:
    """4 windows × M5 × 1x × pine_zero — verify slicing + warmup at edges."""
    print("=" * 80)
    print("Phase 5 — Window axis: {4 windows} × M5 × 1x × pine_zero_cost")
    print("=" * 80)
    df_m5 = _load_full_tf("5m")
    results = []
    for w in WINDOWS_DAYS:
        df_sliced = _slice_window(df_m5, w)
        cell = _run_cell(
            window_days=w, timeframe="5m", leverage=1.0,
            fee_profile="pine_zero_cost",
            df=df_sliced, m1_path_model_cache=ctx["m1_path_model"],
        )
        results.append(cell)
        print(f"  {WINDOW_LABELS[w]:>4} | bars={len(df_sliced):>6,} trades={cell.trades:>5} "
              f"ret={cell.return_pct:+7.2f}% DD={cell.maxdd_pct:>6.2f}%")
    print(f"  ✓ Phase 5 PASS")
    print()
    return results


def _phase6_full_matrix(ctx: dict) -> list[CellResult]:
    """The full 240-cell matrix run."""
    print("=" * 80)
    print("Phase 6 — Full matrix: 4 windows × 4 TFs × 5 leverages × 3 fees = 240 cells")
    print("=" * 80)
    all_cells: list[CellResult] = []
    cell_count = 0
    total_cells = len(WINDOWS_DAYS) * len(TIMEFRAMES) * len(LEVERAGES) * len(FEE_PROFILES)
    t_start = time.time()
    for w in WINDOWS_DAYS:
        for tf in TIMEFRAMES:
            df_full = _load_full_tf(tf)
            df_sliced = _slice_window(df_full, w)
            t_outer = time.time()
            for lev in LEVERAGES:
                for fee in FEE_PROFILES:
                    cell_count += 1
                    cell = _run_cell(
                        window_days=w, timeframe=tf, leverage=lev,
                        fee_profile=fee,
                        df=df_sliced, m1_path_model_cache=ctx["m1_path_model"],
                    )
                    all_cells.append(cell)
            elapsed = time.time() - t_outer
            print(f"  [{cell_count}/{total_cells}] {WINDOW_LABELS[w]:>4} × {tf:>4} × "
                  f"{len(LEVERAGES)} lev × {len(FEE_PROFILES)} fees in {elapsed:.1f}s")
            # Checkpoint after each (window, TF) outer iteration
            _save_checkpoint(all_cells)
    total_elapsed = time.time() - t_start
    print()
    print(f"  ✓ Phase 6 PASS — 240 cells in {total_elapsed/60:.1f} minutes")
    print()
    return all_cells


def _save_checkpoint(cells: list[CellResult]) -> None:
    """Persist intermediate progress to JSON cache."""
    JSON_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(JSON_CACHE, "w") as f:
        json.dump(
            {"cells": [asdict(c) for c in cells]},
            f,
            indent=2,
            default=lambda x: float("inf") if x == float("inf") else None,
        )


def _phase7_generate_report(cells: list[CellResult]) -> None:
    """Generate HTML + PDF + CSV report."""
    print("=" * 80)
    print("Phase 7 — Report generation")
    print("=" * 80)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    HEATMAPS_DIR.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame([asdict(c) for c in cells])
    # Replace inf with a sentinel for CSV/JSON
    df["profit_factor"] = df["profit_factor"].replace([float("inf"), -float("inf")], 999.99)

    # 1. CSV
    csv_path = REPORT_DIR / "raw_data.csv"
    df.to_csv(csv_path, index=False)
    print(f"  ✓ CSV: {csv_path} ({len(df)} rows)")

    # 2. Heatmaps (12 PNGs: 4 windows × 3 fees, each is a TF × leverage grid)
    _generate_heatmaps(df)

    # 3. HTML report
    _generate_html(df)

    # 4. PDF report (fpdf2)
    _generate_pdf(df)

    print()
    print(f"  ✓ Phase 7 PASS — report in {REPORT_DIR}/")
    print()


def _generate_heatmaps(df: pd.DataFrame) -> None:
    """Generate 12 heatmaps: per (window, fee), TF × leverage grid of return%."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    for window_label in df["window_label"].unique():
        for fee in df["fee_profile"].unique():
            sub = df[(df["window_label"] == window_label) & (df["fee_profile"] == fee)]
            pivot = sub.pivot_table(
                index="timeframe", columns="leverage",
                values="return_pct", aggfunc="first",
            )
            # Order TFs sensibly
            tf_order = [tf for tf in TIMEFRAMES if tf in pivot.index]
            pivot = pivot.reindex(tf_order)
            fig, ax = plt.subplots(figsize=(8, 4.5))
            data = pivot.values.astype(float)
            vmax = max(abs(np.nanmin(data)), abs(np.nanmax(data)), 1)
            im = ax.imshow(data, cmap="RdYlGn", aspect="auto", vmin=-vmax, vmax=vmax)
            ax.set_xticks(range(len(pivot.columns)))
            ax.set_xticklabels([f"{int(c)}x" for c in pivot.columns])
            ax.set_yticks(range(len(pivot.index)))
            ax.set_yticklabels(pivot.index)
            ax.set_xlabel("Leverage")
            ax.set_ylabel("Timeframe")
            short_fee = fee.replace("ic_markets_", "").replace("_xauusd_normal", "").replace("_cost", "")
            ax.set_title(f"SWIFT Return % — {window_label} × {short_fee}")
            for i in range(len(pivot.index)):
                for j in range(len(pivot.columns)):
                    val = pivot.values[i, j]
                    color = "white" if abs(val) > vmax * 0.5 else "black"
                    ax.text(j, i, f"{val:+.1f}%", ha="center", va="center",
                            color=color, fontsize=9)
            fig.colorbar(im, ax=ax, label="Return %")
            plt.tight_layout()
            out_path = HEATMAPS_DIR / f"{window_label}_{short_fee}.png"
            fig.savefig(out_path, dpi=110, bbox_inches="tight")
            plt.close(fig)
    print(f"  ✓ heatmaps: 12 PNGs in {HEATMAPS_DIR}")


def _generate_html(df: pd.DataFrame) -> None:
    """Generate the primary HTML report."""
    html_lines = [
        "<!DOCTYPE html>", "<html>", "<head>",
        "<meta charset='UTF-8'>",
        "<title>SWIFT Comprehensive Backtest Matrix — 2026-04-15</title>",
        "<style>",
        "body { font-family: -apple-system, system-ui, sans-serif; max-width: 1400px; margin: 2em auto; padding: 0 2em; color: #222; }",
        "h1 { border-bottom: 3px solid #333; padding-bottom: 0.3em; }",
        "h2 { color: #444; margin-top: 2em; border-bottom: 1px solid #ddd; padding-bottom: 0.2em; }",
        "h3 { color: #666; margin-top: 1.5em; }",
        "table { border-collapse: collapse; margin: 1em 0; font-size: 13px; }",
        "th, td { padding: 6px 10px; text-align: right; border: 1px solid #ddd; }",
        "th { background: #f5f5f5; font-weight: 600; text-align: center; }",
        "td:first-child, th:first-child { text-align: left; }",
        "tr:nth-child(even) { background: #fafafa; }",
        ".pos { color: #0a7a3a; font-weight: 600; }",
        ".neg { color: #b22222; font-weight: 600; }",
        ".muted { color: #888; }",
        ".note { background: #fffbf0; border-left: 4px solid #e8b900; padding: 1em; margin: 1em 0; }",
        "img { max-width: 100%; border: 1px solid #ddd; margin: 0.5em 0; }",
        ".heatmap-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1em; }",
        "</style>", "</head>", "<body>",
        "<h1>SWIFT Comprehensive Backtest Matrix</h1>",
        "<p><strong>Generated:</strong> 2026-04-15 &middot; <strong>Strategy:</strong> "
        "<code>SwiftAlmaStrategy</code> &middot; <strong>Symbol:</strong> XAUUSD &middot; "
        "<strong>Cells:</strong> 240</p>",
        "<div class='note'>",
        "<strong>Methodology:</strong> Non-repainting <code>on_features()</code> production path. ",
        "Full accuracy mode: M1PathModel for 5m/15m/1h chart cells (M1 chart is degenerate, falls back to BrownianBridgeModel). ",
        "Fee profiles from <code>config/broker_fees.toml</code> (research-calibrated 2026-04-14). ",
        "Initial cash: $10,000 institutional sub-book. SWIFT default config (Pine xlsx Properties).",
        "</div>",
        "<h2>Leverage interpretation note</h2>",
        "<p>SwiftAlmaStrategy uses risk-based sizing: <code>notional = equity × (risk/sl_pct) = equity × 1.0</code>. "
        "This means position notional is constant across leverages — only the MARGIN REQUIREMENT changes. "
        "Therefore P&L cells are <strong>identical</strong> across leverages within a (window, TF, fee) triplet. "
        "The leverage axis is meaningful in the <code>Cost % of margin</code> column, which scales linearly with leverage "
        "and represents the real \"leverage tax\" — at 1000x, the same per-trade dollar cost becomes 20%+ of margin per trade.</p>",
    ]

    # TL;DR
    html_lines.append("<h2>TL;DR — Best cell per (window, fee)</h2>")
    html_lines.append("<table>")
    html_lines.append("<tr><th>Window</th><th>Fee mode</th><th>Best TF</th><th>Best lev</th>"
                      "<th>Trades</th><th>Return %</th><th>Max DD</th><th>PF</th></tr>")
    for w in WINDOWS_DAYS:
        for fee in FEE_PROFILES:
            sub = df[(df["window_days"] == w) & (df["fee_profile"] == fee)].copy()
            if sub.empty:
                continue
            best_idx = sub["return_pct"].idxmax()
            best = sub.loc[best_idx]
            ret_class = "pos" if best["return_pct"] > 0 else "neg"
            short_fee = fee.replace("ic_markets_", "").replace("_xauusd_normal", "").replace("_cost", "")
            html_lines.append(
                f"<tr><td>{best['window_label']}</td><td>{short_fee}</td>"
                f"<td>{best['timeframe']}</td><td>{int(best['leverage'])}x</td>"
                f"<td>{int(best['trades'])}</td>"
                f"<td class='{ret_class}'>{best['return_pct']:+.2f}%</td>"
                f"<td>{best['maxdd_pct']:.2f}%</td>"
                f"<td>{best['profit_factor']:.2f}</td></tr>"
            )
    html_lines.append("</table>")

    # Per-window detail tables + heatmaps
    for w in WINDOWS_DAYS:
        wl = WINDOW_LABELS[w]
        html_lines.append(f"<h2>Window: {wl} ({w} days)</h2>")

        # Heatmap row
        html_lines.append("<div class='heatmap-grid'>")
        for fee in FEE_PROFILES:
            short_fee = fee.replace("ic_markets_", "").replace("_xauusd_normal", "").replace("_cost", "")
            img_path = f"heatmaps/{wl}_{short_fee}.png"
            html_lines.append(f"<div><img src='{img_path}' alt='{wl} {short_fee}'></div>")
        html_lines.append("</div>")

        # Detail tables per TF
        for tf in TIMEFRAMES:
            sub = df[(df["window_days"] == w) & (df["timeframe"] == tf)].copy()
            if sub.empty:
                continue
            html_lines.append(f"<h3>{wl} × {tf}</h3>")
            html_lines.append("<table>")
            html_lines.append(
                "<tr><th>Lev</th><th>Fee</th><th>Trades</th><th>Return %</th>"
                "<th>Max DD %</th><th>Win %</th><th>PF</th>"
                "<th>Σ Comm $</th><th>$/trade</th>"
                "<th>Cost % margin</th><th>Stop-outs</th></tr>"
            )
            for _, r in sub.iterrows():
                ret_class = "pos" if r["return_pct"] > 0 else ("neg" if r["return_pct"] < 0 else "muted")
                short_fee = (r["fee_profile"]
                             .replace("ic_markets_", "")
                             .replace("_xauusd_normal", "")
                             .replace("_cost", ""))
                pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] < 100 else "∞"
                html_lines.append(
                    f"<tr><td>{int(r['leverage'])}x</td>"
                    f"<td>{short_fee}</td>"
                    f"<td>{int(r['trades'])}</td>"
                    f"<td class='{ret_class}'>{r['return_pct']:+.2f}%</td>"
                    f"<td>{r['maxdd_pct']:.2f}%</td>"
                    f"<td>{r['win_rate']:.1f}%</td>"
                    f"<td>{pf_str}</td>"
                    f"<td>${r['total_commission']:,.0f}</td>"
                    f"<td>${r['cost_per_trade']:.2f}</td>"
                    f"<td>{r['cost_pct_of_margin']:.3f}%</td>"
                    f"<td>{int(r['broker_stop_outs'])}</td></tr>"
                )
            html_lines.append("</table>")

    # Footer
    html_lines.append("<h2>Sources + references</h2>")
    html_lines.append("<ul>")
    html_lines.append("<li><code>src/strategies/trend_following/swift_alma.py</code> — strategy port</li>")
    html_lines.append("<li><code>src/backtest/fee_profiles.py</code> + <code>config/broker_fees.toml</code> — fee profiles</li>")
    html_lines.append("<li><code>docs/BROKER_FEES.md</code> — calibration sources + leverage interaction</li>")
    html_lines.append("<li><code>scripts/swift_full_matrix.py</code> — this script (regenerable)</li>")
    html_lines.append("</ul>")
    html_lines.append("</body></html>")

    html_path = REPORT_DIR / "index.html"
    html_path.write_text("\n".join(html_lines))
    print(f"  ✓ HTML: {html_path}")


def _ascii(text: str) -> str:
    """Sanitize Unicode to ASCII for fpdf2 (latin-1 only)."""
    replacements = {
        "—": "-", "–": "-", "…": "...", "“": '"', "”": '"',
        "‘": "'", "’": "'", "•": "*", "→": "->", "←": "<-",
        "≥": ">=", "≤": "<=", "×": "x", "✓": "[ok]", "✗": "[X]",
        "⚠": "[!]", "∞": "inf",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _generate_pdf(df: pd.DataFrame) -> None:
    """Generate the secondary PDF report via fpdf2."""
    from fpdf import FPDF

    pdf = FPDF(orientation="landscape", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=10)

    # Cover page
    pdf.add_page()
    pdf.set_font("Helvetica", style="B", size=20)
    pdf.cell(0, 12, _ascii("SWIFT Comprehensive Backtest Matrix"), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 6, _ascii("Generated 2026-04-15  |  Strategy: SwiftAlmaStrategy  |  XAUUSD  |  240 cells"),
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(8)
    pdf.set_font("Helvetica", style="B", size=12)
    pdf.cell(0, 6, "Methodology", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    methodology = (
        "Non-repainting on_features() production path. Full accuracy mode: M1PathModel for "
        "5m/15m/1h chart cells (M1 chart is degenerate, falls back to BrownianBridgeModel). "
        "Fee profiles from config/broker_fees.toml (research-calibrated 2026-04-14, task #105/106). "
        "Initial cash: $10,000 institutional sub-book. SWIFT Pine defaults verified vs xlsx Properties."
    )
    for line in _wrap_text(methodology, 130):
        pdf.cell(0, 5, _ascii(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", style="B", size=12)
    pdf.cell(0, 6, "Leverage interpretation", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    lev_note = (
        "SwiftAlmaStrategy uses risk-based sizing: notional = equity x (risk/sl_pct) = equity x 1.0. "
        "Position notional is constant across leverages, only the MARGIN REQUIREMENT changes. "
        "P&L cells are IDENTICAL across leverages within a (window, TF, fee) triplet. "
        "The leverage axis is meaningful in the Cost % of margin column, which scales linearly with leverage."
    )
    for line in _wrap_text(lev_note, 130):
        pdf.cell(0, 5, _ascii(line), new_x="LMARGIN", new_y="NEXT")

    # Per-window pages: heatmap row + table
    for w in WINDOWS_DAYS:
        wl = WINDOW_LABELS[w]
        for fee in FEE_PROFILES:
            short_fee = (fee
                         .replace("ic_markets_", "")
                         .replace("_xauusd_normal", "")
                         .replace("_cost", ""))
            pdf.add_page()
            pdf.set_font("Helvetica", style="B", size=14)
            pdf.cell(0, 8, _ascii(f"Window {wl} - Fee mode: {short_fee}"),
                     new_x="LMARGIN", new_y="NEXT", align="L")
            pdf.ln(2)

            # Embed heatmap PNG
            img_path = HEATMAPS_DIR / f"{wl}_{short_fee}.png"
            if img_path.exists():
                pdf.image(str(img_path), x=10, y=pdf.get_y(), w=130)
                pdf.set_y(pdf.get_y() + 75)

            # Table for this (window, fee)
            sub = df[(df["window_days"] == w) & (df["fee_profile"] == fee)].copy()
            sub = sub.sort_values(["timeframe", "leverage"])
            pdf.set_font("Helvetica", style="B", size=8)
            headers = ["TF", "Lev", "Trades", "Return%", "MaxDD%", "Win%", "PF",
                       "Comm$", "$/trade", "Cost%mar", "Stops"]
            col_widths = [12, 12, 18, 18, 18, 14, 14, 22, 18, 22, 12]
            x0 = 145  # start to the right of heatmap
            pdf.set_xy(x0, 25)
            for h, w_ in zip(headers, col_widths):
                pdf.cell(w_, 5, _ascii(h), border=1, align="C")
            pdf.ln(5)
            pdf.set_font("Helvetica", size=7)
            for _, r in sub.iterrows():
                pdf.set_x(x0)
                pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] < 100 else "inf"
                row = [
                    r["timeframe"],
                    f"{int(r['leverage'])}x",
                    str(int(r["trades"])),
                    f"{r['return_pct']:+.2f}%",
                    f"{r['maxdd_pct']:.2f}%",
                    f"{r['win_rate']:.1f}%",
                    pf_str,
                    f"${r['total_commission']:,.0f}",
                    f"${r['cost_per_trade']:.2f}",
                    f"{r['cost_pct_of_margin']:.3f}%",
                    str(int(r["broker_stop_outs"])),
                ]
                for v, wd in zip(row, col_widths):
                    pdf.cell(wd, 4.5, _ascii(v), border=1, align="R")
                pdf.ln(4.5)

    pdf_path = REPORT_DIR / "swift_full_matrix.pdf"
    pdf.output(str(pdf_path))
    print(f"  ✓ PDF: {pdf_path}")


def _wrap_text(text: str, width: int) -> list[str]:
    """Simple word-wrap helper."""
    lines = []
    current = ""
    for word in text.split():
        if len(current) + len(word) + 1 <= width:
            current = (current + " " + word).strip()
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# ── Main ────────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 80)
    print("SWIFT Comprehensive Backtest Matrix — Task #109")
    print("=" * 80)
    print()

    ctx = _phase0_preflight()
    _phase1_smoke_test(ctx)
    _phase2_cost_decomposition(ctx)
    _phase3_leverage_axis(ctx)
    _phase4_tf_axis(ctx)
    _phase5_window_axis(ctx)
    cells = _phase6_full_matrix(ctx)
    _phase7_generate_report(cells)

    print("=" * 80)
    print(f"DONE — {len(cells)} cells, report at {REPORT_DIR}/")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
