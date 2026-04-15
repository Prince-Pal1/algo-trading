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
    st.markdown("### 🏷️  Layer 1 — Broker fee profile")
    fees_sorted = sorted(fee_universe)
    picked_fee = st.selectbox(
        "Fee profile (only fees with backtest data shown)",
        fees_sorted,
        help=f"{len(fees_sorted)} fee profiles have facets in storage. "
             f"Pick one to filter the hierarchy below.",
    )

    # ── LAYER 2 — Window time-frame with 🏆 hero ───────────────────────────
    windows_for_fee = window_universe.get(picked_fee, {})
    if not windows_for_fee:
        st.warning(f"No data for fee profile {picked_fee!r}.")
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
                    html_content = abs_html.read_text()
                    st.components.v1.html(html_content, height=900, scrolling=True)
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
        st.markdown("### 📜 Recent deep_backtest runs")
        st.caption(
            "Most recent runs across ALL strategies. "
            "Pick a row to embed its HTML report below. "
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
        else:
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
            st.markdown("### 📊 Report viewer")
            st.caption(
                "Pick one run to embed its full HTML report below. "
                "Use **Open in new tab** for the full-screen Plotly view."
            )

            # Picker labels are descriptive; map back to the raw row by index.
            run_labels = [
                f"{(r['run_timestamp'] or '')[:19]}  ·  {r['strategy_name']}  ·  "
                f"{r.get('description') or r['version_slug']}  ·  "
                f"verdict={r.get('run_verdict') or '—'}  ·  "
                f"return={r.get('run_max_return_pct'):+.2f}%"
                if r.get('run_max_return_pct') is not None else
                f"{(r['run_timestamp'] or '')[:19]}  ·  {r['strategy_name']}  ·  {r['version_slug']}"
                for r in runs
            ]
            picked_run_label = st.selectbox("Select a run", run_labels, key="p7_hist_pick")
            picked_run = runs[run_labels.index(picked_run_label)]

            abs_html_path = _to_absolute(picked_run.get("report_html_path"))
            col_btn1, col_btn2 = st.columns([1, 3])
            with col_btn1:
                if abs_html_path and abs_html_path.exists():
                    st.link_button(
                        "🗔 Open in new tab",
                        f"file://{abs_html_path}",
                        help="Opens the full HTML report in a new browser tab.",
                    )
            with col_btn2:
                st.caption(
                    f"version_id: `{picked_run.get('version_id')}` · "
                    f"report: `{picked_run.get('report_html_path') or '(none)'}`"
                )

            if abs_html_path and abs_html_path.exists():
                try:
                    html_content = abs_html_path.read_text()
                    st.components.v1.html(html_content, height=900, scrolling=True)
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

    # ── 🚀 Run button + live log stream ──────────────────────────────
    if st.button("🚀 Run Deep Backtest", type="primary", disabled=not can_run):
        import time as _time

        st.session_state["p7_running"] = True

        results_log = st.empty()
        status_placeholder = st.empty()
        log_buffer: list[str] = []

        UI_UPDATE_INTERVAL_S = 0.5
        LOG_DISPLAY_TAIL = 200

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
        # Budget: ~10s per cell minimum, 300s floor, 1h ceiling.
        WATCHDOG_TIMEOUT_S = max(300, min(3600, n_cells * 10))

        total_modes = len(modes_list)
        overall_start = _time.time()
        proc = None
        for mode_idx, mode in enumerate(modes_list, start=1):
            mode_start = _time.time()
            status_placeholder.markdown(
                f"**Running mode {mode_idx}/{total_modes}:** `{mode.value}` — {strategy} "
                f"(overall elapsed: {_time.time() - overall_start:.1f}s)"
            )
            cmd = [
                sys.executable,
                "scripts/deep_backtest.py",
                strategy,
                "--leverage-mode", mode.value,
            ] + base_cmd_tail

            log_buffer.append(f"\n{'=' * 70}\n$ {' '.join(cmd)}\n{'=' * 70}\n")
            results_log.code("".join(log_buffer[-LOG_DISPLAY_TAIL:]), language="bash")

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
            for line in proc.stdout:
                log_buffer.append(line)
                now = _time.time()
                if now - mode_start > WATCHDOG_TIMEOUT_S:
                    proc.kill()
                    timed_out = True
                    log_buffer.append(
                        f"\n[WATCHDOG] Mode `{mode.value}` killed after "
                        f"{WATCHDOG_TIMEOUT_S}s timeout.\n"
                    )
                    break
                if now - last_ui_push >= UI_UPDATE_INTERVAL_S:
                    display = "".join(log_buffer[-LOG_DISPLAY_TAIL:])
                    results_log.code(display, language="bash")
                    status_placeholder.markdown(
                        f"**Running mode {mode_idx}/{total_modes}:** `{mode.value}` — {strategy} "
                        f"(mode elapsed: {now - mode_start:.1f}s, "
                        f"overall: {now - overall_start:.1f}s)"
                    )
                    last_ui_push = now
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                timed_out = True

            display = "".join(log_buffer[-LOG_DISPLAY_TAIL:])
            results_log.code(display, language="bash")

            if timed_out:
                status_placeholder.error(
                    f"⏰ Mode `{mode.value}` TIMED OUT after {WATCHDOG_TIMEOUT_S}s. "
                    f"Consider reducing matrix size or increasing the timeout "
                    f"(currently `max(300, cells × 10)` seconds)."
                )
                break

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
            else:
                status_placeholder.error(
                    f"✗ Mode `{mode.value}` CRASHED with exit code {proc.returncode}. "
                    f"Check the log output above for the traceback."
                )
                break

        st.session_state["p7_running"] = False

        if proc is not None and proc.returncode in (0, 1, 2):
            # Invalidate the history cache so the just-completed run shows up
            # immediately when the user switches to the History section.
            _load_recent_runs_cached.clear()
            st.success(
                f"✓ All {total_modes} mode(s) ran to completion. "
                f"Switch to the **📜 History** section to view the new run(s), "
                f"or the **Strategies** page to see the versioned registry. "
                f"Verdicts: exit 0 = DEPLOYABLE, 1 = NEEDS_WF/RESEARCH_ONLY, 2 = FAILED."
            )
            if proc.returncode == 0:
                st.balloons()
