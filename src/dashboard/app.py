"""Streamlit dashboard — interactive UI for backtest results and strategy exploration.

Usage:
    streamlit run src/dashboard/app.py
    # or
    python -m scripts.dashboard

Pages:
    1. Backtest Runs — Browse all individual runs from result_store
    2. Strategy Deep Dive — Select a run → full metrics + charts
    3. Compare Runs — Side-by-side comparison of runs
    4. Imported Strategies — IR + generated code preview
    5. Validation — Protocol overview
    6. Strategies — Versioned registry from src/strategies/storage.py (task #122).
       Per parent → click → see all version variants + max-return cell + verdict +
       embedded HTML report viewer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Add project root to path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.backtest.result_store import ResultStore


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Algo Trading Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

_DB_PATH = "data/trades.db"
_REPORTS_DIR = Path("reports")


@st.cache_resource
def get_store() -> ResultStore:
    return ResultStore(db_path=_DB_PATH)


def _get_conn() -> sqlite3.Connection:
    """Get a direct DB connection for queries."""
    store = get_store()
    return store._ensure_conn()


# ---------------------------------------------------------------------------
# Sidebar Navigation
# ---------------------------------------------------------------------------

st.sidebar.title("Algo Trading")
page = st.sidebar.radio(
    "Navigation",
    [
        "Backtest Runs",
        "Strategy Deep Dive",
        "Compare Runs",
        "Imported Strategies",
        "Validation",
        "Strategies",
        "Run Deep Backtest",
    ],
    index=0,
)


# ---------------------------------------------------------------------------
# Page 1: Backtest Runs Browser
# ---------------------------------------------------------------------------

if page == "Backtest Runs":
    st.title("Backtest Runs")

    try:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT r.id, r.strategy_id, r.symbol, r.timeframe,
                      r.start_date, r.end_date, r.created_at,
                      res.total_return_pct, res.sharpe, res.max_drawdown_pct,
                      res.total_trades, res.profit_factor, res.win_rate_pct
               FROM backtest_runs r
               JOIN backtest_results res ON res.run_id = r.id
               ORDER BY r.created_at DESC"""
        ).fetchall()

        if not rows:
            st.info("No backtest runs found. Run a backtest first.")
        else:
            df = pd.DataFrame(
                rows,
                columns=[
                    "ID", "Strategy", "Symbol", "TF",
                    "Start", "End", "Created",
                    "Return %", "Sharpe", "Max DD %",
                    "Trades", "PF", "Win Rate %",
                ],
            )

            # Filters
            col1, col2, col3 = st.columns(3)
            with col1:
                strategies = ["All"] + sorted(df["Strategy"].unique().tolist())
                sel_strategy = st.selectbox("Strategy", strategies)
            with col2:
                symbols = ["All"] + sorted(df["Symbol"].unique().tolist())
                sel_symbol = st.selectbox("Symbol", symbols)
            with col3:
                min_trades = st.number_input("Min Trades", value=0, min_value=0)

            if sel_strategy != "All":
                df = df[df["Strategy"] == sel_strategy]
            if sel_symbol != "All":
                df = df[df["Symbol"] == sel_symbol]
            if min_trades > 0:
                df = df[df["Trades"] >= min_trades]

            # Color formatting
            st.dataframe(
                df.style.format({
                    "Return %": "{:.2f}%",
                    "Sharpe": "{:.3f}",
                    "Max DD %": "{:.2f}%",
                    "PF": "{:.3f}",
                    "Win Rate %": "{:.1f}%",
                }).map(
                    lambda x: "color: green" if isinstance(x, (int, float)) and x > 0 else
                    "color: red" if isinstance(x, (int, float)) and x < 0 else "",
                    subset=["Return %", "Sharpe"],
                ),
                use_container_width=True,
                hide_index=True,
            )

            st.caption(f"Showing {len(df)} runs")

    except Exception as e:
        st.error(f"Database error: {e}")


