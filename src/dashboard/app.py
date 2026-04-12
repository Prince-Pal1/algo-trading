"""Streamlit dashboard — interactive UI for backtest results and strategy exploration.

Usage:
    streamlit run src/dashboard/app.py
    # or
    python -m scripts.dashboard

Pages:
    1. Catalog Browser — Browse all strategies, filter, sort
    2. Strategy Deep Dive — Select a run → full metrics + charts
    3. Compare Strategies — Side-by-side comparison of runs
    4. Run Backtest — Run a strategy and see results live
    5. Validation Dashboard — Protocol results overview
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
    ["Backtest Runs", "Strategy Deep Dive", "Compare Runs", "Imported Strategies", "Validation"],
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
