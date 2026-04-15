"""Cross-run deep-backtest comparison tool (task #79 — G.7).

Loads 2+ deep_backtest report directories (each produced by
`scripts/deep_backtest.py`) and emits a side-by-side HTML comparison report.

Primary use-case: deciding portfolio composition. "Does donchian_gold dominate
vol_momentum_gold?" becomes answerable in 5 seconds by opening the compare HTML
and reading the recommendation block.

Usage:
    PYTHONPATH=. python3 scripts/deep_backtest_compare.py \\
        reports/deep_backtest_donchian_gold_2026-04-15_115336/ \\
        reports/deep_backtest_vol_momentum_gold_2026-04-15_120234/ \\
        --out reports/compare_donchian_vs_volmom_2026-04-15/

Exit code reflects the best verdict across all runs:
    0 = at least one DEPLOYABLE
    1 = all non-deployable but at least one RESEARCH_ONLY or NEEDS_WF
    2 = all FAILED or unreadable
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Data model ───────────────────────────────────────────────────────────


@dataclass
class RunSummary:
    """Lightweight, post-load view of a deep_backtest run."""

    report_dir: Path
    strategy: str
    timestamp: str
    verdict: str
    verdict_reason: str
    leverage_mode: str
    baseline_leverage: float | None
    best_cell: dict[str, Any]
    walk_forward: dict[str, Any] | None
    attribution: dict[str, dict[str, Any]] | None
    raw: dict[str, Any]

    @classmethod
    def load(cls, report_dir: Path) -> "RunSummary":
        summary_path = report_dir / "summary.json"
        if not summary_path.exists():
            raise FileNotFoundError(f"No summary.json in {report_dir}")
        data = json.loads(summary_path.read_text())
        cfg = data.get("config", {})
        return cls(
            report_dir=report_dir,
            strategy=data.get("strategy", "unknown"),
            timestamp=data.get("timestamp", ""),
            verdict=data.get("verdict", "UNKNOWN"),
            verdict_reason=data.get("verdict_reason", ""),
            leverage_mode=cfg.get("leverage_mode", "n/a"),
            baseline_leverage=cfg.get("baseline_leverage"),
            best_cell=data.get("best_cell") or {},
            walk_forward=data.get("walk_forward"),
            attribution=data.get("attribution"),
            raw=data,
        )


# ── Formatting helpers ───────────────────────────────────────────────────


def _fmt_pct(v: float | None, decimals: int = 2) -> str:
    if v is None:
        return "<span class='muted'>n/a</span>"
    cls = "pos" if v > 0 else ("neg" if v < 0 else "muted")
    return f"<span class='{cls}'>{v:+.{decimals}f}%</span>"


def _fmt_num(v: float | None, decimals: int = 2) -> str:
    if v is None:
        return "<span class='muted'>n/a</span>"
    return f"{v:.{decimals}f}"


def _verdict_badge(verdict: str) -> str:
    v = verdict.upper()
    cls = {
        "DEPLOYABLE": "v-ok",
        "NEEDS_WF": "v-warn",
        "RESEARCH_ONLY": "v-warn",
        "FAILED": "v-fail",
    }.get(v, "v-muted")
    return f"<span class='{cls}'>{v}</span>"


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


# ── Section builders ─────────────────────────────────────────────────────


def _section_header(runs: list[RunSummary]) -> str:
    cols = "".join(
        f"<th>{_esc(r.strategy)}<br><span class='muted small'>"
        f"{_esc(r.timestamp)}</span></th>"
        for r in runs
    )
    return f"<tr><th>Metric</th>{cols}</tr>"


def _section_verdict(runs: list[RunSummary]) -> str:
    rows = [
        f"<tr><th>Verdict</th>" + "".join(
            f"<td>{_verdict_badge(r.verdict)}</td>" for r in runs
        ) + "</tr>",
        f"<tr><th>Leverage mode</th>" + "".join(
            f"<td>{_esc(r.leverage_mode)}</td>" for r in runs
        ) + "</tr>",
        f"<tr><th>Baseline leverage</th>" + "".join(
            f"<td>{r.baseline_leverage if r.baseline_leverage else 'n/a'}</td>"
            for r in runs
        ) + "</tr>",
        f"<tr><th>Verdict reason</th>" + "".join(
            f"<td class='small'>{_esc(r.verdict_reason)}</td>" for r in runs
        ) + "</tr>",
    ]
    return "\n".join(rows)


def _section_best_cell(runs: list[RunSummary]) -> str:
    def cell(r: RunSummary, key: str, default: str = "n/a") -> Any:
        return r.best_cell.get(key, default)

    fields = [
        ("Window", lambda r: cell(r, "window_label")),
        ("Timeframe", lambda r: cell(r, "timeframe")),
        ("Leverage", lambda r: f"{cell(r, 'leverage', 0)}x"),
        ("Fee profile", lambda r: _esc(str(cell(r, "fee_profile")))),
        ("Trades", lambda r: cell(r, "trades")),
        ("Return %", lambda r: _fmt_pct(cell(r, "return_pct", None))),
        ("Max DD %", lambda r: _fmt_pct(
            -abs(cell(r, "maxdd_pct", 0)) if cell(r, "maxdd_pct", None) is not None else None
        )),
        ("Calmar", lambda r: _fmt_num(cell(r, "calmar", None))),
        ("Sharpe", lambda r: _fmt_num(cell(r, "sharpe", None))),
        ("Win rate", lambda r: _fmt_pct(cell(r, "win_rate", None))),
        ("Profit factor", lambda r: _fmt_num(cell(r, "profit_factor", None))),
    ]
    rows = []
    for label, getter in fields:
        cells = "".join(f"<td>{getter(r)}</td>" for r in runs)
        rows.append(f"<tr><th>{label}</th>{cells}</tr>")
    return "\n".join(rows)


def _pick_best_attribution(r: RunSummary) -> dict[str, Any] | None:
    """Find the attribution entry matching the run's best cell."""
    if not r.attribution or not r.best_cell:
        return None
    bc = r.best_cell
    wanted = (
        bc.get("window_days"),
        bc.get("timeframe"),
        bc.get("leverage"),
        bc.get("fee_profile"),
    )
    for entry in r.attribution.values():
        key = (
            entry.get("window_days"),
            entry.get("timeframe"),
            entry.get("leverage"),
            entry.get("fee_profile"),
        )
        if key == wanted:
            return entry
    return None


