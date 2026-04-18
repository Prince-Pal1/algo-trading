#!/usr/bin/env python3
"""Binance WebSocket latency smoke test.

Opens a separate WS connection (does NOT touch the running engine) to
spot `@trade` streams for BTCUSDT + ETHUSDT + XRPUSDT, samples ~30
ticks, and measures wallclock delta between the server-side event
timestamp (`E` in the Binance payload, ms) and our receipt time.

Reports per-symbol min / median / p95 / max / count. Mirrors the
spirit of scripts/ctrader_speed_test.py (connection-level latency smoke),
but read-only — no order placement, zero exposure.

Run: python3 scripts/binance_speed_test.py
"""

from __future__ import annotations

import asyncio
import json
import ssl
import statistics
import time

import certifi
import websockets

SYMBOLS = ["btcusdt", "ethusdt", "xrpusdt"]
STREAMS = "/".join(f"{s}@trade" for s in SYMBOLS)
URL = f"wss://stream.binance.com:9443/stream?streams={STREAMS}"
TARGET_TICKS_PER_SYMBOL = 10
HARD_TIMEOUT_S = 20.0


async def main() -> int:
    print(f"=== Binance WS speed test → {URL} ===\n")
    print(f"Targeting {TARGET_TICKS_PER_SYMBOL} ticks per symbol "
          f"(hard timeout {HARD_TIMEOUT_S:.0f}s)\n")

    latencies: dict[str, list[float]] = {s.upper(): [] for s in SYMBOLS}
    t_connect_start = time.perf_counter()

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    async with websockets.connect(URL, ssl=ssl_ctx, ping_interval=20, ping_timeout=10) as ws:
        t_connected = time.perf_counter()
        print(f"  ✓ connected in {(t_connected - t_connect_start) * 1000:.1f} ms\n")

        deadline = time.perf_counter() + HARD_TIMEOUT_S
        while time.perf_counter() < deadline:
            if all(len(v) >= TARGET_TICKS_PER_SYMBOL for v in latencies.values()):
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            now_ms = time.time() * 1000
            payload = json.loads(raw)
            data = payload.get("data", {})
            symbol = str(data.get("s", "")).upper()
            event_ms = data.get("E")
            if not symbol or event_ms is None:
                continue
            if len(latencies.get(symbol, [])) >= TARGET_TICKS_PER_SYMBOL:
                continue
            lat_ms = now_ms - float(event_ms)
            latencies[symbol].append(lat_ms)

    print("=== RESULTS ===\n")
    header = f"{'symbol':<10} {'n':>3}  {'min':>7}  {'median':>7}  {'p95':>7}  {'max':>7}"
    print(header)
    print("-" * len(header))
    all_samples: list[float] = []
    for sym in sorted(latencies):
        s = latencies[sym]
        if not s:
            print(f"{sym:<10}   0  (no samples)")
            continue
        all_samples.extend(s)
        mn = min(s); mx = max(s); med = statistics.median(s)
        p95 = statistics.quantiles(s, n=20)[18] if len(s) >= 2 else mx
        print(f"{sym:<10} {len(s):>3}  {mn:>6.1f}ms  {med:>6.1f}ms  {p95:>6.1f}ms  {mx:>6.1f}ms")

    if all_samples:
        print()
        print(f"overall n={len(all_samples)}  min={min(all_samples):.1f}ms  "
              f"median={statistics.median(all_samples):.1f}ms  "
              f"mean={statistics.mean(all_samples):.1f}ms  "
              f"max={max(all_samples):.1f}ms")

    ok = all(len(v) >= TARGET_TICKS_PER_SYMBOL for v in latencies.values())
    print(f"\n{'✅ PASS' if ok else '⚠️ PARTIAL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
