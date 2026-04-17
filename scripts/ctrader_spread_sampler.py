#!/usr/bin/env python3
"""Continuously sample XAUUSD bid/ask from cTrader demo for N minutes.

Writes a CSV row per spot event so post-hoc analysis can bucket by hour
and compare against the current backtest spread assumption.

Output: data/ctrader_spread_samples_<timestamp>.csv
Columns: ts_iso, ts_unix_ms, bid, ask, spread

Resilient to one TCP disconnect (auto-reconnects once, then exits with
whatever data was captured).

Usage:
  python3 scripts/ctrader_spread_sampler.py --minutes 240
  python3 scripts/ctrader_spread_sampler.py --minutes 240 --symbol XAUUSD
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

for ln in Path(".env").read_text().splitlines():
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, v = ln.split("=", 1)
        os.environ[k.strip()] = v.strip().split()[0] if v.strip() else ""

from ctrader_open_api import Client, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAErrorRes,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOASpotEvent,
    ProtoOASubscribeSpotsReq,
    ProtoOASubscribeSpotsRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
)
from twisted.internet import reactor


CLIENT_ID = os.environ["CTRADER_CLIENT_ID"]
CLIENT_SECRET = os.environ["CTRADER_CLIENT_SECRET"]
ACCESS_TOKEN = os.environ["CTRADER_ACCESS_TOKEN"]
ACCOUNT_ID = int(os.environ["CTRADER_ACCOUNT_ID"])
HOST = os.environ.get("CTRADER_DEMO_HOST", "demo.ctraderapi.com")
PORT = int(os.environ.get("CTRADER_PORT", "5035"))


state = {
    "symbol_id": None,
    "writer": None,
    "fp": None,
    "csv_path": None,
    "tick_count": 0,
    "start_unix": None,
    "end_unix": None,
    "reconnects_left": 1,
    "subscribed": False,
    "target_symbol": "XAUUSD",
    "last_progress_print": 0.0,
}


def setup_csv(symbol: str) -> Path:
    Path("data").mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = Path(f"data/ctrader_spread_samples_{symbol}_{ts}.csv")
    state["fp"] = open(p, "w", newline="", buffering=1)  # line-buffered
    state["writer"] = csv.writer(state["fp"])
    state["writer"].writerow(["ts_iso", "ts_unix_ms", "bid", "ask", "spread"])
    state["csv_path"] = p
    return p


def stop_clean():
    if state["fp"] is not None:
        state["fp"].close()
        state["fp"] = None
    if reactor.running:
        reactor.callLater(0.1, reactor.stop)


def on_connected(client: Client) -> None:
    print(f"  ✓ TCP connected (reconnect={1 - state['reconnects_left']})", flush=True)
    state["subscribed"] = False
    req = ProtoOAApplicationAuthReq()
    req.clientId = CLIENT_ID
    req.clientSecret = CLIENT_SECRET
    client.send(req)


def on_disconnected(client: Client, reason) -> None:
    elapsed = (time.time() - state["start_unix"]) if state["start_unix"] else 0
    print(f"  ! disconnected after {elapsed:.0f}s ({state['tick_count']} ticks captured) — reason: {reason}", flush=True)
    if state["reconnects_left"] > 0 and time.time() < state["end_unix"]:
        state["reconnects_left"] -= 1
        print(f"  → reconnecting (attempts left after this: {state['reconnects_left']})", flush=True)
        reactor.callLater(2, client.startService)
    else:
        print("  → no reconnects remaining or duration elapsed; finishing.", flush=True)
        stop_clean()


def on_msg(client: Client, message) -> None:
    msg = Protobuf.extract(message)

    if isinstance(msg, ProtoOAErrorRes):
        print(f"  ✗ ERROR {msg.errorCode}: {msg.description}", flush=True)
        stop_clean()
        return

    if isinstance(msg, ProtoOAApplicationAuthRes):
        req = ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = ACCESS_TOKEN
        client.send(req)
        return

    if isinstance(msg, ProtoOAGetAccountListByAccessTokenRes):
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.accessToken = ACCESS_TOKEN
        client.send(req)
        return

    if isinstance(msg, ProtoOAAccountAuthRes):
        if state["symbol_id"] is None:
            req = ProtoOASymbolsListReq()
            req.ctidTraderAccountId = ACCOUNT_ID
            req.includeArchivedSymbols = False
            client.send(req)
        else:
            # Already know the symbol from before — just resubscribe
            req = ProtoOASubscribeSpotsReq()
            req.ctidTraderAccountId = ACCOUNT_ID
            req.symbolId.append(state["symbol_id"])
            client.send(req)
        return

    if isinstance(msg, ProtoOASymbolsListRes):
        for s in msg.symbol:
            if s.symbolName.upper() == state["target_symbol"]:
                state["symbol_id"] = s.symbolId
                break
        if state["symbol_id"] is None:
            print(f"  ✗ {state['target_symbol']} not found", flush=True)
            stop_clean()
            return
        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.symbolId.append(state["symbol_id"])
        client.send(req)
        return

    if isinstance(msg, ProtoOASubscribeSpotsRes):
        state["subscribed"] = True
        print(f"  ✓ subscribed to {state['target_symbol']} — capturing ticks until {datetime.fromtimestamp(state['end_unix'], tz=timezone.utc).isoformat()}", flush=True)
        return

    if isinstance(msg, ProtoOASpotEvent):
        if not (msg.HasField("bid") and msg.HasField("ask")):
            return
        now = time.time()
        if now > state["end_unix"]:
            print(f"  ⏱  duration reached — captured {state['tick_count']} ticks total", flush=True)
            stop_clean()
            return
        bid = float(msg.bid) / 100_000.0
        ask = float(msg.ask) / 100_000.0
        spread = ask - bid
        ts_unix_ms = int(now * 1000)
        ts_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        state["writer"].writerow([ts_iso, ts_unix_ms, f"{bid:.4f}", f"{ask:.4f}", f"{spread:.4f}"])
        state["tick_count"] += 1
        # Print progress every 60s
        if now - state["last_progress_print"] > 60:
            elapsed_min = (now - state["start_unix"]) / 60
            remaining_min = (state["end_unix"] - now) / 60
            print(f"  [{ts_iso}] tick #{state['tick_count']}  bid={bid:.2f} ask={ask:.2f} spread=${spread:.4f}  ({elapsed_min:.1f}m elapsed / {remaining_min:.1f}m left)", flush=True)
            state["last_progress_print"] = now


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=int, default=240, help="Total sampling duration in minutes")
    p.add_argument("--symbol", type=str, default="XAUUSD")
    args = p.parse_args()

    state["target_symbol"] = args.symbol
    state["start_unix"] = time.time()
    state["end_unix"] = state["start_unix"] + args.minutes * 60

    csv_path = setup_csv(args.symbol)
    print(f"=== Spread sampler: {args.symbol} for {args.minutes}m → {csv_path} ===", flush=True)

    client = Client(HOST, PORT, TcpProtocol)
    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(on_disconnected)
    client.setMessageReceivedCallback(on_msg)

    # Hard stop slightly past the duration in case the spot stream goes silent
    reactor.callLater(args.minutes * 60 + 30, lambda: stop_clean() if reactor.running else None)

    # Handle SIGTERM gracefully
    def sigterm_handler(*_):
        print("  ⏹  SIGTERM received — closing CSV and exiting", flush=True)
        stop_clean()
    signal.signal(signal.SIGTERM, sigterm_handler)

    client.startService()
    reactor.run()

    print(f"\n=== DONE: {state['tick_count']} ticks captured to {csv_path} ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
