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
    7. Run Deep Backtest — broker-grouped fees + history + tooltips (task #141).
    8. Order Flow — CVD, divergences and absorption from cached flow bars
       (crypto only; see src/data/cvd.py for why a CFD feed cannot supply them).
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
# Strategies page helpers (task #133 — hierarchical facets tree)
# ---------------------------------------------------------------------------


def _facet_better_than(new: dict, existing: dict | None) -> bool:
    """Compare two facets under keep-best semantics (local mirror of
    `storage._is_better_version_metric`).

    Kept inline so the Strategies page doesn't import the storage internals
    at import time. Same tiebreak order: sane-first → WF Calmar → return_pct.
    """
    if existing is None:
        return True
    new_sane = bool(new.get("sane"))
    old_sane = bool(existing.get("sane"))
    if new_sane != old_sane:
        return new_sane
    new_wf = new.get("wf_calmar")
    old_wf = existing.get("wf_calmar")
    if new_wf is not None and old_wf is not None:
        return float(new_wf) > float(old_wf)
    if new_wf is not None and old_wf is None:
        return True
    if new_wf is None and old_wf is not None:
        return False
    new_ret = new.get("return_pct")
    old_ret = existing.get("return_pct")
    if new_ret is None:
        return False
    if old_ret is None:
        return True
    return float(new_ret) > float(old_ret)


def _format_hero_summary(hero, strategies_by_id) -> str:
    """Inline formatter for the Layer 2 window dropdown labels."""
    if not hero:
        return "(no data)"
    v, facet = hero
    parent = strategies_by_id.get(v.strategy_id)
    name = parent.name if parent else "?"
    ret = facet.get("return_pct")
    return f"{name} {ret:+.1f}%" if ret is not None else name


def _wrap_html_for_light_theme(html_content: str) -> str:
    """Inject a CSS override so the embedded deep_backtest HTML report
    renders with a light background even when Streamlit uses dark theme.

    The deep_backtest reports hard-code `color: #222` on body but leave
    the background unset, so they inherit from the parent iframe color-
    scheme — which under Streamlit dark theme is near-black → dark text
    on dark background → illegible.

    We prepend a hard CSS override that forces white bg + dark text. The
    override uses `!important` on `html, body` + all descendants to beat
    any inherited color-scheme propagation. Applied at read time, so the
    actual file on disk is never modified.

    Task #151.2.
    """
    override = (
        '<style>'
        'html, body { background-color: #ffffff !important; color: #222222 !important; color-scheme: light !important; }'
        'body * { color: inherit; }'
        'table { background-color: #ffffff !important; }'
        'th { background-color: #f5f5f5 !important; color: #222 !important; }'
        'td, th { border-color: #ddd !important; color: #222 !important; }'
        'tr:nth-child(even) { background-color: #fafafa !important; }'
        'h1, h2, h3, h4, h5, h6 { color: #222 !important; }'
        'a { color: #1f6feb !important; }'
        '</style>'
    )
    # Try to inject inside <head> if present, else prepend to the full doc.
    lower = html_content.lower()
    head_close = lower.find("</head>")
    if head_close >= 0:
        return html_content[:head_close] + override + html_content[head_close:]
    return override + html_content


def _compare_runs_table(run_rows: list[dict]) -> pd.DataFrame:
    """Build a master comparison DataFrame from N recent runs' matrix.csv
    files. Each run's cells get a `run_idx` column so the user can see
    which run a cell came from, plus strategy name + leverage_mode.

    Task #151.3. Used by the History section's Combined Comparison view.
    """
    from src.strategies.storage import _to_absolute as _to_abs
    frames: list[pd.DataFrame] = []
    for i, r in enumerate(run_rows):
        report_dir = r.get("run_report_dir") or r.get("report_dir")
        if not report_dir:
            continue
        abs_dir = _to_abs(report_dir)
        if not abs_dir or not abs_dir.exists():
            continue
        csv_path = abs_dir / "matrix.csv"
        if not csv_path.exists():
            continue
        try:
            df = pd.read_csv(csv_path)
        except Exception:
            continue
        df = df.copy()
        df["run_idx"] = i
        df["strategy"] = r.get("strategy_name") or "?"
        df["leverage_mode"] = r.get("leverage_mode") or "—"
        df["when"] = (r.get("run_timestamp") or "")[:19]
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# Phase parser + progress tracker for the Run Deep Backtest page (task #151.5)
#
# The deep_backtest subprocess emits phase markers on stdout like:
#   Phase 0 — Preflight
#   Phase 1 — Matrix (N cells: ...)
#   Phase 2 — Sanity checks
#   Phase 2.5 — RISK_SCALED validation
#   Phase 3 — Leverage validation deep-dive
#   Phase 4 — Walk-forward OOS
#   VERDICT: DEPLOYABLE
#
# We parse these into a PhaseState dataclass and drive a progress bar +
# vertical tick list + friendly status text in the UI.

DEEP_BACKTEST_PHASES: list[tuple[str, str, str]] = [
    # (phase_key, friendly_label, match_substring_in_stdout)
    ("preflight",       "🔍 Phase 0 — Preflight",              "Phase 0"),
    ("matrix",          "🧮 Phase 1 — Matrix (run every cell)", "Phase 1"),
    ("sanity",          "🧠 Phase 2 — Sanity checks",           "Phase 2 —"),
    ("leverage_mode",   "🛡 Phase 2.5 — Leverage-mode validation", "Phase 2.5"),
    ("leverage_lin",    "📐 Phase 3 — Leverage linearity check", "Phase 3"),
    ("walk_forward",    "🎯 Phase 4 — Walk-forward OOS",        "Phase 4"),
    ("verdict",         "⚖️ Phase 5 — Verdict",                 "VERDICT:"),
]


def _phase_status(phase_key: str, current: dict, completed: set) -> str:
    """Return a unicode glyph + label for a phase based on current state.

    ⏳ = pending (hasn't started), 🔄 = in-progress (currently running),
    ✅ = complete, ⊘ = skipped (e.g., phase 4 when wf_enabled=False).
    """
    if phase_key in completed:
        return "✅"
    if current.get("phase_key") == phase_key:
        return "🔄"
    return "⏳"


def _parse_phase_line(line: str) -> str | None:
    """Inspect one stdout line and return the phase_key that it activates,
    or None if the line isn't a phase marker."""
    for phase_key, _label, marker in DEEP_BACKTEST_PHASES:
        if marker in line:
            return phase_key
    return None


def _friendly_status_for_line(line: str, current_phase: str | None) -> str | None:
    """Turn a raw log line into a human-readable status message for the
    friendly-text widget. Returns None if the line isn't interesting.

    Goal: translate the firehose of structlog JSON + print statements
    into short sentences like 'Running window 3mo × TF 1h × leverage 10 …'
    so the user can follow along without reading raw logs.
    """
    s = line.strip()
    if not s:
        return None
    if "Phase 0" in s:
        return "Running preflight — resolving strategy, checking data availability."
    if "Phase 1" in s:
        return "Running the backtest matrix — one full backtest per (window × TF × leverage × fee) combo."
    if "Phase 2 —" in s:
        return "Running sanity checks — trade count, drawdown, cost-as-pct-of-margin, leverage invariance."
    if "Phase 2.5" in s:
        return "Running zero-tolerance leverage-mode validation — hand-trace hand-grades between leverages."
    if "Phase 3" in s:
        return "Running leverage-linearity deep-dive — comparing P&L scaling across leverage levels."
    if "Phase 4" in s:
        return "Running walk-forward OOS — rolling train/test folds + continuous OOS run."
    if "VERDICT:" in s:
        return "Pipeline complete — evaluating final verdict."
    if '"event":"leveraged_backtest_complete"' in s:
        return "Cell complete — moving to the next one."
    if "write_report" in s or "index.html" in s:
        return "Writing HTML / PDF / heatmap report."
    if "attribution" in s.lower() and "computation failed" not in s.lower():
        return "Computing per-cell attribution breakdown (alpha vs leverage amplification vs cost drag)."
    return None


# ---------------------------------------------------------------------------
# Sidebar Navigation
# ---------------------------------------------------------------------------

st.sidebar.title("Algo Trading")

# Active-broker banner — shown on every page (Phase F of fee-manager build).
# One line: [active broker] | XAUUSD profile: [profile] | scenario: [scenario].
# Scenario is auto-detected from the current UTC time so the banner reflects
# the cost regime the engine would use IF a trade fired right now.
try:
    import time as _time
    from src.fees import FeeManager, get_active_broker
    _ab = get_active_broker()
    _now_ms = int(_time.time() * 1000)
    _info = FeeManager.explain(symbol="XAUUSD", timestamp_ms=_now_ms)
    _sev_emoji = {
        "normal": "🟢",
        "news_active": "📰",
        "illiquid": "🌙",
        "volatile": "⚡",
    }.get(_info["scenario"], "🟢")
    st.sidebar.markdown(
        f"**🏦 Active broker:** `{_ab.id}`\n\n"
        f"**{_sev_emoji} Scenario (XAUUSD):** `{_info['scenario']}`\n\n"
        f"**📋 Profile:** `{_info['profile_name']}`\n\n"
        f"_Switch: edit `config/active_broker.toml` or run_ "
        f"`python3 scripts/set_active_broker.py <broker_id>`"
    )
except Exception as _e:
    st.sidebar.info(f"Fee system not loaded: {_e!s}")

st.sidebar.divider()

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
        "Order Flow",
        "Glossary",
    ],
    index=0,
)


