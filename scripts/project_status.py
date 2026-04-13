#!/usr/bin/env python3
"""Project Status Aggregator — single-pane-of-glass status report.

Reads the existing per-subsystem report files and renders a
human-readable Markdown summary in `data/project_status.md`. Designed
to be run periodically (every 30 min via launchd) so that `cat
data/project_status.md` at any point returns the current state of
the entire system.

Inputs:
    data/heartbeat.json               — engine uptime + health
    data/m3s_shadow_report.md         — M3S shadow check + 4-day clock
    data/m3s_shadow_status.json       — machine-readable M3S status
    data/meta_label_shadow_report.md  — meta-label filter status
    data/meta_label_shadow_status.json— machine-readable meta-label status
    data/deflated_sharpe_live.json    — per-strategy DSR + total book
    data/m3s_shadow_clock.json        — consecutive clean days counter
    config/settings.toml              — current operational modes

Output:
    data/project_status.md            — human-readable snapshot

Usage:
    python3 scripts/project_status.py
    python3 scripts/project_status.py --verbose     # also stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib  # py3.11+
except ImportError:
    import tomli as tomllib  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SETTINGS = REPO_ROOT / "config" / "settings.toml"

HEARTBEAT = DATA_DIR / "heartbeat.json"
M3S_REPORT = DATA_DIR / "m3s_shadow_report.md"
M3S_STATUS = DATA_DIR / "m3s_shadow_status.json"
M3S_CLOCK = DATA_DIR / "m3s_shadow_clock.json"
META_REPORT = DATA_DIR / "meta_label_shadow_report.md"
META_STATUS = DATA_DIR / "meta_label_shadow_status.json"
DSR_JSON = DATA_DIR / "deflated_sharpe_live.json"

OUTPUT = DATA_DIR / "project_status.md"


# ── Readers (each returns a dict; missing files → empty dict) ──────


def _read_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _read_toml(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text())
    except Exception:
        return {}


def _file_age_minutes(p: Path) -> int | None:
    if not p.exists():
        return None
    import time
    try:
        return int((time.time() - p.stat().st_mtime) / 60)
    except OSError:
        return None


# ── Report sections ────────────────────────────────────────────────


def _render_engine_section(hb: dict) -> list[str]:
    lines = ["## Engine Health", ""]
    if not hb:
        lines += ["_`data/heartbeat.json` missing — engine not running?_", ""]
        return lines
    status = hb.get("status", "UNKNOWN")
    icon = {"HEALTHY": "✅", "WARNING": "⚠️", "ERROR": "❌"}.get(status, "?")
    lines += [
        f"- **Status:** {icon} `{status}`",
        f"- **Uptime:** `{hb.get('uptime_s', 0)}s`",
        f"- **Tick count:** `{hb.get('tick_count', 0)}`",
        f"- **Candle count:** `{hb.get('candle_count', 0)}`",
        f"- **Open positions:** `{hb.get('open_positions', 0)}`",
        f"- **Equity:** `${hb.get('equity', 0):,.2f}`",
        f"- **Risk server:** {'✅' if hb.get('risk_server_ok') else '❌'}",
        f"- **Signals emitted:** `{hb.get('signal_count', 0)}`",
        f"- **Rejections:** `{hb.get('rejection_count', 0)}`",
        f"- **Strategy exceptions:** `{hb.get('strategy_exceptions', 0)}`",
        f"- **Last heartbeat:** `{hb.get('timestamp', 'n/a')}`",
        "",
    ]
    return lines


def _render_config_modes(cfg: dict) -> list[str]:
    lines = ["## Operational Modes", ""]
    general = cfg.get("general", {})
    m3s = cfg.get("m3s", {})
    meta = cfg.get("meta_label", {})
    # M3S rollout label
    if not m3s.get("enabled"):
        m3s_label = "disabled"
    elif m3s.get("shadow_mode"):
        m3s_label = "shadow"
    else:
        m3s_label = "authoritative"
    # Meta-label rollout label
    if not meta.get("enabled"):
        meta_label = "disabled"
    elif meta.get("shadow_mode"):
        meta_label = "shadow"
    else:
        threshold = float(meta.get("veto_threshold", 0.30))
        meta_label = f"live (veto_threshold={threshold:.2f})" if threshold >= 0.50 \
            else f"advisory (veto_threshold={threshold:.2f})"
    lines += [
        f"- **Engine mode:** `{general.get('mode', 'unknown')}`",
        f"- **M3S:** `{m3s_label}` (mode=`{m3s.get('mode', '?')}`)",
        f"- **Meta-label filter:** `{meta_label}`",
        "",
    ]
    return lines


def _render_m3s_section(status: dict, clock: dict) -> list[str]:
    lines = ["## M3S Shadow Check", ""]
    if not status:
        lines += ["_No M3S shadow status — has `scripts/m3s_shadow_check.py` run?_", ""]
    else:
        overall = status.get("overall_status", "UNKNOWN")
        icon = {"HEALTHY": "✅", "WARNING": "⚠️", "ERROR": "❌", "DISABLED": "⏸"}.get(overall, "?")
        lines += [
            f"- **Overall:** {icon} `{overall}`",
            f"- **Timestamp:** `{status.get('timestamp_utc', 'n/a')}`",
        ]
        state = status.get("m3s_state", {}) or {}
        if state.get("mode"):
            lines.append(f"- **Mode:** `{state.get('mode')}`")
        if state.get("base_equity") is not None:
            lines.append(f"- **Base equity:** `${state.get('base_equity', 0):,.2f}`")
        if state.get("hwm") is not None:
            lines.append(f"- **HWM:** `${state.get('hwm', 0):,.2f}`")
        checks = status.get("checks", [])
        if checks:
            lines.append(f"- **Checks:** {len(checks)} run")
            fails = [c for c in checks if c.get("status") == "FAIL"]
            warns = [c for c in checks if c.get("status") == "WARN"]
            if fails:
                lines.append(f"  - ❌ {len(fails)} FAIL: {', '.join(c['name'] for c in fails)}")
            if warns:
                lines.append(f"  - ⚠️ {len(warns)} WARN: {', '.join(c['name'] for c in warns)}")
        lines.append("")

    clean_days = int((clock or {}).get("consecutive_clean_days", 0))
    last_status = (clock or {}).get("last_status", "?")
    lines += [
        f"**4-Day Promotion Clock:** `{clean_days} / 4` (last run: `{last_status}`)",
        "",
    ]
    if clean_days >= 4:
        lines += ["✅ Clock complete — `scripts/promote_m3s_authoritative.sh` is eligible.", ""]
    return lines


def _render_meta_label_section(status: dict) -> list[str]:
    lines = ["## Meta-Label Filter", ""]
    if not status:
        lines += [
            "_No meta-label status — has `scripts/meta_label_shadow_check.py` run?_",
            "",
        ]
        return lines
    overall = status.get("overall_status", "UNKNOWN")
    icon = {"HEALTHY": "✅", "WARNING": "⚠️", "ERROR": "❌", "DISABLED": "⏸"}.get(overall, "?")
    lines += [
        f"- **Overall:** {icon} `{overall}`",
        f"- **Timestamp:** `{status.get('timestamp_utc', 'n/a')}`",
    ]
    per_strategy = status.get("per_strategy", {}) or {}
    if per_strategy:
        lines.append(f"- **Strategies tracked:** {len(per_strategy)}")
        for name, checks in sorted(per_strategy.items()):
            summary = []
            for c in checks:
                if c.get("name") == "model_artifact":
                    d = c.get("data", {})
                    summary.append(
                        f"type={d.get('model_type', '?')} "
                        f"AUC={d.get('auc', 0.0):.3f}"
                    )
                if c.get("name") == "rolling_auc":
                    d = c.get("data", {})
                    n = d.get("n", 0)
                    if n >= 20:
                        summary.append(f"live_AUC={d.get('auc', 0.0):.3f}")
                    else:
                        summary.append(f"live_n={n}")
            lines.append(f"  - `{name}`: {'  '.join(summary)}")
    lines.append("")
    return lines


def _render_dsr_section(data: dict) -> list[str]:
    lines = ["## Deflated Sharpe (audit-derived)", ""]
    if not data:
        lines += [
            "_No DSR report — has `scripts/deflated_sharpe_from_audit.py` run?_",
            "",
        ]
        return lines
    total = data.get("total_book", {}) or {}
    lines += [
        f"- **Total book:** n=`{total.get('n_samples', 0)}`  "
        f"raw=`{total.get('raw_sharpe', 0.0):+.3f}`  "
        f"deflated=`{total.get('deflated_sharpe', 0.0):+.3f}`",
        f"- **Timestamp:** `{data.get('timestamp_utc', 'n/a')}`",
        f"- **Run prefix:** `{data.get('run_id_prefix') or '*'}`",
        "",
    ]
    per = data.get("per_strategy", []) or []
    if per:
        lines.append("| Strategy | n | Raw | Deflated | Δ Raw | Δ Defl |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for r in per:
            lines.append(
                f"| `{r.get('strategy', '?')}` "
                f"| {r.get('n_samples', 0)} "
                f"| {r.get('raw_sharpe', 0.0):+.3f} "
                f"| {r.get('deflated_sharpe', 0.0):+.3f} "
                f"| {r.get('delta_raw', 0.0):+.3f} "
                f"| {r.get('delta_deflated', 0.0):+.3f} |"
            )
        lines.append("")
    return lines


def _render_gate_section(
    m3s_status: dict,
    meta_status: dict,
    clock: dict,
    dsr: dict,
) -> list[str]:
    """Render the 4 Day-3 promotion gates: pass/fail at a glance."""
    lines = ["## Promotion Gates (Day 3 cron at 2026-04-16 09:07)", ""]

    def mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    g1 = (m3s_status.get("overall_status") in ("HEALTHY", "DISABLED"))
    g2 = int((clock or {}).get("consecutive_clean_days", 0)) >= 4
    g3 = meta_status.get("overall_status") != "ERROR"
    # DSR gate: total_book.deflated_sharpe not regressing > 0.25 vs baseline
    total = dsr.get("total_book", {}) or {}
    live = float(total.get("deflated_sharpe", 0.0) or 0.0)
    n = int(total.get("n_samples", 0) or 0)
    per = dsr.get("per_strategy", []) or []
    if per:
        num = sum(
            float(r.get("baseline_deflated_sharpe", 0.0) or 0.0)
            * int(r.get("baseline_n_samples", 0) or 0)
            for r in per
        )
        den = sum(int(r.get("baseline_n_samples", 0) or 0) for r in per)
        base = num / den if den > 0 else 0.0
    else:
        base = 0.0
    g4 = (n < 20) or ((live - base) >= -0.25)

    lines += [
        f"- {mark(g1)} **Gate 1** — M3S shadow check HEALTHY/DISABLED",
        f"- {mark(g2)} **Gate 2** — M3S promotion clock ≥ 4/4 (current: {int((clock or {}).get('consecutive_clean_days', 0))})",
        f"- {mark(g3)} **Gate 3** — Meta-label shadow check not ERROR",
        f"- {mark(g4)} **Gate 4** — Deflated Sharpe non-regression (live={live:+.3f} base={base:+.3f})",
    ]
    passed = sum([g1, g2, g3, g4])
    lines.append("")
    if passed == 4:
        lines.append("**✅ ALL GATES PASSED — promotion script will run cleanly.**")
    else:
        lines.append(f"**⏳ {passed}/4 gates passed — {4 - passed} blocking.**")
    lines.append("")
    return lines


def _render_file_ages() -> list[str]:
    lines = ["## File Freshness", ""]
    lines.append("| File | Age |")
    lines.append("|---|---|")
    for label, path in [
        ("heartbeat.json", HEARTBEAT),
        ("m3s_shadow_report.md", M3S_REPORT),
        ("meta_label_shadow_report.md", META_REPORT),
        ("deflated_sharpe_live.json", DSR_JSON),
    ]:
        age = _file_age_minutes(path)
        if age is None:
            lines.append(f"| `{label}` | ❌ missing |")
        elif age < 60:
            lines.append(f"| `{label}` | ✅ {age}m ago |")
        elif age < 360:
            lines.append(f"| `{label}` | ⚠️ {age}m ago |")
        else:
            lines.append(f"| `{label}` | ❌ {age}m ago (stale) |")
    lines.append("")
    return lines


# ── Main ──────────────────────────────────────────────────────────


def render_report() -> str:
    hb = _read_json(HEARTBEAT)
    m3s_status = _read_json(M3S_STATUS)
    m3s_clock = _read_json(M3S_CLOCK)
    meta_status = _read_json(META_STATUS)
    dsr = _read_json(DSR_JSON)
    cfg = _read_toml(SETTINGS)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines: list[str] = []
    lines += [f"# Project Status — {now_iso}", ""]
    lines += [
        "_Single-pane-of-glass view. Regenerated every 30 min via launchd "
        "agent `com.algo-trading.project-status`. For live values, read the "
        "source files in `data/` directly._",
        "",
    ]
    lines += _render_config_modes(cfg)
    lines += _render_engine_section(hb)
    lines += _render_m3s_section(m3s_status, m3s_clock)
    lines += _render_meta_label_section(meta_status)
    lines += _render_dsr_section(dsr)
    lines += _render_gate_section(m3s_status, meta_status, m3s_clock, dsr)
    lines += _render_file_ages()

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    report = render_report()
    OUTPUT.write_text(report)

    if args.verbose:
        print(report)
        print()
        print(f"Written: {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
