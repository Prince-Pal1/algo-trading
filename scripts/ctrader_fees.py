#!/usr/bin/env python3
"""Discover live XAUUSD trading costs on cTrader demo.

Queries ProtoOASymbolByIdReq for XAUUSD's full metadata (commission,
contract size, min/max volume, swap rates) AND samples N spot ticks to
compute observed spread distribution.

Translates raw cTrader fields into:
  - $/lot/side (commission)
  - $/lot/round-trip (commission × 2)
  - pip-equivalent spread + $/lot/round-trip from spread
  - total $/lot/round-trip (spread + commission)

Compares against the backtest assumption in config/broker_fees.toml
profile `ic_markets_ctrader_xauusd_normal`.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from statistics import mean, median

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
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
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
TARGET_SYMBOL = "XAUUSD"

state = {
    "symbol_id": None,
    "symbol_meta": None,
    "spreads": [],  # list of (bid, ask, spread_in_quote_units)
    "ok": False,
}


def stop(success: bool) -> None:
    state["ok"] = success
    if reactor.running:
        reactor.callLater(0.1, reactor.stop)


def on_connected(client: Client) -> None:
    print(f"  ✓ TCP connected\n")
    req = ProtoOAApplicationAuthReq()
    req.clientId = CLIENT_ID
    req.clientSecret = CLIENT_SECRET
    client.send(req)


def on_msg(client: Client, message) -> None:
    msg = Protobuf.extract(message)

    if isinstance(msg, ProtoOAErrorRes):
        print(f"  ✗ ERROR {msg.errorCode}: {msg.description}")
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
            if s.symbolName.upper() == TARGET_SYMBOL:
                state["symbol_id"] = s.symbolId
                break
        if state["symbol_id"] is None:
            print(f"  ✗ {TARGET_SYMBOL} not found")
            stop(False)
            return
        # Pull full metadata for the symbol
        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.symbolId.append(state["symbol_id"])
        client.send(req)
        return

    if isinstance(msg, ProtoOASymbolByIdRes):
        if not msg.symbol:
            print("  ✗ no symbol metadata returned")
            stop(False)
            return
        sym = msg.symbol[0]
        state["symbol_meta"] = sym
        # Subscribe to spot ticks
        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.symbolId.append(state["symbol_id"])
        client.send(req)
        return

    if isinstance(msg, ProtoOASubscribeSpotsRes):
        print(f"  ✓ subscribed to {TARGET_SYMBOL} spot — sampling {TARGET_TICKS} ticks...\n")
        return

    if isinstance(msg, ProtoOASpotEvent):
        if not (msg.HasField("bid") and msg.HasField("ask")):
            return
        # cTrader prices are in 1e5 scaled integer units
        bid = float(msg.bid) / 100_000.0
        ask = float(msg.ask) / 100_000.0
        spread = ask - bid
        state["spreads"].append((bid, ask, spread))
        if len(state["spreads"]) % 10 == 0:
            print(f"  ... {len(state['spreads'])}/{TARGET_TICKS} ticks  (latest spread=${spread:.4f})")
        if len(state["spreads"]) >= TARGET_TICKS:
            stop(True)


def report() -> None:
    sym = state["symbol_meta"]
    if sym is None:
        print("\nNo symbol metadata to report.")
        return

    # cTrader symbol metadata: pipPosition is the decimal place of "1 pip"
    # For XAUUSD on IC Markets: pipPosition=2 means 1 pip = 0.01 quote units (= $0.01 per oz)
    # NB: many forex platforms use pipPosition=1 for XAUUSD (= $0.10/oz = "standard pip")
    # Read the field directly so we don't guess.
    pip_position = sym.pipPosition
    pip_size = 10 ** (-pip_position)  # quote-currency units per pip

    # Lot size: commonly 100 oz for gold standard lot, but can be in cents (10000)
    # cTrader lotSize is in HUNDREDTHS of base units. 100 oz lot = 10000 cents.
    lot_units = sym.lotSize / 100.0  # convert from cents to base units (oz)

    # Commission scheduling — cTrader exposes preciseTradingCommissionRate
    # in TENTHS of CENTS per million USD of volume (tradingCommissionRate is
    # also there but in different units for different platforms)
    raw_comm = getattr(sym, "preciseTradingCommissionRate", None)
    raw_comm_legacy = getattr(sym, "tradingCommissionRate", None)
    comm_type = sym.commissionType  # enum: USD_PER_MILLION_USD = 1 etc.
    min_comm = sym.minCommission / 100.0  # in cents

    # Live spread stats
    spreads = [s for _, _, s in state["spreads"]]
    avg_spread = mean(spreads)
    med_spread = median(spreads)
    min_spread = min(spreads)
    max_spread = max(spreads)
    last_bid, last_ask, _ = state["spreads"][-1]

    # Use a representative price for cost computation
    mid_price = (last_bid + last_ask) / 2.0
    notional_per_lot = mid_price * lot_units  # USD notional per 1 lot

    print("\n" + "="*70)
    print(f"  XAUUSD COST DISCOVERY (live cTrader demo, account {ACCOUNT_ID})")
    print("="*70)

    # commissionType enum: 1=USD_PER_MILLION_USD, 2=USD_PER_LOT, 3=PERCENT, 4=QUOTE_CCY_PER_LOT
    comm_type_name = {1: "USD_PER_MILLION_USD", 2: "USD_PER_LOT", 3: "PERCENTAGE_OF_VALUE", 4: "QUOTE_CCY_PER_LOT"}.get(comm_type, f"UNKNOWN({comm_type})")
    print(f"\nSymbol metadata (from cTrader, symbolId={sym.symbolId}):")
    print(f"  pipPosition:              {pip_position}  (1 pip = ${pip_size:.4f} per oz)")
    print(f"  digits:                   {sym.digits}")
    print(f"  lotSize (raw):            {sym.lotSize}  → {lot_units:g} oz per standard lot")
    print(f"  minVolume:                {sym.minVolume / 100.0:g} units")
    print(f"  maxVolume:                {sym.maxVolume / 100.0:g} units")
    print(f"  stepVolume:               {sym.stepVolume / 100.0:g} units")
    print(f"  commission (raw):         {sym.commission}")
    print(f"  commissionType:           {comm_type}  ({comm_type_name})")
    print(f"  preciseTradingCommissionRate: {raw_comm}")
    print(f"  preciseMinCommission:     {getattr(sym, 'preciseMinCommission', 0)}")
    print(f"  minCommission (raw):      {sym.minCommission}")
    swap_long = getattr(sym, "swapLong", None)
    swap_short = getattr(sym, "swapShort", None)
    print(f"  swapLong (overnight):     {swap_long}")
    print(f"  swapShort (overnight):    {swap_short}")
    print(f"  swapCalculationType:      {getattr(sym, 'swapCalculationType', None)}")

    print(f"\nLive spot ticks ({len(spreads)} samples):")
    print(f"  last bid / ask:           ${last_bid:.4f} / ${last_ask:.4f}")
    print(f"  spread:                   ${min_spread:.4f} (min)  ${med_spread:.4f} (median)  ${avg_spread:.4f} (avg)  ${max_spread:.4f} (max)")
    spread_pips_avg = avg_spread / pip_size
    print(f"  avg spread in pips:       {spread_pips_avg:.2f} pips")

    print(f"\nDerived costs (at mid ${mid_price:.4f} × {lot_units:g} oz lot = ${notional_per_lot:,.0f} notional/lot):")
    # Spread cost per round-trip (pay spread once on entry + once on exit)
    spread_cost_oneway_per_lot = avg_spread * lot_units
    spread_cost_rt_per_lot = spread_cost_oneway_per_lot * 2
    print(f"  spread cost / lot / side:        ${spread_cost_oneway_per_lot:.4f}")
    print(f"  spread cost / lot / round-trip:  ${spread_cost_rt_per_lot:.4f}")
    # Commission: cTrader typical = $3 per $100k = $30 per million
    # If preciseTradingCommissionRate is in tenths-of-cents per million:
    #   value = 3000 means $30 per million USD = $3 per $100k
    # Interpret the commission. cTrader exposes both `commission` (legacy, scaled
    # int) and `preciseTradingCommissionRate` (newer, higher resolution). Decoding
    # depends on commissionType.
    comm_per_lot_side = None
    if comm_type == 1:  # USD_PER_MILLION_USD — IC Markets cTrader Raw uses this
        # The `commission` field is in HUNDREDTHS of USD per million.
        # So commission=3000 → $30 per million USD = $3 per $100k notional.
        usd_per_million = sym.commission / 100.0
        comm_per_lot_side = (notional_per_lot / 1_000_000) * usd_per_million
        print(f"  → decoded: ${usd_per_million:.2f} per $1M notional (= ${usd_per_million/10:.4f} per $100k)")
        print(f"  → ${comm_per_lot_side:.4f} per lot per side  →  ${comm_per_lot_side*2:.4f} per lot round-trip")
    elif comm_type == 2:  # USD_PER_LOT
        per_lot = sym.commission / 100.0
        comm_per_lot_side = per_lot
        print(f"  → decoded: ${per_lot:.4f} per lot per side  →  ${per_lot*2:.4f} per lot round-trip")
    else:
        print(f"  → commissionType={comm_type} not yet decoded in this script — see Spotware docs")

    # Backtest assumption (from config/broker_fees.toml: ic_markets_ctrader_xauusd_normal)
    # NB: backtest config uses pip_size = 0.10 (= 1 pip = $0.10 per oz, "standard pip")
    # The cTrader API exposes pipPosition=2 → pip_size = $0.01 per oz ("pipette" / 5-digit notation)
    # So a "0.30 pip" backtest spread (in standard notation) = 3 pipettes = $0.03 per oz
    bt_pip_size_std = 0.10  # what config/broker_fees.toml uses
    bt_spread_pips_std = 0.30
    bt_spread_per_oz = bt_spread_pips_std * bt_pip_size_std
    bt_spread_oneway_per_lot = bt_spread_per_oz * lot_units
    bt_comm_oneway_per_lot = (notional_per_lot / 100_000) * 3.0  # $3 per $100k
    bt_total_rt = (bt_spread_oneway_per_lot * 2) + (bt_comm_oneway_per_lot * 2)

    print(f"\nBacktest assumption (config/broker_fees.toml ic_markets_ctrader_xauusd_normal):")
    print(f"  spread:                   0.30 std pips ($0.03/oz) = ${bt_spread_oneway_per_lot:.4f}/lot/side")
    print(f"  commission:               $3 per $100k = ${bt_comm_oneway_per_lot:.4f}/lot/side")
    print(f"  TOTAL backtest cost:      ${bt_total_rt:.4f}/lot/round-trip")

    # Live total cost
    if comm_per_lot_side is not None:
        live_total_rt = spread_cost_rt_per_lot + (comm_per_lot_side * 2)
        print(f"\nLive cost (avg observed spread + cTrader-published commission):")
        print(f"  spread cost:              ${spread_cost_rt_per_lot:.4f}/lot/round-trip")
        print(f"  commission cost:          ${comm_per_lot_side * 2:.4f}/lot/round-trip")
        print(f"  TOTAL live cost:          ${live_total_rt:.4f}/lot/round-trip")
        diff_abs = live_total_rt - bt_total_rt
        diff_pct = (diff_abs / bt_total_rt) * 100
        verdict = "✅ LIVE WITHIN 10% OF BACKTEST" if abs(diff_pct) < 10 else \
                  "⚠️  LIVE >10% MORE EXPENSIVE — bump backtest assumption" if diff_pct > 0 else \
                  "⚠️  LIVE >10% CHEAPER — backtest is being conservative"
        print(f"\n  Δ vs backtest:           ${diff_abs:+.4f} ({diff_pct:+.1f}%)")
        print(f"  → {verdict}")


def main() -> int:
    print(f"=== XAUUSD fees discovery → {HOST}:{PORT} ===\n")
    client = Client(HOST, PORT, TcpProtocol)
    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(lambda c, r: None)
    client.setMessageReceivedCallback(on_msg)
    client.startService()
    reactor.callLater(60, lambda: stop(len(state["spreads"]) >= 10) if reactor.running else None)
    reactor.run()
    if state["ok"] or state["spreads"]:
        report()
        return 0
    print("\n=== ❌ FAIL — no usable data collected ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
