#!/usr/bin/env python3
import sys
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.generate_scalp_zones_report import detect_zones

df = pd.read_parquet(REPO_ROOT / "data" / "historical" / "XAUUSD_1m.parquet")
df["dt"] = pd.to_datetime(df["timestamp"], unit="ms")

# April 2026 latest
df_latest = df[(df["dt"] >= "2026-04-06") & (df["dt"] <= "2026-04-13")].copy().reset_index(drop=True)
events_latest = detect_zones(df_latest, threshold=85.0, cooldown_bars=10)

print("=== LATEST SCALP ZONES IN DATASET (April 2026) ===")
for ev in sorted(events_latest, key=lambda x: x["dt"])[-12:]:
    print(f"Time: {ev['dt'].strftime('%Y-%m-%d %H:%M:%S UTC')} | Dir: {ev['direction']:5s} | Score: {ev['score']:4.1f}/100 | Price: ${ev['close']:7.2f} | Move: ${ev['net_move']:5.2f} ({ev['atr_norm_move']:.1f}x ATR) | ER: {ev['er']:.2f} | R²: {ev['r2']:.2f}")

# March 24-28
df_march = df[(df["dt"] >= "2026-03-24") & (df["dt"] <= "2026-03-29")].copy().reset_index(drop=True)
events_march = detect_zones(df_march, threshold=85.0, cooldown_bars=10)

print("\n=== TOP SCALP ZONES IN MARCH 24-28 REFERENCE WEEK ===")
for ev in sorted(events_march, key=lambda x: x["score"], reverse=True)[:10]:
    print(f"Time: {ev['dt'].strftime('%Y-%m-%d %H:%M:%S UTC')} | Dir: {ev['direction']:5s} | Score: {ev['score']:4.1f}/100 | Price: ${ev['close']:7.2f} | Move: ${ev['net_move']:5.2f} ({ev['atr_norm_move']:.1f}x ATR) | ER: {ev['er']:.2f} | R²: {ev['r2']:.2f}")
