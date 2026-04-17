#!/usr/bin/env python3
"""cTrader execution-speed smoke test.

Places a MARKET BUY of 0.01 lots (= 1 oz ≈ $4,865 notional) on XAUUSD,
waits for the fill, then immediately closes the position with
ProtoOAClosePositionReq. Measures:

    t_auth           — app_auth → account_auth wallclock
    t_buy_send_fill  — NewOrderReq send → ExecutionEvent filled
    t_close_send_fill — ClosePositionReq send → ExecutionEvent filled
    t_round_trip     — buy-sent → close-filled (whole cycle)

Reports each leg in milliseconds + evaluates vs IC Markets's claimed
sub-20ms ECN execution.

Designed for the DEMO account (CTRADER_ENVIRONMENT=demo in .env). Uses
minimum volume to keep exposure tiny (<$10 notional, margin <$0.01 at
1000× leverage). Never run on live without review.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
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
    ProtoOAClosePositionReq,
    ProtoOAErrorRes,
    ProtoOAExecutionEvent,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOANewOrderReq,
    ProtoOASubscribeSpotsReq,
    ProtoOASubscribeSpotsRes,
    ProtoOASpotEvent,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAExecutionType,
    ProtoOAOrderType,
    ProtoOATradeSide,
)
from twisted.internet import reactor


CLIENT_ID = os.environ["CTRADER_CLIENT_ID"]
CLIENT_SECRET = os.environ["CTRADER_CLIENT_SECRET"]
ACCESS_TOKEN = os.environ["CTRADER_ACCESS_TOKEN"]
ACCOUNT_ID = int(os.environ["CTRADER_ACCOUNT_ID"])
HOST = os.environ.get("CTRADER_DEMO_HOST", "demo.ctraderapi.com")
PORT = int(os.environ.get("CTRADER_PORT", "5035"))

VOLUME = 100  # 0.01 lot = 1 oz of gold (minimum size)


@dataclass
class Timings:
    t_connect_start: float | None = None
    t_connected: float | None = None
    t_app_auth_sent: float | None = None
    t_app_auth_ok: float | None = None
    t_account_auth_sent: float | None = None
    t_account_auth_ok: float | None = None
    t_buy_sent: float | None = None
    t_buy_filled: float | None = None
    t_close_sent: float | None = None
    t_close_filled: float | None = None
    errors: list[str] = field(default_factory=list)
    fill_prices: dict[str, float] = field(default_factory=dict)
    position_id: int | None = None
    symbol_id: int | None = None
    last_spot_bid: float | None = None
    last_spot_ask: float | None = None


state = Timings()


def _now() -> float:
    return time.monotonic()


def _wall() -> str:
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="milliseconds")


def stop(success: bool = True):
    if reactor.running:
        reactor.callLater(0.1, reactor.stop)


def on_connected(client: Client) -> None:
    state.t_connected = _now()
    print(f"  [{_wall()}] ✓ TCP connected")
    req = ProtoOAApplicationAuthReq()
    req.clientId = CLIENT_ID
    req.clientSecret = CLIENT_SECRET
    state.t_app_auth_sent = _now()
    client.send(req)


def on_msg(client: Client, message) -> None:
    msg = Protobuf.extract(message)

    if isinstance(msg, ProtoOAErrorRes):
        err = f"{msg.errorCode}: {msg.description}"
        print(f"  [{_wall()}] ✗ ERROR {err}")
        state.errors.append(err)
        stop(False)
        return

    if isinstance(msg, ProtoOAApplicationAuthRes):
        state.t_app_auth_ok = _now()
        print(f"  [{_wall()}] ✓ app authenticated  ({(state.t_app_auth_ok - state.t_app_auth_sent)*1000:.1f}ms)")
        req = ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = ACCESS_TOKEN
        client.send(req)
        return

    if isinstance(msg, ProtoOAGetAccountListByAccessTokenRes):
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.accessToken = ACCESS_TOKEN
        state.t_account_auth_sent = _now()
        client.send(req)
        return

    if isinstance(msg, ProtoOAAccountAuthRes):
        state.t_account_auth_ok = _now()
        print(f"  [{_wall()}] ✓ account authenticated  ({(state.t_account_auth_ok - state.t_account_auth_sent)*1000:.1f}ms)")
        # Need symbol list to find XAUUSD
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.includeArchivedSymbols = False
        client.send(req)
        return

    if isinstance(msg, ProtoOASymbolsListRes):
        for s in msg.symbol:
            if s.symbolName.upper() == "XAUUSD":
                state.symbol_id = s.symbolId
                break
        if state.symbol_id is None:
            print(f"  [{_wall()}] ✗ XAUUSD not found")
            stop(False)
            return
        print(f"  [{_wall()}] ✓ XAUUSD symbolId={state.symbol_id}")
        # Subscribe to spot so we have a quote before firing the order
        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.symbolId.append(state.symbol_id)
        client.send(req)
        return

    if isinstance(msg, ProtoOASubscribeSpotsRes):
        print(f"  [{_wall()}] ✓ spot subscribed — waiting for first tick to place BUY")
        return

    if isinstance(msg, ProtoOASpotEvent):
        if msg.HasField("bid"):
            state.last_spot_bid = float(msg.bid) / 100_000.0
        if msg.HasField("ask"):
            state.last_spot_ask = float(msg.ask) / 100_000.0
        # Fire the BUY as soon as we've seen a valid bid+ask and haven't
        # sent it yet.
        if (
            state.last_spot_bid
            and state.last_spot_ask
            and state.t_buy_sent is None
        ):
            print(
                f"  [{_wall()}] ▶ firing MARKET BUY 0.01 lot  "
                f"(reference bid={state.last_spot_bid:.2f} ask={state.last_spot_ask:.2f})"
            )
            req = ProtoOANewOrderReq()
            req.ctidTraderAccountId = ACCOUNT_ID
            req.symbolId = state.symbol_id
            req.orderType = ProtoOAOrderType.MARKET
            req.tradeSide = ProtoOATradeSide.BUY
            req.volume = VOLUME
            state.t_buy_sent = _now()
            client.send(req)
        return

    if isinstance(msg, ProtoOAExecutionEvent):
        etype = msg.executionType
        etype_name = ProtoOAExecutionType.Name(etype)
        # ORDER_FILLED = 3 typically
        pos_id = msg.position.positionId if msg.HasField("position") else None
        fill_price = None
        if msg.HasField("order") and msg.order.HasField("executionPrice"):
            fill_price = msg.order.executionPrice

        print(f"  [{_wall()}] ⇒ execution_event type={etype_name} pos={pos_id} fill={fill_price}")

        if etype == ProtoOAExecutionType.Value("ORDER_FILLED"):
            if state.t_buy_filled is None:
                state.t_buy_filled = _now()
                state.position_id = pos_id
                if fill_price:
                    state.fill_prices["buy"] = fill_price
                leg = (state.t_buy_filled - state.t_buy_sent) * 1000
                print(f"  [{_wall()}] ✓ BUY filled ({leg:.1f}ms send→fill)  — now closing")
                if pos_id is None:
                    state.errors.append("BUY filled but positionId missing — cannot close")
                    stop(False)
                    return
                # Close the position
                req = ProtoOAClosePositionReq()
                req.ctidTraderAccountId = ACCOUNT_ID
                req.positionId = pos_id
                req.volume = VOLUME
                state.t_close_sent = _now()
                client.send(req)
            elif state.t_close_filled is None:
                state.t_close_filled = _now()
                if fill_price:
                    state.fill_prices["close"] = fill_price
                leg = (state.t_close_filled - state.t_close_sent) * 1000
                print(f"  [{_wall()}] ✓ CLOSE filled ({leg:.1f}ms send→fill)")
                stop(True)


def main() -> int:
    print(f"=== cTrader SPEED TEST → {HOST}:{PORT} ===\n")
    print(f"  Target: MARKET BUY 0.01 lot XAUUSD → immediate close via ProtoOAClosePositionReq\n")

    state.t_connect_start = _now()
    client = Client(HOST, PORT, TcpProtocol)
    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(lambda c, r: None)
    client.setMessageReceivedCallback(on_msg)
    client.startService()

    reactor.callLater(45, lambda: stop(False) if reactor.running else None)
    reactor.run()

    # Report
    print("\n" + "=" * 70)
    print("  LATENCY REPORT")
    print("=" * 70)

    def _show(label: str, start: float | None, end: float | None) -> None:
        if start is None or end is None:
            print(f"  {label:32s} (n/a — missing timestamp)")
            return
        print(f"  {label:32s} {(end - start) * 1000:8.1f} ms")

    _show("TCP connect → connected",       state.t_connect_start, state.t_connected)
    _show("App auth send → response",      state.t_app_auth_sent, state.t_app_auth_ok)
    _show("Account auth send → response",  state.t_account_auth_sent, state.t_account_auth_ok)
    _show("BUY send → filled",             state.t_buy_sent, state.t_buy_filled)
    _show("CLOSE send → filled",           state.t_close_sent, state.t_close_filled)
    _show("BUY sent → CLOSE filled (rt)",  state.t_buy_sent, state.t_close_filled)

    # P&L from the round-trip (should be a small loss = spread + commission)
    if "buy" in state.fill_prices and "close" in state.fill_prices:
        buy_px = state.fill_prices["buy"]
        close_px = state.fill_prices["close"]
        # 0.01 lot = 1 oz. P&L per oz = (close - buy). cTrader prices are
        # already human-readable (no 1e5 scaling for execution prices — they
        # come through as decimals in this field).
        pnl_per_oz = close_px - buy_px
        print(f"\n  Fill prices: buy=${buy_px} close=${close_px}")
        print(f"  P&L per oz: ${pnl_per_oz}  (1 oz total, ignoring commission)")

    # Verdict
    if state.t_buy_sent and state.t_buy_filled:
        buy_fill_ms = (state.t_buy_filled - state.t_buy_sent) * 1000
        print()
        if buy_fill_ms < 20:
            print(f"  ✅ BUY fill latency {buy_fill_ms:.1f}ms meets IC Markets's sub-20ms ECN claim")
        elif buy_fill_ms < 50:
            print(f"  🟡 BUY fill latency {buy_fill_ms:.1f}ms acceptable but slower than claimed sub-20ms")
        else:
            print(f"  ⚠️  BUY fill latency {buy_fill_ms:.1f}ms is substantially above ECN expectations")

    if state.errors:
        print(f"\n  Errors: {state.errors}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
