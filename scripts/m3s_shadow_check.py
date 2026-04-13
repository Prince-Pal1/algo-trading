#!/usr/bin/env python3
"""M3S Shadow Mode Validator — runs every 6h via launchd.

Purpose: shorten the 4-week passive shadow clock to a 4-day active clock
(compressed from 7 → 4 in Session 22 sprint) by continuously auditing
M3S's proposed scaling decisions against reality. Finds bugs, invariant
violations, and divergence between the shadow state and the paper engine.

Outputs:
    data/m3s_shadow_report.md      Human-readable status — `cat` any time
    data/m3s_shadow_status.json    Machine-readable for dashboards
    data/m3s_shadow_alerts.log     Append-only violation history

Exit codes:
    0   HEALTHY (or DISABLED — M3S not active yet)
    1   WARNING (advisory; doesn't block promotion)
    2   ERROR   (hard invariant violation; blocks promotion)

Promotion criteria (the "4-day clock"):
    4 consecutive days of exit 0 or 1 → Prince may flip `shadow_mode=false`.
    ANY exit 2 resets the clock to day 0.
    Rationale: the checker fires every 6h (16 audits across 4 days),
    giving dense validation even in a short window.

Usage:
    python3 scripts/m3s_shadow_check.py
    python3 scripts/m3s_shadow_check.py --window-days 7   # longer audit window
    python3 scripts/m3s_shadow_check.py --verbose         # per-check detail
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
M3S_DB = DATA_DIR / "m3s.sqlite"
TRADES_DB = DATA_DIR / "trades.db"
REPORT_MD = DATA_DIR / "m3s_shadow_report.md"
STATUS_JSON = DATA_DIR / "m3s_shadow_status.json"
ALERTS_LOG = DATA_DIR / "m3s_shadow_alerts.log"


_MS_PER_DAY = 86_400_000


# ── Result types ─────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    status: str                 # "PASS" | "WARN" | "FAIL" | "SKIP"
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ShadowReport:
    timestamp_utc: str
    overall_status: str         # "HEALTHY" | "WARNING" | "ERROR" | "DISABLED"
    checks: list[CheckResult]
    m3s_state: dict[str, Any]
    summary: dict[str, Any]

    def to_json(self) -> dict:
        return {
            "timestamp_utc": self.timestamp_utc,
            "overall_status": self.overall_status,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "data": c.data}
                for c in self.checks
            ],
            "m3s_state": self.m3s_state,
            "summary": self.summary,
        }


# ── Store access helpers (raw sqlite3, no src.m3s import to stay fast) ──


def open_m3s_store() -> sqlite3.Connection | None:
    if not M3S_DB.exists():
        return None
    conn = sqlite3.connect(str(M3S_DB))
    conn.row_factory = sqlite3.Row
    return conn


def open_trades_db() -> sqlite3.Connection | None:
    if not TRADES_DB.exists():
        return None
    conn = sqlite3.connect(str(TRADES_DB))
    conn.row_factory = sqlite3.Row
    return conn


def load_state(conn: sqlite3.Connection, namespace: str, key: str) -> Any | None:
    try:
        row = conn.execute(
            "SELECT value FROM m3s_state WHERE namespace = ? AND key = ?",
            (namespace, key),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return None


def load_events(
    conn: sqlite3.Connection,
    *,
    event_type: str | None = None,
    since_ms: int | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if since_ms is not None:
        clauses.append("ts_ms >= ?")
        params.append(since_ms)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        cursor = conn.execute(
            f"SELECT id, ts_ms, event_type, payload, inputs_hash "
            f"FROM m3s_events{where} ORDER BY ts_ms ASC, id ASC",
            params,
        )
    except sqlite3.OperationalError:
        return []
    out: list[dict] = []
    for row in cursor.fetchall():
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        out.append({
            "id": row["id"],
            "ts_ms": row["ts_ms"],
            "event_type": row["event_type"],
            "payload": payload,
            "inputs_hash": row["inputs_hash"],
        })
    return out


# ── Invariant checks ────────────────────────────────────────────────


def check_hwm_monotonic(m3s: sqlite3.Connection, since_ms: int) -> CheckResult:
    """Compound-update events must only ratchet HWM up."""
    events = load_events(m3s, event_type="compound_update", since_ms=since_ms)
    if not events:
        return CheckResult(
            "hwm_monotonic", "SKIP",
            "no compound_update events in window", {"n": 0},
        )
    last_hwm = 0.0
    violations = 0
    for ev in events:
        new_hwm = float(ev["payload"].get("new_hwm", 0.0))
        if new_hwm < last_hwm - 1e-9:
            violations += 1
        last_hwm = max(last_hwm, new_hwm)
    if violations > 0:
        return CheckResult(
            "hwm_monotonic", "FAIL",
            f"{violations}/{len(events)} compound updates decreased HWM",
            {"violations": violations, "n": len(events)},
        )
    return CheckResult(
        "hwm_monotonic", "PASS",
        f"{len(events)} compound updates all monotonic",
        {"n": len(events)},
    )


def check_cluster_caps(m3s: sqlite3.Connection, since_ms: int) -> CheckResult:
    """Every allocation decision respects per-strategy and cluster caps."""
    # Load mode to get caps
    mode_payload = load_state(m3s, "compound", "state") or {}
    mode_name = mode_payload.get("mode", "STANDARD")
    per_strategy_cap = {
        "CONSERVATIVE": 0.25,
        "STANDARD": 0.40,
        "GROWTH": 0.50,
        "CUSTOM": 0.60,  # conservative assumption for CUSTOM
    }.get(mode_name, 0.40)

    events = load_events(m3s, event_type="allocation", since_ms=since_ms)
    if not events:
        return CheckResult(
            "cluster_caps", "SKIP",
            "no allocation events in window", {"n": 0},
        )

    violations: list[str] = []
    for ev in events:
        weights = ev["payload"].get("weights", {})
        for name, w in weights.items():
            if w > per_strategy_cap + 1e-6:
                violations.append(f"{name}={w:.3f} > cap={per_strategy_cap}")
        total = sum(weights.values())
        if total > 1.0 + 1e-6:
            violations.append(f"sum(weights)={total:.3f} > 1.0")

    if violations:
        return CheckResult(
            "cluster_caps", "FAIL",
            f"{len(violations)} violations in {len(events)} allocations",
            {"violations": violations[:5], "n": len(events)},
        )
    return CheckResult(
        "cluster_caps", "PASS",
        f"{len(events)} allocations all within {mode_name} caps",
        {"n": len(events), "mode": mode_name, "cap": per_strategy_cap},
    )


def check_allocation_churn(m3s: sqlite3.Connection, since_ms: int) -> CheckResult:
    """Allocation weights shouldn't churn wildly between rebalances."""
    events = load_events(m3s, event_type="allocation", since_ms=since_ms)
    if len(events) < 2:
        return CheckResult(
            "allocation_churn", "SKIP",
            f"need ≥2 allocations, got {len(events)}", {"n": len(events)},
        )
    turnovers: list[float] = []
    prev_weights: dict | None = None
    for ev in events:
        w = ev["payload"].get("weights", {})
        if prev_weights is not None:
            keys = set(prev_weights) | set(w)
            l1 = sum(abs(prev_weights.get(k, 0.0) - w.get(k, 0.0)) for k in keys)
            turnovers.append(l1)
        prev_weights = w
    if not turnovers:
        return CheckResult(
            "allocation_churn", "SKIP",
            "could not compute turnover", {"n": 0},
        )
    avg = sum(turnovers) / len(turnovers)
    max_t = max(turnovers)
    if avg > 0.5:
        return CheckResult(
            "allocation_churn", "WARN",
            f"high weight churn: avg L1={avg:.3f}, max={max_t:.3f}",
            {"avg_turnover": avg, "max_turnover": max_t, "n": len(turnovers)},
        )
    return CheckResult(
        "allocation_churn", "PASS",
        f"stable weights: avg L1={avg:.3f}, max={max_t:.3f}",
        {"avg_turnover": avg, "max_turnover": max_t, "n": len(turnovers)},
    )