# ---------------------------------------------------------------------------
# Page 2: Strategy Deep Dive
# ---------------------------------------------------------------------------

elif page == "Strategy Deep Dive":
    st.title("Strategy Deep Dive")

    try:
        conn = _get_conn()
        runs = conn.execute(
            """SELECT r.id, r.strategy_id, r.symbol, r.timeframe, r.created_at
               FROM backtest_runs r ORDER BY r.created_at DESC"""
        ).fetchall()

        if not runs:
            st.info("No runs found.")
        else:
            run_options = {
                f"#{r[0]} — {r[1]} ({r[2]} {r[3]}) {r[4][:16]}": r[0]
                for r in runs
            }
            selected = st.selectbox("Select Run", list(run_options.keys()))
            run_id = run_options[selected]

            store = get_store()
            metrics = store.get_run_metrics(run_id)

            if metrics:
                # Metric cards
                col1, col2, col3, col4, col5 = st.columns(5)
                col1.metric("Return", f"{metrics.get('total_return_pct', 0):.2f}%")
                col2.metric("Sharpe", f"{metrics.get('sharpe', 0):.3f}")
                col3.metric("Sortino", f"{metrics.get('sortino', 0):.3f}")
                col4.metric("Max DD", f"{metrics.get('max_drawdown_pct', 0):.2f}%")
                col5.metric("Trades", f"{metrics.get('total_trades', 0)}")

                col6, col7, col8, col9, col10 = st.columns(5)
                col6.metric("Win Rate", f"{metrics.get('win_rate_pct', 0):.1f}%")
                col7.metric("Profit Factor", f"{metrics.get('profit_factor', 0):.3f}")
                col8.metric("Calmar", f"{metrics.get('calmar', 0):.3f}")
                col9.metric("Commission", f"${metrics.get('total_commission', 0):.2f}")
                psr = metrics.get('psr')
                col10.metric("PSR", f"{psr:.1%}" if psr else "N/A")

                # Load equity curve from DB
                row = conn.execute(
                    "SELECT equity_curve_json, trades_json FROM backtest_results WHERE run_id = ?",
                    (run_id,),
                ).fetchone()

                if row and row[0]:
                    import orjson
                    equity_data = orjson.loads(row[0])
                    equity = pd.Series(
                        {int(k): v for k, v in equity_data.items()}
                    )

                    from src.backtest.charts import equity_and_drawdown, monthly_heatmap
                    fig = equity_and_drawdown(equity)
                    st.plotly_chart(fig, use_container_width=True)

                    # Monthly heatmap
                    try:
                        eq = equity.copy()
                        eq.index = pd.to_datetime(eq.index, unit="ms")
                        daily = eq.resample("D").last().dropna()
                        if len(daily) > 60:
                            fig2 = monthly_heatmap(equity)
                            st.plotly_chart(fig2, use_container_width=True)
                    except Exception:
                        pass

                # Links to reports
                json_path = _REPORTS_DIR / f"{run_id}.json"
                html_path = _REPORTS_DIR / f"{run_id}.html"
                if json_path.exists():
                    st.download_button("Download JSON", json_path.read_text(), f"{run_id}.json")
                if html_path.exists():
                    st.download_button("Download HTML Report", html_path.read_text(), f"{run_id}.html")
            else:
                st.warning(f"No metrics found for run #{run_id}")

    except Exception as e:
        st.error(f"Error: {e}")


# ---------------------------------------------------------------------------
# Page 3: Compare Runs
# ---------------------------------------------------------------------------

