"""Report generation for deep backtest runs — CSV + PNG heatmaps + HTML + PDF.

Generalizes `scripts/swift_full_matrix.py::_generate_html/_pdf/_heatmaps` into
a strategy-agnostic pipeline driven by DeepBacktestResult. The SWIFT report
remains the visual reference; this module preserves its look and feel so any
strategy reads as a "SWIFT-style" deep-backtest report.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pandas as pd

from src.backtest.deep_backtest import (
    CellResult,
    DeepBacktestResult,
    WINDOW_LABELS,
)


def _ascii(s: str) -> str:
    """Strip non-ASCII for fpdf2 compatibility."""
    replacements = {
        "×": "x", "±": "+/-", "→": "->", "←": "<-",
        "✓": "OK", "✗": "X", "•": "-", "—": "-", "–": "-",
        "…": "...", "≥": ">=", "≤": "<=", "≠": "!=",
        "⚠": "WARN", "█": "#", "₹": "INR", "€": "EUR",
        "\u2212": "-",  # unicode minus
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    return "".join(c if ord(c) < 128 else "?" for c in s)


def _wrap_text(text: str, width: int) -> list[str]:
    """Simple word-wrap helper for PDF body text."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        line = ""
        for word in words:
            test = (line + " " + word).strip()
            if len(test) <= width:
                line = test
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
    return lines or [""]


def _short_fee(fp: str) -> str:
    return (fp
            .replace("ic_markets_", "")
            .replace("_xauusd_normal", "")
            .replace("_cost", ""))


def _fmt_pct(v: float) -> str:
    cls = "pos" if v > 0 else ("neg" if v < 0 else "muted")
    return f"<span class='{cls}'>{v:+.2f}%</span>"


# ── Heatmaps ─────────────────────────────────────────────────────────────