def check_no_dd_frozen_compound(m3s: sqlite3.Connection, since_ms: int) -> CheckResult:
    """Compound base must not advance during a drawdown-frozen period."""
    events = load_events(m3s, event_type="compound_update", since_ms=since_ms)
    if not events:
        return CheckResult(
            "no_dd_frozen_compound", "SKIP",
            "no compound updates", {"n": 0},
        )
    violations = 0
    for ev in events:
        reason = str(ev["payload"].get("reason", ""))
        new_base = float(ev["payload"].get("new_base", 0.0))
        old_base = float(ev["payload"].get("old_base", new_base))
        # If the reason indicates frozen but base moved up, that's a bug.
        if "frozen" in reason.lower() and new_base > old_base + 1e-6:
            violations += 1
    if violations > 0:
        return CheckResult(
            "no_dd_frozen_compound", "FAIL",
            f"{violations} compounds advanced despite frozen reason",
            {"violations": violations, "n": len(events)},
        )
    return CheckResult(
        "no_dd_frozen_compound", "PASS",
        f"{len(events)} compound events respect freeze gate",
        {"n": len(events)},
    )


# ── Divergence + health checks ──────────────────────────────────────


def check_shadow_vs_actual(
    m3s: sqlite3.Connection,
    trades: sqlite3.Connection | None,
) -> CheckResult:
    """Compare M3S compound base to actual paper engine equity."""
    compound = load_state(m3s, "compound", "state")
    if compound is None:
        return CheckResult(
            "shadow_vs_actual", "SKIP",
            "no compound state yet", {},
        )
    m3s_base = float(compound.get("base_equity", 0.0))

    if trades is None:
        return CheckResult(
            "shadow_vs_actual", "SKIP",
            "trades.db missing", {"m3s_base": m3s_base},
        )
    row = trades.execute("SELECT equity FROM paper_equity LIMIT 1").fetchone()
    if row is None:
        return CheckResult(
            "shadow_vs_actual", "SKIP",
            "paper_equity empty", {"m3s_base": m3s_base},
        )
    paper_eq = float(row["equity"])

    if paper_eq <= 0:
        return CheckResult(
            "shadow_vs_actual", "WARN",
            "paper equity is zero", {"m3s_base": m3s_base, "paper": paper_eq},
        )

    divergence_pct = abs(m3s_base - paper_eq) / paper_eq
    data = {
        "m3s_base": m3s_base,
        "paper_equity": paper_eq,
        "divergence_pct": divergence_pct,
    }

    if divergence_pct > 0.01:
        return CheckResult(
            "shadow_vs_actual", "WARN",
            f"divergence {divergence_pct:.2%} > 1% threshold",
            data,
        )
    return CheckResult(
        "shadow_vs_actual", "PASS",
        f"divergence {divergence_pct:.3%} within tolerance",
        data,
    )