elif page == "Compare Runs":
    st.title("Compare Runs")

    try:
        conn = _get_conn()
        runs = conn.execute(
            """SELECT r.id, r.strategy_id, r.symbol, r.timeframe
               FROM backtest_runs r ORDER BY r.created_at DESC"""
        ).fetchall()

        if len(runs) < 2:
            st.info("Need at least 2 runs to compare.")
        else:
            run_options = {f"#{r[0]} — {r[1]} ({r[2]} {r[3]})": r[0] for r in runs}
            selected = st.multiselect(
                "Select runs to compare (2-5)",
                list(run_options.keys()),
                max_selections=5,
            )

            if len(selected) >= 2:
                store = get_store()
                comparison = []
                for label in selected:
                    rid = run_options[label]
                    m = store.get_run_metrics(rid)
                    if m:
                        comparison.append({"Run": label, **m})

                if comparison:
                    df = pd.DataFrame(comparison)
                    display_cols = [
                        "Run", "total_return_pct", "sharpe", "sortino",
                        "max_drawdown_pct", "calmar", "win_rate_pct",
                        "profit_factor", "total_trades",
                    ]
                    existing_cols = [c for c in display_cols if c in df.columns]
                    st.dataframe(df[existing_cols], use_container_width=True, hide_index=True)

                    # Radar chart
                    from src.backtest.charts import strategy_comparison_radar
                    strategies = {}
                    for item in comparison:
                        strategies[item["Run"][:30]] = item
                    fig = strategy_comparison_radar(strategies)
                    st.plotly_chart(fig, use_container_width=True)

    except Exception as e:
        st.error(f"Error: {e}")


# ---------------------------------------------------------------------------
# Page 4: Imported Strategies
# ---------------------------------------------------------------------------

elif page == "Imported Strategies":
    st.title("Imported Strategies")

    imported_dir = Path("strategies/imported")
    if not imported_dir.exists():
        st.info("No imported strategies. Use the import CLI to add strategies.")
    else:
        dirs = sorted(d for d in imported_dir.iterdir() if d.is_dir())
        if not dirs:
            st.info("No imported strategies found.")
        else:
            from src.research.strategy_ir import StrategyIR

            data = []
            for d in dirs:
                ir_file = d / "strategy.yaml"
                if ir_file.exists():
                    try:
                        ir = StrategyIR.from_file(str(ir_file))
                        data.append({
                            "ID": d.name,
                            "Name": ir.name,
                            "Format": ir.source_format,
                            "Markets": ", ".join(ir.markets),
                            "Timeframe": ir.timeframe,
                            "Indicators": ", ".join(ir.get_indicator_names()),
                        })
                    except Exception:
                        data.append({"ID": d.name, "Name": "ERROR", "Format": "-"})

            if data:
                st.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True)

            # Show IR for selected strategy
            selected = st.selectbox("View IR", [d.name for d in dirs])
            if selected:
                ir_file = imported_dir / selected / "strategy.yaml"
                if ir_file.exists():
                    st.code(ir_file.read_text(), language="yaml")

                py_file = imported_dir / selected / "strategy.py"
                if py_file.exists():
                    with st.expander("Generated Python Code"):
                        st.code(py_file.read_text(), language="python")

    st.markdown("---")
    st.markdown("**Import CLI:**")
    st.code(
        "python -m scripts.import_strategy import --format natural "
        '--describe "Buy when 9 EMA crosses 21 EMA"',
        language="bash",
    )


# ---------------------------------------------------------------------------
# Page 5: Validation Dashboard
# ---------------------------------------------------------------------------

