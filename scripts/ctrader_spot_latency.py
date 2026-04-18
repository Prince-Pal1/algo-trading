#!/usr/bin/env python3
"""cTrader spot-event latency smoke test.

READ-ONLY — does NOT place orders. Subscribes to XAUUSD spot, captures
~30 live ticks, and measures wallclock delta between the server-side
event timestamp (ProtoOASpotEvent.timestamp, ms) and our receipt time.

Comparable to scripts/binance_speed_test.py so the two brokers can be
compared on the same "market-data dissemination latency" metric.

XAUUSD market closes weekends (Fri 22:00 UTC → Sun 22:00 UTC). Run
this OUTSIDE the closure window; if no ticks arrive inside 20s, the
script exits with a partial result and the message to retry.

Run: python3 scripts/ctrader_spot_latency.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time
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

TARGET_TICKS = 30
HARD_TIMEOUT = 25.0

state = {
    "symbol_id": None,
    "latencies": [],
    "ok": False,
}


def stop(ok: bool) -> None:
    state["ok"] = ok
    if reactor.running:
        reactor.callLater(0.1, reactor.stop)


def on_connected(client: Client) -> None:
    req = ProtoOAApplicationAuthReq()
    req.clientId = CLIENT_ID
    req.clientSecret = CLIENT_SECRET
    client.send(req)


def on_msg(client: Client, message) -> None:
    msg = Protobuf.extract(message)

    if isinstance(msg, ProtoOAErrorRes):
        print(f"  ✗ error {msg.errorCode}: {msg.description}")
        stop(False)
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
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.includeArchivedSymbols = False
        client.send(req)
        return

    if isinstance(msg, ProtoOASymbolsListRes):
        for s in msg.symbol:
            if s.symbolName.upper() == "XAUUSD":
                state["symbol_id"] = s.symbolId
                break
        if state["symbol_id"] is None:
            print("  ✗ XAUUSD not found")
            stop(False)
            return
        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.symbolId.append(state["symbol_id"])
        client.send(req)
        return

    if isinstance(msg, ProtoOASubscribeSpotsRes):
        print(f"  ✓ subscribed — collecting {TARGET_TICKS} ticks (timeout {HARD_TIMEOUT}s)")
        return

    if isinstance(msg, ProtoOASpotEvent):
        # Server-side event timestamp → local receipt delta
        if not msg.HasField("timestamp"):
            return
        server_ms = float(msg.timestamp)
        now_ms = time.time() * 1000
        lat = now_ms - server_ms
        state["latencies"].append(lat)
        if len(state["latencies"]) % 5 == 0:
            print(f"    {len(state['latencies'])}/{TARGET_TICKS} ticks  (last: {lat:.1f}ms)")
        if len(state["latencies"]) >= TARGET_TICKS:
            stop(True)


def main() -> int:
    print(f"=== cTrader spot-event latency → {HOST}:{PORT} ===\n")
    print(f"Targeting {TARGET_TICKS} XAUUSD ticks (hard timeout {HARD_TIMEOUT:.0f}s)\n")
    client = Client(HOST, PORT, TcpProtocol)
    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(
        lambda c, reason: print(f"  disconnected: {reason}")
    )
    client.setMessageReceivedCallback(on_msg)
    client.startService()
    reactor.callLater(HARD_TIMEOUT, lambda: stop(False) if reactor.running else None)
    reactor.run()

    samples = state["latencies"]
    print("\n=== RESULTS ===\n")
    if not samples:
        print("  ✗ no ticks received — market likely closed (XAUUSD closes Fri 22:00 UTC → Sun 22:00 UTC).")
        return 1
    mn = min(samples); mx = max(samples); med = statistics.median(samples)
    mean = statistics.mean(samples)
    p95 = statistics.quantiles(samples, n=20)[18] if len(samples) >= 2 else mx
    print(f"  n={len(samples)}  min={mn:.1f}ms  median={med:.1f}ms  "
          f"mean={mean:.1f}ms  p95={p95:.1f}ms  max={mx:.1f}ms")
    print(f"\n  (Compare: Binance WS median 48.7ms, p95 52ms, max 65ms on 3 symbols.)")
    return 0 if len(samples) >= TARGET_TICKS else 1


if __name__ == "__main__":
    sys.exit(main())