def check_scheduler_health(m3s: sqlite3.Connection, window_hours: int = 48) -> CheckResult:
    """Verify the scheduler has been ticking at expected cadence.

    Tolerates fresh-boot state: if the M3S db file is younger than
    `window_hours`, we only check the age-of-file window instead of the
    full `window_hours`. Otherwise a fresh boot always warns for 48h.
    """
    now_ms = int(time.time() * 1000)

    # File age in hours
    try:
        age_s = time.time() - M3S_DB.stat().st_mtime
    except (OSError, AttributeError):
        age_s = window_hours * 3_600

    effective_window_h = min(window_hours, max(1, int(age_s / 3600) + 1))
    since_ms = now_ms - effective_window_h * 3_600_000
    events = load_events(m3s, since_ms=since_ms)
    n_events = len(events)

    if n_events == 0:
        # Fresh boot grace: if the db is less than 2h old, this is expected.
        if age_s < 7200:
            return CheckResult(
                "scheduler_health", "PASS",
                f"fresh boot ({int(age_s / 60)} min old), scheduler hasn't ticked yet — grace period",
                {"age_minutes": int(age_s / 60), "window_hours": effective_window_h},
            )
        return CheckResult(
            "scheduler_health", "WARN",
            f"no events in last {effective_window_h}h — scheduler may be stuck",
            {"n": 0, "window_hours": effective_window_h},
        )

    allocation_events = [e for e in events if e["event_type"] == "allocation"]
    return CheckResult(
        "scheduler_health", "PASS",
        f"{n_events} events in last {effective_window_h}h "
        f"({len(allocation_events)} allocations)",
        {
            "n": n_events,
            "allocations": len(allocation_events),
            "window_hours": effective_window_h,
        },
    )


def check_state_loadable(m3s: sqlite3.Connection) -> CheckResult:
    """Sanity check that state namespaces are parseable."""
    namespaces = ["compound", "allocation", "mode"]
    loaded: dict[str, Any] = {}
    missing: list[str] = []
    for ns in namespaces:
        try:
            if ns == "compound":
                v = load_state(m3s, "compound", "state")
                key = "state"
            elif ns == "allocation":
                v = load_state(m3s, "allocation", "latest")
                key = "latest"
            else:
                v = load_state(m3s, "mode", "current")
                key = "current"
            if v is None:
                missing.append(f"{ns}/{key}")
            else:
                loaded[ns] = v
        except Exception as e:
            missing.append(f"{ns}: {e}")
    if missing:
        return CheckResult(
            "state_loadable", "WARN",
            f"missing state namespaces: {', '.join(missing)}",
            {"missing": missing, "loaded": list(loaded.keys())},
        )
    return CheckResult(
        "state_loadable", "PASS",
        f"all state namespaces loaded: {', '.join(loaded.keys())}",
        {"loaded": list(loaded.keys())},
    )