def _section_attribution(runs: list[RunSummary]) -> str:
    entries = [_pick_best_attribution(r) for r in runs]

    if all(e is None for e in entries):
        return (
            "<tr><td colspan='" + str(1 + len(runs)) + "' class='muted'>"
            "No attribution data available in any run — they predate task #79 (G.7)."
            " Re-run deep_backtest to populate attribution."
            "</td></tr>"
        )

    def val(entries_list: list[dict[str, Any] | None], key: str) -> list[float | None]:
        return [(e[key] if e else None) for e in entries_list]

    field_rows = [
        ("Total return %",      val(entries, "total_return"),       "pct"),
        ("Alpha (unlevered)",   val(entries, "alpha_return"),       "pct"),
        ("Leverage amp",        val(entries, "leverage_amplification"), "pct"),
        ("Cost drag",           val(entries, "cost_drag"),          "pct"),
        ("Margin rejection",    val(entries, "margin_rejection_drag"), "pct"),
        ("Residual",            val(entries, "residual"),           "pct"),
    ]
    rows = []
    for label, vals, _kind in field_rows:
        cells = "".join(f"<td>{_fmt_pct(v)}</td>" for v in vals)
        rows.append(f"<tr><th>{label}</th>{cells}</tr>")

    # Alpha share visualization — what fraction of total is unlevered?
    share_cells = []
    for e in entries:
        if e is None or not e.get("total_return"):
            share_cells.append("<td class='muted'>n/a</td>")
            continue
        total = e["total_return"]
        alpha = e["alpha_return"]
        if abs(total) < 1e-9:
            share_cells.append("<td class='muted'>0%</td>")
            continue
        pct = (alpha / total) * 100
        share_cells.append(f"<td>{pct:.0f}% alpha / {100-pct:.0f}% amp</td>")
    rows.append(
        "<tr><th>Alpha vs amp split</th>" + "".join(share_cells) + "</tr>"
    )

    notes_cells = "".join(
        f"<td class='small'>{_esc(e['note']) if e and e.get('note') else ''}</td>"
        for e in entries
    )
    rows.append(f"<tr><th>Notes</th>{notes_cells}</tr>")
    return "\n".join(rows)


