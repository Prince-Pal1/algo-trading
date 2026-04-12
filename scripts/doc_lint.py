#!/usr/bin/env python3
"""doc_lint — single-source-of-truth regression guard.

Fails (exit 1) if phase-status claims leak into any top-level .md file other
than ROADMAP.md. Phase status lives in ROADMAP.md and only ROADMAP.md.

Run manually or wire into pre-commit:

    python3 scripts/doc_lint.py
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROADMAP = ROOT / "ROADMAP.md"

# Files where phase status claims are forbidden. ROADMAP.md is the sole home.
# BLUEPRINT.md is frozen and historical — the file itself has a "SUPERSEDED"
# note for its phase section, so we exclude it from the scan.
# SESSIONS_ARCHIVE.md contains historical session narratives with "COMPLETE"
# tags that describe past session work, not live phase state — excluded.
EXCLUDE = {
    "ROADMAP.md",
    "BLUEPRINT.md",
    "SESSIONS_ARCHIVE.md",
}

# Patterns that indicate a live *project-level* phase-status claim — the
# specific drift vocabulary that caused Session 21 confusion. These are NOT
# matched by session-narrative sub-phase labels like "Phase 0: Bug Fix —
# COMPLETE" (those use a colon and describe past session work, not live
# project phase state).
PATTERNS = [
    # "Phase 3 NOT STARTED", "Phase 3b-2 NOT STARTED"
    re.compile(r"Phase\s+\d+\w*\s+NOT\s+STARTED", re.IGNORECASE),
    # "Phase 2 IN PROGRESS" — the specific stale claim from Session 21
    re.compile(r"Phase\s+\d+\w*\s+IN\s+PROGRESS", re.IGNORECASE),
    # "milestone not met" — stale Phase 2 claim
    re.compile(r"milestone\s+not\s+met", re.IGNORECASE),
    # "awaiting strategy pick" — specific stale phrase
    re.compile(r"awaiting\s+strategy\s+pick", re.IGNORECASE),
]


def scan_file(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """Return list of (line_no, pattern_description, line_text) matches."""
    hits: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[warn] could not read {path}: {exc}", file=sys.stderr)
        return hits

    for i, line in enumerate(text.splitlines(), start=1):
        # Skip lines that are clearly linking to ROADMAP.md
        if "ROADMAP.md" in line:
            continue
        for pat in PATTERNS:
            if pat.search(line):
                hits.append((i, pat.pattern, line.strip()))
                break
    return hits


def check_roadmap_freshness() -> str | None:
    """Warn if ROADMAP.md hasn't been touched in > 7 days."""
    if not ROADMAP.exists():
        return "ROADMAP.md is missing — this is the canonical phase tracker."
    age = dt.datetime.now() - dt.datetime.fromtimestamp(ROADMAP.stat().st_mtime)
    if age.days > 7:
        return f"ROADMAP.md last modified {age.days} days ago — may be stale."
    return None


def main() -> int:
    failed = False

    # Scan top-level .md files
    for md in sorted(ROOT.glob("*.md")):
        if md.name in EXCLUDE:
            continue
        hits = scan_file(md)
        if hits:
            failed = True
            print(f"\n❌ {md.name}: phase-status claims found (belong in ROADMAP.md)")
            for line_no, pat, text in hits:
                print(f"   line {line_no}: {text}")
                print(f"   matched pattern: {pat}")

    # Freshness check (warning only)
    warn = check_roadmap_freshness()
    if warn:
        print(f"\n⚠️  {warn}")

    if failed:
        print(
            "\nRemediation: move phase-status claims to ROADMAP.md and replace "
            "the affected lines with a link to ROADMAP.md."
        )
        return 1

    print("✓ doc_lint: all top-level .md files clean (no phase-status leaks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