# ── Main runner ─────────────────────────────────────────────────────


def run_checks(window_days: int) -> ShadowReport:
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - window_days * _MS_PER_DAY
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    m3s = open_m3s_store()
    trades = open_trades_db()

    # M3S store missing → disabled
    if m3s is None:
        return ShadowReport(
            timestamp_utc=now_iso,
            overall_status="DISABLED",
            checks=[CheckResult(
                "m3s_enabled", "SKIP",
                "data/m3s.sqlite not found — M3S is disabled in config/settings.toml",
                {"db_path": str(M3S_DB)},
            )],
            m3s_state={},
            summary={"window_days": window_days, "since_ms": since_ms},
        )

    try:
        # Probe whether m3s_events table exists
        try:
            m3s.execute("SELECT COUNT(*) FROM m3s_events").fetchone()
        except sqlite3.OperationalError:
            return ShadowReport(
                timestamp_utc=now_iso,
                overall_status="DISABLED",
                checks=[CheckResult(
                    "m3s_tables", "SKIP",
                    "m3s_events table missing — M3S has never written to the store",
                    {},
                )],
                m3s_state={},
                summary={"window_days": window_days},
            )

        checks: list[CheckResult] = [
            check_state_loadable(m3s),
            check_scheduler_health(m3s),
            check_hwm_monotonic(m3s, since_ms),
            check_no_dd_frozen_compound(m3s, since_ms),
            check_cluster_caps(m3s, since_ms),
            check_allocation_churn(m3s, since_ms),
            check_shadow_vs_actual(m3s, trades),
        ]

        # Snapshot state for the report header
        compound_state = load_state(m3s, "compound", "state") or {}
        mode_current = load_state(m3s, "mode", "current") or "UNKNOWN"
        alloc_latest = load_state(m3s, "allocation", "latest") or {}

        summary = {
            "window_days": window_days,
            "since_ms": since_ms,
            "n_events_total": m3s.execute("SELECT COUNT(*) FROM m3s_events").fetchone()[0],
            "n_events_window": len(load_events(m3s, since_ms=since_ms)),
        }

        m3s_state = {
            "mode": mode_current,
            "base_equity": compound_state.get("base_equity"),
            "hwm": compound_state.get("hwm"),
            "last_updated_ts_ms": compound_state.get("last_updated_ts_ms"),
            "last_allocation_method": alloc_latest.get("method"),
            "last_allocation_weights": alloc_latest.get("weights", {}),
        }

        status = _aggregate_status(checks)
        return ShadowReport(
            timestamp_utc=now_iso,
            overall_status=status,
            checks=checks,
            m3s_state=m3s_state,
            summary=summary,
        )

    finally:
        m3s.close()
        if trades is not None:
            trades.close()


def _aggregate_status(checks: list[CheckResult]) -> str:
    has_fail = any(c.status == "FAIL" for c in checks)
    has_warn = any(c.status == "WARN" for c in checks)
    if has_fail:
        return "ERROR"
    if has_warn:
        return "WARNING"
    return "HEALTHY"


# ── Output writers ──────────────────────────────────────────────────