def _generate_heatmaps(result: DeepBacktestResult) -> list[Path]:
    """Generate one heatmap per (window, fee) pair showing TF × leverage Return%."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("  ⚠ matplotlib not available, skipping heatmaps")
        return []

    heatmap_dir = result.report_dir / "heatmaps"
    heatmap_dir.mkdir(parents=True, exist_ok=True)
    df = result.matrix_df

    produced: list[Path] = []
    for window_label in df["window_label"].unique():
        for fee in df["fee_profile"].unique():
            sub = df[(df["window_label"] == window_label) & (df["fee_profile"] == fee)]
            if sub.empty:
                continue
            pivot = sub.pivot_table(
                index="timeframe", columns="leverage",
                values="return_pct", aggfunc="first",
            )
            # Order TFs + leverages as configured
            tf_order = [tf for tf in result.config.timeframes if tf in pivot.index]
            pivot = pivot.reindex(tf_order)
            lev_order = sorted(pivot.columns)
            pivot = pivot[lev_order]

            fig, ax = plt.subplots(figsize=(7, max(2.5, 1 + 0.8 * len(tf_order))))
            data = pivot.values.astype(float)
            finite = data[~np.isnan(data)]
            vmax = max(1.0, abs(finite).max()) if finite.size > 0 else 1.0
            im = ax.imshow(data, cmap="RdYlGn", aspect="auto", vmin=-vmax, vmax=vmax)
            ax.set_xticks(range(len(lev_order)))
            ax.set_xticklabels([f"{int(c)}x" for c in lev_order])
            ax.set_yticks(range(len(pivot.index)))
            ax.set_yticklabels(pivot.index)
            ax.set_xlabel("Leverage")
            ax.set_ylabel("Timeframe")
            ax.set_title(f"{result.strategy_name} Return % — {window_label} × {_short_fee(fee)}")
            for i in range(len(pivot.index)):
                for j in range(len(lev_order)):
                    val = pivot.values[i, j]
                    if pd.isna(val):
                        continue
                    color = "white" if abs(val) > vmax * 0.5 else "black"
                    ax.text(j, i, f"{val:+.1f}%", ha="center", va="center",
                            color=color, fontsize=9)
            fig.colorbar(im, ax=ax, label="Return %")
            plt.tight_layout()
            out = heatmap_dir / f"{window_label}_{_short_fee(fee)}.png"
            fig.savefig(out, dpi=110, bbox_inches="tight")
            plt.close(fig)
            produced.append(out)

    print(f"  ✓ heatmaps: {len(produced)} PNGs in {heatmap_dir}")
    return produced


# ── HTML report ──────────────────────────────────────────────────────────


def _generate_html(result: DeepBacktestResult) -> Path:
    """Generate the primary HTML report — a single scrollable document with
    summary banner, per-window tables, matrix dump, WF results, and verdict."""
    cfg = result.config
    df = result.matrix_df

    html = [
        "<!DOCTYPE html>", "<html lang='en'>", "<head>", "<meta charset='UTF-8'>",
        f"<title>Deep Backtest — {result.strategy_name} — {result.timestamp}</title>",
        "<style>",
        "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;"
        " max-width: 1400px; margin: 2em auto; padding: 0 2em; color: #222; }",
        "h1 { border-bottom: 3px solid #333; padding-bottom: 0.3em; }",
        "h2 { color: #444; margin-top: 2em; border-bottom: 1px solid #ddd; padding-bottom: 0.2em; }",
        "h3 { color: #666; margin-top: 1.5em; }",
        "table { border-collapse: collapse; margin: 1em 0; font-size: 13px; }",
        "th, td { padding: 6px 10px; text-align: right; border: 1px solid #ddd; }",
        "th { background: #f5f5f5; font-weight: 600; text-align: center; }",
        "td:first-child, th:first-child { text-align: left; }",
        "tr:nth-child(even) { background: #fafafa; }",
        ".pos { color: #0a7a3a; font-weight: 600; }",
        ".neg { color: #b22222; font-weight: 600; }",
        ".muted { color: #888; }",
        ".verdict-DEPLOYABLE { background: #e3f4e5; border-left: 5px solid #0a7a3a; padding: 1em 1.2em; margin: 1em 0; }",
        ".verdict-RESEARCH_ONLY { background: #fff8e1; border-left: 5px solid #e8b900; padding: 1em 1.2em; margin: 1em 0; }",
        ".verdict-NEEDS_WF { background: #e1f0ff; border-left: 5px solid #2176c4; padding: 1em 1.2em; margin: 1em 0; }",
        ".verdict-FAILED { background: #fde5e5; border-left: 5px solid #b22222; padding: 1em 1.2em; margin: 1em 0; }",
        ".note { background: #fffbf0; border-left: 4px solid #e8b900; padding: 1em; margin: 1em 0; }",
        ".best { background: #fff7cc; font-weight: 700; }",
        ".kv { margin: 0.3em 0; }",
        ".kv b { display: inline-block; min-width: 200px; color: #555; }",
        "img.heatmap { max-width: 100%; border: 1px solid #ddd; padding: 4px; margin: 0.5em 0; }",
        "</style>", "</head>", "<body>",
    ]

    # Header
    html.append(f"<h1>Deep Backtest — {result.strategy_name}</h1>")
    html.append(f"<p class='muted'>Generated {result.timestamp} &middot; "
                f"{cfg.symbol} &middot; {len(result.matrix)} matrix cells &middot; "
                f"{result.elapsed_seconds:.1f}s elapsed</p>")

    # Verdict banner
    html.append(f"<div class='verdict-{result.verdict}'>")
    html.append(f"<h2 style='margin-top:0;border:none;'>VERDICT: {result.verdict}</h2>")
    html.append(f"<p>{result.verdict_reason}</p>")
    html.append("</div>")

    # Configuration summary
    html.append("<h2>Configuration</h2>")
    html.append("<div class='kv'><b>Strategy:</b> " + result.strategy_name + "</div>")
    if cfg.strategy_params:
        html.append("<div class='kv'><b>Params:</b> <code>"
                    + ", ".join(f"{k}={v}" for k, v in cfg.strategy_params.items())
                    + "</code></div>")
    html.append(f"<div class='kv'><b>Timeframes:</b> {', '.join(cfg.timeframes)}</div>")
    html.append(f"<div class='kv'><b>Windows (days):</b> {', '.join(str(w) for w in cfg.window_days)}</div>")
    html.append(f"<div class='kv'><b>Leverages:</b> {', '.join(f'{int(l)}x' for l in cfg.leverages)}</div>")
    html.append(f"<div class='kv'><b>Fee profiles:</b> {', '.join(cfg.fee_profiles)}</div>")
    html.append(f"<div class='kv'><b>Initial cash:</b> ${cfg.initial_cash:,.0f}</div>")
    if cfg.wf_enabled:
        html.append(f"<div class='kv'><b>Walk-forward:</b> {cfg.wf_n_folds} × {cfg.wf_fold_days}d "
                    f"(gate Calmar ≥ {cfg.wf_gate_calmar})"
                    + (", with retune" if cfg.wf_retune else ", fixed params") + "</div>")

    # Best cell
    if result.best_cell:
        bc = result.best_cell
        html.append("<h2>Best cell (by Calmar)</h2>")
        html.append("<table>")
        html.append("<tr><th>Window</th><th>TF</th><th>Leverage</th><th>Fees</th>"
                    "<th>Return</th><th>Max DD</th><th>Calmar</th><th>Sharpe</th>"
                    "<th>Trades</th><th>Win%</th><th>PF</th></tr>")
        html.append("<tr class='best'>")
        html.append(f"<td>{bc.window_label}</td><td>{bc.timeframe}</td>"
                    f"<td>{int(bc.leverage)}x</td><td>{_short_fee(bc.fee_profile)}</td>"
                    f"<td>{_fmt_pct(bc.return_pct)}</td><td>{bc.maxdd_pct:.2f}%</td>"
                    f"<td>{bc.calmar:+.3f}</td><td>{bc.sharpe:+.3f}</td>"
                    f"<td>{bc.trades}</td><td>{bc.win_rate:.1f}%</td>"
                    f"<td>{bc.profit_factor:.2f}</td>")
        html.append("</tr></table>")

    # Sanity checks
    html.append("<h2>Sanity checks</h2>")
    sanity = result.sanity_checks
    html.append("<ul>")
    html.append(f"<li>Error cells: {sanity.get('error_cells', 0)}</li>")
    if "trade_count" in sanity:
        tc = sanity["trade_count"]
        html.append(f"<li>Trade counts: min={tc['min']} max={tc['max']} "
                    f"mean={tc['mean']:.0f} zero={tc['zero_trade_cells']}</li>")
    li = sanity.get("leverage_invariance", {})
    html.append(f"<li>Leverage invariance: {li.get('invariant_triplets', 0)} invariant / "
                f"{li.get('variant_triplets', 0)} variant triplets"
                + (" ⚠" if li.get('all_invariant') else "") + "</li>")
    if "max_cost_pct_of_margin" in sanity:
        html.append(f"<li>Max cost/margin: {sanity['max_cost_pct_of_margin']:.2f}%</li>")
    html.append(f"<li>Extreme DD cells (>50%): {sanity.get('extreme_dd_cells', 0)}</li>")
    html.append("</ul>")

    # Phase 2.5 — Leverage mode validation (task #115)
    if result.leverage_mode_validation is not None:
        lmv = result.leverage_mode_validation
        status = "✓ PASS" if lmv.passed else "✗ FAIL"
        bg = "e3f4e5" if lmv.passed else "fde5e5"
        border = "0a7a3a" if lmv.passed else "b22222"
        html.append("<h2>Phase 2.5 — Leverage mode validation (task #115)</h2>")
        html.append(
            f"<div style='background:#{bg};border-left:5px solid #{border};"
            f"padding:1em 1.2em;margin:1em 0;'>"
            f"<h3 style='margin-top:0;border:none;'>{status} — mode: {lmv.mode.value}</h3>"
            f"<p>Zero-tolerance hand-trace validation that the leverage_mode "
            f"transform produced the expected mathematical behavior. "
            f"{len(lmv.failures)} failure(s), {len(lmv.warnings)} warning(s).</p>"
            f"</div>"
        )
        if lmv.failures:
            html.append("<h3 style='color:#b22222;'>HARD failures (forced verdict = FAILED)</h3>")
            html.append("<ul>")
            for f in lmv.failures:
                html.append(f"<li><code>{f}</code></li>")
            html.append("</ul>")
        if lmv.warnings:
            html.append("<h3 style='color:#8a6500;'>Warnings (non-fatal)</h3>")
            html.append("<ul>")
            for w in lmv.warnings:
                html.append(f"<li>{w}</li>")
            html.append("</ul>")
        if lmv.hand_trace:
            html.append("<h3>Hand-trace</h3>")
            html.append("<div class='kv'>")
            for k, v in lmv.hand_trace.items():
                if isinstance(v, dict):
                    html.append(f"<div><b>{k}:</b> <code>{v}</code></div>")
                else:
                    html.append(f"<div><b>{k}:</b> {v}</div>")
            html.append("</div>")
        if lmv.assertion_results:
            html.append("<h3>Assertion results</h3>")
            html.append("<table>")
            html.append("<tr><th>Assertion</th><th>Passed</th><th>Details</th></tr>")
            for name, res in lmv.assertion_results.items():
                if not isinstance(res, dict):
                    continue
                passed = res.get("passed")
                if passed is None:
                    continue
                mark = "✓" if passed else "✗"
                details = ", ".join(f"{k}={v}" for k, v in res.items() if k != "passed")
                html.append(f"<tr><td>{name}</td><td>{mark}</td><td><code>{details}</code></td></tr>")
            html.append("</table>")

    # Leverage validation deep-dive
    if result.leverage_validation:
        lv = result.leverage_validation
        html.append("<h2>Leverage validation deep-dive</h2>")
        html.append(f"<div class='note'><b>{'✓ PASS' if lv.hand_trace_passed else '✗ FAIL'}</b> — "
                    f"{lv.invariant_reason}</div>")
        html.append("<h3>Hand-trace comparison</h3>")
        d = lv.hand_trace_details
        html.append("<table>")
        html.append(f"<tr><th>Field</th><th>{int(d['lev_low'])}x</th>"
                    f"<th>{int(d['lev_high'])}x</th><th>Δ</th></tr>")
        rows = [
            ("Trades", d["trades_low"], d["trades_high"]),
            ("Final equity", f"${d['final_equity_low']:.2f}", f"${d['final_equity_high']:.2f}"),
            ("Total P&L", f"${d['total_pnl_low']:.2f}", f"${d['total_pnl_high']:.2f}"),
            ("Total margin", f"${d['total_margin_low']:.2f}", f"${d['total_margin_high']:.2f}"),
        ]
        for label, lo, hi in rows:
            html.append(f"<tr><td>{label}</td><td>{lo}</td><td>{hi}</td><td>—</td></tr>")
        if d.get("margin_ratio_actual"):
            html.append(f"<tr><td>Margin ratio</td><td>1.00</td>"
                        f"<td>{d['margin_ratio_actual']:.4f}</td>"
                        f"<td>expected {d['margin_ratio_expected']:.0f}x</td></tr>")
        html.append("</table>")

    # Walk-forward results
    if result.walk_forward:
        wf = result.walk_forward
        html.append("<h2>Walk-forward OOS validation</h2>")
        html.append(f"<div class='kv'><b>Config:</b> {wf.n_folds} folds × {wf.fold_days}d "
                    f"on {wf.timeframe} × {_short_fee(wf.fee_profile)}</div>")

        html.append("<h3>Three metrics, three stories</h3>")
        html.append("<table>")
        html.append("<tr><th>Metric</th><th>Value</th><th>Gate ≥ " +
                    f"{wf.gate_calmar}</th></tr>")
        html.append("<tr><td>Per-fold mean Calmar</td>"
                    f"<td>{wf.mean_calmar:+.3f} ± {wf.std_calmar:.3f}</td>"
                    f"<td>{'✓ pass' if wf.gate_per_fold_passed else '✗ fail'}</td></tr>")
        html.append("<tr><td>Continuous full-window Calmar</td>"
                    f"<td>{wf.continuous_calmar:+.3f}</td>"
                    f"<td>{'✓ pass' if wf.continuous_gate_passed else '✗ fail'}</td></tr>")
        html.append("</table>")

        html.append("<h3>Per-fold breakdown</h3>")
        html.append("<table>")
        html.append("<tr><th>Fold</th><th>Window</th><th>Trades</th><th>Return</th>"
                    "<th>Max DD</th><th>Calmar</th><th>Sharpe</th><th>Win%</th><th>PF</th></tr>")
        for f in wf.folds:
            pf_str = f"{f.profit_factor:.2f}" if f.profit_factor != float('inf') else "inf"
            html.append("<tr>")
            html.append(f"<td>{f.fold}</td>"
                        f"<td>{f.start_ts[:10]} → {f.end_ts[:10]}</td>"
                        f"<td>{f.trades}</td>"
                        f"<td>{_fmt_pct(f.return_pct)}</td>"
                        f"<td>{f.max_dd_pct:.2f}%</td>"
                        f"<td>{f.calmar:+.3f}</td>"
                        f"<td>{f.sharpe:+.3f}</td>"
                        f"<td>{f.win_rate:.1f}%</td>"
                        f"<td>{pf_str}</td>")
            html.append("</tr>")
        html.append("</table>")
        html.append(f"<p>Profitable folds: {wf.profitable_folds} / {wf.n_folds}. "
                    f"Total trades: {wf.total_trades}.</p>")

    # ── Task #79 (G.7) — Attribution decomposition ──────────────────────
    if result.attribution:
        html.append("<h2>Attribution — where did the return come from?</h2>")
        html.append(
            "<p class='muted'>Decomposes each cell's return into "
            "<b>alpha</b> (unlevered edge), <b>leverage amplification</b>, "
            "<b>cost drag</b> (commissions), and <b>margin rejection drag</b> "
            "(lost signals). The math is an approximation — see "
            "<code>docs/LEVERAGE_STRATEGY_DESIGN.md §9</code> for limits.</p>"
        )

        # Best cell breakdown — prominent card with visual proportions
        if result.best_cell is not None:
            bc = result.best_cell
            key = (bc.window_days, bc.timeframe, bc.leverage, bc.fee_profile)
            ba = result.attribution.get(key)
            if ba is not None:
                tot = ba.total_return
                # Compute visual widths as fractions of max absolute value
                max_abs = max(
                    abs(ba.alpha_return), abs(ba.leverage_amplification),
                    abs(ba.cost_drag), abs(ba.margin_rejection_drag), 1.0,
                )
                def _bar(v: float) -> str:
                    pct = (abs(v) / max_abs) * 100.0
                    color = "#0a7a3a" if v >= 0 else "#b22222"
                    return (
                        f"<div style='background:{color};height:14px;"
                        f"width:{pct:.1f}%;display:inline-block;"
                        f"vertical-align:middle;'></div>"
                    )

                html.append("<h3>Best cell decomposition</h3>")
                html.append(
                    f"<div class='note'><b>{bc.window_label} × {bc.timeframe} × "
                    f"{int(bc.leverage)}x × {_short_fee(bc.fee_profile)}</b> → "
                    f"total return <b>{_fmt_pct(tot)}</b></div>"
                )
                html.append("<table style='width:100%;'>")
                html.append("<tr><th>Component</th><th style='width:50%;'>"
                            "Contribution</th><th>Value</th><th>% of total</th></tr>")
                rows = [
                    ("Alpha (unlevered edge)", ba.alpha_return),
                    ("Leverage amplification", ba.leverage_amplification),
                    ("Cost drag (commissions)", ba.cost_drag),
                    ("Margin rejection drag", ba.margin_rejection_drag),
                    ("Residual (compounding drift)", ba.residual),
                ]
                for label, value in rows:
                    pct_of_total = (value / tot * 100.0) if abs(tot) > 1e-9 else 0.0
                    html.append(
                        f"<tr><td>{label}</td>"
                        f"<td>{_bar(value)}</td>"
                        f"<td>{_fmt_pct(value)}</td>"
                        f"<td class='muted'>{pct_of_total:+.1f}%</td></tr>"
                    )
                html.append("</table>")
                if ba.note:
                    html.append(f"<p class='muted'><i>Note: {ba.note}</i></p>")
                if ba.baseline_cell_leverage is not None:
                    html.append(
                        f"<p class='muted'>Baseline cell leverage for alpha "
                        f"computation: <code>{ba.baseline_cell_leverage:g}x</code></p>"
                    )

        # Full matrix attribution table
        html.append("<h3>Full matrix attribution</h3>")
        html.append("<table>")
        html.append("<tr><th>Window</th><th>TF</th><th>Lev</th><th>Fees</th>"
                    "<th>Total</th><th>Alpha</th><th>Lev amp</th>"
                    "<th>Cost drag</th><th>Margin drag</th><th>Residual</th></tr>")
        for key in sorted(result.attribution.keys()):
            ba = result.attribution[key]
            wl = WINDOW_LABELS.get(ba.window_days, f"{ba.window_days}d")
            html.append(
                f"<tr>"
                f"<td>{wl}</td>"
                f"<td>{ba.timeframe}</td>"
                f"<td>{int(ba.leverage)}x</td>"
                f"<td>{_short_fee(ba.fee_profile)}</td>"
                f"<td>{_fmt_pct(ba.total_return)}</td>"
                f"<td>{_fmt_pct(ba.alpha_return)}</td>"
                f"<td>{_fmt_pct(ba.leverage_amplification)}</td>"
                f"<td>{_fmt_pct(ba.cost_drag)}</td>"
                f"<td>{_fmt_pct(ba.margin_rejection_drag)}</td>"
                f"<td>{_fmt_pct(ba.residual)}</td>"
                f"</tr>"
            )
        html.append("</table>")
        html.append(
            "<p class='muted'><i>How to read: higher leverage amplification = "
            "more return from the leverage dial; higher alpha = more from "
            "signal quality. Large residuals signal that compounding drift or "
            "path-dependent effects (stop-outs, margin calls) are dominating "
            "the math — treat those cells' decomposition with caution.</i></p>"
        )

    # Heatmap image references
    heatmap_dir = result.report_dir / "heatmaps"
    if heatmap_dir.exists() and any(heatmap_dir.iterdir()):
        html.append("<h2>Heatmaps</h2>")
        for window_label in cfg.window_days:
            wl = WINDOW_LABELS.get(window_label, f"{window_label}d")
            for fee in cfg.fee_profiles:
                img = heatmap_dir / f"{wl}_{_short_fee(fee)}.png"
                if img.exists():
                    html.append(f"<h3>{wl} × {_short_fee(fee)}</h3>")
                    html.append(f"<img class='heatmap' src='heatmaps/{img.name}' alt='{wl} {fee}'/>")

    # Full matrix dump
    html.append("<h2>Full matrix ({} cells)</h2>".format(len(result.matrix)))
    html.append("<table>")
    html.append("<tr><th>Window</th><th>TF</th><th>Lev</th><th>Fees</th>"
                "<th>Trades</th><th>Return</th><th>Max DD</th><th>Calmar</th>"
                "<th>Sharpe</th><th>Win%</th><th>PF</th><th>Comm$</th>"
                "<th>Cost%/margin</th><th>Stops</th></tr>")
    for c in sorted(result.matrix, key=lambda x: (x.window_days, x.timeframe, x.leverage, x.fee_profile)):
        is_best = result.best_cell and (
            c.window_label == result.best_cell.window_label
            and c.timeframe == result.best_cell.timeframe
            and c.leverage == result.best_cell.leverage
            and c.fee_profile == result.best_cell.fee_profile
        )
        tr_class = "best" if is_best else ""
        pf_str = f"{c.profit_factor:.2f}" if c.profit_factor != float('inf') else "inf"
        html.append(f"<tr class='{tr_class}'>")
        html.append(f"<td>{c.window_label}</td><td>{c.timeframe}</td>"
                    f"<td>{int(c.leverage)}x</td><td>{_short_fee(c.fee_profile)}</td>"
                    f"<td>{c.trades}</td><td>{_fmt_pct(c.return_pct)}</td>"
                    f"<td>{c.maxdd_pct:.2f}%</td><td>{c.calmar:+.3f}</td>"
                    f"<td>{c.sharpe:+.3f}</td><td>{c.win_rate:.1f}%</td>"
                    f"<td>{pf_str}</td><td>${c.total_commission:,.0f}</td>"
                    f"<td>{c.cost_pct_of_margin:.3f}%</td>"
                    f"<td>{c.broker_stop_outs}</td>")
        html.append("</tr>")
    html.append("</table>")

    html.append("</body></html>")

    out_path = result.report_dir / "index.html"
    out_path.write_text("\n".join(html))
    print(f"  ✓ HTML: {out_path}")
    return out_path


# ── PDF report ───────────────────────────────────────────────────────────


def _generate_pdf(result: DeepBacktestResult) -> Path | None:
    """Generate a landscape A4 PDF with cover page + per-window heatmap + tables."""
    try:
        from fpdf import FPDF
    except ImportError:
        print("  ⚠ fpdf2 not installed, skipping PDF")
        return None

    cfg = result.config
    pdf = FPDF(orientation="landscape", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=10)

    # Cover page
    pdf.add_page()
    pdf.set_font("Helvetica", style="B", size=20)
    pdf.cell(0, 12, _ascii(f"Deep Backtest — {result.strategy_name}"),
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 6, _ascii(
        f"Generated {result.timestamp}  |  {cfg.symbol}  |  "
        f"{len(result.matrix)} matrix cells"
    ), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(8)

    # Verdict block
    pdf.set_font("Helvetica", style="B", size=14)
    pdf.cell(0, 8, _ascii(f"Verdict: {result.verdict}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    for line in _wrap_text(_ascii(result.verdict_reason), 130):
        pdf.cell(0, 5, _ascii(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Configuration summary
    pdf.set_font("Helvetica", style="B", size=12)
    pdf.cell(0, 6, "Configuration", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    config_lines = [
        f"Timeframes:   {', '.join(cfg.timeframes)}",
        f"Windows:      {', '.join(str(w) + 'd' for w in cfg.window_days)}",
        f"Leverages:    {', '.join(str(int(l)) + 'x' for l in cfg.leverages)}",
        f"Fee profiles: {', '.join(cfg.fee_profiles)}",
        f"Initial cash: ${cfg.initial_cash:,.0f}",
    ]
    if cfg.wf_enabled:
        config_lines.append(
            f"Walk-forward: {cfg.wf_n_folds} x {cfg.wf_fold_days}d, "
            f"gate Calmar >= {cfg.wf_gate_calmar}"
            + (", retune" if cfg.wf_retune else ", fixed params")
        )
    if cfg.strategy_params:
        config_lines.append(
            f"Strategy params: " + ", ".join(f"{k}={v}" for k, v in cfg.strategy_params.items())
        )
    for line in config_lines:
        pdf.cell(0, 5, _ascii(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Best cell summary
    if result.best_cell:
        bc = result.best_cell
        pdf.set_font("Helvetica", style="B", size=12)
        pdf.cell(0, 6, "Best cell (by Calmar)", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 5, _ascii(
            f"{bc.window_label} x {bc.timeframe} x {int(bc.leverage)}x x {_short_fee(bc.fee_profile)}: "
            f"return {bc.return_pct:+.2f}%, max DD {bc.maxdd_pct:.2f}%, Calmar {bc.calmar:+.3f}, "
            f"Sharpe {bc.sharpe:+.3f}, {bc.trades} trades"
        ), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    # Walk-forward summary
    if result.walk_forward:
        wf = result.walk_forward
        pdf.set_font("Helvetica", style="B", size=12)
        pdf.cell(0, 6, "Walk-forward OOS summary", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 5, _ascii(
            f"{wf.n_folds} x {wf.fold_days}d folds on {wf.timeframe} x {_short_fee(wf.fee_profile)}"
        ), new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, _ascii(
            f"Per-fold Calmar: {wf.mean_calmar:+.3f} +/- {wf.std_calmar:.3f}  "
            f"({'PASS' if wf.gate_per_fold_passed else 'FAIL'} gate)"
        ), new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, _ascii(
            f"Continuous run Calmar: {wf.continuous_calmar:+.3f}  "
            f"({'PASS' if wf.continuous_gate_passed else 'FAIL'} gate)"
        ), new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, _ascii(
            f"Profitable folds: {wf.profitable_folds} / {wf.n_folds}  |  "
            f"Total trades: {wf.total_trades}"
        ), new_x="LMARGIN", new_y="NEXT")

    # Leverage validation section
    if result.leverage_validation:
        pdf.ln(4)
        pdf.set_font("Helvetica", style="B", size=12)
        pdf.cell(0, 6, "Leverage validation deep-dive", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 5, _ascii(
            f"Auto-triggered (matrix showed leverage-invariant P&L)  "
            f"|  Hand-trace: {'PASS' if result.leverage_validation.hand_trace_passed else 'FAIL'}"
        ), new_x="LMARGIN", new_y="NEXT")
        for line in _wrap_text(_ascii(result.leverage_validation.invariant_reason), 130):
            pdf.cell(0, 5, _ascii(line), new_x="LMARGIN", new_y="NEXT")

    # Per-window pages with heatmaps + tables
    df = result.matrix_df
    heatmap_dir = result.report_dir / "heatmaps"
    for w in cfg.window_days:
        wl = WINDOW_LABELS.get(w, f"{w}d")
        for fee in cfg.fee_profiles:
            pdf.add_page()
            pdf.set_font("Helvetica", style="B", size=14)
            pdf.cell(0, 8, _ascii(f"Window {wl}  |  Fee mode: {_short_fee(fee)}"),
                     new_x="LMARGIN", new_y="NEXT", align="L")
            pdf.ln(2)

            # Embed heatmap PNG if available
            img_path = heatmap_dir / f"{wl}_{_short_fee(fee)}.png"
            if img_path.exists():
                try:
                    pdf.image(str(img_path), x=10, y=pdf.get_y(), w=130)
                    pdf.set_y(pdf.get_y() + 75)
                except Exception:
                    pass

            # Table for this (window, fee)
            sub = df[(df["window_days"] == w) & (df["fee_profile"] == fee)].copy()
            if sub.empty:
                continue
            sub = sub.sort_values(["timeframe", "leverage"])
            pdf.set_font("Helvetica", style="B", size=8)
            headers = ["TF", "Lev", "Trades", "Return%", "MaxDD%", "Calmar",
                       "Sharpe", "Win%", "PF", "Cost%mar", "Stops"]
            col_widths = [14, 14, 20, 22, 20, 20, 20, 16, 16, 22, 14]
            x0 = 145
            pdf.set_xy(x0, 25)
            for h, wdt in zip(headers, col_widths):
                pdf.cell(wdt, 5, _ascii(h), border=1, align="C")
            pdf.ln(5)
            pdf.set_font("Helvetica", size=7)
            for _, r in sub.iterrows():
                pdf.set_x(x0)
                pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] < 100 else "inf"
                row = [
                    r["timeframe"],
                    f"{int(r['leverage'])}x",
                    str(int(r["trades"])),
                    f"{r['return_pct']:+.2f}%",
                    f"{r['maxdd_pct']:.2f}%",
                    f"{r['calmar']:+.3f}",
                    f"{r['sharpe']:+.3f}",
                    f"{r['win_rate']:.1f}%",
                    pf_str,
                    f"{r['cost_pct_of_margin']:.3f}%",
                    str(int(r["broker_stop_outs"])),
                ]
                for v, wdt in zip(row, col_widths):
                    pdf.cell(wdt, 4.5, _ascii(str(v)), border=1, align="R")
                pdf.ln(4.5)

    # Walk-forward dedicated page
    if result.walk_forward:
        wf = result.walk_forward
        pdf.add_page()
        pdf.set_font("Helvetica", style="B", size=14)
        pdf.cell(0, 8, _ascii("Walk-forward per-fold breakdown"),
                 new_x="LMARGIN", new_y="NEXT", align="L")
        pdf.set_font("Helvetica", style="B", size=9)
        headers = ["Fold", "Start", "End", "Trades", "Return%", "DD%",
                   "Calmar", "Sharpe", "Win%", "PF"]
        col_widths = [15, 40, 40, 18, 22, 20, 22, 22, 18, 18]
        for h, wdt in zip(headers, col_widths):
            pdf.cell(wdt, 6, _ascii(h), border=1, align="C")
        pdf.ln(6)
        pdf.set_font("Helvetica", size=8)
        for f in wf.folds:
            pf_str = f"{f.profit_factor:.2f}" if f.profit_factor != float('inf') else "inf"
            row = [
                str(f.fold), f.start_ts[:10], f.end_ts[:10],
                str(f.trades),
                f"{f.return_pct:+.2f}%",
                f"{f.max_dd_pct:.2f}%",
                f"{f.calmar:+.3f}",
                f"{f.sharpe:+.3f}",
                f"{f.win_rate:.1f}%",
                pf_str,
            ]
            for v, wdt in zip(row, col_widths):
                pdf.cell(wdt, 5, _ascii(str(v)), border=1, align="R")
            pdf.ln(5)
        pdf.ln(3)
        pdf.set_font("Helvetica", size=9)
        pdf.cell(0, 5, _ascii(
            f"Mean return: {wf.mean_return_pct:+.2f}% +/- {wf.std_return_pct:.2f}  |  "
            f"Mean Calmar: {wf.mean_calmar:+.3f} +/- {wf.std_calmar:.3f}  |  "
            f"Continuous Calmar: {wf.continuous_calmar:+.3f}"
        ), new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, _ascii(
            f"Profitable folds: {wf.profitable_folds} / {wf.n_folds}  |  Gate: "
            f"{'PASS' if wf.continuous_gate_passed else 'FAIL'}"
        ), new_x="LMARGIN", new_y="NEXT")

    # Task #79 (G.7) — Attribution summary page
    if result.attribution and result.best_cell is not None:
        bc = result.best_cell
        key = (bc.window_days, bc.timeframe, bc.leverage, bc.fee_profile)
        ba = result.attribution.get(key)
        if ba is not None:
            pdf.add_page()
            pdf.set_font("Helvetica", style="B", size=14)
            pdf.cell(0, 8, _ascii("Attribution — where did the return come from?"),
                     new_x="LMARGIN", new_y="NEXT", align="L")
            pdf.set_font("Helvetica", size=10)
            pdf.cell(0, 5, _ascii(
                f"Best cell: {bc.window_label} x {bc.timeframe} x "
                f"{int(bc.leverage)}x x {_short_fee(bc.fee_profile)}"
            ), new_x="LMARGIN", new_y="NEXT")
            pdf.cell(0, 5, _ascii(f"Total return: {ba.total_return:+.2f}%"),
                     new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
            pdf.set_font("Helvetica", style="B", size=10)
            pdf.cell(80, 6, "Component", border=1)
            pdf.cell(30, 6, "Value", border=1, align="R")
            pdf.cell(30, 6, "% of total", border=1, align="R")
            pdf.ln(6)
            pdf.set_font("Helvetica", size=9)
            tot = ba.total_return if abs(ba.total_return) > 1e-9 else 1.0
            rows = [
                ("Alpha (unlevered edge)", ba.alpha_return),
                ("Leverage amplification", ba.leverage_amplification),
                ("Cost drag (commissions)", ba.cost_drag),
                ("Margin rejection drag", ba.margin_rejection_drag),
                ("Residual (compounding)", ba.residual),
            ]
            for label, value in rows:
                pct = (value / tot * 100.0)
                pdf.cell(80, 5, _ascii(label), border=1)
                pdf.cell(30, 5, _ascii(f"{value:+.2f}%"), border=1, align="R")
                pdf.cell(30, 5, _ascii(f"{pct:+.1f}%"), border=1, align="R")
                pdf.ln(5)
            pdf.ln(3)
            if ba.note:
                pdf.set_font("Helvetica", size=9)
                for line in _wrap_text(f"Note: {ba.note}", 100):
                    pdf.cell(0, 5, _ascii(line), new_x="LMARGIN", new_y="NEXT")
            if ba.baseline_cell_leverage is not None:
                pdf.set_font("Helvetica", size=9)
                pdf.cell(0, 5, _ascii(
                    f"Baseline cell leverage: {ba.baseline_cell_leverage:g}x "
                    f"(used for alpha computation)"
                ), new_x="LMARGIN", new_y="NEXT")

    pdf_path = result.report_dir / "report.pdf"
    pdf.output(str(pdf_path))
    print(f"  ✓ PDF:  {pdf_path}")
    return pdf_path


# ── Public entry ─────────────────────────────────────────────────────────


def write_report(result: DeepBacktestResult) -> dict[str, Path]:
    """Write all report artifacts: CSV (already done), heatmaps, HTML, PDF.

    Returns a dict of {artifact_name: path} for the caller.
    """
    print("=" * 80)
    print(f"Phase 6 — Report generation → {result.report_dir}")
    print("=" * 80)

    out: dict[str, Path] = {}
    if result.config.generate_heatmaps:
        paths = _generate_heatmaps(result)
        if paths:
            out["heatmaps"] = paths[0].parent

    if result.config.generate_html:
        out["html"] = _generate_html(result)

    if result.config.generate_pdf:
        pdf_path = _generate_pdf(result)
        if pdf_path:
            out["pdf"] = pdf_path

    # CSV is always produced by run_deep_backtest._write_json_summary, just link
    csv_path = result.report_dir / "matrix.csv"
    if csv_path.exists():
        out["csv"] = csv_path

    print()
    return out
