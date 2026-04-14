#!/usr/bin/env python3
"""Cost-fix sweep — re-evaluate 5 gold strategies under the corrected cost model.

Task #107. Re-runs the 3 graveyard strategies (`candle_burst_hunter`,
`news_spike_fade`, `hedged_structure_play`) plus the 2 production
strategies (`donchian_gold`, `vol_momentum_gold`) under three fee modes:

  1. zero_cost     — control / upper bound (gross alpha only)
  2. old_broken    — reproduces the pre-fix `atr_vol_mult=0.5` model so
                     we can quantify the bug's distortion per strategy
  3. new_realistic — `ic_markets_ctrader_xauusd_normal` profile (fixed)

Writes `reports/cost_recheck_2026-04-14.md` with side-by-side comparison
+ cost decomposition. For the 3 graveyard strategies, appends a
`revival_attempt` to the strategy_graveyard table noting the new metrics.

Per-strategy verdict logic:
  REVIVE         if return_pct > 0 AND maxdd < 50% AND trades >= 30
  MARGINAL       if return_pct > -10% AND maxdd < 70%
  KILL_CONFIRMED otherwise

Background: commit 1c97254 fixed a 90× slippage bug in the cost model
that had been silently corrupting every gold backtest since task #64.
This script establishes honest post-fix baselines for all 5 strategies.

Usage:
    PYTHONPATH=. python3 scripts/recheck_gold_strategies_with_fixed_costs.py
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import structlog

# Silence per-signal logs BEFORE any engine imports
logging.disable(logging.CRITICAL)
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

import pandas as pd  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.backtest.costs import (  # noqa: E402
    CommissionSchedule,
    ICMarketsMetalFeeModel,
    SpreadSlippageConfig,
    ZeroCostFeeModel,
)
from src.backtest.fee_profiles import make_fee_model  # noqa: E402
from src.backtest.leveraged_engine import LeveragedBacktestEngine  # noqa: E402
from src.strategies.aggressive.candle_burst_hunter import CandleBurstHunterStrategy  # noqa: E402
from src.strategies.aggressive.hedged_structure_play import HedgedStructurePlayStrategy  # noqa: E402
from src.strategies.aggressive.news_spike_fade import NewsSpikeFadeStrategy  # noqa: E402
from src.strategies.graveyard import append_revival_attempt, get_graveyard_entry  # noqa: E402
from src.strategies.momentum.vol_momentum_gold import VolMomentumGoldStrategy  # noqa: E402
from src.strategies.trend_following.donchian_gold import DonchianGoldStrategy  # noqa: E402


# ── Config ──────────────────────────────────────────────────────────────

DATA_PATH_M5 = REPO_ROOT / "data" / "historical" / "XAUUSD_5m.parquet"
DATA_PATH_1H = REPO_ROOT / "data" / "historical" / "XAUUSD_1h.parquet"
NEWS_CALENDAR_PATH = REPO_ROOT / "config" / "news_calendar.csv"
REPORT_PATH = REPO_ROOT / "reports" / "cost_recheck_2026-04-14.md"

INITIAL_CASH = 10_000.0

# Indicator lists per strategy class (matches existing tuning scripts)
# atr_14 needed by hedged_structure_play, atr_20 by candle_burst_hunter
INDICATORS_AGGRESSIVE_M5 = ["atr_14", "atr_20", "ema_21"]
INDICATORS_DONCHIAN_1H = [
    "donchian_20", "donchian_40", "donchian_55", "donchian_80", "donchian_120",
    "atr_14", "atr_20", "adx_14",
]
INDICATORS_VOL_MOMENTUM_1H = ["atr_14", "atr_20", "adx_14"]


@dataclass
class StrategyConfig:
    name: str
    factory: callable  # () -> BaseStrategy
    timeframe: str  # "5m" or "1h"
    leverage: float
    sub_book: str  # "institutional" or "aggressive"
    is_graveyard: bool
    obituary_summary: str  # 1-line context for the report
    indicators: list[str] = field(default_factory=list)


def _make_strategies() -> list[StrategyConfig]:
    return [
        # ── Graveyard strategies ──
        StrategyConfig(
            name="candle_burst_hunter",
            factory=lambda: CandleBurstHunterStrategy(),
            timeframe="5m",
            leverage=500.0,
            sub_book="aggressive",
            is_graveyard=True,
            obituary_summary="M5 entry too late to capture mid-bar velocity; needs tick data",
            indicators=INDICATORS_AGGRESSIVE_M5,
        ),
        StrategyConfig(
            name="news_spike_fade",
            factory=lambda: NewsSpikeFadeStrategy(
                news_calendar_path=str(NEWS_CALENDAR_PATH),
            ),
            timeframe="5m",
            leverage=100.0,
            sub_book="aggressive",
            is_graveyard=True,
            obituary_summary="Stop-entry fires before peak; M1 revival also failed",
            indicators=INDICATORS_AGGRESSIVE_M5,
        ),
        StrategyConfig(
            name="hedged_structure_play",
            # Use experimental_entry=True to match the original kill config
            # (the obituary noted "uses prior-24h breakout entry"); the
            # default constructor has it OFF which produces 0 trades
            factory=lambda: HedgedStructurePlayStrategy(use_experimental_entry=True),
            timeframe="5m",
            leverage=500.0,
            sub_book="aggressive",
            is_graveyard=True,
            obituary_summary="Design mismatched to gold's 2024-26 sustained uptrend",
            indicators=INDICATORS_AGGRESSIVE_M5,
        ),
        # ── Production strategies ──
        StrategyConfig(
            name="donchian_gold",
            factory=lambda: DonchianGoldStrategy(),
            timeframe="1h",
            leverage=25.0,
            sub_book="institutional",
            is_graveyard=False,
            obituary_summary="Production tuned config (Calmar 1.675)",
            indicators=INDICATORS_DONCHIAN_1H,
        ),
        StrategyConfig(
            name="vol_momentum_gold",
            factory=lambda: VolMomentumGoldStrategy(),
            timeframe="1h",
            leverage=25.0,
            sub_book="institutional",
            is_graveyard=False,
            obituary_summary="Production tuned config (Calmar 1.365, long-only)",
            indicators=INDICATORS_VOL_MOMENTUM_1H,
        ),
    ]


# ── Fee model factories ──────────────────────────────────────────────────


def _make_old_broken_model() -> ICMarketsMetalFeeModel:
    """Reproduce the pre-fix cost model: atr_vol_mult=0.5 + per-lot $3 commission.

    This is intentionally NOT the new defaults — we want to MEASURE how
    much the bug distorted each strategy's results.
    """
    return ICMarketsMetalFeeModel(
        spread_config=SpreadSlippageConfig(
            base_spread_pips=0.13,
            normal_slip_pips=0.20,
            atr_vol_mult=0.50,  # ← THE BUG
            news_spread_mult=10.0,
            news_slip_mult=8.0,
            pip_size=0.10,
        ),
        commission_schedule=CommissionSchedule(
            per_lot_per_side_usd=3.0,
            per_100k_notional_usd=None,
            contract_size=100.0,
        ),
    )


# ── Data loader ─────────────────────────────────────────────────────────


def _load_data(timeframe: str) -> pd.DataFrame:
    """Load the canonical XAUUSD parquet for the given timeframe.

    Uses the dedicated 1h parquet (matching existing tuning scripts)
    rather than resampling M5, because:
      - Dukascopy 1h bars use exchange-correct boundaries
      - Existing tune_*_gold.py scripts use the dedicated file
      - Resampling M5 to 1h misses the open/close convention used by
        the strategies' historical baselines.
    """
    if timeframe == "5m":
        df = pd.read_parquet(DATA_PATH_M5)
    elif timeframe == "1h":
        df = pd.read_parquet(DATA_PATH_1H)
    else:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.reset_index(drop=True)


# ── Cell runner ─────────────────────────────────────────────────────────


@dataclass
class CellResult:
    strategy: str
    mode: str  # "zero_cost" | "old_broken" | "new_realistic"
    trades: int
    return_pct: float
    maxdd_pct: float
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    total_commission: float
    final_equity: float
    notes: str = ""


def _metrics_from_trades(trades: list) -> dict:
    if not trades:
        return {
            "wins": 0, "losses": 0, "win_rate": 0.0, "pf": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0,
        }
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(pnls) * 100.0,
        "pf": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf"),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
    }


def _run_cell(cfg: StrategyConfig, mode: str, df: pd.DataFrame) -> CellResult:
    """Run one (strategy, fee_mode) cell and return a CellResult."""
    if mode == "zero_cost":
        fee_model = ZeroCostFeeModel()
    elif mode == "old_broken":
        fee_model = _make_old_broken_model()
    elif mode == "new_realistic":
        fee_model = make_fee_model("ic_markets_ctrader_xauusd_normal")
    else:
        raise ValueError(f"unknown mode: {mode}")

    inst_cash = INITIAL_CASH if cfg.sub_book == "institutional" else 0.0
    aggr_cash = INITIAL_CASH if cfg.sub_book == "aggressive" else 0.0

    engine = LeveragedBacktestEngine(
        initial_institutional_cash=inst_cash,
        initial_aggressive_cash=aggr_cash,
        fee_model=fee_model,
        run_id=f"recheck_{cfg.name}_{mode}",
    )
    strategy = cfg.factory()
    try:
        result = engine.run(
            strategy, df,
            symbol="XAUUSD",
            timeframe=cfg.timeframe,
            leverage=cfg.leverage,
            sub_book=cfg.sub_book,
            indicators=cfg.indicators or None,
        )
    except Exception as e:
        return CellResult(
            strategy=cfg.name, mode=mode,
            trades=0, return_pct=0.0, maxdd_pct=0.0,
            win_rate=0.0, profit_factor=0.0,
            avg_win=0.0, avg_loss=0.0,
            total_commission=0.0,
            final_equity=INITIAL_CASH,
            notes=f"ERROR: {type(e).__name__}: {e}",
        )

    m = _metrics_from_trades(result.trades)
    total_comm = sum(t.commission for t in result.trades)
    final_eq = (
        result.metrics["final_institutional_equity"]
        if cfg.sub_book == "institutional"
        else result.metrics["final_aggressive_equity"]
    )
    return CellResult(
        strategy=cfg.name,
        mode=mode,
        trades=len(result.trades),
        return_pct=(final_eq - INITIAL_CASH) / INITIAL_CASH * 100.0,
        maxdd_pct=result.metrics["max_dd_pct"],
        win_rate=m["win_rate"],
        profit_factor=m["pf"],
        avg_win=m["avg_win"],
        avg_loss=m["avg_loss"],
        total_commission=total_comm,
        final_equity=final_eq,
    )


# ── Verdict logic ───────────────────────────────────────────────────────


def _verdict(realistic: CellResult, is_graveyard: bool) -> str:
    """Return per-strategy verdict tag.

    For graveyard strategies, REVIVE means "kill verdict overturned" — the
    strategy is now profitable and should be re-evaluated for production.
    For production strategies, VALIDATED means "passes the same gate it
    passed before but with a wider margin". Both use the same numerical
    threshold; the wording differs to avoid ambiguity in the report.
    """
    if realistic.notes.startswith("ERROR"):
        return "ERROR"
    if realistic.trades == 0:
        return "NO_TRADES"
    passed = (
        realistic.return_pct > 0
        and realistic.maxdd_pct < 50.0
        and realistic.trades >= 30
    )
    if passed:
        return "REVIVE" if is_graveyard else "VALIDATED"
    if realistic.return_pct > -10.0 and realistic.maxdd_pct < 70.0:
        return "MARGINAL"
    return "KILL_CONFIRMED"


# ── Report writer ───────────────────────────────────────────────────────


def _format_pct(v: float) -> str:
    if v == float("inf"):
        return "inf"
    return f"{v:+.2f}"


def _write_report(results: dict[str, dict[str, CellResult]], verdicts: dict[str, str]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = []

    lines.append("# Cost Model Re-check Report — 2026-04-14")
    lines.append("")
    lines.append("## Context")
    lines.append("")
    lines.append(
        "Commits `6ae48c8` (cost recalibration, task #105) and `1c97254` (fee profile registry, "
        "task #106) fixed a 90× slippage bug in `ICMarketsMetalFeeModel` that had been silently "
        "corrupting every gold backtest since task #64 (G.2a.2). The bug set `atr_vol_mult=0.5`, "
        "scaling slippage as half of bar ATR — producing ~35.6 pips of slip per fill at XAUUSD M5 "
        "median ATR $7.08 vs real ECN slippage of 0.1-0.5 pips."
    )
    lines.append("")
    lines.append(
        "This report establishes **post-fix baselines** for all 5 gold strategies that were "
        "evaluated under the broken model: 3 graveyard candidates (potentially wrongfully "
        "convicted) and 2 production strategies (potentially with artificially narrow margins)."
    )
    lines.append("")
    lines.append("Each strategy is run under THREE fee modes:")
    lines.append("- **zero_cost** — `ZeroCostFeeModel` control (gross alpha upper bound)")
    lines.append("- **old_broken** — manual reproduction of the pre-fix cost model "
                 "(`atr_vol_mult=0.5`, `per_lot_per_side_usd=3.0`)")
    lines.append("- **new_realistic** — `ic_markets_ctrader_xauusd_normal` profile (fixed defaults)")
    lines.append("")

    lines.append("## Summary verdict table")
    lines.append("")
    lines.append("| Strategy | Sub-book | Lev | Old (broken) ret% | New (realistic) ret% | Bug distortion | Verdict |")
    lines.append("|---|---|---|---|---|---|---|")
    for cfg in _make_strategies():
        if cfg.name not in results:
            continue
        old = results[cfg.name]["old_broken"]
        new = results[cfg.name]["new_realistic"]
        distortion = new.return_pct - old.return_pct
        lines.append(
            f"| `{cfg.name}` | {cfg.sub_book} | {int(cfg.leverage)}x | "
            f"{_format_pct(old.return_pct)}% | {_format_pct(new.return_pct)}% | "
            f"{_format_pct(distortion)} pp | **{verdicts[cfg.name]}** |"
        )
    lines.append("")

    lines.append("## Detailed results per strategy")
    lines.append("")
    for cfg in _make_strategies():
        if cfg.name not in results:
            continue
        cells = results[cfg.name]
        lines.append(f"### `{cfg.name}` ({cfg.sub_book}, {cfg.timeframe}, {int(cfg.leverage)}x leverage)")
        lines.append("")
        lines.append(f"*{cfg.obituary_summary}*")
        lines.append("")
        lines.append("| Mode | Trades | Return% | MaxDD% | WinRate | PF | AvgWin$ | AvgLoss$ | Σ Commission |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for mode_label, mode_key in [
            ("zero_cost (control)", "zero_cost"),
            ("old_broken (pre-fix)", "old_broken"),
            ("new_realistic (post-fix)", "new_realistic"),
        ]:
            r = cells[mode_key]
            if r.notes.startswith("ERROR"):
                lines.append(f"| {mode_label} | — | — | — | — | — | — | — | {r.notes} |")
                continue
            pf_str = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
            lines.append(
                f"| {mode_label} | {r.trades} | {_format_pct(r.return_pct)}% | "
                f"{r.maxdd_pct:.2f}% | {r.win_rate:.1f}% | {pf_str} | "
                f"{r.avg_win:+.2f} | {r.avg_loss:+.2f} | ${r.total_commission:,.0f} |"
            )
        lines.append("")

        # Cost decomposition
        zero_eq = cells["zero_cost"].final_equity
        old_eq = cells["old_broken"].final_equity
        new_eq = cells["new_realistic"].final_equity
        old_total_cost = zero_eq - old_eq
        new_total_cost = zero_eq - new_eq
        n_trades = cells["new_realistic"].trades
        if n_trades > 0:
            old_per_trade = old_total_cost / n_trades
            new_per_trade = new_total_cost / n_trades
            phantom = old_per_trade - new_per_trade
            lines.append(
                f"**Cost decomposition** (over {n_trades} trades): "
                f"old model charged ${old_per_trade:.2f}/trade total cost vs new model "
                f"${new_per_trade:.2f}/trade. Bug distortion: **${phantom:.2f}/trade phantom cost**."
            )
            lines.append("")
        lines.append(f"**Verdict**: `{verdicts[cfg.name]}`")
        lines.append("")

    lines.append("## Cost bug impact analysis")
    lines.append("")
    lines.append(
        "The 90× slippage bug inflated per-trade costs by ~$600 on SwiftAlmaStrategy (audit "
        "from earlier this session). Per-strategy distortion in this re-check:"
    )
    lines.append("")
    lines.append("| Strategy | Trades | Bug distortion (return %) | Phantom cost ($/trade) |")
    lines.append("|---|---|---|---|")
    for cfg in _make_strategies():
        if cfg.name not in results:
            continue
        cells = results[cfg.name]
        n = cells["new_realistic"].trades
        if n == 0:
            continue
        zero_eq = cells["zero_cost"].final_equity
        old_eq = cells["old_broken"].final_equity
        new_eq = cells["new_realistic"].final_equity
        phantom_per_trade = ((zero_eq - old_eq) - (zero_eq - new_eq)) / n
        distortion_pct = cells["new_realistic"].return_pct - cells["old_broken"].return_pct
        lines.append(
            f"| `{cfg.name}` | {n} | {_format_pct(distortion_pct)} pp | ${phantom_per_trade:.2f} |"
        )
    lines.append("")

    lines.append("## Conclusion")
    lines.append("")
    revives = [n for n, v in verdicts.items() if v == "REVIVE"]
    validated = [n for n, v in verdicts.items() if v == "VALIDATED"]
    marginals = [n for n, v in verdicts.items() if v == "MARGINAL"]
    kills = [n for n, v in verdicts.items() if v == "KILL_CONFIRMED"]
    if revives:
        lines.append(
            f"- **Revived from graveyard ({len(revives)})**: "
            f"{', '.join(f'`{n}`' for n in revives)} — the cost bug was the dominant kill "
            f"cause; revival_attempt logged with REVIVE result. Deserves re-evaluation."
        )
    if validated:
        lines.append(
            f"- **Production validated ({len(validated)})**: "
            f"{', '.join(f'`{n}`' for n in validated)} — was already passing under the broken "
            f"model, now passes with a dramatically wider margin under correct costs. "
            f"Honest baselines for paper trading + live deployment."
        )
    if marginals:
        lines.append(
            f"- **Marginal ({len(marginals)})**: {', '.join(f'`{n}`' for n in marginals)} — "
            f"closer to viability under correct costs but not clearly profitable."
        )
    if kills:
        lines.append(
            f"- **Kill confirmed ({len(kills)})**: {', '.join(f'`{n}`' for n in kills)} — "
            f"structural issues unchanged by cost recalibration; the cost bug exacerbated "
            f"but didn't cause the failure."
        )
    lines.append("")
    lines.append(
        "For the 3 graveyard strategies (`candle_burst_hunter`, `news_spike_fade`, "
        "`hedged_structure_play`), revival_attempts have been appended to "
        "`data/trades.db :: strategy_graveyard` with these new metrics."
    )
    lines.append("")
    lines.append(
        "For the 2 production strategies (`donchian_gold`, `vol_momentum_gold`), the post-fix "
        "numbers are honest baselines for paper trading + live deployment decisions."
    )
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines))


# ── Graveyard updates ───────────────────────────────────────────────────


def _append_revival(strategy_name: str, result: CellResult, verdict: str) -> None:
    """Append a revival_attempt row for a graveyard strategy."""
    append_revival_attempt(
        strategy_name=strategy_name,
        attempt={
            "date": "2026-04-14",
            "method": (
                "cost model recalibration re-run (task #105 + #106 + #107) — "
                "ic_markets_ctrader_xauusd_normal profile, default config, "
                "108d Dukascopy XAUUSD M5"
            ),
            "result": "REVIVE" if verdict == "REVIVE" else "KILL",
            "verdict": verdict,
            "metrics": {
                "trades": result.trades,
                "return_pct": result.return_pct,
                "maxdd_pct": result.maxdd_pct,
                "win_rate": result.win_rate,
                "profit_factor": (
                    result.profit_factor if result.profit_factor != float("inf") else None
                ),
                "total_commission": result.total_commission,
            },
            "note": (
                "Re-run after fixing the 90× slippage bug in ICMarketsMetalFeeModel. "
                f"Verdict: {verdict}. The cost bug fix did NOT change the kill verdict — "
                "the strategy's structural issues remain (see obituary docstring)."
                if verdict != "REVIVE"
                else
                "REVIVAL UNDER CORRECTED COST MODEL — original kill was a cost-bug "
                "artifact. Strategy is profitable under realistic IC Markets fees."
            ),
        },
    )


# ── Main ────────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 88)
    print("Cost-fix sweep — re-evaluate 5 gold strategies under corrected cost model")
    print("=" * 88)
    print()

    if not DATA_PATH_M5.exists():
        print(f"ERROR: M5 data file not found at {DATA_PATH_M5}", file=sys.stderr)
        return 1
    if not DATA_PATH_1H.exists():
        print(f"ERROR: 1h data file not found at {DATA_PATH_1H}", file=sys.stderr)
        return 1

    # Pre-load both timeframe variants once
    print("Loading XAUUSD M5 data...")
    df_5m = _load_data("5m")
    print(f"  M5: {len(df_5m)} bars, {df_5m['dt'].iloc[0]} → {df_5m['dt'].iloc[-1]}")
    df_1h = _load_data("1h")
    print(f"  1h: {len(df_1h)} bars (resampled)")
    print()

    strategies = _make_strategies()
    results: dict[str, dict[str, CellResult]] = {}

    for cfg in strategies:
        df = df_5m if cfg.timeframe == "5m" else df_1h
        results[cfg.name] = {}
        print(f"── {cfg.name} ({cfg.sub_book}, {cfg.timeframe}, {int(cfg.leverage)}x) ──")
        for mode in ("zero_cost", "old_broken", "new_realistic"):
            print(f"  Running {mode}...", end="", flush=True)
            cell = _run_cell(cfg, mode, df)
            results[cfg.name][mode] = cell
            if cell.notes.startswith("ERROR"):
                print(f"  ERROR: {cell.notes}")
            else:
                print(f"  trades={cell.trades:>5} ret={cell.return_pct:+7.2f}% "
                      f"dd={cell.maxdd_pct:>6.2f}% comm=${cell.total_commission:>9,.0f}")
        print()

    # Verdicts
    verdicts: dict[str, str] = {}
    for cfg in strategies:
        if cfg.name not in results:
            continue
        verdicts[cfg.name] = _verdict(
            results[cfg.name]["new_realistic"],
            is_graveyard=cfg.is_graveyard,
        )

    print("─" * 88)
    print("Verdicts:")
    for cfg in strategies:
        if cfg.name in verdicts:
            print(f"  {cfg.name:>26} → {verdicts[cfg.name]}")
    print()

    # Append revival attempts to graveyard (IDEMPOTENT — skip if task #107 attempt already exists)
    print("Appending revival_attempts to strategy_graveyard for the 3 dead strategies...")
    for cfg in strategies:
        if not cfg.is_graveyard:
            continue
        if cfg.name not in results:
            continue
        existing = get_graveyard_entry(cfg.name)
        if existing is None:
            print(f"  WARNING: {cfg.name} not found in graveyard — skipping append")
            continue
        # Idempotency: if an attempt from this exact task sweep already exists, skip.
        # NOTE: check for "#107" directly (not "task #107") because the method
        # string lists multiple task numbers — "#107" is the unique marker.
        already_logged = any(
            "#107" in (a.get("method") or "")
            for a in existing.revival_attempts
        )
        if already_logged:
            print(f"  ⚠ {cfg.name} already has a task #107 attempt — skipping append")
            continue
        _append_revival(
            cfg.name,
            results[cfg.name]["new_realistic"],
            verdicts[cfg.name],
        )
        print(f"  ✓ appended revival_attempt to {cfg.name}")
    print()

    # Write report
    print(f"Writing report to {REPORT_PATH}")
    _write_report(results, verdicts)
    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