def write_markdown_report(report: ShadowReport) -> None:
    STATUS_ICONS = {
        "PASS": "✅",
        "WARN": "⚠️",
        "FAIL": "❌",
        "SKIP": "⏭",
    }
    OVERALL_ICONS = {
        "HEALTHY": "✅",
        "WARNING": "⚠️",
        "ERROR": "❌",
        "DISABLED": "⏸",
    }

    lines: list[str] = []
    lines.append(f"# M3S Shadow Check — {report.timestamp_utc}")
    lines.append("")
    lines.append(
        f"**Status:** {OVERALL_ICONS.get(report.overall_status, '?')} "
        f"{report.overall_status}"
    )
    lines.append("")

    if report.overall_status == "DISABLED":
        lines.append(
            "M3S is disabled or has never run. Activate by editing "
            "`config/settings.toml` → `[m3s] enabled = true` and restarting the engine."
        )
        lines.append("")

    if report.m3s_state:
        lines.append("## Current M3S State")
        lines.append("")
        lines.append(f"- **Mode:** `{report.m3s_state.get('mode', 'n/a')}`")
        base = report.m3s_state.get("base_equity")
        hwm = report.m3s_state.get("hwm")
        if base is not None:
            lines.append(f"- **Compound base equity:** `${base:,.2f}`")
        if hwm is not None:
            lines.append(f"- **HWM:** `${hwm:,.2f}`")
        method = report.m3s_state.get("last_allocation_method")
        if method:
            lines.append(f"- **Last allocation method:** `{method}`")
        weights = report.m3s_state.get("last_allocation_weights") or {}
        if weights:
            w_str = ", ".join(f"{k}={v:.3f}" for k, v in sorted(weights.items()))
            lines.append(f"- **Last weights:** `{w_str}`")
        lines.append("")

    lines.append("## Checks")
    lines.append("")
    lines.append("| Check | Status | Detail |")
    lines.append("|---|---|---|")
    for c in report.checks:
        icon = STATUS_ICONS.get(c.status, "?")
        detail = c.detail.replace("|", "/")
        lines.append(f"| `{c.name}` | {icon} {c.status} | {detail} |")
    lines.append("")

    if report.summary:
        lines.append("## Summary")
        lines.append("")
        for k, v in report.summary.items():
            lines.append(f"- **{k}:** `{v}`")
        lines.append("")

    # Day counter — clock: consecutive healthy/warning days
    # Compressed from 7 → 4 days in Session 22 sprint (16 audits @ 6h cadence)
    clock_days = _read_clock_days()
    lines.append("## 4-Day Promotion Clock")
    lines.append("")
    if report.overall_status == "ERROR":
        lines.append("❌ **Clock reset to day 0** — hard invariant violation.")
    elif report.overall_status == "DISABLED":
        lines.append("⏸ **Clock not started** — M3S not active.")
    else:
        lines.append(f"Consecutive clean days: **{clock_days} / 4**")
        if clock_days >= 4:
            lines.append("")
            lines.append(
                "✅ **Clock complete.** Prince may flip `shadow_mode=false` "
                "in `config/settings.toml` for authoritative mode."
            )
    lines.append("")

    lines.append(f"_Next check in 6 hours. Last written: {report.timestamp_utc}_")
    lines.append("")

    REPORT_MD.write_text("\n".join(lines))


def write_json_status(report: ShadowReport) -> None:
    STATUS_JSON.write_text(json.dumps(report.to_json(), indent=2, default=str))


def append_alerts_log(report: ShadowReport) -> None:
    if report.overall_status in ("HEALTHY", "DISABLED"):
        return
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS_LOG.open("a") as f:
        f.write(f"[{report.timestamp_utc}] {report.overall_status}\n")
        for c in report.checks:
            if c.status in ("FAIL", "WARN"):
                f.write(f"  - {c.name} {c.status}: {c.detail}\n")


# ── Promotion clock (consecutive clean days) ────────────────────────


_CLOCK_FILE = DATA_DIR / "m3s_shadow_clock.json"


def _read_clock_days() -> int:
    if not _CLOCK_FILE.exists():
        return 0
    try:
        data = json.loads(_CLOCK_FILE.read_text())
        return int(data.get("consecutive_clean_days", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0


def _update_clock(status: str) -> None:
    """Update the 7-day clock based on the current run's status.

    Increments by 1 day if a full calendar day has passed since the last
    update AND status is HEALTHY or WARNING. Resets to 0 on ERROR.
    Stays the same on DISABLED.
    """
    if status == "DISABLED":
        return

    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    data: dict = {}
    if _CLOCK_FILE.exists():
        try:
            data = json.loads(_CLOCK_FILE.read_text())
        except (json.JSONDecodeError, TypeError):
            data = {}

    last_day = data.get("last_update_day", "")
    clean_days = int(data.get("consecutive_clean_days", 0))

    if status == "ERROR":
        clean_days = 0
    elif last_day != today_str:
        # New calendar day and not an error → increment
        clean_days += 1

    data.update({
        "last_update_day": today_str,
        "consecutive_clean_days": clean_days,
        "last_status": status,
        "last_updated_utc": now_utc.isoformat(timespec="seconds"),
    })
    _CLOCK_FILE.write_text(json.dumps(data, indent=2))


# ── Entrypoint ───────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-days", type=int, default=7,
                        help="look-back window for invariant checks (default 7)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    report = run_checks(window_days=args.window_days)

    write_markdown_report(report)
    write_json_status(report)
    append_alerts_log(report)
    _update_clock(report.overall_status)

    if args.verbose:
        print(f"Status: {report.overall_status}")
        for c in report.checks:
            print(f"  [{c.status}] {c.name}: {c.detail}")
        print(f"Report: {REPORT_MD}")
        print(f"Status JSON: {STATUS_JSON}")

    return {
        "HEALTHY": 0,
        "DISABLED": 0,
        "WARNING": 1,
        "ERROR": 2,
    }.get(report.overall_status, 2)


if __name__ == "__main__":
    sys.exit(main())
