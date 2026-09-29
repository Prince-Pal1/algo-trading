"""Deep backtest of the MNQ hybrid scalp, run the way the video describes it.

    python3 -m scripts.mnq_backtest --fee-profile cme_mnq_retail
    python3 -m scripts.mnq_backtest --fee-profile cme_mnq_retail --refine   # Dukascopy data

Reads data/mnq/mnq_1m.parquet if present (5-minute candles are built from it,
and the 1-minute bars settle intrabar ordering), otherwise mnq_5m.parquet.
Fetch data first with scripts.mnq_data.

Four runs:
    video_long         long only, fixed 4 contracts           (video: PF 1.527)
    video_short        short only, fixed 4, no stop           (video: PF 1.413)
    video_short_daily  as above + previous-day-red filter     (video: 24:33)
    trader             both modules, 0.5% risk sizing, max-loss stop on the
                       short: the rules as the trader actually applies them

Each is also run with no costs at all, which is how TradingView tests by
default, and the video's own few weeks are cut out for comparison.

--refine settles trades that even 1-minute bars can't (the long filled and
maybe reached its target inside the same minute) with Dukascopy 1-second
bars, fetched only for those candles and cached.

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
    MNQHybridConfig, by_period, make_futures_fee_model, merge_fine, module_stats,
    normalise, resample_1m_to_5m, simulate, uncertain_windows,
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

# The video's test dates aren't given. It went out in mid-September 2026 (it
# plugs a live event on "Thursday, September 17th") and says the short's curve
# "started out very strong weeks ago", so its window is the few months before.
VIDEO_WINDOW = ("2026-07-01", "2026-09-12")

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
VIDEO_LONG = dict(long_on=True, short_on=False, fixed_qty=4)


def load_bars(start: str | None, end: str | None):
    m1_path, m5_path = DATA_DIR / "mnq_1m.parquet", DATA_DIR / "mnq_5m.parquet"
    if m1_path.exists():
        bars1 = normalise(pd.read_parquet(m1_path))
        source = str(m1_path)
    elif m5_path.exists():
        bars1, source = None, str(m5_path)
    else:
        sys.exit(f"  no data in {DATA_DIR}/. Fetch it first:  python3 -m scripts.mnq_data --help")

    label = Path(source).with_suffix(".source.txt")
    if label.exists():
        source = f"{label.read_text().strip()} ({source})"

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


def _entry_et(trades) -> pd.DatetimeIndex:
    return pd.to_datetime([t.entry_ts for t in trades], unit="ms", utc=True).tz_convert(TZ)


def _in_window(trades, window):
    a = pd.Timestamp(window[0], tz=TZ).value // 1_000_000
    b = pd.Timestamp(window[1], tz=TZ).value // 1_000_000
    return [t for t in trades if a <= t.entry_ts < b]


def _rth(trades):
    """Entries 09:35-15:55 New York: the cash session, opening candle excluded."""
    if not trades:
        return []
    et = _entry_et(trades)
    hhmm = et.hour * 100 + et.minute
    return [t for t, x in zip(trades, hhmm) if 935 <= x < 1600]


def by_hour(trades) -> list[dict]:
    """Trades, net and PF by the New York hour of entry."""
    if not trades:
        return []
    df = pd.DataFrame({"hour": _entry_et(trades).hour, "pnl": [t.pnl for t in trades]})
    rows = []
    for hour, g in df.groupby("hour"):
        won, lost = g.loc[g["pnl"] > 0, "pnl"].sum(), -g.loc[g["pnl"] <= 0, "pnl"].sum()
        rows.append({"hour": int(hour), "trades": int(len(g)), "net_pnl": round(float(g["pnl"].sum()), 2),
                     "profit_factor": round(float(won / lost), 3) if lost > 0 else None})
    return rows


# ── the runs ───────────────────────────────────────────────────────────────

class Runner:
    """Runs configurations against one data set. bars1 orders events inside a
    candle; after --refine it carries 1-second bars for some minutes."""

    def __init__(self, bars5, bars1, fee, zero, ambiguity):
        self.bars5, self.bars1, self.fee, self.zero, self.ambiguity = bars5, bars1, fee, zero, ambiguity

    def sim(self, overrides, fee=None, ambiguity=None):
        cfg = MNQHybridConfig(ambiguity=ambiguity or self.ambiguity, **overrides)
        return simulate(self.bars5, cfg, fee or self.fee, bars1=self.bars1)

    def long_results(self, sweep: bool) -> dict:
        """Every run that trades the long: the ones whose guesses --refine settles."""
        out = {f"{key}/{fee.name}": self.sim(ov, fee) for key, _, ov in RUNS
               if ov.get("long_on", True) for fee in (self.fee, self.zero)}
        out["video_long/optimistic"] = self.sim(VIDEO_LONG, ambiguity="optimistic")
        if sweep:
            out.update({f"sweep/{m}": self.sim(dict(VIDEO_LONG, band_mult=m)) for m in BAND_SWEEP})
        return out


def refine(bars1, runner_for, sweep: bool, rounds: int = 2):
    """Swap in 1-second bars for the candles holding uncertain trades. Settling
    one trade can shift the next, so it goes round again."""
    from scripts.mnq_data import fetch_dukascopy_seconds
    merged = bars1
    info = {"rounds": 0, "candles_asked": 0, "minutes_swapped": 0, "minutes_disagreed": 0}
    for _ in range(rounds):
        results = runner_for(merged).long_results(sweep)
        if "bracket_before" not in info:
            fee = runner_for(merged).fee.name
            info["bracket_before"] = {
                "conservative": module_stats(results[f"video_long/{fee}"].trades, 500).get("net_pnl", 0.0),
                "optimistic": module_stats(results["video_long/optimistic"].trades, 500).get("net_pnl", 0.0)}
        windows = set()
        for res in results.values():
            windows |= uncertain_windows(res.trades)
        print(f"  refine: {len(windows):,} candles hold an uncertain trade")
        if not windows:
            break
        fine = fetch_dukascopy_seconds(windows)
        merged, swapped, disagreed = merge_fine(bars1, fine)
        info.update(rounds=info["rounds"] + 1, candles_asked=info["candles_asked"] + len(windows),
                    minutes_swapped=swapped, minutes_disagreed=disagreed)
        print(f"  refine: {swapped:,} minutes now ordered by 1-second bars "
              f"({disagreed:,} didn't match their 1-minute bar and were left alone)")
    return merged, info


def summarise(key, title, res, zero_res, limit):
    trades = res.trades
    return {
        "key": key, "title": title,
        "config": {k: v for k, v in res.config.__dict__.items()},
        "overall": module_stats(trades, limit),
        "before_costs": module_stats(zero_res.trades, limit),
        "long": module_stats([t for t in trades if t.module == "long"], limit),
        "short": module_stats([t for t in trades if t.module == "short"], limit),
        "rth": module_stats(_rth(trades), limit),
        "counts": res.counts,
        "quarterly": by_period(trades, "Q").to_dict(orient="records"),
        "by_hour": by_hour(trades),
        "video_window": module_stats(_in_window(zero_res.trades, VIDEO_WINDOW), limit),
    }


def store_run(store, key, res, config_json):
    ohlcv = res.bars.rename(columns={"ts": "timestamp"})
    return store.save_run(
        strategy_id=f"mnq_hybrid_{key}", symbol="MNQ", timeframe="5m",
        equity=daily_equity(res.equity, res.config.starting_balance), trades=res.trades,
        ohlcv_df=ohlcv, strategy_version="1.1", config_json=config_json,
        source_format="transcript", initial_capital=res.config.starting_balance,
    )


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
    order = "1-minute bars" if d["used_1m"] else "5-minute only (see the ordering section)"
    if summary.get("refine"):
        order += (f", plus 1-second bars for {summary['refine']['minutes_swapped']:,} minutes "
                  "that 1-minute bars couldn't settle")
    L.append(f"**Data:** {d['source']}  \n**Range:** {d['start']} to {d['end']} "
             f"({d['candles_5m']:,} five-minute candles, {d['sessions']} sessions)  \n"
             f"**Intrabar order:** {order}  \n"
             f"**Costs:** `{summary['fee_profile']}`, {summary['fee_note']}  \n"
             f"**Ambiguity policy:** {summary['ambiguity']}\n")

    L.append("## Results against the video\n")
    L.append("| Run | Trades | Net P&L | PF | Win rate | Max DD | Worst trade | Over $500 loss "
             "| PF before costs | Net before costs | Video PF | Video win | Video DD |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in summary["runs"]:
        s, z, v = r["overall"], r["before_costs"], VIDEO.get(r["key"], {})
        if not s.get("trades"):
            L.append(f"| {r['title']} | 0 | | | | | | | | | | | |")
            continue
        L.append(f"| {r['title']} | {s['trades']:,} | {_money(s['net_pnl'])} | {_fmt_pf(s['profit_factor'])} | "
                 f"{_fmt_pct(s['win_rate'])} | {_money(s['max_drawdown_usd'])} | {_money(s['worst_trade'])} | "
                 f"{s['limit_breaches']} | {_fmt_pf(z.get('profit_factor'))} | {_money(z.get('net_pnl'))} | "
                 f"{_fmt_pf(v.get('profit_factor'))} | {_fmt_pct(v.get('win_rate'))} | "
                 f"{_money(v.get('max_drawdown_usd'))} |")
    L.append("\nVideo figures: the long from its backtest section, the short at 22:42, both with a fixed 4 "
             "contracts. TradingView charges nothing unless told to, so the before-costs columns are the "
             "fairer comparison.\n")

    L.append("## The video's own weeks\n")
    L.append(f"The video's test dates aren't given. It went out in mid-September 2026 and says the short "
             f"'started out very strong weeks ago'. This is {VIDEO_WINDOW[0]} to {VIDEO_WINDOW[1]}, no costs, "
             "fixed 4 contracts: as close to the video's test as the data allows.\n")
    L.append("| Run | Trades | Net P&L | PF | Win rate | Max DD | Video PF | Video win | Video DD |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in summary["runs"]:
        if r["key"] == "trader":
            continue
        s, v = r["video_window"], VIDEO.get(r["key"], {})
        if not s.get("trades"):
            L.append(f"| {r['title']} | 0 | | | | | | | |")
            continue
        L.append(f"| {r['title']} | {s['trades']} | {_money(s['net_pnl'])} | {_fmt_pf(s['profit_factor'])} | "
                 f"{_fmt_pct(s['win_rate'])} | {_money(s['max_drawdown_usd'])} | "
                 f"{_fmt_pf(v.get('profit_factor'))} | {_fmt_pct(v.get('win_rate'))} | "
                 f"{_money(v.get('max_drawdown_usd'))} |")
    L.append("")

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
    L.append("With a 1.5 ATR stop and a 2 ATR target, one 5-minute candle can hit both. The long also "
             "fills partway down a candle that started above its target, so whether the target came "
             "after the fill can be unknowable from the bars.\n")
    L.append("| Run | Settled by candle alone | Settled by finer bars | Candle-colour heuristic | Still a guess |")
    L.append("|---|---|---|---|---|")
    for r in summary["runs"]:
        c = r["overall"].get("confidence", {})
        n = max(1, r["overall"].get("trades", 0))
        L.append(f"| {r['title']} | {c.get(RESOLVED, 0)} | {c.get(RESOLVED_1M, 0)} | {c.get(HEURISTIC, 0)} | "
                 f"{c.get(UNCERTAIN, 0)} ({c.get(UNCERTAIN, 0) / n:.1%}) |")
    b = summary["bracket"]
    L.append(f"\nThe long, if every remaining guess went against it: **{_money(b['conservative'])}**. "
             f"If every one went its way: **{_money(b['optimistic'])}**. The truth is in between; "
             "the closer these two are, the less the guessing matters.")
    rf = summary.get("refine") or {}
    if rf.get("bracket_before"):
        bb = rf["bracket_before"]
        L.append(f"\nWith 1-minute bars alone the same range was {_money(bb['conservative'])} to "
                 f"{_money(bb['optimistic'])}. 1-second bars for {rf['minutes_swapped']:,} minutes narrowed it "
                 f"to the figures above ({rf['minutes_disagreed']:,} minutes disagreed with their 1-minute bar "
                 "and were left alone).")
    L.append("")

    L.append("## By hour of entry (New York)\n")
    rows = {r["key"]: {x["hour"]: x for x in r["by_hour"]} for r in summary["runs"]}
    L.append("| Hour | Long trades | Long net | Long PF | Short trades | Short net | Short PF |")
    L.append("|---|---|---|---|---|---|---|")
    for hour in range(24):
        lo, sh = rows.get("video_long", {}).get(hour, {}), rows.get("video_short", {}).get(hour, {})
        if not lo and not sh:
            continue
        L.append(f"| {hour:02d}:00 | {lo.get('trades', 0)} | {_money(lo.get('net_pnl'))} | "
                 f"{_fmt_pf(lo.get('profit_factor'))} | {sh.get('trades', 0)} | {_money(sh.get('net_pnl'))} | "
                 f"{_fmt_pf(sh.get('profit_factor'))} |")
    for r in summary["runs"][:2]:
        s = r["rth"]
        if s.get("trades"):
            L.append(f"\n{r['title']}, cash session only (09:35-16:00): {s['trades']:,} trades, "
                     f"{_money(s['net_pnl'])}, PF {_fmt_pf(s['profit_factor'])}.")
    L.append("\nFixed 4 contracts, with costs. The video never says which hours the trader works.\n")

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
    ap.add_argument("--refine", action="store_true",
                    help="settle uncertain trades with Dukascopy 1-second bars (Dukascopy data only)")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--db", default="data/trades.db")
    ap.add_argument("--no-store", action="store_true", help="skip SQLite/HTML, write the summary only")
    ap.add_argument("--no-sweep", action="store_true")
    args = ap.parse_args(argv)

    fee = make_futures_fee_model(args.fee_profile)
    zero = make_futures_fee_model("cme_mnq_zero")
    bars5, bars1, source = load_bars(args.start, args.end)

    t0 = pd.to_datetime(bars5["ts"].iloc[0], unit="ms", utc=True).tz_convert(TZ)
    t1 = pd.to_datetime(bars5["ts"].iloc[-1], unit="ms", utc=True).tz_convert(TZ)
    print(f"\n  MNQ hybrid scalp  |  {t0:%Y-%m-%d} -> {t1:%Y-%m-%d}  |  {len(bars5):,} candles"
          f"  |  1-min ordering: {'yes' if bars1 is not None else 'NO'}  |  {fee.name}\n")

    refine_info = None
    order_bars = bars1
    if args.refine:
        if bars1 is None or not source.startswith("Dukascopy"):
            sys.exit("  --refine fetches Dukascopy 1-second bars, so it needs Dukascopy 1-minute data "
                     "(python3 -m scripts.mnq_data dukascopy ...)")
        order_bars, refine_info = refine(
            bars1, lambda b1: Runner(bars5, b1, fee, zero, args.ambiguity), sweep=not args.no_sweep)
    runner = Runner(bars5, order_bars, fee, zero, args.ambiguity)

    store = None
    if not args.no_store:
        from src.backtest.result_store import ResultStore
        store = ResultStore(db_path=args.db, reports_dir=args.reports)

    runs = []
    first_res = None
    for key, title, overrides in RUNS:
        res, zero_res = runner.sim(overrides), runner.sim(overrides, fee=zero)
        out = summarise(key, title, res, zero_res, res.config.risk_limit_usd)
        if store is not None and res.trades:
            record = store_run(store, key, res, json.dumps(out["config"], default=str))
            out["run_id"] = record.run_id
            out["html"] = str(getattr(record, "html_path", ""))
        runs.append(out)
        first_res = first_res or res
        s, z = out["overall"], out["before_costs"]
        if s.get("trades"):
            print(f"  {title:<58} {s['trades']:>6,} trades  {_money(s['net_pnl']):>10}  "
                  f"PF {_fmt_pf(s['profit_factor']):>5}  win {_fmt_pct(s['win_rate']):>4}  "
                  f"DD {_money(s['max_drawdown_usd']):>8}  |  before costs PF {_fmt_pf(z.get('profit_factor'))}")
        else:
            print(f"  {title:<58} no trades")

    lo = module_stats(runner.sim(VIDEO_LONG).trades, 500)
    hi = module_stats(runner.sim(VIDEO_LONG, ambiguity="optimistic").trades, 500)
    bracket = {"conservative": lo.get("net_pnl", 0.0), "optimistic": hi.get("net_pnl", 0.0)}

    sweep = []
    if not args.no_sweep:
        for mult in BAND_SWEEP:
            s = module_stats(runner.sim(dict(VIDEO_LONG, band_mult=mult)).trades, 500)
            sweep.append({"band_mult": mult, "trades": s.get("trades", 0), "net_pnl": s.get("net_pnl", 0.0),
                          "profit_factor": s.get("profit_factor"), "win_rate": s.get("win_rate")})

    summary = {
        "data": {
            "source": source, "used_1m": bars1 is not None,
            "start": f"{t0:%Y-%m-%d}", "end": f"{t1:%Y-%m-%d}",
            "candles_5m": int(len(bars5)),
            "sessions": int(first_res.bars["session"].nunique()),
        },
        "fee_profile": fee.name, "fee_note": fee.note, "ambiguity": args.ambiguity,
        "refine": refine_info,
        "runs": runs,
        "band_sweep": sweep,
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
