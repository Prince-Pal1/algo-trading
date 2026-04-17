#!/usr/bin/env python3
"""cTrader live connection smoke test.

Connects to demo.ctraderapi.com:5035, walks the full auth chain, and
prints out:
  - The trading accounts linked to your access_token (with the protobuf
    `ctidTraderAccountId` you need in .env)
  - First 200 symbols on the chosen account (verifies XAUUSD is present)
  - A live spot quote for XAUUSD (3 ticks)

Exit 0 if all stages succeed.

Run AFTER filling .env with CLIENT_ID + CLIENT_SECRET + ACCESS_TOKEN +
ACCOUNT_ID. The script prints whether the ACCOUNT_ID needs updating.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Load .env into os.environ
for ln in Path(".env").read_text().splitlines():
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, v = ln.split("=", 1)
        os.environ[k.strip()] = v.strip().split()[0] if v.strip() else ""

from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
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
ENV_ACCOUNT_ID = int(os.environ.get("CTRADER_ACCOUNT_ID", "0"))
HOST = os.environ.get("CTRADER_DEMO_HOST", "demo.ctraderapi.com")
PORT = int(os.environ.get("CTRADER_PORT", "5035"))

state = {
    "ctid_account_id": None,
    "human_account_number": None,
    "xauusd_symbol_id": None,
    "spot_count": 0,
    "ok": False,
}

TARGET_TICKS = 3


def stop(success: bool) -> None:
    state["ok"] = success
    if reactor.running:
        reactor.callLater(0.1, reactor.stop)


def on_connected(client: Client) -> None:
    print(f"  ✓ TCP connected to {HOST}:{PORT}")
    req = ProtoOAApplicationAuthReq()
    req.clientId = CLIENT_ID
    req.clientSecret = CLIENT_SECRET
    client.send(req)


def on_disconnected(client: Client, reason) -> None:
    print(f"  ! disconnected: {reason}")


def on_msg(client: Client, message) -> None:
    msg = Protobuf.extract(message)

    if isinstance(msg, ProtoOAErrorRes):
        print(f"  ✗ ERROR {msg.errorCode}: {msg.description}")
        stop(False)
        return

    if isinstance(msg, ProtoOAApplicationAuthRes):
        print("  ✓ application authenticated")
        req = ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = ACCESS_TOKEN
        client.send(req)
        return

    if isinstance(msg, ProtoOAGetAccountListByAccessTokenRes):
        print(f"\n=== {len(msg.ctidTraderAccount)} TRADING ACCOUNT(S) LINKED TO TOKEN ===")
        for a in msg.ctidTraderAccount:
            tag = ""
            if a.traderLogin == ENV_ACCOUNT_ID:
                tag = " ★ matches CTRADER_ACCOUNT_ID in .env"
            print(f"  ctidTraderAccountId: {a.ctidTraderAccountId}")
            print(f"  traderLogin:         {a.traderLogin}{tag}")
            print(f"  isLive:              {a.isLive}")
            print()
            if state["ctid_account_id"] is None:
                state["ctid_account_id"] = a.ctidTraderAccountId
                state["human_account_number"] = a.traderLogin
        if state["ctid_account_id"] is None:
            print("  ✗ no accounts linked")
            stop(False)
            return
        if state["ctid_account_id"] != ENV_ACCOUNT_ID:
            print(f"  → .env CTRADER_ACCOUNT_ID needs update:")
            print(f"      current value:        {ENV_ACCOUNT_ID} (this is the human-facing traderLogin)")
            print(f"      protobuf API needs:   {state['ctid_account_id']} (this is ctidTraderAccountId)")
        else:
            print(f"  ✓ .env CTRADER_ACCOUNT_ID = {ENV_ACCOUNT_ID} is correct for protobuf API")
        # Continue: account auth
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = state["ctid_account_id"]
        req.accessToken = ACCESS_TOKEN
        client.send(req)
        return

    if isinstance(msg, ProtoOAAccountAuthRes):
        print(f"\n  ✓ account authenticated (ctidTraderAccountId={msg.ctidTraderAccountId})")
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = state["ctid_account_id"]
        req.includeArchivedSymbols = False
        client.send(req)
        return

    if isinstance(msg, ProtoOASymbolsListRes):
        symbols = list(msg.symbol)
        print(f"\n=== {len(symbols)} SYMBOLS AVAILABLE (showing XAUUSD-related) ===")
        gold = [s for s in symbols if "XAU" in s.symbolName.upper() or "GOLD" in s.symbolName.upper()]
        for s in gold:
            print(f"  {s.symbolName:15s}  symbolId={s.symbolId}  category={s.symbolCategoryId}  enabled={s.enabled}")
            if s.symbolName.upper() == "XAUUSD" and state["xauusd_symbol_id"] is None:
                state["xauusd_symbol_id"] = s.symbolId
        if state["xauusd_symbol_id"] is None:
            print("  ! XAUUSD not found by exact name — picking first XAU symbol")
            if gold:
                state["xauusd_symbol_id"] = gold[0].symbolId
        if state["xauusd_symbol_id"] is None:
            print("  ✗ no gold symbol available — cannot subscribe to spot")
            stop(False)
            return
        # Subscribe to spot for XAUUSD
        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = state["ctid_account_id"]
        req.symbolId.append(state["xauusd_symbol_id"])
        client.send(req)
        print(f"\n=== SUBSCRIBING TO XAUUSD SPOT (waiting for {TARGET_TICKS} ticks) ===")
        return

    if isinstance(msg, ProtoOASubscribeSpotsRes):
        print("  ✓ spot subscription confirmed")
        return

    if isinstance(msg, ProtoOASpotEvent):
        bid = float(msg.bid) / 100_000.0 if msg.HasField("bid") else None
        ask = float(msg.ask) / 100_000.0 if msg.HasField("ask") else None
        if bid is None and ask is None:
            return
        state["spot_count"] += 1
        spread = (ask - bid) if bid and ask else None
        print(f"  tick #{state['spot_count']}: bid=${bid:.3f}  ask=${ask:.3f}  spread=${spread:.3f}" if spread else f"  tick #{state['spot_count']}: bid={bid} ask={ask}")
        if state["spot_count"] >= TARGET_TICKS:
            print(f"\n  ✓ received {TARGET_TICKS} live ticks — connection is healthy")
            stop(True)


def main() -> int:
    print(f"=== cTrader smoke test → {HOST}:{PORT} (env={'demo' if 'demo' in HOST else 'live'}) ===\n")
    client = Client(HOST, PORT, TcpProtocol)
    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(on_disconnected)
    client.setMessageReceivedCallback(on_msg)
    client.startService()

    # Hard timeout
    reactor.callLater(20, lambda: stop(state["spot_count"] >= TARGET_TICKS) if reactor.running else None)
    reactor.run()

    print(f"\n=== RESULT: {'✅ PASS' if state['ok'] else '❌ FAIL'} ===")
    if state["ok"] and state["ctid_account_id"] != ENV_ACCOUNT_ID:
        print("\nACTION REQUIRED: update .env line 15:")
        print(f"  CTRADER_ACCOUNT_ID={state['ctid_account_id']}")
        print(f"  (replace the current value {ENV_ACCOUNT_ID} which is the human-facing traderLogin)")
    return 0 if state["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
