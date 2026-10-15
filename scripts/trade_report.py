#!/usr/bin/env python3
"""Build an HTML dashboard of executed (closed) round-trips + running trades per strategy.

Realized P&L is reconstructed by FIFO-matching individual BUY/SELL fills into
round-trips per (strategy, symbol). Handles long, short, and position flips.
Open positions (running trades) come straight from paper_positions.
"""
import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DBS = [
    ("crypto", "/Users/prince/algo-trading/data/trades.db"),
    ("gold",   "/Users/prince/algo-trading/data/trades_gold.db"),
]
OUT = "/Users/prince/algo-trading/data/trade_report.html"


def fetch(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    trades = con.execute(
        "SELECT timestamp, symbol, side, price, quantity, commission, strategy "
        "FROM trades ORDER BY timestamp ASC"
    ).fetchall()
    positions = con.execute("SELECT * FROM paper_positions").fetchall()
    eq = con.execute(
        "SELECT equity, initial_capital, trade_count FROM paper_equity "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    con.close()
    return trades, positions, eq


def fifo_roundtrips(trades):
    """Return list of closed round-trips and per-strategy commission totals."""
    lots = defaultdict(list)          # (strategy,symbol) -> [ [qty_signed, price, ts] ]
    roundtrips = []
    comm_by_strat = defaultdict(float)
    for t in trades:
        comm_by_strat[t["strategy"]] += t["commission"] or 0.0
        key = (t["strategy"], t["symbol"])
        sign = 1 if t["side"].upper() == "BUY" else -1
        remaining = t["quantity"]
        price = t["price"]
        ts = t["timestamp"]
        q = lots[key]
        # If existing lots are opposite sign, close against them FIFO.
        while remaining > 1e-12 and q and (q[0][0] > 0) != (sign > 0):
            lot = q[0]
            lot_qty = abs(lot[0])
            match = min(remaining, lot_qty)
            lot_is_long = lot[0] > 0
            pnl = match * (price - lot[1]) * (1 if lot_is_long else -1)
            roundtrips.append({
                "strategy": t["strategy"], "symbol": t["symbol"],
                "dir": "LONG" if lot_is_long else "SHORT",
                "qty": match, "entry": lot[1], "exit": price,
                "entry_ts": lot[2], "exit_ts": ts, "pnl": pnl,
            })
            lot[0] += -match if lot_is_long else match
            remaining -= match
            if abs(lot[0]) < 1e-12:
                q.pop(0)
        if remaining > 1e-12:
            q.append([remaining * sign, price, ts])
    return roundtrips, comm_by_strat


def fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def money(x):
    return f"${x:,.2f}"


def pnl_span(x):
    cls = "pos" if x >= 0 else "neg"
    sign = "+" if x >= 0 else "−"
    return f'<span class="{cls}">{sign}${abs(x):,.2f}</span>'


# ---- gather ----
all_rt, all_pos, comm_all = [], [], defaultdict(float)
books = {}
for book, db in DBS:
    trades, positions, eq = fetch(db)
    rt, comm = fifo_roundtrips(trades)
    for r in rt:
        r["book"] = book
    all_rt.extend(rt)
    for p in positions:
        d = dict(p); d["book"] = book; all_pos.append(d)
    for k, v in comm.items():
        comm_all[k] += v
    books[book] = eq

# per-strategy aggregation
strat = defaultdict(lambda: {"n": 0, "wins": 0, "gross": 0.0, "book": ""})
for r in all_rt:
    s = strat[r["strategy"]]
    s["n"] += 1
    s["wins"] += 1 if r["pnl"] > 0 else 0
    s["gross"] += r["pnl"]
    s["book"] = r["book"]
open_by_strat = defaultdict(lambda: {"upnl": 0.0, "n": 0})
for p in all_pos:
    open_by_strat[p["strategy_name"]]["upnl"] += p["unrealized_pnl"]
    open_by_strat[p["strategy_name"]]["n"] += 1

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# combined header numbers
crypto_eq, gold_eq = books["crypto"], books["gold"]
comb_eq = crypto_eq["equity"] + gold_eq["equity"]
comb_init = crypto_eq["initial_capital"] + gold_eq["initial_capital"]
comb_ret = (comb_eq / comb_init - 1) * 100

# ---- build HTML ----
recent = sorted(all_rt, key=lambda r: r["exit_ts"], reverse=True)[:40]

rows_recent = "\n".join(
    f"<tr><td>{fmt_ts(r['exit_ts'])}</td><td>{r['book']}</td><td>{r['symbol']}</td>"
    f"<td><span class='tag {r['dir'].lower()}'>{r['dir']}</span></td>"
    f"<td>{r['strategy']}</td><td class='num'>{r['qty']:.4f}</td>"
    f"<td class='num'>{r['entry']:,.4f}</td><td class='num'>{r['exit']:,.4f}</td>"
    f"<td class='num'>{pnl_span(r['pnl'])}</td></tr>"
    for r in recent
)

rows_open = "\n".join(
    f"<tr><td>{fmt_ts(p['opened_at'])}</td><td>{p['book']}</td><td>{p['symbol']}</td>"
    f"<td><span class='tag {p['side'].lower()}'>{p['side']}</span></td>"
    f"<td>{p['strategy_name']}</td><td class='num'>{p['quantity']:.4f}</td>"
    f"<td class='num'>{p['entry_price']:,.4f}</td><td class='num'>{p['current_price']:,.4f}</td>"
    f"<td class='num'>{pnl_span(p['unrealized_pnl'])}</td></tr>"
    for p in sorted(all_pos, key=lambda p: p["opened_at"], reverse=True)
)

strat_rows = []
for name in sorted(strat, key=lambda n: strat[n]["gross"] + open_by_strat[n]["upnl"], reverse=True):
    s = strat[name]
    comm = comm_all.get(name, 0.0)
    net = s["gross"] - comm
    upnl = open_by_strat[name]["upnl"]
    total = net + upnl
    wr = (s["wins"] / s["n"] * 100) if s["n"] else 0
    strat_rows.append(
        f"<tr><td>{name}</td><td>{s['book']}</td><td class='num'>{s['n']}</td>"
        f"<td class='num'>{wr:.0f}%</td><td class='num'>{pnl_span(s['gross'])}</td>"
        f"<td class='num'>−${comm:,.2f}</td><td class='num'>{pnl_span(net)}</td>"
        f"<td class='num'>{open_by_strat[name]['n'] or ''}</td>"
        f"<td class='num'>{pnl_span(upnl) if open_by_strat[name]['n'] else '—'}</td>"
        f"<td class='num'><b>{pnl_span(total)}</b></td></tr>"
    )
strat_rows = "\n".join(strat_rows)

html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Algo Trading — Trade Report</title>
<style>
:root {{ --bg:#0d1117; --card:#161b22; --line:#21262d; --txt:#c9d1d9; --mut:#8b949e;
        --pos:#3fb950; --neg:#f85149; --acc:#58a6ff; }}
* {{ box-sizing:border-box; }}
body {{ background:var(--bg); color:var(--txt); font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:28px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.sub {{ color:var(--mut); margin-bottom:22px; font-size:13px; }}
.cards {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:26px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 20px; min-width:165px; }}
.card .lbl {{ color:var(--mut); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
.card .val {{ font-size:24px; font-weight:600; margin-top:6px; }}
h2 {{ font-size:16px; margin:28px 0 10px; border-left:3px solid var(--acc); padding-left:10px; }}
table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line);
         border-radius:10px; overflow:hidden; }}
th,td {{ padding:9px 12px; text-align:left; border-bottom:1px solid var(--line); white-space:nowrap; }}
th {{ background:#1c2230; color:var(--mut); font-size:12px; text-transform:uppercase; letter-spacing:.03em; }}
tr:last-child td {{ border-bottom:none; }}
tr:hover td {{ background:#1b2230; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }}
.tag {{ font-size:11px; padding:2px 7px; border-radius:5px; font-weight:600; }}
.tag.long,.tag.buy {{ background:rgba(63,185,80,.15); color:var(--pos); }}
.tag.short,.tag.sell {{ background:rgba(248,81,73,.15); color:var(--neg); }}
.note {{ color:var(--mut); font-size:12px; margin-top:8px; }}
</style></head><body>
<h1>Algo Trading — Trade Report</h1>
<div class="sub">Generated {now} · realized P&amp;L reconstructed by FIFO-matching fills into round-trips</div>

<div class="cards">
  <div class="card"><div class="lbl">Combined Equity</div><div class="val">{money(comb_eq)}</div></div>
  <div class="card"><div class="lbl">Combined Return</div><div class="val {'pos' if comb_ret>=0 else 'neg'}">{'+' if comb_ret>=0 else '−'}{abs(comb_ret):.2f}%</div></div>
  <div class="card"><div class="lbl">Crypto Book</div><div class="val {'pos' if crypto_eq['equity']>=crypto_eq['initial_capital'] else 'neg'}">{money(crypto_eq['equity'])}</div></div>
  <div class="card"><div class="lbl">Gold Book</div><div class="val {'pos' if gold_eq['equity']>=gold_eq['initial_capital'] else 'neg'}">{money(gold_eq['equity'])}</div></div>
  <div class="card"><div class="lbl">Closed Round-trips</div><div class="val">{len(all_rt)}</div></div>
  <div class="card"><div class="lbl">Open (running)</div><div class="val">{len(all_pos)}</div></div>
</div>

<h2>Running Trades (open positions)</h2>
<table><thead><tr><th>Opened</th><th>Book</th><th>Symbol</th><th>Side</th><th>Strategy</th>
<th class="num">Qty</th><th class="num">Entry</th><th class="num">Current</th><th class="num">Unrealized P&amp;L</th></tr></thead>
<tbody>{rows_open}</tbody></table>

<h2>Per-Strategy Summary</h2>
<table><thead><tr><th>Strategy</th><th>Book</th><th class="num">Closed</th><th class="num">Win%</th>
<th class="num">Gross Realized</th><th class="num">Commission</th><th class="num">Net Realized</th>
<th class="num">Open</th><th class="num">Unrealized</th><th class="num">Total P&amp;L</th></tr></thead>
<tbody>{strat_rows}</tbody></table>
<div class="note">Net Realized = gross round-trip P&amp;L minus commissions. Total = Net Realized + Unrealized (open).</div>

<h2>Recent Executed Trades (closed round-trips, latest 40)</h2>
<table><thead><tr><th>Closed (UTC)</th><th>Book</th><th>Symbol</th><th>Dir</th><th>Strategy</th>
<th class="num">Qty</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">P&amp;L</th></tr></thead>
<tbody>{rows_recent}</tbody></table>
<div class="note">A "round-trip" = an entry fill matched to its closing fill (FIFO). vol_momentum live P&amp;L is quarantined pending ≥30 days of clean post-Session-28 data.</div>

</body></html>"""

with open(OUT, "w") as f:
    f.write(html)
print("WROTE", OUT)
print(f"round-trips={len(all_rt)} open={len(all_pos)} combined_eq={comb_eq:.2f} ret={comb_ret:.2f}%")
for name in sorted(strat):
    s = strat[name]
    print(f"  {name:24s} closed={s['n']:3d} gross={s['gross']:+8.2f} comm={comm_all.get(name,0):7.2f}")