def _section_walk_forward(runs: list[RunSummary]) -> str:
    if all(r.walk_forward is None for r in runs):
        return (
            "<tr><td colspan='" + str(1 + len(runs)) + "' class='muted'>"
            "No walk-forward data in any run."
            "</td></tr>"
        )

    def wf(r: RunSummary, key: str) -> Any:
        return (r.walk_forward or {}).get(key)

    rows = []
    fields = [
        ("Continuous return %",  lambda r: _fmt_pct(wf(r, "continuous_return_pct"))),
        ("Continuous max DD %",  lambda r: _fmt_pct(
            -abs(wf(r, "continuous_dd_pct")) if wf(r, "continuous_dd_pct") is not None else None
        )),
        ("Continuous Calmar",    lambda r: _fmt_num(wf(r, "continuous_calmar"))),
        ("Gate passed",          lambda r: str(wf(r, "continuous_gate_passed") or "n/a")),
        ("Fold count",           lambda r: len((wf(r, "folds") or []))),
    ]
    for label, getter in fields:
        cells = "".join(f"<td>{getter(r)}</td>" for r in runs)
        rows.append(f"<tr><th>{label}</th>{cells}</tr>")
    return "\n".join(rows)


def _annualized_from_wf(r: RunSummary) -> float | None:
    """Extract annualized return from walk-forward verdict string if present,
    else fall back to the best cell's return_pct (not annualized)."""
    if r.walk_forward and r.walk_forward.get("continuous_return_pct") is not None:
        # approximate: total * (365 / (fold_days * n_folds))
        folds = r.walk_forward.get("folds") or []
        fold_days = r.walk_forward.get("fold_days") or 0
        if folds and fold_days:
            days = len(folds) * fold_days
            total = r.walk_forward["continuous_return_pct"]
            if days > 0:
                return total * (365.0 / days)
    # fall-back — use best-cell raw return as a weak proxy
    return r.best_cell.get("return_pct")


def _portfolio_estimate(runs: list[RunSummary]) -> tuple[float, list[float]]:
    """Simple equal-weight portfolio estimate: allocation = 1/N for N runs,
    portfolio return = sum(weight_i * ann_return_i)."""
    weight = 1.0 / len(runs)
    annualized = [_annualized_from_wf(r) or 0.0 for r in runs]
    portfolio = sum(w * a for w, a in zip([weight] * len(runs), annualized))
    return portfolio, annualized


def _section_portfolio(runs: list[RunSummary]) -> str:
    portfolio, annualized = _portfolio_estimate(runs)
    rows = []
    # Allocation row
    alloc_cells = "".join(
        f"<td>{1.0/len(runs)*100:.0f}%</td>" for _ in runs
    )
    rows.append(f"<tr><th>Equal-weight allocation</th>{alloc_cells}</tr>")

    ann_cells = "".join(f"<td>{_fmt_pct(a)}</td>" for a in annualized)
    rows.append(f"<tr><th>Est. annualized return</th>{ann_cells}</tr>")

    total_col = f"<td colspan='{len(runs)}'>{_fmt_pct(portfolio)}</td>"
    rows.append(f"<tr><th>Portfolio (equal-weight)</th>{total_col}</tr>")
    return "\n".join(rows)


