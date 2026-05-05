"""Generate a self-contained HTML portfolio + per-strategy trade analysis report.

Reads `data/trades.db` (crypto engine) and `data/trades_gold.db` (gold engine).
Computes per-strategy round-trip P&L, win rate, equity curve, drawdown, etc.
Writes to `docs/reports/portfolio_<YYYY-MM-DD>.html`.

Run: python3 scripts/generate_portfolio_report.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
DBS = [
    ("crypto", REPO / "data" / "trades.db"),
    ("gold", REPO / "data" / "trades_gold.db"),
]
OUT_DIR = REPO / "docs" / "reports"


@dataclass
class Trade:
    id: int
    order_id: str
    timestamp: int
    symbol: str
    side: str
    price: float
    quantity: float
    commission: float
    strategy: str
    engine: str

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp / 1000, tz=timezone.utc)

    @property
    def notional(self) -> float:
        return self.price * self.quantity


@dataclass
class RoundTrip:
    """Pair of (entry, exit) for a single position."""

    strategy: str
    symbol: str
    engine: str
    entry: Trade
    exit: Trade

    @property
    def is_long(self) -> bool:
        return self.entry.side == "BUY"

    @property
    def pnl(self) -> float:
        # gross PnL minus both commissions
        if self.is_long:
            gross = (self.exit.price - self.entry.price) * self.entry.quantity
        else:
            gross = (self.entry.price - self.exit.price) * self.entry.quantity
        return gross - self.entry.commission - self.exit.commission

    @property
    def pnl_pct(self) -> float:
        if self.entry.notional <= 0:
            return 0.0
        return (self.pnl / self.entry.notional) * 100.0

    @property
    def duration_h(self) -> float:
        return (self.exit.timestamp - self.entry.timestamp) / 1000.0 / 3600.0


def load_trades() -> list[Trade]:
    rows: list[Trade] = []
    for engine_name, db_path in DBS:
        if not db_path.exists():
            continue
        con = sqlite3.connect(str(db_path))
        cur = con.execute(
            "SELECT id, order_id, timestamp, symbol, side, price, quantity, "
            "commission, strategy FROM trades ORDER BY timestamp"
        )
        for r in cur.fetchall():
            rows.append(
                Trade(
                    id=r[0],
                    order_id=r[1],
                    timestamp=r[2],
                    symbol=r[3],
                    side=r[4],
                    price=r[5],
                    quantity=r[6],
                    commission=r[7] or 0.0,
                    strategy=r[8] or "unknown",
                    engine=engine_name,
                )
            )
        con.close()
    return rows


def pair_round_trips(trades: list[Trade]) -> tuple[list[RoundTrip], list[Trade]]:
    """Pair entries with exits per (strategy, symbol). FIFO. Return open trades."""
    by_key: dict[tuple[str, str, str], list[Trade]] = defaultdict(list)
    closed: list[RoundTrip] = []
    open_trades: list[Trade] = []
    for t in sorted(trades, key=lambda x: x.timestamp):
        key = (t.strategy, t.symbol, t.engine)
        stack = by_key[key]
        if not stack:
            stack.append(t)
            continue
        last = stack[-1]
        if last.side != t.side:
            # opposite — close
            closed.append(RoundTrip(t.strategy, t.symbol, t.engine, last, t))
            stack.pop()
        else:
            # same side — accumulate (treat as additional entry)
            stack.append(t)
    for stack in by_key.values():
        open_trades.extend(stack)
    return closed, open_trades


def load_paper_equity(db_path: Path) -> Optional[tuple[float, float, int]]:
    if not db_path.exists():
        return None
    con = sqlite3.connect(str(db_path))
    cur = con.execute("SELECT equity, initial_capital, trade_count FROM paper_equity LIMIT 1")
    row = cur.fetchone()
    con.close()
    return row if row else None


def load_open_positions(db_path: Path, engine: str) -> list[dict]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute(
            "SELECT symbol, side, quantity, avg_entry_price, mark_price, unrealized_pnl, "
            "strategy, opened_at FROM paper_positions"
        )
        rows = [
            {
                "symbol": r[0],
                "side": r[1],
                "quantity": r[2],
                "avg_entry": r[3],
                "mark": r[4],
                "upnl": r[5],
                "strategy": r[6],
                "opened_at": r[7],
                "engine": engine,
            }
            for r in cur.fetchall()
        ]
    except sqlite3.OperationalError:
        rows = []
    con.close()
    return rows


def aggregate_by_strategy(rts: list[RoundTrip]) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(
        lambda: {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "wins_pnl": 0.0,
            "losses_pnl": 0.0,
            "best": None,
            "worst": None,
            "avg_duration_h": 0.0,
            "engine": "",
        }
    )
    for rt in rts:
        s = out[rt.strategy]
        s["n"] += 1
        s["total_pnl"] += rt.pnl
        s["avg_duration_h"] += rt.duration_h
        s["engine"] = rt.engine
        if rt.pnl > 0:
            s["wins"] += 1
            s["wins_pnl"] += rt.pnl
        else:
            s["losses"] += 1
            s["losses_pnl"] += rt.pnl
        if s["best"] is None or rt.pnl > s["best"]:
            s["best"] = rt.pnl
        if s["worst"] is None or rt.pnl < s["worst"]:
            s["worst"] = rt.pnl
    for s in out.values():
        s["avg_duration_h"] = s["avg_duration_h"] / s["n"] if s["n"] else 0.0
        s["win_rate"] = (s["wins"] / s["n"] * 100.0) if s["n"] else 0.0
        s["avg_pnl"] = s["total_pnl"] / s["n"] if s["n"] else 0.0
        s["avg_win"] = s["wins_pnl"] / s["wins"] if s["wins"] else 0.0
        s["avg_loss"] = s["losses_pnl"] / s["losses"] if s["losses"] else 0.0
        s["profit_factor"] = (
            (s["wins_pnl"] / -s["losses_pnl"]) if s["losses_pnl"] < 0 else float("inf") if s["wins"] else 0.0
        )
    return out


def equity_curve_per_engine(rts: list[RoundTrip], initial: float = 10000.0) -> list[dict]:
    """Time-series equity per engine — closed-trade cumulative PnL added to initial."""
    by_engine: dict[str, list[RoundTrip]] = defaultdict(list)
    for rt in rts:
        by_engine[rt.engine].append(rt)
    series = []
    for engine, ts in by_engine.items():
        ts_sorted = sorted(ts, key=lambda x: x.exit.timestamp)
        eq = initial
        points = [{"t": (ts_sorted[0].entry.timestamp if ts_sorted else 0), "eq": initial}]
        for rt in ts_sorted:
            eq += rt.pnl
            points.append({"t": rt.exit.timestamp, "eq": eq})
        series.append({"engine": engine, "points": points})
    return series


def compute_drawdown(points: list[dict]) -> tuple[float, float]:
    """Max drawdown $ and %."""
    peak = None
    max_dd = 0.0
    max_dd_pct = 0.0
    for p in points:
        eq = p["eq"]
        if peak is None or eq > peak:
            peak = eq
        dd = peak - eq
        dd_pct = (dd / peak) * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct
    return max_dd, max_dd_pct


def fmt_dt(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_money(x: float) -> str:
    return f"{'+' if x >= 0 else ''}${x:,.2f}"


def fmt_pct(x: float) -> str:
    return f"{'+' if x >= 0 else ''}{x:.2f}%"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    trades = load_trades()
    if not trades:
        print("no trades found in any DB", file=sys.stderr)
        return 1

    round_trips, open_trades = pair_round_trips(trades)

    # Engine-level equity + drawdown
    crypto_eq = load_paper_equity(REPO / "data" / "trades.db")
    gold_eq = load_paper_equity(REPO / "data" / "trades_gold.db")
    open_positions = load_open_positions(REPO / "data" / "trades.db", "crypto") + load_open_positions(
        REPO / "data" / "trades_gold.db", "gold"
    )

    by_strategy = aggregate_by_strategy(round_trips)
    eq_series = equity_curve_per_engine(round_trips)

    # totals
    total_pnl = sum(rt.pnl for rt in round_trips)
    total_wins = sum(1 for rt in round_trips if rt.pnl > 0)
    total_losses = sum(1 for rt in round_trips if rt.pnl <= 0)
    total_trades = len(round_trips)
    portfolio_eq = (crypto_eq[0] if crypto_eq else 0) + (gold_eq[0] if gold_eq else 0)
    portfolio_initial = (crypto_eq[1] if crypto_eq else 0) + (gold_eq[1] if gold_eq else 0)
    portfolio_pnl_pct = ((portfolio_eq - portfolio_initial) / portfolio_initial * 100) if portfolio_initial else 0

    # build HTML
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = OUT_DIR / f"portfolio_{today}.html"

    # per-engine drawdowns
    engine_dd: dict[str, tuple[float, float]] = {}
    for s in eq_series:
        engine_dd[s["engine"]] = compute_drawdown(s["points"])

    html = build_html(
        round_trips=round_trips,
        open_trades=open_trades,
        open_positions=open_positions,
        by_strategy=by_strategy,
        eq_series=eq_series,
        engine_dd=engine_dd,
        crypto_eq=crypto_eq,
        gold_eq=gold_eq,
        total_pnl=total_pnl,
        total_trades=total_trades,
        total_wins=total_wins,
        total_losses=total_losses,
        portfolio_eq=portfolio_eq,
        portfolio_initial=portfolio_initial,
        portfolio_pnl_pct=portfolio_pnl_pct,
    )
    out_path.write_text(html)
    print(f"wrote {out_path}")
    return 0


def build_html(**ctx) -> str:
    rts: list[RoundTrip] = ctx["round_trips"]
    open_trades: list[Trade] = ctx["open_trades"]
    open_positions: list[dict] = ctx["open_positions"]
    by_strategy: dict[str, dict] = ctx["by_strategy"]
    eq_series = ctx["eq_series"]
    engine_dd = ctx["engine_dd"]
    crypto_eq = ctx["crypto_eq"]
    gold_eq = ctx["gold_eq"]

    # Build rows
    strat_rows = ""
    for s, agg in sorted(by_strategy.items(), key=lambda x: -x[1]["total_pnl"]):
        pnl_class = "pos" if agg["total_pnl"] >= 0 else "neg"
        wr_class = "pos" if agg["win_rate"] >= 50 else "neg"
        pf_class = "pos" if agg["profit_factor"] > 1.0 and agg["profit_factor"] != float("inf") else "neg"
        pf_disp = f"{agg['profit_factor']:.2f}" if agg["profit_factor"] != float("inf") else "∞"
        strat_rows += f"""
        <tr>
            <td><strong>{s}</strong> <span class='engine-tag'>{agg['engine']}</span></td>
            <td>{agg['n']}</td>
            <td class='{wr_class}'>{agg['win_rate']:.1f}%</td>
            <td class='{pnl_class}'>{fmt_money(agg['total_pnl'])}</td>
            <td>{fmt_money(agg['avg_pnl'])}</td>
            <td class='pos'>{fmt_money(agg['avg_win'])}</td>
            <td class='neg'>{fmt_money(agg['avg_loss'])}</td>
            <td class='pos'>{fmt_money(agg['best'] or 0)}</td>
            <td class='neg'>{fmt_money(agg['worst'] or 0)}</td>
            <td class='{pf_class}'>{pf_disp}</td>
            <td>{agg['avg_duration_h']:.1f}h</td>
        </tr>"""

    # Round-trip table (closed trades)
    trade_rows = ""
    for rt in sorted(rts, key=lambda x: -x.exit.timestamp)[:50]:
        pnl_class = "pos" if rt.pnl >= 0 else "neg"
        side_arrow = "⬆" if rt.is_long else "⬇"
        trade_rows += f"""
        <tr>
            <td>{fmt_dt(rt.exit.timestamp)}</td>
            <td><span class='engine-tag'>{rt.engine}</span></td>
            <td>{rt.strategy}</td>
            <td>{rt.symbol}</td>
            <td>{side_arrow} {rt.entry.side}</td>
            <td>${rt.entry.price:.4f}</td>
            <td>${rt.exit.price:.4f}</td>
            <td>{rt.entry.quantity:.4f}</td>
            <td class='{pnl_class}'>{fmt_money(rt.pnl)}</td>
            <td class='{pnl_class}'>{fmt_pct(rt.pnl_pct)}</td>
            <td>{rt.duration_h:.1f}h</td>
        </tr>"""

    # Open positions
    open_rows = ""
    for p in open_positions:
        upnl_class = "pos" if (p.get("upnl") or 0) >= 0 else "neg"
        opened_str = ""
        if p.get("opened_at"):
            try:
                opened_str = fmt_dt(int(p["opened_at"]))
            except (ValueError, TypeError):
                opened_str = str(p.get("opened_at", ""))
        open_rows += f"""
        <tr>
            <td><span class='engine-tag'>{p['engine']}</span></td>
            <td>{p.get('strategy') or 'unknown'}</td>
            <td>{p.get('symbol')}</td>
            <td>{p.get('side')}</td>
            <td>{p.get('quantity', 0):.4f}</td>
            <td>${p.get('avg_entry', 0):.4f}</td>
            <td>${p.get('mark', 0):.4f}</td>
            <td class='{upnl_class}'>{fmt_money(p.get('upnl') or 0)}</td>
            <td>{opened_str}</td>
        </tr>"""

    # Equity series JSON for chart
    eq_json = json.dumps(eq_series)

    # Engine summary cards
    crypto_card = ""
    if crypto_eq:
        eq, init, n = crypto_eq
        ret_pct = (eq - init) / init * 100
        ret_class = "pos" if ret_pct >= 0 else "neg"
        dd, dd_pct = engine_dd.get("crypto", (0, 0))
        crypto_card = f"""
        <div class='card'>
            <h3>Crypto Engine (Binance)</h3>
            <div class='big {ret_class}'>${eq:,.2f}</div>
            <div class='small'>Initial: ${init:,.2f}</div>
            <div class='kv'><span>Return</span> <span class='{ret_class}'>{fmt_pct(ret_pct)}</span></div>
            <div class='kv'><span>Trades</span> <span>{n}</span></div>
            <div class='kv'><span>Max DD</span> <span class='neg'>{fmt_money(-dd)} ({dd_pct:.1f}%)</span></div>
        </div>"""
    gold_card = ""
    if gold_eq:
        eq, init, n = gold_eq
        ret_pct = (eq - init) / init * 100
        ret_class = "pos" if ret_pct >= 0 else "neg"
        dd, dd_pct = engine_dd.get("gold", (0, 0))
        gold_card = f"""
        <div class='card'>
            <h3>Gold Engine (cTrader)</h3>
            <div class='big {ret_class}'>${eq:,.2f}</div>
            <div class='small'>Initial: ${init:,.2f}</div>
            <div class='kv'><span>Return</span> <span class='{ret_class}'>{fmt_pct(ret_pct)}</span></div>
            <div class='kv'><span>Trades</span> <span>{n}</span></div>
            <div class='kv'><span>Max DD</span> <span class='neg'>{fmt_money(-dd)} ({dd_pct:.1f}%)</span></div>
        </div>"""

    # Portfolio summary card
    p_eq = ctx["portfolio_eq"]
    p_init = ctx["portfolio_initial"]
    p_pct = ctx["portfolio_pnl_pct"]
    p_class = "pos" if p_pct >= 0 else "neg"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Portfolio Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; background: #0d1117; color: #c9d1d9; max-width: 1400px; margin: 0 auto; padding: 24px; line-height: 1.5; }}
  h1 {{ color: #f0f6fc; border-bottom: 2px solid #30363d; padding-bottom: 12px; }}
  h2 {{ color: #58a6ff; margin-top: 36px; padding-top: 12px; border-top: 1px solid #21262d; }}
  h3 {{ color: #d2a8ff; margin: 0 0 12px 0; font-size: 14px; }}
  .summary {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin: 24px 0; }}
  .card {{ background: #161b22; padding: 20px; border-radius: 8px; border: 1px solid #30363d; }}
  .card.portfolio {{ border-color: #58a6ff; background: #0e1a2c; }}
  .big {{ font-size: 32px; font-weight: 700; margin: 8px 0; }}
  .small {{ font-size: 12px; color: #8b949e; margin-bottom: 8px; }}
  .kv {{ display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #21262d; font-size: 13px; }}
  .kv:last-child {{ border-bottom: none; }}
  .pos {{ color: #56d364; }}
  .neg {{ color: #f85149; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13px; }}
  th {{ background: #161b22; color: #58a6ff; text-align: left; padding: 10px; border-bottom: 2px solid #30363d; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #21262d; }}
  tr:hover {{ background: #161b22; }}
  .engine-tag {{ background: #1c2128; border: 1px solid #30363d; padding: 2px 6px; border-radius: 4px; font-size: 11px; color: #8b949e; margin-left: 4px; }}
  .chart-wrap {{ background: #161b22; padding: 20px; border-radius: 8px; border: 1px solid #30363d; margin: 16px 0; }}
  canvas {{ max-width: 100%; height: 360px; }}
  .scroll {{ overflow-x: auto; }}
  .footer {{ text-align: center; color: #8b949e; margin-top: 40px; padding-top: 20px; border-top: 1px solid #21262d; font-size: 12px; }}
</style>
</head>
<body>

<h1>📊 Portfolio Report — Algo Trading</h1>
<p>Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · Closed round-trips: {len(rts)} · Open positions: {len(open_positions)}</p>

<h2>1️⃣ Portfolio Summary</h2>
<div class='summary'>
    <div class='card portfolio'>
        <h3>Combined Portfolio</h3>
        <div class='big {p_class}'>${p_eq:,.2f}</div>
        <div class='small'>Initial: ${p_init:,.2f}</div>
        <div class='kv'><span>Total Return</span> <span class='{p_class}'>{fmt_pct(p_pct)}</span></div>
        <div class='kv'><span>Total Trades</span> <span>{ctx['total_trades']}</span></div>
        <div class='kv'><span>Wins / Losses</span> <span class='pos'>{ctx['total_wins']}</span> / <span class='neg'>{ctx['total_losses']}</span></div>
        <div class='kv'><span>Win Rate</span> <span>{(ctx['total_wins']/ctx['total_trades']*100 if ctx['total_trades'] else 0):.1f}%</span></div>
        <div class='kv'><span>Realized P&L</span> <span class='{p_class}'>{fmt_money(ctx['total_pnl'])}</span></div>
    </div>
    {crypto_card}
    {gold_card}
</div>

<h2>2️⃣ Equity Curves</h2>
<div class='chart-wrap'>
    <canvas id='equityChart'></canvas>
</div>

<h2>3️⃣ Per-Strategy Breakdown</h2>
<div class='scroll'>
<table>
<thead>
<tr>
    <th>Strategy</th>
    <th>Trades</th>
    <th>Win Rate</th>
    <th>Total P&L</th>
    <th>Avg P&L</th>
    <th>Avg Win</th>
    <th>Avg Loss</th>
    <th>Best</th>
    <th>Worst</th>
    <th>Profit Factor</th>
    <th>Avg Duration</th>
</tr>
</thead>
<tbody>
{strat_rows}
</tbody>
</table>
</div>

<h2>4️⃣ Open Positions ({len(open_positions)})</h2>
<div class='scroll'>
<table>
<thead>
<tr>
    <th>Engine</th>
    <th>Strategy</th>
    <th>Symbol</th>
    <th>Side</th>
    <th>Qty</th>
    <th>Entry</th>
    <th>Mark</th>
    <th>uPnL</th>
    <th>Opened</th>
</tr>
</thead>
<tbody>
{open_rows or "<tr><td colspan='9' style='text-align:center; color:#8b949e;'>No open positions</td></tr>"}
</tbody>
</table>
</div>

<h2>5️⃣ Recent Closed Trades (last 50)</h2>
<div class='scroll'>
<table>
<thead>
<tr>
    <th>Closed</th>
    <th>Engine</th>
    <th>Strategy</th>
    <th>Symbol</th>
    <th>Side</th>
    <th>Entry</th>
    <th>Exit</th>
    <th>Qty</th>
    <th>P&L</th>
    <th>P&L %</th>
    <th>Duration</th>
</tr>
</thead>
<tbody>
{trade_rows or "<tr><td colspan='11' style='text-align:center; color:#8b949e;'>No closed trades</td></tr>"}
</tbody>
</table>
</div>

<div class='footer'>
algo-trading · trades.db (crypto) + trades_gold.db (gold) · Generated by scripts/generate_portfolio_report.py
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
const eqSeries = {eq_json};
const datasets = eqSeries.map(s => ({{
    label: s.engine,
    data: s.points.map(p => ({{ x: p.t, y: p.eq }})),
    borderColor: s.engine === 'crypto' ? '#58a6ff' : '#d2a8ff',
    backgroundColor: s.engine === 'crypto' ? 'rgba(88,166,255,0.1)' : 'rgba(210,168,255,0.1)',
    fill: false,
    tension: 0.1,
    pointRadius: 2,
    borderWidth: 2,
}}));
new Chart(document.getElementById('equityChart'), {{
    type: 'line',
    data: {{ datasets }},
    options: {{
        scales: {{
            x: {{ type: 'linear', position: 'bottom', ticks: {{ color: '#8b949e', callback: v => new Date(v).toISOString().slice(0,10) }} }},
            y: {{ ticks: {{ color: '#8b949e', callback: v => '$' + v.toLocaleString() }} }}
        }},
        plugins: {{ legend: {{ labels: {{ color: '#c9d1d9' }} }} }},
        responsive: true,
        maintainAspectRatio: false,
    }}
}});
</script>

</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