# ---------------------------------------------------------------------------
# Instrument-class mapping (task #141)
# ---------------------------------------------------------------------------
# Used by the Run Deep Backtest fee selector to filter the broker/platform
# tree to profiles that match the selected strategy's primary market.
# Adding a new market is a one-line change — the fee tree, the filter, and
# the dashboard UI all read from this dict.
INSTRUMENT_CLASS_BY_MARKET: dict[str, str] = {
    "XAUUSD": "xauusd_metals",
    "EURUSD": "fx_majors",
    "GBPUSD": "fx_majors",
    "USDJPY": "fx_majors",
    # Future: "BTCUSDT": "crypto_perp", etc.
}


# ---------------------------------------------------------------------------
# Cached recent-runs loader (task #141.2)
# ---------------------------------------------------------------------------
# Defined at module level so both the History section and the Run section
# can reach it (Run needs to invalidate the cache via .clear() after a
# subprocess completes so the just-finished run shows up immediately).
# TTL 30s is a belt-and-braces fallback in case .clear() is skipped.
@st.cache_data(ttl=30)
def _load_recent_runs_cached(db_path: str, limit: int, cutoff_days: int):
    from src.strategies.storage import list_recent_runs
    return list_recent_runs(
        limit=limit, cutoff_days=cutoff_days, db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Cached order-flow series discovery
# ---------------------------------------------------------------------------
# Flow bars are stored by ParquetStore under the pseudo-timeframe "<tf>_cvd",
# so "BTCUSDT_5m_cvd" means symbol BTCUSDT at 5m. TTL is short so a `scripts.cvd
# fetch` run shows up without restarting the dashboard.
@st.cache_data(ttl=20)
def _list_flow_series() -> list[tuple[str, str]]:
    """(symbol, timeframe) pairs that have cached flow bars."""
    from src.data.agg_trades import list_flow_series

    return list_flow_series()


@st.cache_data(ttl=20)
def _load_flow_bars(symbol: str, timeframe: str):
    from src.data.agg_trades import flow_series_key
    from src.data.storage import ParquetStore

    df = ParquetStore().load(symbol, flow_series_key(timeframe))
    return df.sort_values("timestamp").reset_index(drop=True)


@st.cache_data(ttl=20)
def _list_depth_symbols() -> list[str]:
    from src.data.depth_recorder import DepthStore

    return DepthStore().list_symbols()


@st.cache_data(ttl=20)
def _load_depth(symbol: str):
    from src.data.depth_recorder import DepthStore

    return DepthStore().load(symbol)


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
                width="stretch",
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
                    st.plotly_chart(fig, width="stretch")

                    # Monthly heatmap
                    try:
                        eq = equity.copy()
                        eq.index = pd.to_datetime(eq.index, unit="ms")
                        daily = eq.resample("D").last().dropna()
                        if len(daily) > 60:
                            fig2 = monthly_heatmap(equity)
                            st.plotly_chart(fig2, width="stretch")
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
                    st.dataframe(df[existing_cols], width="stretch", hide_index=True)

                    # Radar chart
                    from src.backtest.charts import strategy_comparison_radar
                    strategies = {}
                    for item in comparison:
                        strategies[item["Run"][:30]] = item
                    fig = strategy_comparison_radar(strategies)
                    st.plotly_chart(fig, width="stretch")

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
                st.dataframe(pd.DataFrame(data), width="stretch", hide_index=True)

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

    st.dataframe(protocols, width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# Page 6: Strategies — hierarchical registry (task #133 Option 1B)
# ---------------------------------------------------------------------------
#
# Four-layer hierarchy:
#   Layer 1 (filter)  — fee profile dropdown (only fees with data)
#   Layer 2 (🏆 hero) — window time-frame hero across ALL strategies
#   Layer 3 (⭐ hero) — per (strategy × leverage_mode), hero across leverages×tfs
#   Layer 4 (leaf)    — individual (leverage, candle_tf) rows with vanity flag
#
# All hierarchy computation is done in memory from pre-parsed facets_json so
# page load is O(N_versions) with no N+1 queries. The facets are keep-best
# maintained at write-time by `record_deep_backtest_result` so the Dashboard
# is a pure reader — never triggers recomputation.
# ---------------------------------------------------------------------------

elif page == "Strategies":
    from src.strategies.storage import (
        list_strategies,
        list_versions,
        list_version_runs,
        query_vanity_traps,
        _to_absolute,
    )

    st.title("Strategies — hierarchical registry")
    st.caption(
        "Four-layer view: **fee → window → strategy+mode → leverage×tf**. "
        "Heroes (🏆 / ⭐) auto-selected by keep-best (sane → WF Calmar → max return). "
        "Pages are read-only; facets are maintained at write-time by `record_deep_backtest_result`."
    )

    # ── Load all data ONCE ─────────────────────────────────────────────────
    # O(1) query + O(N_versions) in-memory parse. Adding new strategies,
    # modes, leverages, windows, or fees does not require any re-indexing —
    # they simply show up as new facet keys and new slug rows.
    strategies = list_strategies(db_path=_DB_PATH)
    all_versions = list_versions(db_path=_DB_PATH)  # one query, no strategy filter

    if not all_versions:
        st.info(
            "No strategies in storage yet. Run a deep_backtest to auto-populate, "
            "or use `python3 scripts/strategies.py register <name>` to stub one. "
            "Run `python3 scripts/strategies_backfill.py` to import existing reports."
        )
        st.stop()

    # Index strategies by id for fast lookup (parent metadata + display name)
    strategies_by_id = {s.id: s for s in strategies}

    # ── Vanity-trap audit (front-and-center)
    vanity = query_vanity_traps(db_path=_DB_PATH)
    if vanity:
        st.warning(
            f"⚠️ {len(vanity)} vanity trap(s) — DEPLOYABLE verdicts with "
            f"sane=False max-return cells. Audit before deploying live."
        )
        with st.expander(f"View {len(vanity)} vanity trap(s)"):
            vanity_rows = [
                {
                    "slug": v.version_slug,
                    "strategy": strategies_by_id.get(v.strategy_id).name
                        if strategies_by_id.get(v.strategy_id) else "?",
                    "max_return_pct": v.max_return_pct,
                    "warning": v.max_return_warning,
                    "verdict": v.verdict,
                }
                for v in vanity
            ]
            st.dataframe(pd.DataFrame(vanity_rows), width="stretch", hide_index=True)

    # ── Facets universe: which fees/windows have ANY data? ─────────────────
    # Walk every version's facets dict once. Build:
    #   fee_universe:     set of all fee_profile values present anywhere
    #   window_universe:  {fee_profile: [window_labels sorted by window_days]}
    #   facet_lookup:     {(fee, window, version_id): facet payload}
    fee_universe: set[str] = set()
    window_universe: dict[str, dict[str, int]] = {}  # fee → {window_label: window_days}
    # facet_lookup: index for fast Layer 2-4 rendering
    facet_lookup: dict[tuple[str, str], list[tuple[object, dict]]] = {}

    for v in all_versions:
        for key, facet in (v.facets or {}).items():
            # key is "{window_label}::{fee_profile}"
            if "::" not in key:
                continue
            window_label, fee_profile = key.split("::", 1)
            fee_universe.add(fee_profile)
            window_universe.setdefault(fee_profile, {})[window_label] = int(facet.get("window_days", 0))
            facet_lookup.setdefault((fee_profile, window_label), []).append((v, facet))

    if not fee_universe:
        st.info(
            "Strategy versions exist but none have facets_json yet. "
            "Run `python3 scripts/strategies_migrate_v6.py --apply` to backfill, "
            "or re-run a deep_backtest on any version to populate facets."
        )
        st.stop()

    # ── LAYER 1 — Fee profile filter ───────────────────────────────────────
    # Union of (a) fees that actually have backtest data AND (b) fees in the
    # registry for XAUUSD. Registry-only fees get an "(no data)" suffix so
    # the user can see ALL broker options (MT4 + cTrader + stress + news +
    # pine) even if they haven't run a backtest against them yet. Task #151.1.
    #
    # Design: use the formatted labels as the `options=` list directly and
    # reverse-lookup to the raw fee key. Streamlit's AppTest has a quirk
    # where `format_func` gets double-applied during widget state readback,
    # which breaks when the formatted label isn't a valid key. Bypassing
    # format_func sidesteps the quirk entirely.
    from src.backtest.fee_profiles import list_profiles as _list_profiles
    st.markdown("### 🏷️  Layer 1 — Broker fee profile")
    registry_fees: list[str] = list(_list_profiles(instrument_class="xauusd_metals"))
    # Pine zero-cost has instrument_class=(none) so add it manually
    if "pine_zero_cost" in _list_profiles():
        registry_fees.append("pine_zero_cost")
    registry_fees_set = set(registry_fees)

    all_fees_ordered: list[str] = sorted(fee_universe | registry_fees_set)
    # Build label → raw-key reverse map
    label_to_fee: dict[str, str] = {}
    fee_option_labels: list[str] = []
    for fee in all_fees_ordered:
        has_data = fee in fee_universe
        label = fee if has_data else f"{fee}  (no data)"
        label_to_fee[label] = fee
        fee_option_labels.append(label)

    picked_fee_label = st.selectbox(
        "Fee profile",
        fee_option_labels,
        help=(
            f"{len(fee_universe)} of {len(all_fees_ordered)} fee profiles "
            f"have backtest data in storage. Registry-only fees are marked "
            f"with a `(no data)` suffix — pick them to see an empty state "
            f"prompting you to run a deep backtest against that fee."
        ),
    )
    picked_fee = label_to_fee[picked_fee_label]

    # ── LAYER 2 — Window time-frame with 🏆 hero ───────────────────────────
    windows_for_fee = window_universe.get(picked_fee, {})
    if not windows_for_fee:
        st.info(
            f"📭 **No backtest data yet for `{picked_fee}`.** "
            f"Run a deep backtest against this fee profile from the "
            f"**Run Deep Backtest** page to populate the hierarchy."
        )
        st.stop()

    # Sort windows by window_days so "1mo, 3mo, 6mo, 1y" shows in time order
    windows_sorted = sorted(windows_for_fee.items(), key=lambda x: x[1])
    window_labels = [w for w, _ in windows_sorted]

    # Compute the Layer-2 hero for EACH window: best cell across ALL strategies
    # at this fee × window. This is the 🏆 "best model for this fee+window".
    window_heroes: dict[str, tuple[object, dict] | None] = {}
    for w_label in window_labels:
        bucket = facet_lookup.get((picked_fee, w_label), [])
        hero: tuple[object, dict] | None = None
        for v, facet in bucket:
            if hero is None:
                hero = (v, facet)
                continue
            # Sane-first → WF Calmar → return_pct
            _, hero_facet = hero
            if _facet_better_than(facet, hero_facet):
                hero = (v, facet)
        window_heroes[w_label] = hero

    st.markdown("### ⏱  Layer 2 — Window time-frame (🏆 = best across all strategies)")
    picked_window = st.selectbox(
        "Window",
        window_labels,
        format_func=lambda w: (
            f"{w}  🏆 {_format_hero_summary(window_heroes.get(w), strategies_by_id)}"
        ),
    )
    hero_layer2 = window_heroes.get(picked_window)
    if hero_layer2:
        v_hero, f_hero = hero_layer2
        parent_hero = strategies_by_id.get(v_hero.strategy_id)
        st.success(
            f"🏆 **Layer 2 hero** for {picked_fee} × {picked_window}: "
            f"**{parent_hero.name if parent_hero else '?'}** · "
            f"{v_hero.description or v_hero.version_slug} → "
            f"return **{f_hero.get('return_pct'):+.2f}%**, "
            f"DD {f_hero.get('maxdd_pct'):.1f}%, Calmar {f_hero.get('calmar'):.2f}, "
            f"{f_hero.get('trades')} trades"
        )

    # ── LAYER 3 — Strategy + leverage_mode with ⭐ hero ─────────────────────
    st.markdown("### 📐  Layer 3 — Strategy × leverage mode (⭐ = hero per group)")
    st.caption(
        "One row per (strategy, leverage_mode). ⭐ marks the best leverage×tf "
        "within that group for the selected fee × window."
    )

    # Group all facets for (picked_fee, picked_window) by (strategy_id, leverage_mode)
    layer3_groups: dict[tuple[int, str], list[tuple[object, dict]]] = {}
    for v, facet in facet_lookup.get((picked_fee, picked_window), []):
        mode = v.leverage_mode or "—"
        layer3_groups.setdefault((v.strategy_id, mode), []).append((v, facet))

    if not layer3_groups:
        st.info(f"No versions for {picked_fee} × {picked_window}.")
    else:
        # Build rollup rows — one per Layer 3 group, with ⭐ hero metrics
        layer3_rows = []
        hero_lookup: dict[tuple[int, str], tuple[object, dict]] = {}
        for (sid, mode), bucket in layer3_groups.items():
            hero: tuple[object, dict] | None = None
            for v, facet in bucket:
                if hero is None or _facet_better_than(facet, hero[1]):
                    hero = (v, facet)
            if hero is None:
                continue
            hero_lookup[(sid, mode)] = hero
            v_star, f_star = hero
            parent = strategies_by_id.get(sid)
            layer3_rows.append({
                "strategy": parent.name if parent else "?",
                "leverage_mode": mode,
                "⭐ hero": f"{v_star.description or v_star.version_slug}",
                "return_pct": f_star.get("return_pct"),
                "max_dd_pct": f_star.get("maxdd_pct"),
                "calmar": f_star.get("calmar"),
                "wf_calmar": f_star.get("wf_calmar"),
                "trades": f_star.get("trades"),
                "sane": "✓" if f_star.get("sane") else "⚠",
                "verdict": f_star.get("verdict") or v_star.verdict or "—",
            })
        # Sort rollup by sanity-first (sane=✓ beats ⚠), then return desc
        layer3_rows.sort(
            key=lambda r: (r["sane"] != "✓", -(r["return_pct"] or -1e18))
        )
        st.dataframe(pd.DataFrame(layer3_rows), width="stretch", hide_index=True)

        # ── LAYER 4 — Expand a (strategy × mode) group to see leverage×tf leaves
        st.markdown("### 🔬  Layer 4 — Leverage × candle timeframe leaves")
        st.caption(
            "Pick a (strategy × mode) group to inspect every leaf. "
            "⚠ flags vanity traps (thin trades, huge DD, or low Calmar)."
        )

        group_options = [
            f"{strategies_by_id.get(sid).name if strategies_by_id.get(sid) else '?'} · {mode}"
            for (sid, mode) in layer3_groups.keys()
        ]
        picked_group_label = st.selectbox("Pick a (strategy × mode) group", group_options)
        picked_idx = group_options.index(picked_group_label)
        picked_key = list(layer3_groups.keys())[picked_idx]
        leaves = layer3_groups[picked_key]

        # Render each leaf as a row keyed by (leverage, timeframe).
        # IMPORTANT (task #146 bug #4 fix): We must keep the raw (v, facet)
        # tuples aligned with the rendered rows AFTER sorting. The previous
        # code sorted `leaf_rows` in-place but kept a separate `leaves` list
        # unsorted, then looked up by the sorted index into the UNSORTED
        # list — so the user picked leaf N but saw leaf Y's data. Build a
        # combined list of (row_dict, v, facet) tuples and sort them as
        # one unit so the picker index is always correct.
        leaf_tuples: list[tuple[dict, Any, dict]] = []
        for v, facet in leaves:
            row = {
                "slug": v.version_slug,
                "leverage": facet.get("leverage"),
                "timeframe": facet.get("timeframe"),
                "return_pct": facet.get("return_pct"),
                "max_dd_pct": facet.get("maxdd_pct"),
                "calmar": facet.get("calmar"),
                "wf_calmar": facet.get("wf_calmar"),
                "trades": facet.get("trades"),
                "sane": "✓" if facet.get("sane") else "⚠",
                "warning": facet.get("warning") or "",
                "⭐": "⭐" if hero_lookup.get(picked_key) and hero_lookup[picked_key][0].id == v.id else "",
            }
            leaf_tuples.append((row, v, facet))
        # Sort the tuples — all three sort keys stay aligned.
        leaf_tuples.sort(
            key=lambda t: (t[0]["sane"] != "✓", -(t[0]["return_pct"] or -1e18))
        )
        leaf_rows = [t[0] for t in leaf_tuples]
        st.dataframe(pd.DataFrame(leaf_rows), width="stretch", hide_index=True)

        # ── Embedded HTML report viewer for the picked leaf ────────────────
        st.markdown("### View deep_backtest HTML report")
        leaf_picker_options = [
            f"{r['slug']}  ({r['⭐']}return {r['return_pct']:+.2f}%)"
            if r["return_pct"] is not None else r["slug"]
            for r in leaf_rows
        ]
        picked_leaf_label = st.selectbox("Pick a leaf to view its HTML report", leaf_picker_options)
        picked_leaf_idx = leaf_picker_options.index(picked_leaf_label)
        # Use the aligned tuple list so the index maps correctly.
        _, v_pick, facet_pick = leaf_tuples[picked_leaf_idx]
        st.caption(f"slug: `{v_pick.version_slug}`  ·  mode: `{v_pick.leverage_mode or '—'}`  ·  baseline L: `{v_pick.baseline_leverage}`")
        if facet_pick.get("warning"):
            st.warning(f"⚠ Vanity flag for this leaf: **{facet_pick['warning']}**")
        if facet_pick.get("verdict_reason") or v_pick.verdict_reason:
            st.caption(f"Verdict reason: {facet_pick.get('verdict_reason') or v_pick.verdict_reason}")

        # Prefer the facet's report_html_path (travels with the winning cell)
        # over the version row's top-level path. When keep-best rolls a cell
        # forward from a prior run, the facet has the right report to show.
        html_path = facet_pick.get("report_html_path") or v_pick.report_html_path
        if html_path:
            abs_html = _to_absolute(html_path)
            if abs_html and abs_html.exists():
                try:
                    raw_html = abs_html.read_text()
                    # Task #151.2 — force light theme so dark-mode users can
                    # actually READ the report (reports hard-code color:#222
                    # with no bg, so they're illegible in dark theme).
                    wrapped = _wrap_html_for_light_theme(raw_html)
                    st.components.v1.html(wrapped, height=900, scrolling=True)
                    # Task #151.4 — download button instead of file:// link
                    # because browsers block file:// from web origins.
                    st.download_button(
                        label="⬇️ Download HTML report",
                        data=raw_html,
                        file_name=abs_html.name,
                        mime="text/html",
                        help="Saves the full HTML report to your machine so "
                             "you can open it in a browser tab outside Streamlit.",
                    )
                except Exception as e:
                    st.error(f"Failed to render HTML: {e}")
            else:
                st.error(f"HTML file not found at {abs_html}")
        else:
            st.info("No HTML report path on this facet (run deep_backtest with --no-html?)")

        # ── Run history for this version
        history = list_version_runs(
            version_id=v_pick.id,
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
                    width="stretch",
                    hide_index=True,
                )




# ---------------------------------------------------------------------------
# Page 8: Glossary — technical term reference (task #141.3)
# ---------------------------------------------------------------------------
# Renders from LEVERAGE_MODE_HELP + TECHNICAL_TERMS dicts at runtime so
# definitions never drift from the `help=` kwargs sprinkled across page 7.
# Single source of truth: `src/backtest/deep_backtest_interactive.py`.

# ---------------------------------------------------------------------------
# Page 8: Order Flow — CVD, divergences, absorption (2026-09-21)
# ---------------------------------------------------------------------------
# Reads flow bars produced by `python -m scripts.cvd fetch`. Crypto only: a CFD
# feed carries no trade size or aggressor side, so there is nothing to compute
# on the gold book (see src/data/cvd.py and ARCHITECTURE Known Gotchas).

elif page == "Order Flow":
    st.title("Order Flow — CVD")

    series = _list_flow_series()
    if not series:
        st.info(
            "No cached flow bars yet.\n\n"
            "Fetch some first (free Binance data, no API key):\n\n"
            "```\npython3 -m scripts.cvd fetch BTCUSDT --tf 5m --start 2026-09-20\n```\n\n"
            "Start with a single day — one day of BTCUSDT aggTrades is already a "
            "sizable download."
        )
    else:
        symbols = sorted({sym for sym, _ in series})
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        symbol = c1.selectbox("Symbol", symbols)
        timeframes = [tf for sym, tf in series if sym == symbol]
        timeframe = c2.selectbox("Timeframe", timeframes)
        lookback = c3.number_input(
            "Divergence lookback", min_value=2, max_value=200, value=20, step=1,
            help="Bars over which the price change and CVD change are compared.",
        )
        z_threshold = c4.number_input(
            "Min |z|", min_value=0.1, max_value=5.0, value=1.0, step=0.1,
            help="Both the price leg and the CVD leg must exceed this to count.",
        )

        bars = _load_flow_bars(symbol, timeframe)
        if bars.empty:
            st.warning(f"{symbol} {timeframe} has no rows.")
        else:
            window = st.slider(
                "Bars shown (most recent)",
                min_value=50,
                max_value=max(50, len(bars)),
                value=min(400, len(bars)),
                step=25,
            )
            view = bars.tail(window).reset_index(drop=True)

            from src.dashboard.flow_charts import price_cvd_panels
            from src.data.cvd import delta_ratio, detect_absorption, detect_divergences

            divergences = detect_divergences(
                view, lookback=int(lookback), z_threshold=float(z_threshold)
            )
            absorption = detect_absorption(view, range_window=int(lookback))

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Bars", f"{len(view):,}")
            m2.metric("CVD (end)", f"{view['cvd'].iloc[-1]:,.1f}")
            m3.metric("Divergences", f"{len(divergences)}")
            m4.metric("Absorption bars", f"{len(absorption)}")

            fig = price_cvd_panels(
                view, divergences, absorption,
                title=f"{symbol} {timeframe} — order flow",
            )
            st.plotly_chart(fig, width="stretch")

            st.caption(
                "Price and CVD sit in separate panels on a shared time axis rather "
                "than on two y-scales — a dual axis lets the scaling decide whether "
                "the two series appear to agree. Compare the **shapes**."
            )

            # Table view — required companion to the chart, and the full list
            # when the chart caps its shaded bands.
            tab_div, tab_abs, tab_bars = st.tabs(
                ["Divergences", "Absorption", "Bars"]
            )
            with tab_div:
                if divergences.empty:
                    st.write("None at these settings.")
                else:
                    st.dataframe(
                        divergences.assign(
                            time=pd.to_datetime(
                                divergences["timestamp"], unit="ms", utc=True
                            )
                        )[["time", "kind", "price_z", "cvd_z", "strength", "close"]],
                        width="stretch",
                        hide_index=True,
                    )
            with tab_abs:
                if absorption.empty:
                    st.write("None at these settings.")
                else:
                    st.dataframe(
                        absorption.assign(
                            time=pd.to_datetime(
                                absorption["timestamp"], unit="ms", utc=True
                            )
                        )[["time", "kind", "delta_ratio", "range_ratio", "close"]],
                        width="stretch",
                        hide_index=True,
                    )
            with tab_bars:
                st.dataframe(
                    view.assign(
                        time=pd.to_datetime(view["timestamp"], unit="ms", utc=True),
                        delta_ratio=delta_ratio(view),
                    )[[
                        "time", "close", "buy_volume", "sell_volume",
                        "delta", "delta_ratio", "cvd", "trade_count",
                    ]].tail(200),
                    width="stretch",
                    hide_index=True,
                )

            st.warning(
                "These are **screens, not signals**. No edge has been researched or "
                "validated for either pattern — nothing here is wired to a strategy, "
                "and divergence against a strong trend is a classic losing fade. "
                "See `STRATEGY_DEVELOPMENT_PROCESS.md` Stage 1."
            )

        # ── Depth heatmap ──────────────────────────────────────────────
        # Resting liquidity over time — the passive side of the book, which
        # CVD cannot see. Recorded separately by `scripts.depth record`.
        st.divider()
        st.subheader("Depth heatmap")

        depth_symbols = _list_depth_symbols()
        if not depth_symbols:
            st.info(
                "No depth recorded yet.\n\n"
                "```\npython3 -m scripts.depth record BTCUSDT --duration 300\n```\n\n"
                "Depth is the passive side of the book — the walls CVD cannot see. "
                "It has to be recorded live; unlike trades, Binance publishes no "
                "historical order-book archive."
            )
        else:
            d1, d2, d3 = st.columns([2, 1, 1])
            depth_symbol = d1.selectbox(
                "Depth symbol", depth_symbols,
                index=depth_symbols.index(symbol) if symbol in depth_symbols else 0,
            )
            price_bins = d2.number_input(
                "Max price rows", min_value=20, max_value=600, value=160, step=20,
                help="Ceiling on price rows. Buckets align to the inferred tick "
                     "size when that fits within this ceiling.",
            )
            clip = d3.number_input(
                "Colour clip %ile", min_value=50.0, max_value=100.0,
                value=99.0, step=0.5,
                help="Book sizes are heavily skewed — one large wall mapped "
                     "linearly washes out everything else.",
            )

            depth_rows = _load_depth(depth_symbol)
            if depth_rows.empty:
                st.warning(f"{depth_symbol} has no depth rows.")
            else:
                from src.dashboard.flow_charts import depth_heatmap
                from src.data.depth_recorder import to_heatmap_grid

                grid, note = to_heatmap_grid(
                    depth_rows, price_bins=int(price_bins), time_bins=900
                )
                h1, h2, h3 = st.columns(3)
                h1.metric("Samples", f"{depth_rows['timestamp'].nunique():,}")
                h2.metric("Rows", f"{len(depth_rows):,}")
                h3.metric("Price rows", f"{grid.shape[0]:,}")

                st.plotly_chart(
                    depth_heatmap(
                        grid,
                        title=f"{depth_symbol} — order book depth",
                        clip_percentile=float(clip),
                        note=note,
                    ),
                    width="stretch",
                )
                st.caption(
                    "Dark horizontal bands are resting size that persisted — walls. "
                    "A wall that holds when price tests it is meaningful; one that "
                    "vanishes before price arrives tells you nothing. The colour "
                    "ramp is a single hue so there are no false thresholds where a "
                    "hue would flip."
                )


elif page == "Glossary":
    from src.backtest.deep_backtest_interactive import (
        LEVERAGE_MODE_HELP,
        TECHNICAL_TERMS,
    )

    st.title("📖 Dashboard Glossary")
    st.caption(
        "Definitions of every technical term used in the Run Deep Backtest "
        "and Strategies pages. Same text powers the hover tooltips — edit "
        "`src/backtest/deep_backtest_interactive.py` to update both at once."
    )

    col_search, _ = st.columns([2, 3])
    with col_search:
        search = st.text_input(
            "🔍 Filter",
            value="",
            placeholder="Type to filter terms (e.g. 'kelly', 'walk', 'vanity')",
        )

    def _matches(term: str, definition: str) -> bool:
        if not search:
            return True
        q = search.lower()
        return q in term.lower() or q in definition.lower()

    st.markdown("---")
    st.markdown("## ⚙️ Leverage modes")
    st.caption(
        "Each mode is a different position-sizing strategy. Pick the mode "
        "that matches how you want leverage to interact with your strategy's "
        "signal. See `docs/LEVERAGE_STRATEGY_DESIGN.md` for the full research."
    )
    for mode_name, definition in LEVERAGE_MODE_HELP.items():
        if not _matches(mode_name, definition):
            continue
        with st.container():
            st.markdown(f"#### `{mode_name}`")
            st.markdown(definition)
            st.markdown("")

    st.markdown("---")
    st.markdown("## 📚 Technical terms")
    st.caption("Alphabetical. Matches the `help=` tooltips on Run Deep Backtest page 7.")
    for term, definition in sorted(TECHNICAL_TERMS.items()):
        if not _matches(term, definition):
            continue
        with st.container():
            st.markdown(f"#### `{term}`")
            st.markdown(definition)
            st.markdown("")

    st.markdown("---")
    st.markdown("## 🔗 Further reading")
    st.markdown(
        """
        - **`docs/LEVERAGE_STRATEGY_DESIGN.md`** — full leverage research + mode assignment recommendations
        - **`docs/BROKER_FEES.md`** — fee profile catalog + cost-asymmetry analysis
        - **`ROADMAP.md`** — phase table + discovered-task history
        - **`ARCHITECTURE.md`** — module registry + data flow diagrams
        """
    )


# ---------------------------------------------------------------------------
# Page 7: Run Deep Backtest — broker-grouped fees + history + tooltips (task #141)
# ---------------------------------------------------------------------------

elif page == "Run Deep Backtest":
    import subprocess

    from src.backtest.deep_backtest_interactive import (
        LEVERAGES_CATALOG,
        LEVERAGE_MODES_CATALOG,
        LEVERAGE_MODE_HELP,
        TECHNICAL_TERMS,
        TIMEFRAMES_CATALOG,
        WINDOWS_CATALOG,
    )
    from src.backtest.deep_backtest import _check_window_availability
    from src.backtest.fee_profiles import group_profiles_by_broker_platform
    from src.strategies.router import STRATEGY_REGISTRY as _CLASS_REGISTRY
    from src.strategies.storage import _to_absolute, list_recent_runs

    st.title("🚀 Run Deep Backtest")
    st.caption(
        "Browser-native alternative to the `questionary` TUI. Pick dimensions, "
        "hit Run, watch output stream live. Result auto-captures into the "
        "**Strategies** page when it finishes. 📖 See the **Glossary** page "
        "for definitions of every technical term."
    )

    # ── Section switcher ──────────────────────────────────────────────
    # Use `st.radio` (not `st.tabs`) so only the selected section's body
    # evaluates on each rerun. Tabs would re-evaluate all three bodies on
    # every 2Hz UI refresh during streaming, hammering the DB.
    section = st.radio(
        "Section",
        ["▶ Run", "📜 History"],
        horizontal=True,
        help="Run = new backtest. History = replay recent runs with embedded reports.",
        key="p7_section",
    )

    # Safety guard: if a subprocess is currently running and the user
    # switches away from Run, warn them and stop rendering the other
    # section until they come back.
    if st.session_state.get("p7_running") and section != "▶ Run":
        st.warning(
            "⚠️ A deep backtest is currently running. Switch back to the "
            "**▶ Run** section to see the live log stream. History is "
            "disabled while a run is in progress."
        )
        st.stop()

    # ─────────────────────────────────────────────────────────────────
    #                        📜 HISTORY SECTION
    # ─────────────────────────────────────────────────────────────────
    if section == "📜 History":
        from datetime import datetime as _dt, timedelta as _td

        st.markdown("### 📜 Recent deep_backtest runs")
        st.caption(
            "Most recent runs across ALL strategies, newest first. "
            "Pick one or more runs below to see a **combined comparison** "
            "of every cell across your selection — leverage modes side by side, "
            "windows × timeframes × leverages × fees in systematic tables. "
            "Cache TTL: 30s — completed runs appear within 30s without a reload."
        )

        col_hist1, col_hist2 = st.columns([3, 1])
        with col_hist2:
            hist_limit = st.number_input(
                "Max rows", value=20, min_value=5, max_value=100, step=5,
                help="Recent N runs to show.",
            )
            hist_cutoff = st.number_input(
                "Cutoff (days)", value=30, min_value=1, max_value=365, step=1,
                help="Hide runs older than N days.",
            )

        try:
            runs = _load_recent_runs_cached(_DB_PATH, int(hist_limit), int(hist_cutoff))
        except Exception as e:
            st.error(f"Failed to load run history: {type(e).__name__}: {e}")
            runs = []

        if not runs:
            st.info(
                "No runs in the selected window. Run a deep_backtest from the "
                "**▶ Run** section to populate the history."
            )
            st.stop()

        # ── History table with checkboxes for multi-select ──────────────
        with col_hist1:
            hist_df = pd.DataFrame([
                {
                    "when": (r["run_timestamp"] or "")[:19],
                    "strategy": r["strategy_name"],
                    "description": r.get("description") or r["version_slug"],
                    "mode": r.get("leverage_mode") or "—",
                    "L": r.get("baseline_leverage"),
                    "tf": r.get("timeframe") or "—",
                    "verdict": r.get("run_verdict") or "—",
                    "return_pct": r.get("run_max_return_pct"),
                    "calmar": r.get("run_best_calmar"),
                    "sane": "✓" if r.get("run_max_return_sane") else
                            ("⚠" if r.get("run_max_return_sane") is False else "—"),
                    "cells": r.get("matrix_n_cells"),
                }
                for r in runs
            ])
            st.dataframe(hist_df, width="stretch", hide_index=True)

        st.markdown("---")

        # ── Session auto-detect: group runs within N minutes of each other
        # AND same strategy. These are runs from a single multi-mode dashboard
        # subprocess spawn (one run per leverage mode).
        SESSION_WINDOW_MIN = 5
        sessions: list[list[int]] = []  # list of run indices per session
        used: set[int] = set()
        for i, r in enumerate(runs):
            if i in used:
                continue
            try:
                ti = _dt.fromisoformat(r["run_timestamp"])
            except Exception:
                sessions.append([i])
                used.add(i)
                continue
            group = [i]
            used.add(i)
            for j in range(i + 1, len(runs)):
                if j in used:
                    continue
                try:
                    tj = _dt.fromisoformat(runs[j]["run_timestamp"])
                except Exception:
                    continue
                if (runs[j]["strategy_name"] == r["strategy_name"]
                        and abs((ti - tj).total_seconds()) <= SESSION_WINDOW_MIN * 60):
                    group.append(j)
                    used.add(j)
            sessions.append(group)

        # Build the session picker — one entry per auto-detected session
        session_labels: list[str] = []
        for group in sessions:
            base = runs[group[0]]
            ts = (base["run_timestamp"] or "")[:19]
            modes = sorted({runs[i].get("leverage_mode") or "—" for i in group})
            mode_str = " / ".join(modes)
            session_labels.append(
                f"{ts} · {base['strategy_name']} · {len(group)} run(s) · modes={mode_str}"
            )

        st.markdown("### 🧪 Pick a session to compare")
        st.caption(
            f"Auto-detected {len(sessions)} session(s). Runs within "
            f"{SESSION_WINDOW_MIN}min of each other on the same strategy are "
            f"grouped as one session (one dashboard 'Run' click → one session)."
        )
        picked_session_label = st.selectbox(
            "Session",
            session_labels,
            key="p7_session_pick",
            help="Each session groups all leverage-mode subprocesses spawned "
                 "from a single dashboard Run click so you can compare them "
                 "side by side.",
        )
        picked_session_idx = session_labels.index(picked_session_label)
        session_run_indices = sessions[picked_session_idx]
        session_runs = [runs[i] for i in session_run_indices]

        st.markdown("#### Runs in this session")
        session_df = pd.DataFrame([
            {
                "when": (r["run_timestamp"] or "")[:19],
                "mode": r.get("leverage_mode") or "—",
                "verdict": r.get("run_verdict") or "—",
                "return_pct": r.get("run_max_return_pct"),
                "calmar": r.get("run_best_calmar"),
                "sane": "✓" if r.get("run_max_return_sane") else
                        ("⚠" if r.get("run_max_return_sane") is False else "—"),
                "cells": r.get("matrix_n_cells"),
            }
            for r in session_runs
        ])
        st.dataframe(session_df, width="stretch", hide_index=True)

        # ── Combined comparison tables — one per leverage mode ──────────
        st.markdown("### 📊 Combined comparison (all runs in session)")
        st.caption(
            "**One table per leverage mode.** Rows = leverage × fee profile. "
            "Columns = window × timeframe. Cells = return / calmar / DD / sanity. "
            "Hero cell per table is the highest-return sane cell."
        )

        combined_df = _compare_runs_table(session_runs)
        if combined_df.empty:
            st.warning(
                "No `matrix.csv` files found for runs in this session. The "
                "report directories may have been deleted, or the runs were "
                "aborted before writing the matrix."
            )
        else:
            # Pre-compute hero cell per mode for ⭐ highlighting
            from src.strategies.storage import _is_better_version_metric

            # Group by mode first
            modes_in_session = sorted(combined_df["leverage_mode"].unique())
            for mode in modes_in_session:
                mode_df = combined_df[combined_df["leverage_mode"] == mode].copy()
                st.markdown(f"#### ⚙️ Leverage mode: `{mode}`")

                # Find hero cell for this mode — max return among sane cells
                hero_idx = None
                best_metric = None
                for idx, row in mode_df.iterrows():
                    sane = (row["trades"] >= 30 and row["maxdd_pct"] < 60
                            and row["calmar"] > 0.2)
                    cand = {"sane": sane, "return_pct": row["return_pct"], "wf_calmar": None}
                    if _is_better_version_metric(cand, best_metric):
                        hero_idx = idx
                        best_metric = cand

                # Build pretty comparison table: rows=(leverage, fee), cols=(window, tf)
                mode_df["hero"] = mode_df.index.map(lambda i: "⭐" if i == hero_idx else "")
                mode_df["sane"] = mode_df.apply(
                    lambda r: "✓" if (
                        r["trades"] >= 30 and r["maxdd_pct"] < 60 and r["calmar"] > 0.2
                    ) else "⚠",
                    axis=1,
                )
                display_cols = [
                    "hero", "window_label", "timeframe", "leverage", "fee_profile",
                    "trades", "return_pct", "maxdd_pct", "calmar", "sharpe", "sane",
                ]
                present_cols = [c for c in display_cols if c in mode_df.columns]
                pretty = mode_df[present_cols].copy()
                # Sort: hero on top, then sane first, then return desc
                pretty["_sort_key"] = pretty.apply(
                    lambda r: (
                        0 if r.get("hero") == "⭐" else 1,
                        0 if r.get("sane") == "✓" else 1,
                        -(r.get("return_pct") or -1e18),
                    ),
                    axis=1,
                )
                pretty = pretty.sort_values("_sort_key").drop(columns=["_sort_key"])
                st.dataframe(pretty, width="stretch", hide_index=True)

                # Plus a pivoted "at-a-glance" view: rows=leverage, cols=(window × tf)
                # showing return_pct only. Lets the user eyeball linearity.
                try:
                    pivot = mode_df.pivot_table(
                        index=["leverage", "fee_profile"],
                        columns=["window_label", "timeframe"],
                        values="return_pct",
                        aggfunc="first",
                    )
                    if not pivot.empty:
                        with st.expander(f"📐 Return % pivot for `{mode}` (leverage × window×tf)"):
                            st.dataframe(
                                pivot.round(2),
                                width="stretch",
                            )
                except Exception as e:
                    st.caption(f"(pivot table unavailable: {type(e).__name__})")

            # ── Master comparison: ALL cells from ALL modes in one table
            st.markdown("#### 🗃️  Master: every cell across every mode")
            st.caption(
                "Single flat table with ALL cells from ALL runs in this session. "
                "Filter / sort / export via the table UI."
            )
            master_cols = [
                "leverage_mode", "window_label", "timeframe", "leverage",
                "fee_profile", "trades", "return_pct", "maxdd_pct", "calmar",
                "sharpe", "when", "strategy",
            ]
            present_master = [c for c in master_cols if c in combined_df.columns]
            st.dataframe(
                combined_df[present_master].sort_values("return_pct", ascending=False),
                width="stretch",
                hide_index=True,
            )

        # ── Individual run HTML viewer (legacy, collapsible) ────────────
        with st.expander("🗐 View individual run HTML report (legacy viewer)"):
            # Picker labels are descriptive; map back to the raw row by index.
            session_run_labels = [
                f"{(r['run_timestamp'] or '')[:19]}  ·  mode={r.get('leverage_mode') or '—'}  ·  "
                f"verdict={r.get('run_verdict') or '—'}  ·  "
                f"return={r.get('run_max_return_pct'):+.2f}%"
                if r.get('run_max_return_pct') is not None else
                f"{(r['run_timestamp'] or '')[:19]}  ·  mode={r.get('leverage_mode') or '—'}"
                for r in session_runs
            ]
            picked_run_label = st.selectbox(
                "Individual run to embed",
                session_run_labels,
                key="p7_single_run_pick",
            )
            picked_run = session_runs[session_run_labels.index(picked_run_label)]

            abs_html_path = _to_absolute(picked_run.get("report_html_path"))
            if abs_html_path and abs_html_path.exists():
                try:
                    raw_html = abs_html_path.read_text()
                    # Task #151.4 — download button replaces the broken
                    # file:// link_button. Browsers block file:// URLs
                    # from web-origin iframes.
                    st.download_button(
                        label="⬇️ Download HTML report",
                        data=raw_html,
                        file_name=abs_html_path.name,
                        mime="text/html",
                        help="Download the full report HTML so you can open "
                             "it in a new browser tab outside Streamlit.",
                    )
                    st.caption(
                        f"report_dir: `{picked_run.get('run_report_dir') or '(none)'}`"
                    )
                    # Task #151.2 — light-theme wrapper for dark-mode users
                    wrapped = _wrap_html_for_light_theme(raw_html)
                    st.components.v1.html(wrapped, height=900, scrolling=True)
                except Exception as e:
                    st.error(f"Failed to render HTML: {e}")
            else:
                st.warning(
                    "No HTML report found for this run. The row exists in "
                    "`strategy_version_runs` but the report_html_path is "
                    "null or the file was moved/deleted."
                )

        st.stop()  # History section has its own content, skip the Run body

    # ─────────────────────────────────────────────────────────────────
    #                          ▶ RUN SECTION
    # ─────────────────────────────────────────────────────────────────

    st.info(
        "ℹ️ **Deep backtest is currently XAUUSD-only.** The picker below only "
        "shows strategies that (a) can be instantiated with no kwargs and "
        "(b) declare XAUUSD as a supported market. Crypto strategies like "
        "`bb_rsi_mr` and `vol_momentum` use a different data path — run them "
        "via `python -m scripts.backtest run <name>` from the terminal."
    )

    # ── Strategy picker — filter to deep-backtest-compatible strategies ──
    compatible_names: list[str] = []
    compatible_instances: dict[str, Any] = {}
    incompatible_reasons: dict[str, str] = {}
    for name, cls in _CLASS_REGISTRY.items():
        try:
            inst = cls()
        except Exception as e:
            incompatible_reasons[name] = f"init error: {type(e).__name__}"
            continue
        markets = getattr(inst, "markets", []) or []
        if "XAUUSD" not in markets:
            incompatible_reasons[name] = f"markets={markets} (no XAUUSD)"
            continue
        compatible_names.append(name)
        compatible_instances[name] = inst
    compatible_names.sort()

    if not compatible_names:
        st.error(
            "No deep_backtest-compatible strategies found. Every strategy in "
            "`router.STRATEGY_REGISTRY` either fails to instantiate or doesn't "
            "declare XAUUSD in its markets list. Add a gold strategy first."
        )
        st.stop()

    # ── 🎯 Strategy + symbol ──────────────────────────────────────────
    st.markdown("### 🎯 Strategy")
    col_strat, col_cash = st.columns([2, 1])
    with col_strat:
        strategy = st.selectbox(
            "Strategy",
            compatible_names,
            help=(
                f"{len(compatible_names)} deep-backtest-compatible strategies. "
                f"{len(incompatible_reasons)} others hidden. "
                f"Auto-detected from `STRATEGY_REGISTRY`."
            ),
        )
    with col_cash:
        initial_cash = st.number_input(
            "Initial cash ($)",
            value=10000.0, step=1000.0, min_value=1000.0,
            help=TECHNICAL_TERMS["initial_cash"],
        )
    symbol = st.text_input(
        "Symbol",
        value="XAUUSD",
        help=TECHNICAL_TERMS["symbol"],
    )

    if incompatible_reasons:
        with st.expander(f"🙈 Hidden strategies ({len(incompatible_reasons)}) — incompatible with gold deep_backtest"):
            for n, reason in sorted(incompatible_reasons.items()):
                st.caption(f"• `{n}` — {reason}")

    st.divider()

    # ── 📅 Windows & timeframes ──────────────────────────────────────
    st.markdown("### 📅 Windows & timeframes")

    window_labels = []
    window_values = {}
    for days, label in WINDOWS_CATALOG:
        try:
            available, reason = _check_window_availability(days, "1h", symbol)
        except Exception as e:
            available, reason = False, f"probe error: {type(e).__name__}"
        tag = label if available else f"{label}  (NOT AVAILABLE — {reason})"
        window_labels.append(tag)
        window_values[tag] = (days, available)

    col_w, col_tf = st.columns(2)
    with col_w:
        picked_windows = st.multiselect(
            "Time windows",
            window_labels,
            default=[lbl for lbl in window_labels if window_values[lbl][1] and window_values[lbl][0] in (90, 365)],
            help=TECHNICAL_TERMS["window"],
        )
        window_days_list = [window_values[lbl][0] for lbl in picked_windows if window_values[lbl][1]]

    with col_tf:
        tf_labels = [f"{tf} — {desc}" for tf, desc in TIMEFRAMES_CATALOG]
        tf_map = {f"{tf} — {desc}": tf for tf, desc in TIMEFRAMES_CATALOG}
        picked_tfs = st.multiselect(
            "Timeframes",
            tf_labels,
            default=[l for l in tf_labels if tf_map[l] in ("1h",)],
            help=TECHNICAL_TERMS["timeframe"],
        )
        timeframes_list = [tf_map[l] for l in picked_tfs]

    st.divider()

    # ── 💰 Broker-specific fees ───────────────────────────────────────
    st.markdown("### 💰 Broker & fees")
    st.caption(
        "**New in task #141:** Pick broker → platforms → cost scenario. "
        "The dropdown is now broker-grouped so you can see the MT4 vs cTrader "
        "distinction clearly. Resolves to the concrete fee profile keys the "
        "subprocess consumes."
    )

    # Derive instrument_class from the selected strategy's market list.
    inst_obj = compatible_instances.get(strategy)
    inst_markets = getattr(inst_obj, "markets", []) if inst_obj else []
    primary_market = inst_markets[0] if inst_markets else "XAUUSD"
    instrument_class = INSTRUMENT_CLASS_BY_MARKET.get(primary_market, "xauusd_metals")

    col_broker, col_platform, col_scenario = st.columns([1, 2, 2])
    with col_scenario:
        scenarios = ["normal", "news_active", "stress", "pine_faithful"]
        picked_scenario = st.radio(
            "Cost scenario",
            scenarios,
            index=0,
            help=TECHNICAL_TERMS["scenario"],
        )

    # For pine_faithful scenario we ignore instrument_class so the
    # broker-less `(none)` pine_zero_cost profile is reachable.
    fee_tree = group_profiles_by_broker_platform(
        instrument_class=None if picked_scenario == "pine_faithful" else instrument_class,
        scenario=picked_scenario,
    )

    with col_broker:
        broker_options = sorted(fee_tree.keys()) if fee_tree else []
        if not broker_options:
            st.warning(
                f"No profiles for scenario={picked_scenario!r}, "
                f"market={primary_market!r}."
            )
            picked_broker = None
            fees_list = []
        else:
            default_broker_idx = (
                broker_options.index("IC Markets")
                if "IC Markets" in broker_options else 0
            )
            picked_broker = st.radio(
                "Broker",
                broker_options,
                index=default_broker_idx,
                help=TECHNICAL_TERMS["broker"],
            )

    with col_platform:
        if picked_broker:
            platforms = sorted(fee_tree[picked_broker].keys())
            default_platforms = [p for p in platforms if p == "mt4"] or platforms[:1]
            picked_platforms = st.multiselect(
                "Platforms",
                platforms,
                default=default_platforms,
                help=TECHNICAL_TERMS["platform"],
            )
            # Resolve to concrete profile keys
            fees_list = []
            for p in picked_platforms:
                fees_list.extend(fee_tree[picked_broker].get(p, []))
        else:
            fees_list = []

    if fees_list:
        st.caption(f"→ Resolved to **{len(fees_list)}** profile(s): `{', '.join(fees_list)}`")

    st.divider()

    # ── ⚡ Leverage + modes ──────────────────────────────────────────
    st.markdown("### ⚡ Leverage & modes")
    col_levs, col_baseline = st.columns([3, 1])
    with col_levs:
        picked_levs = st.multiselect(
            "Leverages (x)",
            [str(l) for l in LEVERAGES_CATALOG],
            default=["10"],
            help=TECHNICAL_TERMS["leverage"],
        )
        leverages_list = [float(x) for x in picked_levs]
    with col_baseline:
        baseline_lev = st.number_input(
            "Baseline leverage",
            value=10.0, step=1.0, min_value=1.0, max_value=1000.0,
            help=TECHNICAL_TERMS["baseline_leverage"],
        )

    # Leverage modes — with per-mode tooltips from LEVERAGE_MODE_HELP
    mode_labels = [desc for _, desc in LEVERAGE_MODES_CATALOG]
    mode_map = {desc: mode for mode, desc in LEVERAGE_MODES_CATALOG}
    mode_help_lines = [
        f"**{mode.value}** — {LEVERAGE_MODE_HELP.get(mode.value, '')}"
        for mode, _ in LEVERAGE_MODES_CATALOG
    ]
    picked_modes = st.multiselect(
        "Leverage modes (one subprocess per mode, separate reports)",
        mode_labels,
        default=[desc for mode, desc in LEVERAGE_MODES_CATALOG if mode.value == "margin_capped"],
        help="\n\n".join(mode_help_lines),
    )
    modes_list = [mode_map[l] for l in picked_modes]

    st.divider()

    # ── 📊 Walk-forward + advanced ───────────────────────────────────
    st.markdown("### 📊 Walk-forward")
    col_wf1, col_wf2 = st.columns(2)
    with col_wf1:
        wf_enabled = st.checkbox(
            "Walk-forward OOS validation",
            value=True,
            help=TECHNICAL_TERMS["walk_forward"],
        )
    with col_wf2:
        wf_retune = st.checkbox(
            "WF retune (per-fold grid search)",
            value=False,
            disabled=not wf_enabled,
            help=TECHNICAL_TERMS["wf_retune"],
        )

    with st.expander("⚙️ Advanced options"):
        version_slug_override = st.text_input(
            "Explicit version slug (optional)",
            value="",
            help=TECHNICAL_TERMS["version_slug"] + (
                " Setting this overrides the auto-generated triplet. "
                "Useful for naming bespoke variants (e.g. `optimized_v2`)."
            ),
        )
        strategy_params_raw = st.text_input(
            "Strategy param overrides",
            value="",
            help=TECHNICAL_TERMS["strategy_params"],
        )
        col_kelly1, col_kelly2, col_kelly3 = st.columns(3)
        with col_kelly1:
            kelly_win_rate = st.number_input(
                "Kelly win rate",
                value=0.0, step=0.05, min_value=0.0, max_value=1.0,
                help=TECHNICAL_TERMS["kelly_win_rate"],
            )
        with col_kelly2:
            kelly_payoff = st.number_input(
                "Kelly payoff ratio",
                value=0.0, step=0.5, min_value=0.0,
                help=TECHNICAL_TERMS["kelly_payoff_ratio"],
            )
        with col_kelly3:
            kelly_fraction = st.number_input(
                "Kelly fraction",
                value=0.5, step=0.25, min_value=0.0, max_value=1.0,
                help=TECHNICAL_TERMS["kelly_fraction"],
            )

    st.divider()

    # ── 🔢 Cell-count preview (metric row) ───────────────────────────
    n_cells = (
        max(1, len(window_days_list)) *
        max(1, len(timeframes_list)) *
        max(1, len(leverages_list)) *
        max(1, len(fees_list)) *
        max(1, len(modes_list))
    )
    est_runtime_s = max(1, n_cells * 2)
    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    col_m1.metric("Cells", n_cells, help=TECHNICAL_TERMS["matrix"])
    col_m2.metric("Est. runtime", f"{est_runtime_s}s" if est_runtime_s < 120 else f"{est_runtime_s // 60}m")
    col_m3.metric("Modes", len(modes_list) or 1)
    col_m4.metric("Fees", len(fees_list) or 1)

    can_run = (
        len(window_days_list) > 0 and
        len(timeframes_list) > 0 and
        len(fees_list) > 0 and
        len(leverages_list) > 0 and
        len(modes_list) > 0
    )
    if not can_run:
        st.warning("⚠️ Tick at least one value in every dimension to enable the Run button.")

    # ── 🚀 Run button + progress-bar UI (task #151.5) ─────────────────
    if st.button("🚀 Run Deep Backtest", type="primary", disabled=not can_run):
        import time as _time

        st.session_state["p7_running"] = True

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

        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])

        # Subprocess watchdog — kill on timeout to prevent UI hangs
        WATCHDOG_TIMEOUT_S = max(300, min(3600, n_cells * 10))

        total_modes = len(modes_list)
        # Estimate per-mode runtime: ~2s per cell minimum, 2s floor
        per_mode_cells = n_cells // max(1, total_modes)
        est_seconds_per_mode = max(5.0, per_mode_cells * 2.0)
        est_total_s = est_seconds_per_mode * total_modes

        st.markdown("### 🚀 Running deep backtest")
        top_col1, top_col2, top_col3, top_col4 = st.columns(4)
        mode_counter = top_col1.empty()
        cell_counter = top_col2.empty()
        elapsed_counter = top_col3.empty()
        eta_counter = top_col4.empty()

        overall_progress = st.progress(0.0, text="Waiting to start…")

        # Friendly status text — rewritten from raw logs via _friendly_status_for_line
        friendly_status = st.empty()

        # Validation tick list — one st.status per phase per mode
        # We use a single nested layout: for each mode, render a st.status
        # container with the phase list inside.
        st.markdown("#### 🧪 Validation checklist")
        st.caption(
            "Each phase turns 🔄 while running and ✅ when complete. "
            "Click any row to see the raw log lines from that phase."
        )

        mode_status_cols = st.columns(total_modes)
        mode_phase_states: list[dict] = [
            {"phase_key": None, "completed": set(), "lines": {k: [] for k, _, _ in DEEP_BACKTEST_PHASES},
             "status_widget": None, "tick_widgets": {}}
            for _ in range(total_modes)
        ]

        def _render_mode_panel(mode_idx: int, mode_obj, state: dict, col):
            """Render one mode's phase checklist in its column."""
            with col:
                st.markdown(f"**Mode {mode_idx + 1}/{total_modes}: `{mode_obj.value}`**")
                for phase_key, label, _marker in DEEP_BACKTEST_PHASES:
                    # Skip walk_forward phase if WF disabled
                    if phase_key == "walk_forward" and not wf_enabled:
                        st.markdown(f"⊘ {label}  *(skipped — WF off)*")
                        state["completed"].add(phase_key)
                        continue
                    glyph = _phase_status(phase_key, state, state["completed"])
                    tick_slot = state["tick_widgets"].get(phase_key)
                    if tick_slot is None:
                        tick_slot = st.empty()
                        state["tick_widgets"][phase_key] = tick_slot
                    tick_slot.markdown(f"{glyph} {label}")

        for i, m in enumerate(modes_list):
            _render_mode_panel(i, m, mode_phase_states[i], mode_status_cols[i])

        # Collapsible raw log (hidden by default per Prince's request)
        raw_log_expander = st.expander("🧾 Show raw logs (advanced)", expanded=False)
        with raw_log_expander:
            results_log = st.empty()
        log_buffer: list[str] = []
        LOG_DISPLAY_TAIL = 200
        UI_UPDATE_INTERVAL_S = 0.5

        overall_start = _time.time()
        proc = None
        crashed = False
        for mode_idx, mode in enumerate(modes_list, start=1):
            mode_start = _time.time()
            state = mode_phase_states[mode_idx - 1]
            mode_col = mode_status_cols[mode_idx - 1]

            mode_counter.metric("Mode", f"{mode_idx}/{total_modes}")
            cell_counter.metric("Cells per mode", per_mode_cells)
            elapsed_counter.metric("Elapsed", f"{int(_time.time() - overall_start)}s")
            eta_counter.metric("ETA", f"{int(max(0, est_total_s - (_time.time() - overall_start)))}s")

            friendly_status.info(f"🚀 Starting mode **{mode.value}** ({mode_idx}/{total_modes})…")

            cmd = [
                sys.executable,
                "scripts/deep_backtest.py",
                strategy,
                "--leverage-mode", mode.value,
            ] + base_cmd_tail

            log_buffer.append(f"\n{'=' * 70}\n$ {' '.join(cmd)}\n{'=' * 70}\n")

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

            last_ui_push = _time.time()
            timed_out = False
            current_phase = None
            for line in proc.stdout:
                log_buffer.append(line)

                # Phase detection — if this line activates a new phase, mark
                # the previous one complete and flip the tick to 🔄.
                new_phase = _parse_phase_line(line)
                if new_phase is not None and new_phase != current_phase:
                    if current_phase is not None:
                        state["completed"].add(current_phase)
                    current_phase = new_phase
                    state["phase_key"] = new_phase
                    # Re-render the mode panel
                    _render_mode_panel(mode_idx - 1, mode, state, mode_col)

                # Friendly status translation
                friendly = _friendly_status_for_line(line, current_phase)
                if friendly:
                    friendly_status.info(f"🔄 **{mode.value}** — {friendly}")

                now = _time.time()
                if now - mode_start > WATCHDOG_TIMEOUT_S:
                    proc.kill()
                    timed_out = True
                    log_buffer.append(
                        f"\n[WATCHDOG] Mode `{mode.value}` killed after "
                        f"{WATCHDOG_TIMEOUT_S}s timeout.\n"
                    )
                    break

                # Throttled UI updates for the progress bar, metrics, and raw log
                if now - last_ui_push >= UI_UPDATE_INTERVAL_S:
                    completed_modes_frac = (mode_idx - 1) / max(1, total_modes)
                    within_mode_frac = min(
                        1.0, (now - mode_start) / max(1.0, est_seconds_per_mode)
                    )
                    pct = completed_modes_frac + within_mode_frac / max(1, total_modes)
                    pct = max(0.0, min(1.0, pct))
                    overall_progress.progress(
                        pct,
                        text=f"Mode {mode_idx}/{total_modes}: {mode.value} — {int(pct * 100)}%",
                    )
                    elapsed_counter.metric("Elapsed", f"{int(now - overall_start)}s")
                    eta_counter.metric(
                        "ETA",
                        f"{int(max(0, est_total_s - (now - overall_start)))}s",
                    )
                    # Raw log still updates for users who open the expander
                    results_log.code(
                        "".join(log_buffer[-LOG_DISPLAY_TAIL:]), language="bash",
                    )
                    last_ui_push = now

            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                timed_out = True

            # Mark whatever phase was current as complete
            if current_phase is not None:
                state["completed"].add(current_phase)
            # Mark every phase complete on clean exit (catches phases whose
            # stdout marker wasn't parsed, e.g. VERDICT line came inline)
            if not timed_out:
                for phase_key, _label, _marker in DEEP_BACKTEST_PHASES:
                    if phase_key == "walk_forward" and not wf_enabled:
                        continue
                    state["completed"].add(phase_key)
            state["phase_key"] = None
            _render_mode_panel(mode_idx - 1, mode, state, mode_col)

            results_log.code("".join(log_buffer[-LOG_DISPLAY_TAIL:]), language="bash")

            if timed_out:
                friendly_status.error(
                    f"⏰ Mode `{mode.value}` TIMED OUT after {WATCHDOG_TIMEOUT_S}s. "
                    f"Reduce matrix size or increase the timeout."
                )
                crashed = True
                break

            VERDICT_CODE_MAP = {
                0: ("success", "✓", "DEPLOYABLE"),
                1: ("warning", "⚠", "NEEDS_WF / RESEARCH_ONLY"),
                2: ("warning", "⚠", "FAILED (verdict)"),
            }
            if proc.returncode in VERDICT_CODE_MAP:
                level, glyph, verdict_label = VERDICT_CODE_MAP[proc.returncode]
                if level == "success":
                    friendly_status.success(
                        f"{glyph} Mode `{mode.value}` → **{verdict_label}**"
                    )
                else:
                    friendly_status.warning(
                        f"{glyph} Mode `{mode.value}` → **{verdict_label}** "
                        f"(legitimate research outcome, not a crash)"
                    )
            else:
                friendly_status.error(
                    f"✗ Mode `{mode.value}` CRASHED with exit code {proc.returncode}. "
                    f"Open the raw logs expander for the traceback."
                )
                crashed = True
                break

        # Final UI update — snap to 100%
        overall_progress.progress(
            1.0 if not crashed else 0.0,
            text="Complete" if not crashed else "Aborted",
        )
        elapsed_counter.metric("Elapsed", f"{int(_time.time() - overall_start)}s")
        eta_counter.metric("ETA", "0s")
        st.session_state["p7_running"] = False

        if not crashed and proc is not None and proc.returncode in (0, 1, 2):
            _load_recent_runs_cached.clear()
            friendly_status.success(
                f"✓ All {total_modes} mode(s) ran to completion in "
                f"{int(_time.time() - overall_start)}s. "
                f"Switch to the **📜 History** section to view the combined "
                f"comparison across every leverage mode / window / timeframe."
            )
            if proc.returncode == 0:
                st.balloons()
