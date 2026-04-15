"""Smoke + structural tests for the deep_backtest framework.

Keeps test time <30s by running a small grid (1 window × 1 TF × 2 leverages
× 1 fee = 2 cells) on SwiftAlmaStrategy with reports disabled. Verifies:
    - Pipeline runs end-to-end with no exceptions
    - All phases populate the result object
    - Leverage validation auto-triggers when P&L invariance is detected
    - JSON/CSV artifacts land on disk
    - Matrix cells have the expected schema
    - Verdict is one of the known labels
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.backtest.deep_backtest import (
    CellResult,
    DeepBacktestConfig,
    DeepBacktestResult,
    LeverageValidationResult,
    WalkForwardSummary,
    run_deep_backtest,
)


@pytest.fixture
def tiny_config(tmp_path: Path) -> DeepBacktestConfig:
    """Minimal config that runs the full pipeline in ~15s."""
    return DeepBacktestConfig(
        strategy="swift_alma",
        strategy_params={},
        symbol="XAUUSD",
        timeframes=["15m"],
        window_days=[30],
        leverages=[1.0, 100.0],
        fee_profiles=["ic_markets_mt4_xauusd_normal"],
        initial_cash=10_000.0,
        wf_enabled=True,
        wf_fold_days=90,
        wf_n_folds=2,
        wf_gate_calmar=0.5,
        wf_retune=False,
        leverage_validation_enabled=True,
        out_dir=tmp_path / "deep_bt_test",
        generate_html=False,      # skip rendering for speed
        generate_pdf=False,
        generate_heatmaps=False,
        progress=False,
    )


class TestDeepBacktestPipeline:
    """End-to-end pipeline smoke tests."""

    def test_pipeline_runs_and_returns_structured_result(self, tiny_config):
        result = run_deep_backtest(tiny_config)
        assert isinstance(result, DeepBacktestResult)
        assert result.strategy_name == "swift_alma"
        assert result.verdict in {"DEPLOYABLE", "RESEARCH_ONLY", "NEEDS_WF", "FAILED"}
        assert result.verdict_reason  # non-empty string
        assert result.elapsed_seconds > 0

    def test_matrix_has_expected_cell_count(self, tiny_config):
        result = run_deep_backtest(tiny_config)
        expected = (len(tiny_config.window_days) * len(tiny_config.timeframes)
                    * len(tiny_config.leverages) * len(tiny_config.fee_profiles))
        assert len(result.matrix) == expected, \
            f"expected {expected} cells, got {len(result.matrix)}"

    def test_matrix_cells_have_schema(self, tiny_config):
        result = run_deep_backtest(tiny_config)
        for cell in result.matrix:
            assert isinstance(cell, CellResult)
            assert cell.window_days == 30
            assert cell.timeframe == "15m"
            assert cell.leverage in {1.0, 100.0}
            assert cell.fee_profile == "ic_markets_mt4_xauusd_normal"
            # trades >= 0, equity > 0 (no broker wipeouts on 30d × 15m × MT4)
            assert cell.trades >= 0
            assert cell.final_equity > 0

    def test_sanity_checks_populated(self, tiny_config):
        result = run_deep_backtest(tiny_config)
        sanity = result.sanity_checks
        assert "error_cells" in sanity
        assert "leverage_invariance" in sanity
        assert sanity["error_cells"] == 0
        # Best cell is populated when there's at least one profitable cell
        if sanity.get("best_cell"):
            bc = sanity["best_cell"]
            for key in ("window", "timeframe", "leverage", "return_pct",
                        "maxdd_pct", "calmar", "trades"):
                assert key in bc

    def test_leverage_validation_triggered_by_swift_invariance(self, tiny_config):
        """SwiftAlmaStrategy's risk_pct == sl_pct design makes P&L
        leverage-invariant. The deep_backtest framework should detect this
        and auto-trigger the leverage validation phase."""
        result = run_deep_backtest(tiny_config)
        li = result.sanity_checks["leverage_invariance"]
        # With only 2 leverages and invariance, we should flag the triplet
        assert li["invariant_triplets"] >= 1
        assert li["all_invariant"] is True

        # Phase 3 should have run
        assert result.leverage_validation is not None
        assert isinstance(result.leverage_validation, LeverageValidationResult)
        assert result.leverage_validation.invariant_detected is True
        assert result.leverage_validation.hand_trace_passed is True
        # The explanation should mention risk-based sizing or max-margin cap
        reason = result.leverage_validation.invariant_reason.lower()
        assert "sizing" in reason or "margin" in reason

    def test_walk_forward_runs_when_enabled(self, tiny_config):
        result = run_deep_backtest(tiny_config)
        assert result.walk_forward is not None
        assert isinstance(result.walk_forward, WalkForwardSummary)
        wf = result.walk_forward
        assert wf.n_folds == tiny_config.wf_n_folds
        assert wf.fold_days == tiny_config.wf_fold_days
        assert len(wf.folds) == wf.n_folds
        assert wf.total_trades >= 0
        # Gate flags are bools
        assert isinstance(wf.gate_per_fold_passed, bool)
        assert isinstance(wf.continuous_gate_passed, bool)

    def test_json_and_csv_artifacts_written(self, tiny_config):
        result = run_deep_backtest(tiny_config)
        summary_path = result.report_dir / "summary.json"
        csv_path = result.report_dir / "matrix.csv"
        assert summary_path.exists()
        assert csv_path.exists()

        loaded = json.loads(summary_path.read_text())
        assert loaded["strategy"] == "swift_alma"
        assert loaded["verdict"] == result.verdict
        assert loaded["matrix_n_cells"] == len(result.matrix)
        assert loaded["walk_forward"] is not None

    def test_wf_skipped_when_disabled(self, tiny_config, tmp_path):
        tiny_config.wf_enabled = False
        tiny_config.out_dir = tmp_path / "no_wf"
        result = run_deep_backtest(tiny_config)
        assert result.walk_forward is None
        # Verdict should still be set (NEEDS_WF when matrix-only)
        assert result.verdict in {"DEPLOYABLE", "RESEARCH_ONLY", "NEEDS_WF", "FAILED"}


class TestStrategyResolution:
    """Cover the three strategy-spec paths: registry key, class, dotted path."""

    def test_registry_key_resolves(self, tiny_config):
        # tiny_config uses "swift_alma" — if this passes, registry lookup works
        result = run_deep_backtest(tiny_config)
        assert result.strategy_name == "swift_alma"

    def test_dotted_path_resolves(self, tmp_path):
        config = DeepBacktestConfig(
            strategy="src.strategies.trend_following.swift_alma:SwiftAlmaStrategy",
            timeframes=["15m"],
            window_days=[30],
            leverages=[1.0],
            fee_profiles=["ic_markets_mt4_xauusd_normal"],
            wf_enabled=False,
            leverage_validation_enabled=False,
            out_dir=tmp_path / "dotted",
            generate_html=False,
            generate_pdf=False,
            generate_heatmaps=False,
            progress=False,
        )
        result = run_deep_backtest(config)
        assert len(result.matrix) == 1
        # resolve_strategy_name strips the dotted path to the class name
        assert "swiftalma" in result.strategy_name.lower()

    def test_unknown_registry_key_raises(self, tmp_path):
        config = DeepBacktestConfig(
            strategy="no_such_strategy_xyz",
            timeframes=["15m"],
            window_days=[30],
            leverages=[1.0],
            fee_profiles=["ic_markets_mt4_xauusd_normal"],
            wf_enabled=False,
            leverage_validation_enabled=False,
            out_dir=tmp_path / "unknown",
            progress=False,
        )
        with pytest.raises((RuntimeError, KeyError)):
            run_deep_backtest(config)