elif page == "Validation":
    st.title("Validation Dashboard")
    st.markdown("Run validation protocols via CLI:")
    st.code(
        "python -m scripts.backtest validate sma_crossover --tier lite\n"
        "python -m scripts.backtest validate bb_rsi_mr --mode crash_stress",
        language="bash",
    )

    st.markdown("---")
    st.markdown("### Available Protocols")

    protocols = pd.DataFrame([
        {"Tier": "Lite", "Protocol": "smoke", "Description": "30-day sanity check", "Runtime": "~10s"},
        {"Tier": "Lite", "Protocol": "spot_check", "Description": "3-month trade generation check", "Runtime": "~20s"},
        {"Tier": "Lite", "Protocol": "psr_check", "Description": "Probabilistic Sharpe Ratio", "Runtime": "instant"},
        {"Tier": "Standard", "Protocol": "multi_interval", "Description": "Same TF, 6 time windows", "Runtime": "~3min"},
        {"Tier": "Standard", "Protocol": "multi_timeframe", "Description": "Same window, 4 TFs", "Runtime": "~3min"},
        {"Tier": "Standard", "Protocol": "commission_sweep", "Description": "Commission sensitivity", "Runtime": "~2min"},
        {"Tier": "Standard", "Protocol": "regime_test", "Description": "Bull/bear/sideways", "Runtime": "~2min"},
        {"Tier": "Standard", "Protocol": "monte_carlo", "Description": "10K trade shuffles", "Runtime": "~30s"},
        {"Tier": "Standard", "Protocol": "crash_stress", "Description": "6 known crash events", "Runtime": "~2min"},
        {"Tier": "Standard", "Protocol": "deflated_sharpe", "Description": "Multiple testing correction", "Runtime": "instant"},
    ])

    st.dataframe(protocols, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Page 6: Strategies — versioned registry (task #122 G.8)
# ---------------------------------------------------------------------------

elif page == "Strategies":
    from dataclasses import asdict
    from src.strategies.storage import (
        list_strategies,
        list_versions,
        list_version_runs,
        query_vanity_traps,
        _to_absolute,
    )

    st.title("Strategies — versioned registry")
    st.caption(
        "Auto-populated from `run_deep_backtest()`. Each parent strategy → "
        "its variants (different leverage modes, params, timeframes) → "
        "max-return cell + walk-forward Calmar + clickable HTML report."
    )

    # ── Vanity-trap audit (front-and-center)
    vanity = query_vanity_traps(db_path=_DB_PATH)
    if vanity:
        st.warning(
            f"⚠️ {len(vanity)} vanity trap(s) detected — DEPLOYABLE versions "
            f"with sane=False max-return cells. Audit before deploying live."
        )
        with st.expander(f"View {len(vanity)} vanity trap(s)"):
            vanity_rows = [
                {
                    "slug": v.version_slug,
                    "max_return_pct": v.max_return_pct,
                    "warning": v.max_return_warning,
                    "verdict": v.verdict,
                    "report_dir": v.report_dir,
                }
                for v in vanity
            ]
            st.dataframe(pd.DataFrame(vanity_rows), use_container_width=True, hide_index=True)

    # ── Parent strategy selector
    strategies = list_strategies(db_path=_DB_PATH)
    if not strategies:
        st.info(
            "No strategies in storage yet. Run a deep_backtest to auto-populate, "
            "or use `python3 scripts/strategies.py register <name>` to stub one. "
            "Run `python3 scripts/strategies_backfill.py` to import existing reports."
        )
    else:
        # Headline table
        st.subheader("All strategies")
        rollups = []
        for s in strategies:
            versions = list_versions(strategy_name=s.name, db_path=_DB_PATH)
            best = None
            for v in versions:
                if v.max_return_pct is not None:
                    if best is None or v.max_return_pct > (best.max_return_pct or -1e18):
                        best = v
            last_bt = max(
                (v.last_backtested_at for v in versions if v.last_backtested_at),
                default="never",
            )
            rollups.append({
                "name": s.name,
                "family": s.family or "—",
                "tier": s.tier or "—",
                "status": s.status,
                "versions": len(versions),
                "last_backtested": last_bt[:10] if last_bt != "never" else "never",
                "best_max_return_pct": best.max_return_pct if best else None,
                "best_sane": "✓" if (best is None or best.max_return_sane is None or best.max_return_sane) else "⚠",
            })
        st.dataframe(pd.DataFrame(rollups), use_container_width=True, hide_index=True)

        # ── Strategy detail
        st.markdown("---")
        st.subheader("Strategy detail")
        picked = st.selectbox("Pick a strategy", [s.name for s in strategies])
        s = next(s for s in strategies if s.name == picked)

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Status", s.status)
        with col2:
            st.metric("Family", s.family or "—")
        with col3:
            st.metric("Tier", s.tier or "—")
        if s.description:
            st.markdown(f"_{s.description}_")
        if s.markets:
            st.markdown(f"**Markets:** {', '.join(s.markets)}")
        if s.leverage_range:
            st.markdown(f"**Leverage range:** {s.leverage_range[0]:g}–{s.leverage_range[1]:g}x")

        # ── Versions table
        versions = list_versions(strategy_name=s.name, db_path=_DB_PATH)
        if not versions:
            st.info(f"No versions for {s.name}. Run deep_backtest to auto-populate.")
        else:
            st.markdown(f"### Versions ({len(versions)})")
            # Column order prioritizes the human-readable description over
            # the slug. The slug is the DB key (stable, idempotent) but the
            # description tells the reader what the variant actually IS.
            version_rows = []
            for v in versions:
                cell = v.max_return_cell or {}
                version_rows.append({
                    "description": v.description or f"(no name — {v.version_slug})",
                    "verdict": v.verdict or "—",
                    "max_return_pct": v.max_return_pct,
                    "max_dd_pct": cell.get("maxdd_pct"),
                    "calmar": cell.get("calmar"),
                    "wf_calmar": v.wf_continuous_calmar,
                    "wf_return_pct": v.wf_continuous_return_pct,
                    "trades": cell.get("trades"),
                    "sane": "✓" if v.max_return_sane else ("⚠" if v.max_return_sane is False else "—"),
                    "last_backtested": (v.last_backtested_at or "")[:10],
                    "slug": v.version_slug,
                })
            st.dataframe(pd.DataFrame(version_rows), use_container_width=True, hide_index=True)

            # ── Embedded report viewer
            st.markdown("### View deep_backtest HTML report")
            # Picker shows the human description; fall back to slug when None
            picker_options = [
                (v.description or v.version_slug) for v in versions
            ]
            picked_label = st.selectbox(
                "Pick a version",
                picker_options,
                key=f"version_picker_{s.name}",
            )
            v = next(
                v for v in versions
                if (v.description or v.version_slug) == picked_label
            )
            st.caption(f"slug: `{v.version_slug}`")
            if v.max_return_warning:
                st.warning(f"⚠ Max-return cell vanity flag: **{v.max_return_warning}**")
            if v.verdict_reason:
                st.caption(f"Verdict reason: {v.verdict_reason}")

            if v.report_html_path:
                abs_html = _to_absolute(v.report_html_path)
                if abs_html and abs_html.exists():
                    try:
                        html_content = abs_html.read_text()
                        st.components.v1.html(html_content, height=900, scrolling=True)
                    except Exception as e:
                        st.error(f"Failed to render HTML: {e}")
                else:
                    st.error(f"HTML file not found at {abs_html}")
            else:
                st.info("No HTML report path on this version (run deep_backtest with --no-html?)")

            # ── Run history for this version
            history = list_version_runs(
                strategy_name=s.name,
                version_slug=picked_slug,
                limit=50,
                db_path=_DB_PATH,
            )
            if len(history) > 1:
                with st.expander(f"Run history ({len(history)} runs)"):
                    st.dataframe(
                        pd.DataFrame([
                            {
                                "run_timestamp": (h.get("run_timestamp") or "")[:19],
                                "verdict": h.get("verdict"),
                                "max_return_pct": h.get("max_return_pct"),
                                "best_calmar": h.get("best_calmar"),
                                "matrix_n_cells": h.get("matrix_n_cells"),
                            }
                            for h in history
                        ]),
                        use_container_width=True,
                        hide_index=True,
                    )


# ---------------------------------------------------------------------------
# Page 7: Run Deep Backtest — Streamlit-native alternative to questionary TUI
# ---------------------------------------------------------------------------

elif page == "Run Deep Backtest":
    import subprocess

    from src.backtest.deep_backtest_interactive import (
        FEES_CATALOG,
        LEVERAGES_CATALOG,
        LEVERAGE_MODES_CATALOG,
        TIMEFRAMES_CATALOG,
        WINDOWS_CATALOG,
    )
    from src.backtest.deep_backtest import _check_window_availability
    from src.strategies.router import STRATEGY_REGISTRY as _CLASS_REGISTRY

    st.title("Run Deep Backtest")
    st.caption(
        "Browser-native alternative to the `questionary` TUI. Pick dimensions, "
        "hit Run, watch output stream live. Result auto-captures into the "
        "Strategies page when it finishes."
    )

    # ── Strategy picker ──────────────────────────────────────────────
    strategy_names = sorted(_CLASS_REGISTRY.keys())
    if not strategy_names:
        st.error("No strategies registered in `router.STRATEGY_REGISTRY`.")
        st.stop()

    strategy = st.selectbox(
        "Strategy",
        strategy_names,
        help="Registered strategy class to backtest. Sourced from `src/strategies/router.py::STRATEGY_REGISTRY`.",
    )

    # ── Symbol (defaults to XAUUSD for gold stack) ───────────────────
    col_s1, col_s2 = st.columns(2)
    with col_s1:
        symbol = st.text_input("Symbol", value="XAUUSD")
    with col_s2:
        initial_cash = st.number_input("Initial cash ($)", value=10000.0, step=1000.0, min_value=1000.0)

    st.markdown("---")

    # ── Matrix dimensions ────────────────────────────────────────────
    st.subheader("Matrix dimensions")
    st.caption("Multi-select tick boxes — each combination becomes one matrix cell.")

    # Windows with per-window availability check (probe 1h data for the symbol)
    window_labels = []
    window_values = {}
    for days, label in WINDOWS_CATALOG:
        try:
            available, reason = _check_window_availability(days, "1h", symbol)
        except Exception as e:
            available, reason = False, f"probe error: {type(e).__name__}"
        if available:
            tag = label
        else:
            tag = f"{label}  (NOT AVAILABLE — {reason})"
        window_labels.append(tag)
        window_values[tag] = (days, available)

    col_w, col_tf = st.columns(2)
    with col_w:
        picked_windows = st.multiselect(
            "Time windows",
            window_labels,
            default=[lbl for lbl in window_labels if window_values[lbl][1] and window_values[lbl][0] in (90, 365)],
        )
        # Filter out unavailable picks
        window_days_list = [window_values[lbl][0] for lbl in picked_windows if window_values[lbl][1]]

    with col_tf:
        tf_labels = [f"{tf} — {desc}" for tf, desc in TIMEFRAMES_CATALOG]
        tf_map = {f"{tf} — {desc}": tf for tf, desc in TIMEFRAMES_CATALOG}
        picked_tfs = st.multiselect(
            "Timeframes",
            tf_labels,
            default=[l for l in tf_labels if tf_map[l] in ("1h",)],
        )
        timeframes_list = [tf_map[l] for l in picked_tfs]

    # Fees + leverages
    col_f, col_l = st.columns(2)
    with col_f:
        fee_labels = [desc for _, desc in FEES_CATALOG]
        fee_map = {desc: key for key, desc in FEES_CATALOG}
        picked_fees = st.multiselect(
            "Fee profiles",
            fee_labels,
            default=[desc for _, desc in FEES_CATALOG if "MT4" in desc],
        )
        fees_list = [fee_map[l] for l in picked_fees]

    with col_l:
        picked_levs = st.multiselect(
            "Leverages (x)",
            [str(l) for l in LEVERAGES_CATALOG],
            default=["10"],
        )
        leverages_list = [float(x) for x in picked_levs]

    # Leverage modes (multi-select → runs pipeline once per mode)
    mode_labels = [desc for _, desc in LEVERAGE_MODES_CATALOG]
    mode_map = {desc: mode for mode, desc in LEVERAGE_MODES_CATALOG}
    picked_modes = st.multiselect(
        "Leverage modes (one run per mode, separate report dirs)",
        mode_labels,
        default=[desc for mode, desc in LEVERAGE_MODES_CATALOG if mode.value == "margin_capped"],
    )
    modes_list = [mode_map[l] for l in picked_modes]

    st.markdown("---")

    # ── Walk-forward + advanced ──────────────────────────────────────
    col_wf1, col_wf2, col_wf3 = st.columns(3)
    with col_wf1:
        wf_enabled = st.checkbox("Walk-forward OOS validation", value=True)
    with col_wf2:
        wf_retune = st.checkbox("WF retune (per-fold grid search)", value=False, disabled=not wf_enabled)
    with col_wf3:
        baseline_lev = st.number_input(
            "Baseline leverage (RISK_SCALED only)", value=10.0, step=1.0, min_value=1.0, max_value=1000.0,
        )

    with st.expander("Advanced options"):
        version_slug_override = st.text_input(
            "Explicit version slug (optional)",
            value="",
            help="Overrides the auto-generated `{mode}_L{baseline}_{tf}` slug. "
                 "Useful for naming bespoke variants (e.g. `optimized_v2`).",
        )
        strategy_params_raw = st.text_input(
            "Strategy param overrides (key=value, comma-separated)",
            value="",
            help="E.g. `session_filter=true,max_risk_per_trade=0.02`",
        )
        col_kelly1, col_kelly2, col_kelly3 = st.columns(3)
        with col_kelly1:
            kelly_win_rate = st.number_input("Kelly win rate (0-1)", value=0.0, step=0.05, min_value=0.0, max_value=1.0)
        with col_kelly2:
            kelly_payoff = st.number_input("Kelly payoff ratio", value=0.0, step=0.5, min_value=0.0)
        with col_kelly3:
            kelly_fraction = st.number_input("Kelly fraction", value=0.5, step=0.25, min_value=0.0, max_value=1.0)

    # ── Preview + cell count ─────────────────────────────────────────
    n_cells = (
        max(1, len(window_days_list)) *
        max(1, len(timeframes_list)) *
        max(1, len(leverages_list)) *
        max(1, len(fees_list)) *
        max(1, len(modes_list))
    )
    st.info(
        f"**Planned matrix:** {len(window_days_list) or 1} windows × "
        f"{len(timeframes_list) or 1} TFs × "
        f"{len(leverages_list) or 1} leverages × "
        f"{len(fees_list) or 1} fees × "
        f"{len(modes_list) or 1} modes = **{n_cells} cells**  "
        f"(runtime estimate: ~{max(1, n_cells * 2)}s)"
    )

    can_run = (
        len(window_days_list) > 0 and
        len(timeframes_list) > 0 and
        len(fees_list) > 0 and
        len(leverages_list) > 0 and
        len(modes_list) > 0
    )
    if not can_run:
        st.warning("Tick at least one value in every dimension to enable the Run button.")

    # ── Run button + live log stream ─────────────────────────────────
    if st.button("🚀 Run Deep Backtest", type="primary", disabled=not can_run):
        # Build the CLI args we would pass to scripts/deep_backtest.py.
        # We use `--non-interactive` to skip the TUI since the browser is the UI.
        # Multi-mode runs are handled by passing multiple --leverage-mode flags
        # OR by running the subprocess once per mode. The deep_backtest.py CLI
        # supports ONE mode per invocation (the TUI handles multi-mode by
        # looping), so we do the same.

        results_log = st.empty()
        status_placeholder = st.empty()
        log_buffer: list[str] = []

        # Extra args shared across modes
        base_cmd_tail = [
            "--non-interactive",
            "--symbol", symbol,
            "--timeframes", ",".join(timeframes_list),
            "--windows", ",".join(str(w) for w in window_days_list),
            "--leverages", ",".join(str(l) for l in leverages_list),
            "--fees", ",".join(fees_list),
            "--initial-cash", str(initial_cash),
            "--baseline-leverage", str(baseline_lev),
        ]
        if not wf_enabled:
            base_cmd_tail.append("--no-wf")
        if wf_retune:
            base_cmd_tail.append("--wf-retune")
        if version_slug_override.strip():
            base_cmd_tail.extend(["--version-slug", version_slug_override.strip()])
        if strategy_params_raw.strip():
            base_cmd_tail.extend(["--strategy-params", strategy_params_raw.strip()])
        if kelly_win_rate > 0:
            base_cmd_tail.extend(["--kelly-win-rate", str(kelly_win_rate)])
        if kelly_payoff > 0:
            base_cmd_tail.extend(["--kelly-payoff-ratio", str(kelly_payoff)])
        if kelly_fraction != 0.5:
            base_cmd_tail.extend(["--kelly-fraction", str(kelly_fraction)])

        # One subprocess per mode (same as TUI's multi-mode loop)
        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])

        total_modes = len(modes_list)
        for mode_idx, mode in enumerate(modes_list, start=1):
            status_placeholder.markdown(
                f"**Running mode {mode_idx}/{total_modes}:** `{mode.value}` — {strategy}"
            )
            cmd = [
                sys.executable,
                "scripts/deep_backtest.py",
                strategy,
                "--leverage-mode", mode.value,
            ] + base_cmd_tail

            log_buffer.append(f"\n{'=' * 70}\n$ {' '.join(cmd)}\n{'=' * 70}\n")
            results_log.code("".join(log_buffer), language="bash")

            proc = subprocess.Popen(
                cmd,
                cwd=str(Path(__file__).resolve().parents[2]),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                log_buffer.append(line)
                # Cap the log display to the last ~200 lines to keep Streamlit responsive
                display = "".join(log_buffer[-200:])
                results_log.code(display, language="bash")
            proc.wait()

            # scripts/deep_backtest.py uses exit codes as verdict signals:
            #   0 = DEPLOYABLE
            #   1 = NEEDS_WF / RESEARCH_ONLY  ← legitimate research outcomes
            #   2 = FAILED (verdict) — pipeline ran, strategy failed gates
            #   anything else = real crash (import error, bad config, etc.)
            #
            # All of {0, 1, 2} mean the pipeline completed and a report was
            # written. The auto-capture hook already landed a row in the
            # strategies table. Only exit codes outside {0, 1, 2} are true
            # crashes we should surface as errors.
            VERDICT_CODE_MAP = {
                0: ("success", "✓", "DEPLOYABLE"),
                1: ("warning", "⚠", "NEEDS_WF / RESEARCH_ONLY"),
                2: ("warning", "⚠", "FAILED (verdict)"),
            }
            if proc.returncode in VERDICT_CODE_MAP:
                level, glyph, verdict_label = VERDICT_CODE_MAP[proc.returncode]
                msg = (
                    f"{glyph} Mode `{mode.value}` → **{verdict_label}** "
                    f"(mode {mode_idx}/{total_modes}, exit {proc.returncode})"
                )
                if level == "success":
                    status_placeholder.success(msg)
                else:
                    status_placeholder.warning(msg)
                # Continue to next mode — non-DEPLOYABLE is expected research output
            else:
                status_placeholder.error(
                    f"✗ Mode `{mode.value}` CRASHED with exit code {proc.returncode} "
                    f"— this is a real pipeline error, not a verdict. "
                    f"Check the log output above for the traceback."
                )
                break

        # Final summary — accept {0, 1, 2} as "all modes ran"
        if proc.returncode in (0, 1, 2):
            st.success(
                f"All {total_modes} mode(s) ran to completion. "
                f"Switch to the **Strategies** page to see the new version row(s) — "
                f"auto-capture landed them in `strategy_versions` automatically. "
                f"Verdicts: exit 0 = DEPLOYABLE, 1 = NEEDS_WF/RESEARCH_ONLY, 2 = FAILED."
            )
            if proc.returncode == 0:
                st.balloons()  # celebration only for DEPLOYABLE