def _build_recommendation(runs: list[RunSummary]) -> str:
    """Produce a human-readable recommendation block."""
    scored: list[tuple[float, RunSummary]] = []
    for r in runs:
        ann = _annualized_from_wf(r) or 0.0
        verdict_bonus = {
            "DEPLOYABLE": 1000.0,
            "NEEDS_WF": 100.0,
            "RESEARCH_ONLY": 0.0,
            "FAILED": -1000.0,
        }.get(r.verdict.upper(), 0.0)
        score = ann + verdict_bonus
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    dominant = scored[0][1]
    rest = [x[1] for x in scored[1:]]

    lines: list[str] = []
    lines.append("<h2>Portfolio recommendation</h2>")
    lines.append("<div class='reco'>")

    lines.append(
        f"<p><strong>Dominant return engine:</strong> {_esc(dominant.strategy)}"
        f" ({_verdict_badge(dominant.verdict)})"
        f" — est. annualized {_fmt_pct(_annualized_from_wf(dominant))}</p>"
    )
    if dominant.attribution:
        a = _pick_best_attribution(dominant)
        if a and a.get("total_return"):
            alpha_pct = a["alpha_return"] / a["total_return"] * 100
            lines.append(
                f"<p class='small'>Attribution — {alpha_pct:.0f}% alpha, "
                f"{100-alpha_pct:.0f}% leverage amplification at "
                f"{dominant.best_cell.get('leverage')}x "
                f"({dominant.leverage_mode}).</p>"
            )

    for r in rest:
        lines.append(
            f"<p><strong>Diversifier / shelved:</strong> {_esc(r.strategy)}"
            f" ({_verdict_badge(r.verdict)})"
            f" — est. annualized {_fmt_pct(_annualized_from_wf(r))}</p>"
        )

    portfolio, _ = _portfolio_estimate(runs)
    lines.append(
        f"<p><strong>Equal-weight portfolio estimate:</strong> "
        f"{_fmt_pct(portfolio)}/yr across {len(runs)} runs. "
        f"This is a naive lower bound — real allocation should overweight the "
        f"dominant engine and underweight RESEARCH_ONLY diversifiers."
        f"</p>"
    )
    lines.append(
        "<p class='muted small'>Attribution decomposition is an approximation. "
        "Residual &gt; 5% indicates compounding drift; residual &gt; 15% flags "
        "unreliable attribution for that cell. See "
        "<code>docs/LEVERAGE_STRATEGY_DESIGN.md</code> §9.</p>"
    )
    lines.append("</div>")
    return "\n".join(lines)


# ── HTML assembly ────────────────────────────────────────────────────────


_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1200px; margin: 24px auto; color: #222; line-height: 1.45; }
h1 { font-size: 24px; margin-top: 24px; }
h2 { font-size: 18px; border-bottom: 2px solid #333; padding-bottom: 4px;
     margin-top: 32px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }
th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: left;
         vertical-align: top; }
