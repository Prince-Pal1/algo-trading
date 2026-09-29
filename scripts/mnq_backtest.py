"""Deep backtest of the MNQ hybrid scalp, run the way the video describes it.

    python3 -m scripts.mnq_backtest --fee-profile cme_mnq_retail
    python3 -m scripts.mnq_backtest --fee-profile cme_mnq_zero      # idealised

Reads data/mnq/mnq_1m.parquet if present (5-minute candles are built from it,
and the 1-minute bars settle intrabar ordering), otherwise mnq_5m.parquet.
Fetch data first with scripts.mnq_data.

Four runs:
    video_long         long only, fixed 4 contracts           (video: PF 1.527)
    video_short        short only, fixed 4, no stop           (video: PF 1.413)
    video_short_daily  as above + previous-day-red filter     (video: 24:33)
    trader             both modules, 0.5% risk sizing, max-loss stop on the
                       short: the rules as the trader actually applies them

Each run is stored through ResultStore.save_run() (SQLite + JSON + HTML).
A summary comparing all four against the video's numbers goes to
reports/mnq_hybrid_summary.{md,json}.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from src.strategies.futures.mnq_hybrid import (
    RESOLVED, RESOLVED_1M, HEURISTIC, UNCERTAIN, TZ,
    MNQHybridConfig, by_period, make_futures_fee_model, module_stats,
    normalise, resample_1m_to_5m, simulate,
)

DATA_DIR = Path("data/mnq")

# The video's reported numbers, for the comparison table.
VIDEO = {
    "video_long": {"profit_factor": 1.527, "win_rate": 0.50, "max_drawdown_usd": 2614,
                   "source": "the video's long backtest, fixed 4 contracts; win rate 'slightly more than 50%'"},
    "video_short": {"profit_factor": 1.413, "win_rate": 0.52, "max_drawdown_usd": 1142,
                    "source": "22:42, fixed 4 contracts, no stop"},
    "video_short_daily": {"trades": 60, "source": "24:33-24:43, 'only 60' trades; no other numbers given"},
}

RUNS = [
    ("video_long", "Long, as in the video",
     dict(long_on=True, short_on=False, fixed_qty=4)),
    ("video_short", "Short, as in the video",
     dict(long_on=False, short_on=True, fixed_qty=4, short_stop_on=False)),
    ("video_short_daily", "Short + previous-day-red filter",
     dict(long_on=False, short_on=True, fixed_qty=4, short_stop_on=False, daily_filter=True)),
    ("trader", "As the trader trades (both, 0.5% sizing, max-loss stop)",
     dict(long_on=True, short_on=True, short_stop_on=True)),
]

BAND_SWEEP = [2.5, 2.8, 3.1, 3.4, 3.7]


def load_bars(start: str | None, end: str | None):
    m1_path, m5_path = DATA_DIR / "mnq_1m.parquet", DATA_DIR / "mnq_5m.parquet"
    if m1_path.exists():
        bars1 = normalise(pd.read_parquet(m1_path))
        source = str(m1_path)
    elif m5_path.exists():
        bars1, source = None, str(m5_path)
    else:
        sys.exit(f"  no data in {DATA_DIR}/. Fetch it first:  python3 -m scripts.mnq_data --help")

    def clip(df):
        if start:
            df = df[df["ts"] >= pd.Timestamp(start, tz=TZ).value // 1_000_000]
        if end:
            df = df[df["ts"] < pd.Timestamp(end, tz=TZ).value // 1_000_000]
        return df.reset_index(drop=True)

    if bars1 is not None:
        bars1 = clip(bars1)
        bars5 = resample_1m_to_5m(bars1)
    else:
        bars5 = clip(normalise(pd.read_parquet(m5_path)))
    if len(bars5) < 100:
        sys.exit(f"  only {len(bars5)} 5-minute candles in range; not enough to test anything")
    return bars5, bars1, source


def daily_equity(equity: pd.Series, starting_balance: float) -> pd.Series:
    """One point per New York day, opening with the starting balance the day
    before. Metrics resample to daily anyway, and the per-candle series would
    put ~200k points into every stored report. Index is Unix ms."""
    eq = equity.copy()
    eq.index = pd.to_datetime(eq.index, unit="ms", utc=True).tz_convert(TZ)
    daily = eq.resample("D").last().ffill().dropna()
    start = pd.Series([float(starting_balance)], index=[daily.index[0] - pd.Timedelta(days=1)])
    daily = pd.concat([start, daily])
    # Not .astype("int64") // 1e6: pandas 3 keeps the resolution it was built
    # with (ms here), so that gives a 1970 timestamp and a Sharpe of 0.
    ms = (daily.index.tz_convert("UTC") - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)
    return pd.Series(daily.to_numpy(dtype=float), index=np.asarray(ms, dtype="int64"))


def run_one(key, title, overrides, bars5, bars1, fee, ambiguity, store):
    cfg = MNQHybridConfig(ambiguity=ambiguity, **overrides)
    res = simulate(bars5, cfg, fee, bars1=bars1)
    limit = cfg.risk_limit_usd
    out = {
        "key": key, "title": title,
        "config": {k: v for k, v in cfg.__dict__.items()},
        "overall": module_stats(res.trades, limit),
        "long": module_stats([t for t in res.trades if t.module == "long"], limit),
        "short": module_stats([t for t in res.trades if t.module == "short"], limit),
        "counts": res.counts,
        "quarterly": by_period(res.trades, "Q").to_dict(orient="records"),
    }
    if store is not None and res.trades:
        ohlcv = res.bars.rename(columns={"ts": "timestamp"})
        record = store.save_run(
            strategy_id=f"mnq_hybrid_{key}", symbol="MNQ", timeframe="5m",
            equity=daily_equity(res.equity, cfg.starting_balance), trades=res.trades,
            ohlcv_df=ohlcv, strategy_version="1.0",
            config_json=json.dumps(out["config"], default=str),
            source_format="transcript", initial_capital=cfg.starting_balance,
        )
        out["run_id"] = record.run_id
        out["html"] = str(getattr(record, "html_path", ""))
    return out, res


def band_sweep(bars5, bars1, fee, ambiguity):
    rows = []
    for mult in BAND_SWEEP:
        cfg = MNQHybridConfig(ambiguity=ambiguity, long_on=True, short_on=False,
                              fixed_qty=4, band_mult=mult)
        s = module_stats(simulate(bars5, cfg, fee, bars1=bars1).trades, cfg.risk_limit_usd)
        rows.append({"band_mult": mult, "trades": s.get("trades", 0),
                     "net_pnl": s.get("net_pnl", 0.0), "profit_factor": s.get("profit_factor"),
                     "win_rate": s.get("win_rate")})
    return rows


# ── report ─────────────────────────────────────────────────────────────────

def _fmt_pf(v):
    return "—" if v is None else f"{v:.2f}"


def _fmt_pct(v):
    return "—" if v is None else f"{v * 100:.0f}%"


def _money(v):
    return "—" if v is None else f"${v:,.0f}"


def write_report(summary: dict, out_dir: Path) -> Path:
    L = []
    d = summary["data"]
    L.append("# MNQ hybrid scalp: backtest report\n")
    L.append(f"**Data:** {d['source']}  \n**Range:** {d['start']} to {d['end']} "
             f"({d['candles_5m']:,} five-minute candles, {d['sessions']} sessions)  \n"
             f"**Intrabar order:** {'1-minute bars' if d['used_1m'] else '5-minute only (see the ordering section)'}  \n"
             f"**Costs:** `{summary['fee_profile']}`, {summary['fee_note']}  \n"
             f"**Ambiguity policy:** {summary['ambiguity']}\n")

    L.append("## Results against the video\n")
    L.append("| Run | Trades | Net P&L | PF | Win rate | Max DD | Worst trade | Over $500 loss | Video PF | Video win | Video DD |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in summary["runs"]:
        s, v = r["overall"], VIDEO.get(r["key"], {})
        if not s.get("trades"):
            L.append(f"| {r['title']} | 0 | | | | | | | | | |")
            continue
        L.append(f"| {r['title']} | {s['trades']:,} | {_money(s['net_pnl'])} | {_fmt_pf(s['profit_factor'])} | "
                 f"{_fmt_pct(s['win_rate'])} | {_money(s['max_drawdown_usd'])} | {_money(s['worst_trade'])} | "
                 f"{s['limit_breaches']} | {_fmt_pf(v.get('profit_factor'))} | {_fmt_pct(v.get('win_rate'))} | "
                 f"{_money(v.get('max_drawdown_usd'))} |")
    L.append("\nVideo figures: the long from its backtest section, the short at 22:42, both with a fixed 4 contracts. "
             "The video doesn't give its date range or costs, so a close match isn't expected. "
             "What matters is whether the result points the same way.\n")

    trader = next((r for r in summary["runs"] if r["key"] == "trader"), None)
    if trader and trader["overall"].get("trades"):
        L.append("## As the trader actually trades\n")
        for mod in ("long", "short"):
            s = trader[mod]
            if s.get("trades"):
                L.append(f"- **{mod.title()}:** {s['trades']:,} trades, {_money(s['net_pnl'])}, PF "
                         f"{_fmt_pf(s['profit_factor'])}, win {_fmt_pct(s['win_rate'])}, avg "
                         f"{s['avg_contracts']} contracts, exits {s['exit_reasons']}")
        L.append(f"- Losses over the ${trader['overall']['limit_usd']:,.0f} per-position limit: "
                 f"**{trader['overall']['limit_breaches']}** (slippage and fees sit on top of the sized stop)\n")

    L.append("## How much was measured and how much was guessed\n")
    L.append("With a 1.5 ATR stop and a 2 ATR target, one 5-minute candle can hit both. "
             "OHLC data can't say which came first; 1-minute bars usually can.\n")
    L.append("| Run | Settled by candle alone | Settled by 1-min bars | Candle-colour heuristic | Still a guess |")
    L.append("|---|---|---|---|---|")
    for r in summary["runs"]:
        c = r["overall"].get("confidence", {})
        n = max(1, r["overall"].get("trades", 0))
        L.append(f"| {r['title']} | {c.get(RESOLVED, 0)} | {c.get(RESOLVED_1M, 0)} | {c.get(HEURISTIC, 0)} | "
                 f"{c.get(UNCERTAIN, 0)} ({c.get(UNCERTAIN, 0) / n:.1%}) |")
    if summary.get("bracket"):
        b = summary["bracket"]
        L.append(f"\nWithout 1-minute data the long's result depends on that guess: "
                 f"**{_money(b['conservative'])}** if every ambiguous candle stopped out first, "
                 f"**{_money(b['optimistic'])}** if every one hit the target first. The truth is in between.\n")

    L.append("## By quarter (the trader run)\n")
    if trader and trader["quarterly"]:
        L.append("| Quarter | Module | Trades | Net P&L |")
        L.append("|---|---|---|---|")
        for q in trader["quarterly"]:
            L.append(f"| {q['period']} | {q['module']} | {q['trades']} | {_money(q['net_pnl'])} |")
    L.append("\nAn edge that lives in one quarter isn't an edge. The video itself says the "
             "short's equity curve 'is basically going sideways now'.\n")

    L.append("## Is 3.1 special?\n")
    L.append("The video calls the band multiplier 'suspiciously specific' (6:27-7:52). If the result "
             "only works at exactly 3.1, it's fitted to the past rather than a real effect.\n")
    L.append("| Band x ATR | Trades | Net P&L | PF | Win rate |")
    L.append("|---|---|---|---|---|")
    for row in summary["band_sweep"]:
        L.append(f"| {row['band_mult']} | {row['trades']:,} | {_money(row['net_pnl'])} | "
                 f"{_fmt_pf(row['profit_factor'])} | {_fmt_pct(row['win_rate'])} |")

    L.append("\n## What this does and doesn't test\n")
    L.append("- **Tested:** the rules as the video states them, and the presenter's objective "
             "version of the shooting star.\n"
             "- **Not tested:** the trader's discretion. The presenter says the trader \"eyeballs\" "
             "the short candle (17:35-18:24) and sometimes holds longer or exits earlier (26:08). His real "
             "results (PF 1.25 over 703 trades) include that; this doesn't.\n"
             "- **Assumed:** 09:30 New York as the opening candle, contract rolls closing any open "
             "position, and no short opened into a halt or the weekend. None of these are in the video.\n")

    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "mnq_hybrid_summary.md"
    md.write_text("\n".join(L))
    (out_dir / "mnq_hybrid_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return md


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(prog="scripts.mnq_backtest", description=__doc__.split("\n")[0])
    ap.add_argument("--fee-profile", required=True,
                    help="cme_mnq_retail or cme_mnq_zero; there is deliberately no default")
    ap.add_argument("--start", help="first day, New York (YYYY-MM-DD)")
    ap.add_argument("--end", help="stop before this day")
    ap.add_argument("--ambiguity", default="conservative",
                    choices=["conservative", "heuristic", "optimistic"])
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--db", default="data/trades.db")
    ap.add_argument("--no-store", action="store_true", help="skip SQLite/HTML, write the summary only")
    ap.add_argument("--no-sweep", action="store_true")
    args = ap.parse_args(argv)

    fee = make_futures_fee_model(args.fee_profile)
    bars5, bars1, source = load_bars(args.start, args.end)

    store = None
    if not args.no_store:
        from src.backtest.result_store import ResultStore
        store = ResultStore(db_path=args.db, reports_dir=args.reports)

    t0 = pd.to_datetime(bars5["ts"].iloc[0], unit="ms", utc=True).tz_convert(TZ)
    t1 = pd.to_datetime(bars5["ts"].iloc[-1], unit="ms", utc=True).tz_convert(TZ)
    print(f"\n  MNQ hybrid scalp  |  {t0:%Y-%m-%d} -> {t1:%Y-%m-%d}  |  {len(bars5):,} candles"
          f"  |  1-min ordering: {'yes' if bars1 is not None else 'NO'}  |  {fee.name}\n")

    runs = []
    first_res = None
    for key, title, overrides in RUNS:
        out, res = run_one(key, title, overrides, bars5, bars1, fee, args.ambiguity, store)
        runs.append(out)
        first_res = first_res or res
        s = out["overall"]
        if s.get("trades"):
            print(f"  {title:<58} {s['trades']:>6,} trades  {_money(s['net_pnl']):>10}  "
                  f"PF {_fmt_pf(s['profit_factor']):>5}  win {_fmt_pct(s['win_rate']):>4}  "
                  f"DD {_money(s['max_drawdown_usd']):>8}")
        else:
            print(f"  {title:<58} no trades")

    bracket = None
    if bars1 is None:
        lo = module_stats(simulate(bars5, MNQHybridConfig(ambiguity="conservative", short_on=False,
                                                          fixed_qty=4), fee).trades, 500)
        hi = module_stats(simulate(bars5, MNQHybridConfig(ambiguity="optimistic", short_on=False,
                                                          fixed_qty=4), fee).trades, 500)
        bracket = {"conservative": lo.get("net_pnl", 0.0), "optimistic": hi.get("net_pnl", 0.0)}

    summary = {
        "data": {
            "source": source, "used_1m": bars1 is not None,
            "start": f"{t0:%Y-%m-%d}", "end": f"{t1:%Y-%m-%d}",
            "candles_5m": int(len(bars5)),
            "sessions": int(first_res.bars["session"].nunique()),
        },
        "fee_profile": fee.name, "fee_note": fee.note, "ambiguity": args.ambiguity,
        "runs": runs,
        "band_sweep": [] if args.no_sweep else band_sweep(bars5, bars1, fee, args.ambiguity),
        "bracket": bracket,
    }
    md = write_report(summary, Path(args.reports))
    if store is not None:
        store.close()
    print(f"\n  report:  {md}")
    print(f"  data:    {md.with_suffix('.json')}")
    if store is not None:
        print(f"  per-run HTML reports are in {args.reports}/ (run ids: "
              f"{', '.join(str(r.get('run_id')) for r in runs if r.get('run_id'))})")
    return summary


if __name__ == "__main__":
    main()
