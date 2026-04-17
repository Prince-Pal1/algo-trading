#!/usr/bin/env python3
"""cTrader speed test — looped version.

Authenticates once, then runs N BUY→CLOSE round-trips back-to-back on
the same persistent TCP + protobuf session. Reports per-trade latency
+ averages + identifies cold-start overhead.

Usage:
    python3 scripts/ctrader_speed_test_loop.py [--n 5]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median, stdev

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
VOLUME = 100  # 0.01 lot = 1 oz


@dataclass
class TradeTiming:
    idx: int
    t_buy_sent: float | None = None
    t_buy_filled: float | None = None
    t_close_sent: float | None = None
    t_close_filled: float | None = None
    buy_price: float | None = None
    close_price: float | None = None
    position_id: int | None = None

    @property
    def buy_ms(self) -> float | None:
        if self.t_buy_sent and self.t_buy_filled:
            return (self.t_buy_filled - self.t_buy_sent) * 1000
        return None

    @property
    def close_ms(self) -> float | None:
        if self.t_close_sent and self.t_close_filled:
            return (self.t_close_filled - self.t_close_sent) * 1000
        return None

    @property
    def rt_ms(self) -> float | None:
        if self.t_buy_sent and self.t_close_filled:
            return (self.t_close_filled - self.t_buy_sent) * 1000
        return None

    @property
    def complete(self) -> bool:
        return self.t_close_filled is not None


@dataclass
class LoopState:
    trades: list[TradeTiming] = field(default_factory=list)
    current_idx: int = -1
    symbol_id: int | None = None
    last_bid: float | None = None
    last_ask: float | None = None
    t_auth_complete: float | None = None
    target_n: int = 5
    errors: list[str] = field(default_factory=list)
    # Flag so we start the first trade only after BOTH spot subscribe
    # confirm AND a first tick arrived (which gives us bid/ask).
    ready_to_trade: bool = False


state = LoopState()


def _now() -> float:
    return time.monotonic()


def stop(success: bool = True):
    if reactor.running:
        reactor.callLater(0.1, reactor.stop)


def _fire_buy(client: Client) -> None:
    state.current_idx += 1
    t = TradeTiming(idx=state.current_idx)
    state.trades.append(t)
    print(
        f"  [trade {t.idx+1}/{state.target_n}] firing BUY  "
        f"(bid={state.last_bid:.2f} ask={state.last_ask:.2f})"
    )
    req = ProtoOANewOrderReq()
    req.ctidTraderAccountId = ACCOUNT_ID
    req.symbolId = state.symbol_id
    req.orderType = ProtoOAOrderType.MARKET
    req.tradeSide = ProtoOATradeSide.BUY
    req.volume = VOLUME
    t.t_buy_sent = _now()
    client.send(req)


def on_connected(client: Client) -> None:
    print("  ✓ TCP connected")
    req = ProtoOAApplicationAuthReq()
    req.clientId = CLIENT_ID
    req.clientSecret = CLIENT_SECRET
    client.send(req)


def on_msg(client: Client, message) -> None:
    msg = Protobuf.extract(message)

    if isinstance(msg, ProtoOAErrorRes):
        print(f"  ✗ ERROR {msg.errorCode}: {msg.description}")
        state.errors.append(msg.description)
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
                state.symbol_id = s.symbolId
                break
        if state.symbol_id is None:
            print("  ✗ XAUUSD not found")
            stop(False)
            return
        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.symbolId.append(state.symbol_id)
        state.t_auth_complete = _now()
        client.send(req)
        return

    if isinstance(msg, ProtoOASubscribeSpotsRes):
        print("  ✓ auth + symbol ready — starting loop")
        # Wait for first spot tick to actually fire the first order
        return

    if isinstance(msg, ProtoOASpotEvent):
        if msg.HasField("bid"):
            state.last_bid = float(msg.bid) / 100_000.0
        if msg.HasField("ask"):
            state.last_ask = float(msg.ask) / 100_000.0
        # If we have bid+ask and haven't started trading yet, fire trade 1
        if state.last_bid and state.last_ask and state.current_idx == -1:
            _fire_buy(client)
        return

    if isinstance(msg, ProtoOAExecutionEvent):
        etype = msg.executionType
        if etype != ProtoOAExecutionType.Value("ORDER_FILLED"):
            return  # ignore ACCEPTED / REJECTED_* until FILLED

        if state.current_idx < 0 or not state.trades:
            return
        t = state.trades[state.current_idx]
        fill_price = (
            msg.order.executionPrice if msg.HasField("order") and msg.order.HasField("executionPrice") else None
        )
        pos_id = msg.position.positionId if msg.HasField("position") else None

        # BUY leg completion
        if t.t_buy_filled is None:
            t.t_buy_filled = _now()
            t.buy_price = fill_price
            t.position_id = pos_id
            print(f"    BUY filled ({t.buy_ms:.1f}ms) @ {fill_price} pos={pos_id} → closing")
            if pos_id is None:
                state.errors.append(f"trade {t.idx}: BUY filled but positionId missing")
                stop(False)
                return
            req = ProtoOAClosePositionReq()
            req.ctidTraderAccountId = ACCOUNT_ID
            req.positionId = pos_id
            req.volume = VOLUME
            t.t_close_sent = _now()
            client.send(req)
            return

        # CLOSE leg completion
        if t.t_close_filled is None:
            t.t_close_filled = _now()
            t.close_price = fill_price
            print(f"    CLOSE filled ({t.close_ms:.1f}ms) @ {fill_price}  | round-trip {t.rt_ms:.1f}ms")

            # Any more trades?
            if state.current_idx + 1 < state.target_n:
                # Small pause to avoid hammering the server
                reactor.callLater(0.1, _fire_buy, client)
            else:
                stop(True)


def _report():
    print("\n" + "=" * 72)
    print("  SPEED TEST RESULTS")
    print("=" * 72)
    complete = [t for t in state.trades if t.complete]
    print(f"  Completed: {len(complete)} / {state.target_n} trades")
    if not complete:
        return

    buy_ms = [t.buy_ms for t in complete]
    close_ms = [t.close_ms for t in complete]
    rt_ms = [t.rt_ms for t in complete]

    print()
    print(f"  {'trade':>6s}  {'BUY ms':>9s}  {'CLOSE ms':>9s}  {'RT ms':>9s}  {'buy px':>10s}  {'close px':>10s}  {'P&L/oz':>8s}")
    for t in complete:
        pnl = (t.close_price - t.buy_price) if t.buy_price and t.close_price else None
        pnl_s = f"{pnl:+.4f}" if pnl is not None else "   -   "
        print(f"  {t.idx+1:>6d}  {t.buy_ms:>9.1f}  {t.close_ms:>9.1f}  {t.rt_ms:>9.1f}  ${t.buy_price:>9.2f}  ${t.close_price:>9.2f}  {pnl_s:>8s}")

    def _stat(label: str, vals: list[float]):
        if not vals:
            return
        m = mean(vals); md = median(vals); mn = min(vals); mx = max(vals)
        sd = stdev(vals) if len(vals) > 1 else 0.0
        print(f"  {label:<14s} avg={m:6.1f}  med={md:6.1f}  min={mn:6.1f}  max={mx:6.1f}  stdev={sd:5.1f}")

    print()
    _stat("BUY send→fill", buy_ms)
    _stat("CLOSE send→fill", close_ms)
    _stat("Round-trip", rt_ms)

    # Cold vs warm: exclude the first trade as "cold"
    if len(complete) >= 2:
        warm_buy = [t.buy_ms for t in complete[1:]]
        warm_close = [t.close_ms for t in complete[1:]]
        cold = complete[0]
        print()
        print(f"  Cold-start (trade 1):          BUY {cold.buy_ms:.1f}ms  CLOSE {cold.close_ms:.1f}ms")
        print(f"  Warm steady state (trades 2+): BUY avg {mean(warm_buy):.1f}ms  CLOSE avg {mean(warm_close):.1f}ms")
        cold_overhead = cold.buy_ms - mean(warm_buy)
        if cold_overhead > 100:
            print(f"  → Cold-start overhead on BUY: {cold_overhead:+.1f}ms (significant cold-path cost)")
        else:
            print(f"  → Cold-start overhead on BUY: {cold_overhead:+.1f}ms (negligible)")

    # Verdict
    warm_buy_avg = mean([t.buy_ms for t in complete[1:]]) if len(complete) >= 2 else (complete[0].buy_ms if complete else 999)
    print()
    if warm_buy_avg < 50:
        print(f"  ✅ Warm BUY avg {warm_buy_avg:.1f}ms is within acceptable range for swing/position strategies")
    elif warm_buy_avg < 200:
        print(f"  🟡 Warm BUY avg {warm_buy_avg:.1f}ms — OK for swing, marginal for scalping (scalp edges get eaten)")
    else:
        print(f"  ⚠️  Warm BUY avg {warm_buy_avg:.1f}ms — too slow for scalping; check network RTT to demo server")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=5)
    args = p.parse_args()
    state.target_n = args.n

    print(f"=== cTrader speed loop — {args.n} back-to-back trades, shared connection ===")
    client = Client(HOST, PORT, TcpProtocol)
    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(lambda c, r: None)
    client.setMessageReceivedCallback(on_msg)
    client.startService()

    # Budget: 10 sec per trade + 10 sec auth
    reactor.callLater(10 + args.n * 10, lambda: stop(False) if reactor.running else None)
    reactor.run()

    _report()
    return 0 if not state.errors else 1


if __name__ == "__main__":
    sys.exit(main())