th { background: #f4f4f4; font-weight: 600; }
.pos { color: #0a7a3a; font-weight: 600; }
.neg { color: #b22222; font-weight: 600; }
.muted { color: #888; }
.small { font-size: 11px; }
.v-ok   { background: #0a7a3a; color: #fff; padding: 2px 8px; border-radius: 3px; font-size: 12px; font-weight: 700; }
.v-warn { background: #c58a00; color: #fff; padding: 2px 8px; border-radius: 3px; font-size: 12px; font-weight: 700; }
.v-fail { background: #b22222; color: #fff; padding: 2px 8px; border-radius: 3px; font-size: 12px; font-weight: 700; }
.v-muted{ background: #888;    color: #fff; padding: 2px 8px; border-radius: 3px; font-size: 12px; font-weight: 700; }
.reco { border: 1px solid #333; background: #fffbe6; padding: 12px 18px;
        border-radius: 4px; margin: 12px 0; }
code { font-family: Monaco, Menlo, monospace; background: #f4f4f4;
       padding: 1px 4px; border-radius: 2px; }
"""


def build_html(runs: list[RunSummary]) -> str:
    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append("<html><head><meta charset='utf-8'>")
    parts.append(f"<title>Deep-backtest comparison — {len(runs)} runs</title>")
    parts.append(f"<style>{_CSS}</style>")
    parts.append("</head><body>")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts.append(f"<h1>Deep-backtest comparison</h1>")
    parts.append(
        f"<p class='muted small'>Generated {now} · "
        f"{len(runs)} runs · task #79 (G.7)</p>"
    )

    # Verdict + config
    parts.append("<h2>Verdict &amp; configuration</h2>")
    parts.append("<table>")
    parts.append(_section_header(runs))
    parts.append(_section_verdict(runs))
    parts.append("</table>")

    # Best cell
    parts.append("<h2>Best cell</h2>")
    parts.append("<table>")
    parts.append(_section_header(runs))
    parts.append(_section_best_cell(runs))
    parts.append("</table>")

    # Attribution
    parts.append("<h2>Attribution — where did the return come from?</h2>")
    parts.append(
        "<p class='small muted'>Decomposes each run's best-cell return into "
        "unlevered alpha, leverage amplification, cost drag, margin-rejection drag, "
        "and unexplained residual. Requires task #79 attribution data.</p>"
    )
    parts.append("<table>")
    parts.append(_section_header(runs))
    parts.append(_section_attribution(runs))
    parts.append("</table>")

    # Walk-forward
    parts.append("<h2>Walk-forward OOS</h2>")
    parts.append("<table>")
    parts.append(_section_header(runs))
    parts.append(_section_walk_forward(runs))
    parts.append("</table>")

    # Portfolio
    parts.append("<h2>Portfolio (equal-weight)</h2>")
    parts.append("<table>")
    parts.append(_section_header(runs))
    parts.append(_section_portfolio(runs))
    parts.append("</table>")

    # Recommendation
    parts.append(_build_recommendation(runs))

    parts.append("</body></html>")
    return "\n".join(parts)


# ── CLI ──────────────────────────────────────────────────────────────────


def _compute_exit_code(runs: list[RunSummary]) -> int:
    verdicts = [r.verdict.upper() for r in runs]
    if "DEPLOYABLE" in verdicts:
        return 0
    if any(v in {"NEEDS_WF", "RESEARCH_ONLY"} for v in verdicts):
        return 1
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Compare 2+ deep_backtest run directories side-by-side."
    )
    ap.add_argument(
        "report_dirs", nargs="+", type=Path,
        help="Two or more deep_backtest report directories "
             "(each containing summary.json + matrix.csv).",
    )
    ap.add_argument(
        "--out", type=Path, default=None,
        help="Output directory for the comparison report. "
             "Defaults to reports/compare_<timestamp>/",
    )
    args = ap.parse_args(argv)

    if len(args.report_dirs) < 2:
        print("error: need at least 2 report directories to compare", file=sys.stderr)
        return 2

    runs: list[RunSummary] = []
    for d in args.report_dirs:
        try:
            runs.append(RunSummary.load(d))
        except Exception as exc:
            print(f"error loading {d}: {exc}", file=sys.stderr)
            return 2

    out_dir = args.out or (
        Path("reports") / f"compare_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    html = build_html(runs)
    out_path = out_dir / "index.html"
    out_path.write_text(html)

    print(f"[compare] Wrote {out_path}")
    print(f"[compare] Runs compared: {len(runs)}")
    for r in runs:
        print(f"  - {r.strategy} ({r.verdict}) @ {r.report_dir}")

    return _compute_exit_code(runs)


if __name__ == "__main__":
    sys.exit(main())
