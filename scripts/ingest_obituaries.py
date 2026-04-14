#!/usr/bin/env python3
"""One-time migration: parse obituary docstrings + ingest to strategy_graveyard.

Reads the 3 killed aggressive strategies' docstring headers and extracts:
  - kill_date (from "2026-04-XX" patterns inside the History section)
  - task_ids (from "task #XX" mentions)
  - root_cause_long (the "Root cause:" paragraph)
  - revival_conditions (the "Revisit when:" / "Revisit only as" sentence)
  - kill_category (manual classifier — see _classify())
  - research_artifacts (attaches data/tier5_m1_revival.json + data/tier5_baseline.json when relevant)

Calls `record_kill()` for each. Idempotent on (strategy_name, strategy_version)
so re-running the script produces the same 3 rows with updated_at advanced.

Usage:
    python3 scripts/ingest_obituaries.py

Exit codes:
    0 — success
    1 — parsing error or DB error
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Make sure src/ is importable when run as a script
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.strategies.graveyard import (  # noqa: E402
    GraveyardEntry,
    record_kill,
)


# ── Config ──────────────────────────────────────────────────────────────

OBIT_FILES = [
    "src/strategies/aggressive/candle_burst_hunter.py",
    "src/strategies/aggressive/news_spike_fade.py",
    "src/strategies/aggressive/hedged_structure_play.py",
]

# Manual classifier — update when a new kill is added
_CATEGORY_MAP: dict[str, str] = {
    "candle_burst_hunter": "NOISE",
    "news_spike_fade": "NOISE",
    "hedged_structure_play": "PREMISE",
}

_TAGS_MAP: dict[str, list[str]] = {
    "candle_burst_hunter": ["aggressive", "tier5", "scalper", "tick-blocked"],
    "news_spike_fade": ["aggressive", "tier5", "news-event", "tick-blocked"],
    "hedged_structure_play": ["aggressive", "tier5", "hedged", "premise-mismatch"],
}

_REASON_SHORT: dict[str, str] = {
    "candle_burst_hunter": "M5 mid-bar velocity needs tick data; M1 revival failed (ATR filter degenerates)",
    "news_spike_fade": "Bar-level fade can't time peak; M1 peak-reversal fails (noise swamps trigger)",
    "hedged_structure_play": "Design mismatched to gold's sustained uptrend; all configs wiped",
}

_REVIVAL_CONDITIONS: dict[str, str] = {
    "candle_burst_hunter": (
        "Need multi-bar velocity primitive (cumulative signed travel over N bars) "
        "OR tick-level delta-price/delta-time. Revisit ONLY as a new strategy with a "
        "different signal architecture, not as a parameter tune."
    ),
    "news_spike_fade": (
        "Need volume confirmation (spike exhausted when volume collapses) OR real "
        "tick-level velocity, OR longer stall-confirmation window (3+ bars of no new high). "
        "95.7% retracement premise is real but bar/M1 execution can't capture it."
    ),
    "hedged_structure_play": (
        "Revisit ONLY in a ranging regime OR with a regime filter that gates the primary "
        "entry. Current design uses naive prior-24h breakout which over-fires in trending "
        "markets like 2024-2026 gold."
    ),
}


# ── Parsers ─────────────────────────────────────────────────────────────


def _read_docstring(path: str) -> str:
    """Return the top-of-file docstring (first triple-quoted block)."""
    src = Path(path).read_text()
    m = re.search(r'"""(.*?)"""', src, re.DOTALL)
    if not m:
        raise ValueError(f"no docstring found in {path}")
    return m.group(1)


def _extract_kill_date(docstring: str) -> str:
    """Find the most recent YYYY-MM-DD in the docstring."""
    dates = re.findall(r"(\d{4}-\d{2}-\d{2})", docstring)
    if not dates:
        return "2026-04-14"  # fallback to today
    # Return the latest (lexicographic sort works on ISO dates)
    return max(dates)


def _extract_task_ids(docstring: str) -> list[int]:
    """Find all `task #NN` mentions."""
    ids = re.findall(r"task #(\d+)", docstring)
    return sorted({int(i) for i in ids})


def _extract_root_cause(docstring: str) -> str | None:
    """Grab the 'Root cause:' paragraph (up to next double-newline or 'KILL')."""
    m = re.search(
        r"Root cause[s]?:?\s*(.*?)(?=\n\n|\nKILL|\nDesign|\nRetain)",
        docstring,
        re.DOTALL,
    )
    if not m:
        return None
    text = m.group(1).strip()
    # Flatten whitespace
    text = re.sub(r"\s+", " ", text)
    return text


def _collect_research_artifacts(strategy_name: str) -> list[dict]:
    """Attach any local JSON research artifacts that mention this strategy."""
    artifacts: list[dict] = []
    candidates = [
        ("data/tier5_m1_revival.json", "Tier 5 M1 revival research (task #100)"),
        ("data/tier5_baseline.json", "Tier 5 infrastructure baseline (G.2h.3)"),
    ]
    for path, summary in candidates:
        abs_path = REPO_ROOT / path
        if not abs_path.exists():
            continue
        try:
            content = json.loads(abs_path.read_text())
        except (OSError, ValueError):
            continue
        # Heuristic: artifact is relevant if the strategy name appears
        # somewhere in its text
        if strategy_name not in json.dumps(content):
            continue
        artifacts.append({
            "name": Path(path).stem,
            "path": path,
            "summary": summary,
        })
    return artifacts


def _build_entry(strategy_file: str) -> GraveyardEntry:
    """Parse one obituary file into a GraveyardEntry."""
    path = Path(strategy_file)
    strategy_name = path.stem  # "candle_burst_hunter"
    docstring = _read_docstring(strategy_file)

    return GraveyardEntry(
        strategy_name=strategy_name,
        strategy_version="v1",
        kill_date=_extract_kill_date(docstring),
        kill_commit="be4e2d2",
        kill_category=_CATEGORY_MAP.get(strategy_name, "PREMISE"),
        kill_reason_short=_REASON_SHORT.get(strategy_name, "Killed — see obituary"),
        root_cause_long=_extract_root_cause(docstring),
        revival_conditions=_REVIVAL_CONDITIONS.get(strategy_name),
        obituary_source_file=strategy_file,
        backtest_metrics=None,  # No single "last backtest" number — see artifacts
        research_artifacts=_collect_research_artifacts(strategy_name),
        task_ids=_extract_task_ids(docstring),
        tags=_TAGS_MAP.get(strategy_name, ["aggressive"]),
    )


# ── Main ────────────────────────────────────────────────────────────────


def main() -> int:
    db_path = os.environ.get("GRAVEYARD_DB", "data/trades.db")
    print(f"Ingesting obituaries into {db_path}...")
    print()

    ingested: list[str] = []
    for sf in OBIT_FILES:
        try:
            entry = _build_entry(sf)
        except Exception as e:
            print(f"  FAILED {sf}: {e}")
            return 1

        row_id = record_kill(entry, db_path=db_path)
        print(f"  ✓ {entry.strategy_name} {entry.strategy_version}")
        print(f"      kill_date={entry.kill_date}  category={entry.kill_category}")
        print(f"      task_ids={entry.task_ids}  tags={entry.tags}")
        print(f"      row_id={row_id}")
        print()
        ingested.append(entry.strategy_name)

    print(f"Ingested: {', '.join(ingested)} ({len(ingested)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
