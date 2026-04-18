#!/usr/bin/env python3
"""Binance REST round-trip latency smoke test.

Hits three representative public endpoints 10× each and measures
wallclock round-trip time:

  /api/v3/ping            — tiniest possible response (0 bytes body)
  /api/v3/time            — serverTime reference, also gives clock skew
  /api/v3/ticker/bookTicker?symbol=BTCUSDT  — small-body market data

No auth, no orders, no exposure. Comparable to cTrader's 6ms raw
TCP RTT figure in commit 8800c7b (but this also includes TLS
renegotiation cost since we open fresh connections per request —
representative of "cold" order latency, not warm keepalive).

Run: python3 scripts/binance_rest_latency.py
"""

from __future__ import annotations

import ssl
import statistics
import sys
import time
import urllib.request

import certifi

ENDPOINTS = [
    ("ping",          "https://api.binance.com/api/v3/ping"),
    ("serverTime",    "https://api.binance.com/api/v3/time"),
    ("bookTicker",    "https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT"),
]
N = 10

SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def hit(url: str) -> tuple[float, int]:
    t0 = time.perf_counter()
    req = urllib.request.Request(url, headers={"User-Agent": "algo-trading/speed-test"})
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=5) as resp:
        body = resp.read()
    t1 = time.perf_counter()
    return (t1 - t0) * 1000.0, len(body)


def main() -> int:
    print(f"=== Binance REST latency → api.binance.com ({N}× per endpoint) ===\n")

    all_latencies: dict[str, list[float]] = {}
    body_sizes: dict[str, int] = {}

    # Warm-up: one throwaway call to prime DNS + any intermediate caches.
    try:
        hit(ENDPOINTS[0][1])
    except Exception as e:
        print(f"  ✗ warm-up failed: {e}")
        return 1

    for label, url in ENDPOINTS:
        samples: list[float] = []
        size = 0
        for _ in range(N):
            try:
                lat, size = hit(url)
                samples.append(lat)
            except Exception as e:
                print(f"  {label}: ✗ {e}")
        all_latencies[label] = samples
        body_sizes[label] = size

    header = f"{'endpoint':<14} {'n':>3}  {'min':>7}  {'median':>7}  {'p95':>7}  {'max':>7}  body"
    print(header)
    print("-" * (len(header) + 4))
    for label in all_latencies:
        s = all_latencies[label]
        if not s:
            print(f"{label:<14}   0  (no samples)")
            continue
        mn = min(s); mx = max(s); med = statistics.median(s)
        p95 = statistics.quantiles(s, n=20)[18] if len(s) >= 2 else mx
        print(f"{label:<14} {len(s):>3}  {mn:>6.1f}ms  {med:>6.1f}ms  {p95:>6.1f}ms  {mx:>6.1f}ms   {body_sizes[label]}B")

    combined = [x for s in all_latencies.values() for x in s]
    if combined:
        print()
        print(f"overall n={len(combined)}  min={min(combined):.1f}ms  "
              f"median={statistics.median(combined):.1f}ms  "
              f"mean={statistics.mean(combined):.1f}ms  "
              f"max={max(combined):.1f}ms")
        # The tiniest endpoint (ping) is dominated by TCP+TLS+server-side
        # response; the median across those runs is a good proxy for the
        # minimum possible order-place latency from this location.
        if all_latencies.get("ping"):
            ping_med = statistics.median(all_latencies["ping"])
            print(f"\n  ping-median ≈ {ping_med:.1f}ms — this is the floor for any "
                  f"signed REST call from this machine to api.binance.com\n  "
                  f"(cTrader TCP RTT was 6ms per commit 8800c7b).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
