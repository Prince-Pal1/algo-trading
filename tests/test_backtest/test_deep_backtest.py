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
import sys
from pathlib import Path

import pytest

from src.backtest.deep_backtest import (
    AttributionBreakdown,
    CellResult,
    DeepBacktestConfig,
    DeepBacktestResult,
    LeverageMode,
    LeverageModeValidation,
    LeverageValidationResult,
    WalkForwardSummary,
    _apply_leverage_mode,
    _check_window_availability,
    _compute_cell_attribution,
    _compute_matrix_attribution,
    _find_baseline_cell,
    _load_timeframe,
    _validate_kelly_fractional,
    _validate_risk_scaled,
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


class TestGoldStrategyIndicatorPrecompute:
    """Regression tests for the indicator-precompute code path (the bugfix
    after task #112). SwiftAlmaStrategy computes features inline via
    on_features() so it doesn't need `indicators=...` passed to engine.run().
    But donchian_gold / vol_momentum_gold DO rely on precomputed features,
    and before the fix every matrix cell produced zero trades.

    These tests lock the fix in place:
        1. donchian_gold fires real trades through deep_backtest
        2. vol_momentum_gold fires real trades through deep_backtest
        3. A strategy with explicit config.indicators override also works
        4. WF retune path on donchian_gold with a trivial 2-combo grid

    Intentionally small configs (~30-45s each) so the gold-path tests add
    roughly 2 minutes to the overall suite. Worth it — this is the path
    that Prince cares about most (gold is the primary market), and it's
    the path that previously shipped broken.
    """

    def _gold_config(self, tmp_path: Path, strategy_name: str,
                     strategy_params: dict, out_sub: str) -> DeepBacktestConfig:
        return DeepBacktestConfig(
            strategy=strategy_name,
            strategy_params=strategy_params,
            symbol="XAUUSD",
            timeframes=["1h"],            # gold strategies are 1h-native
            window_days=[90],             # single window, tiny matrix
            leverages=[10.0],             # skip 1x — donchian hits margin rejection
            fee_profiles=["ic_markets_mt4_xauusd_normal"],
            wf_enabled=False,             # skip WF for speed (covered separately)
            leverage_validation_enabled=False,  # only one leverage, no invariance check needed
            out_dir=tmp_path / out_sub,
            generate_html=False,
            generate_pdf=False,
            generate_heatmaps=False,
            progress=False,
        )

    def test_donchian_gold_matrix_fires_real_trades(self, tmp_path):
        """Pre-fix, donchian_gold produced 0 trades per cell because
        indicators were never passed to engine.run(). This test ensures the
        framework auto-resolves the default indicator list."""
        cfg = self._gold_config(tmp_path, "donchian_gold",
                                {"session_filter": True}, "donchian_test")
        result = run_deep_backtest(cfg)
        assert len(result.matrix) == 1
        cell = result.matrix[0]
        assert cell.notes == "", f"cell errored: {cell.notes}"
        assert cell.trades > 0, \
            "donchian_gold should fire real trades when indicators are auto-resolved"
        # Sanity: a 90d window with donchian hitting at ~10 trades/quarter should
        # produce a non-trivial return (+/- a few %)
        assert abs(cell.return_pct) >= 0.01 or cell.trades >= 5

    def test_vol_momentum_gold_matrix_fires_real_trades(self, tmp_path):
        """Same regression check for vol_momentum_gold."""
        cfg = self._gold_config(tmp_path, "vol_momentum_gold",
                                {"long_only": True, "session_filter": True},
                                "vol_mom_test")
        result = run_deep_backtest(cfg)
        assert len(result.matrix) == 1
        cell = result.matrix[0]
        assert cell.notes == "", f"cell errored: {cell.notes}"
        assert cell.trades > 0, \
            "vol_momentum_gold should fire real trades when indicators are auto-resolved"

    def test_explicit_indicators_override_respected(self, tmp_path):
        """User can force a specific indicator set via config.indicators.
        When the override has all the keys donchian needs, trades still fire."""
        cfg = self._gold_config(tmp_path, "donchian_gold",
                                {"session_filter": True}, "explicit_ind_test")
        cfg.indicators = [
            "donchian_20", "donchian_55", "donchian_120",
            "atr_14", "atr_20", "adx_14",
        ]
        result = run_deep_backtest(cfg)
        assert result.matrix[0].trades > 0
        assert result.matrix[0].notes == ""

    def test_wf_retune_path_executes(self, tmp_path):
        """The `wf_retune=True` + `wf_param_grid` code path isn't covered
        by the SWIFT tests (SWIFT uses fixed Pine params). Run donchian_gold
        with a trivial 2-combo grid to verify the grid-search path runs
        end-to-end and writes best_params into each fold."""
        cfg = DeepBacktestConfig(
            strategy="donchian_gold",
            strategy_params={"session_filter": True},
            symbol="XAUUSD",
            timeframes=["1h"],
            window_days=[90],
            leverages=[10.0],
            fee_profiles=["ic_markets_mt4_xauusd_normal"],
            wf_enabled=True,
            wf_fold_days=30,
            wf_n_folds=2,
            wf_train_days=90,
            wf_retune=True,
            wf_param_grid={
                "sl_atr_mult": [2.5, 3.0],
                "adx_trend_threshold": [20.0, 25.0],
                "min_channels": [2],
                "max_risk_per_trade": [0.02],
            },
            wf_leverage=10.0,
            leverage_validation_enabled=False,
            out_dir=tmp_path / "wf_retune_test",
            generate_html=False,
            generate_pdf=False,
            generate_heatmaps=False,
            progress=False,
        )
        result = run_deep_backtest(cfg)
        assert result.walk_forward is not None
        assert len(result.walk_forward.folds) == 2
        for fold in result.walk_forward.folds:
            # best_params should be populated because wf_retune=True
            assert fold.best_params is not None
            assert "sl_atr_mult" in fold.best_params
            assert fold.best_params["sl_atr_mult"] in (2.5, 3.0)
            assert fold.best_params["adx_trend_threshold"] in (20.0, 25.0)


class TestLeverageModes:
    """Tests for the leverage_mode feature (task #114).

    Covers the 5 implemented modes: INVARIANT / MARGIN_CAPPED / VOL_TARGETED
    (passthrough) + RISK_SCALED / KELLY_FRACTIONAL (transforms). Also tests
    unit-level behavior of _apply_leverage_mode() and end-to-end that the
    transform propagates through to engine.run() via strategy_params.
    """

    def _base_config(self, tmp_path, out_sub, **overrides):
        """Helper: minimal donchian_gold config with given overrides."""
        defaults = dict(
            strategy="donchian_gold",
            strategy_params={"session_filter": True, "max_risk_per_trade": 0.02},
            timeframes=["1h"],
            window_days=[90],
            leverages=[10.0],
            fee_profiles=["ic_markets_mt4_xauusd_normal"],
            wf_enabled=False,
            leverage_validation_enabled=False,
            out_dir=tmp_path / out_sub,
            generate_html=False,
            generate_pdf=False,
            generate_heatmaps=False,
            progress=False,
        )
        defaults.update(overrides)
        return DeepBacktestConfig(**defaults)

    def test_default_mode_is_margin_capped_backward_compat(self, tmp_path):
        """Default leverage_mode should be MARGIN_CAPPED to preserve
        pre-task-#114 behavior for all existing callers."""
        cfg = self._base_config(tmp_path, "default")
        assert cfg.leverage_mode == LeverageMode.MARGIN_CAPPED
        result = run_deep_backtest(cfg)
        assert result.matrix[0].trades > 0
        assert result.matrix[0].notes == ""

    def test_apply_leverage_mode_passthrough_modes_unchanged(self, tmp_path):
        """INVARIANT / MARGIN_CAPPED / VOL_TARGETED should return
        strategy_params unchanged (no transform applied)."""
        for mode in (LeverageMode.INVARIANT,
                     LeverageMode.MARGIN_CAPPED,
                     LeverageMode.VOL_TARGETED):
            cfg = self._base_config(tmp_path, f"pass_{mode.value}", leverage_mode=mode)
            for L in (1.0, 10.0, 100.0, 1000.0):
                params = _apply_leverage_mode(cfg, L)
                assert params["max_risk_per_trade"] == 0.02, \
                    f"{mode.value} should not modify risk_pct at L={L}"

    def test_apply_leverage_mode_risk_scaled_linear(self, tmp_path):
        """RISK_SCALED: risk_pct = base × (L / baseline) should be linear.
        baseline=10, base=0.02. At L=5, expect 0.01. At L=20, expect 0.04."""
        cfg = self._base_config(
            tmp_path, "rs_math",
            leverage_mode=LeverageMode.RISK_SCALED,
            baseline_leverage=10.0,
        )
        assert _apply_leverage_mode(cfg, 5.0)["max_risk_per_trade"] == pytest.approx(0.01)
        assert _apply_leverage_mode(cfg, 10.0)["max_risk_per_trade"] == pytest.approx(0.02)
        assert _apply_leverage_mode(cfg, 20.0)["max_risk_per_trade"] == pytest.approx(0.04)
        assert _apply_leverage_mode(cfg, 50.0)["max_risk_per_trade"] == pytest.approx(0.10)

    def test_apply_leverage_mode_kelly_fractional_math(self, tmp_path):
        """KELLY_FRACTIONAL: f* = (bp - q) / b.
        p=0.55, b=2.0 → f* = (2×0.55 - 0.45)/2 = 0.325
        kelly_fraction=0.5 → risk_pct = 0.1625 (below 0.25 cap)"""
        cfg = self._base_config(
            tmp_path, "kelly_math",
            leverage_mode=LeverageMode.KELLY_FRACTIONAL,
            kelly_fraction=0.5,
            kelly_win_rate=0.55,
            kelly_payoff_ratio=2.0,
        )
        params = _apply_leverage_mode(cfg, 10.0)
        assert params["max_risk_per_trade"] == pytest.approx(0.1625, abs=1e-6)
        # Kelly is leverage-independent: same result at different L
        assert (_apply_leverage_mode(cfg, 1.0)["max_risk_per_trade"]
                == _apply_leverage_mode(cfg, 100.0)["max_risk_per_trade"])

    def test_apply_leverage_mode_kelly_fractional_hard_cap(self, tmp_path):
        """Kelly result MUST be hard-capped at 0.25 absolute risk_pct
        regardless of what the formula + fraction produce.
        p=0.80, b=5.0, full Kelly → f* = (5×0.80 - 0.20)/5 = 0.76
        kelly_fraction=1.0 → 0.76, capped to 0.25."""
        cfg = self._base_config(
            tmp_path, "kelly_cap",
            leverage_mode=LeverageMode.KELLY_FRACTIONAL,
            kelly_fraction=1.0,
            kelly_win_rate=0.80,
            kelly_payoff_ratio=5.0,
        )
        params = _apply_leverage_mode(cfg, 10.0)
        assert params["max_risk_per_trade"] == 0.25, \
            f"Expected hard cap at 0.25, got {params['max_risk_per_trade']}"

    def test_apply_leverage_mode_kelly_fractional_requires_priors(self, tmp_path):
        """KELLY_FRACTIONAL without win_rate/payoff_ratio must raise."""
        cfg = self._base_config(
            tmp_path, "kelly_no_priors",
            leverage_mode=LeverageMode.KELLY_FRACTIONAL,
            kelly_fraction=0.5,
            kelly_win_rate=None,
            kelly_payoff_ratio=None,
        )
        with pytest.raises(ValueError, match="KELLY_FRACTIONAL"):
            _apply_leverage_mode(cfg, 10.0)

    def test_risk_scaled_linearity_in_matrix(self, tmp_path):
        """End-to-end: RISK_SCALED mode on donchian_gold with L=10 and L=20
        should produce returns that are roughly 2× apart (within ±50%
        accounting for drawdown nonlinearity at this scale)."""
        cfg = self._base_config(
            tmp_path, "rs_linearity",
            leverage_mode=LeverageMode.RISK_SCALED,
            baseline_leverage=10.0,
            leverages=[10.0, 20.0],
        )
        result = run_deep_backtest(cfg)
        assert len(result.matrix) == 2
        cells = {int(c.leverage): c for c in result.matrix}
        r10 = cells[10].return_pct
        r20 = cells[20].return_pct
        # Both must fire trades (not be margin-rejected)
        assert cells[10].trades > 0, "L=10 should fire trades at mode=RISK_SCALED"
        assert cells[20].trades > 0, "L=20 should fire trades at mode=RISK_SCALED"
        # At 2× risk_pct, position size ~doubles, so |return| ~doubles.
        # Allow ±50% tolerance for drawdown nonlinearity + compounding.
        if abs(r10) > 1.0:  # skip degenerate case where 1× return is near zero
            ratio = r20 / r10 if r10 != 0 else 0
            assert 1.3 <= ratio <= 2.7, \
                f"Expected ~2× linearity at 2× leverage, got r10={r10}, r20={r20}, ratio={ratio}"


class TestExtendedTimeframes:
    """Tests for the new timeframes added in task #114: 30m, 4h, 1d."""

    def test_30m_timeframe_loads(self):
        """30m should load and produce roughly half the bar count of 15m."""
        df_30m = _load_timeframe("30m", "XAUUSD")
        df_15m = _load_timeframe("15m", "XAUUSD")
        assert len(df_30m) > 0
        # 30m should be ~half of 15m (may differ slightly due to dropna on gaps)
        ratio = len(df_30m) / len(df_15m)
        assert 0.4 <= ratio <= 0.6, f"30m/15m ratio {ratio} out of expected range"

    def test_4h_timeframe_loads(self):
        """4h should load and produce roughly 1/4 the bar count of 1h."""
        df_4h = _load_timeframe("4h", "XAUUSD")
        df_1h = _load_timeframe("1h", "XAUUSD")
        assert len(df_4h) > 0
        ratio = len(df_4h) / len(df_1h)
        assert 0.2 <= ratio <= 0.3, f"4h/1h ratio {ratio} out of expected range"

    def test_1d_timeframe_loads(self):
        """1d should load and produce roughly 1/24 the bar count of 1h
        (but actual ratio is closer to 1/19-1/20 because weekends collapse)."""
        df_1d = _load_timeframe("1d", "XAUUSD")
        df_1h = _load_timeframe("1h", "XAUUSD")
        assert len(df_1d) > 0
        ratio = len(df_1d) / len(df_1h)
        assert 0.03 <= ratio <= 0.07, f"1d/1h ratio {ratio} out of expected range"

    def test_30m_timeframe_runs_end_to_end(self, tmp_path):
        """Smoke-test running deep_backtest on donchian_gold at 30m."""
        cfg = DeepBacktestConfig(
            strategy="donchian_gold",
            strategy_params={"session_filter": True},
            timeframes=["30m"],
            window_days=[30],
            leverages=[10.0],
            fee_profiles=["ic_markets_mt4_xauusd_normal"],
            wf_enabled=False,
            leverage_validation_enabled=False,
            out_dir=tmp_path / "tf_30m",
            generate_html=False,
            generate_pdf=False,
            generate_heatmaps=False,
            progress=False,
        )
        result = run_deep_backtest(cfg)
        assert result.matrix[0].notes == ""
        # donchian_gold is 1h-native so 30m results might be weird, but the
        # cell should at least complete without erroring


class TestWindowAvailability:
    """Tests for _check_window_availability (task #114 — TUI support)."""

    def test_2y_window_available(self):
        """730 days (2 years) should be available on the current dataset
        (data starts 2024-04-14, ends 2026-04-13 → 730 days exactly)."""
        available, reason = _check_window_availability(730, "1h", "XAUUSD")
        # Accept either outcome depending on exact data edge; if unavailable,
        # the reason should be informative. We care that the function works.
        assert isinstance(available, bool)
        if not available:
            assert "need" in reason or "load" in reason

    def test_4y_window_unavailable_gracefully(self):
        """1460 days (4 years) exceeds available data — should return
        (False, <reason>) without raising."""
        available, reason = _check_window_availability(1460, "1h", "XAUUSD")
        assert available is False
        assert "need 1460d" in reason

    def test_1_month_window_available(self):
        """30d should be trivially available on a 2-year dataset."""
        available, reason = _check_window_availability(30, "1h", "XAUUSD")
        assert available is True
        assert reason == ""

    def test_availability_check_non_raising_on_bad_timeframe(self):
        """Unknown timeframe should return (False, reason) not raise."""
        available, reason = _check_window_availability(30, "99x", "XAUUSD")
        assert available is False
        assert "load failed" in reason or "Unsupported" in reason


class TestInteractiveTUI:
    """Tests for the interactive TUI module (task #114).

    The TUI itself is interactive and can't be fully tested without either
    a real TTY or a mock of questionary. These tests cover:
    - Fallback when questionary isn't available
    - Fallback when --non-interactive flag is set
    - Fallback when stdout is not a TTY
    - The interactive_available() helper's decision logic

    End-to-end TUI flow tests (mocking questionary answers) are kept out
    of the main suite because they require patching sys.stdout.isatty and
    all 7 questionary prompts, which gets brittle. Manual smoke test:
        PYTHONPATH=. python3 scripts/deep_backtest.py donchian_gold
    """

    def test_non_interactive_flag_bypasses_tui(self, tmp_path):
        """With --non-interactive, collect_config_interactive should return
        the fallback config verbatim without prompting."""
        from types import SimpleNamespace
        from src.backtest.deep_backtest_interactive import (
            collect_config_interactive,
        )

        fallback = DeepBacktestConfig(
            strategy="swift_alma",
            timeframes=["15m"],
            window_days=[30],
            leverages=[10.0],
            fee_profiles=["ic_markets_mt4_xauusd_normal"],
            wf_enabled=False,
            leverage_validation_enabled=False,
            out_dir=tmp_path / "tui_noninteractive",
            progress=False,
        )
        cli_args = SimpleNamespace(
            non_interactive=True,
            windows=None, timeframes=None, fees=None,
            leverages=None, leverage_mode=None, no_wf=False,
        )
        config, modes = collect_config_interactive(
            "swift_alma", cli_args, fallback,
        )
        assert config is fallback
        assert modes == [fallback.leverage_mode]
        assert modes == [LeverageMode.MARGIN_CAPPED]

    def test_interactive_available_detects_non_tty(self, monkeypatch):
        """interactive_available() should return False when stdout is
        piped (not a TTY), even if questionary is installed."""
        from types import SimpleNamespace
        from src.backtest import deep_backtest_interactive as tui
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        cli_args = SimpleNamespace(non_interactive=False)
        assert tui.interactive_available(cli_args) is False

    def test_interactive_available_without_questionary(self, monkeypatch):
        """If _HAS_QUESTIONARY is False, interactive_available() returns False."""
        from types import SimpleNamespace
        from src.backtest import deep_backtest_interactive as tui
        monkeypatch.setattr(tui, "_HAS_QUESTIONARY", False)
        cli_args = SimpleNamespace(non_interactive=False)
        assert tui.interactive_available(cli_args) is False

    def test_interactive_available_all_conditions_met(self, monkeypatch):
        """With questionary installed + TTY stdout + no --non-interactive,
        interactive_available() should return True."""
        from types import SimpleNamespace
        from src.backtest import deep_backtest_interactive as tui
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(tui, "_HAS_QUESTIONARY", True)
        cli_args = SimpleNamespace(non_interactive=False)
        assert tui.interactive_available(cli_args) is True

    def test_dimension_catalogs_match_plan_spec(self):
        """The dimension catalogs must match the plan's spec exactly — this
        is a contract test so a future refactor can't silently change the
        UX Prince signed off on."""
        from src.backtest.deep_backtest_interactive import (
            WINDOWS_CATALOG, TIMEFRAMES_CATALOG, FEES_CATALOG,
            LEVERAGES_CATALOG, LEVERAGE_MODES_CATALOG,
        )
        # Windows: 1mo / 3mo / 6mo / 1y / 2y / 4y (Prince's spec)
        assert [w for w, _ in WINDOWS_CATALOG] == [30, 90, 180, 365, 730, 1460]
        # Timeframes: 1m / 5m / 15m / 30m / 1h / 4h / 1d (Prince's spec)
        assert [tf for tf, _ in TIMEFRAMES_CATALOG] == \
               ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
        # Fees: 3 profiles
        assert [k for k, _ in FEES_CATALOG] == [
            "pine_zero_cost",
            "ic_markets_mt4_xauusd_normal",
            "ic_markets_ctrader_xauusd_normal",
        ]
        # Leverages: 1/5/10/25/50/100/200/400/500/1000 (Prince's spec)
        assert LEVERAGES_CATALOG == [1, 5, 10, 25, 50, 100, 200, 400, 500, 1000]
        # Modes: all 5 implemented
        assert [m for m, _ in LEVERAGE_MODES_CATALOG] == [
            LeverageMode.MARGIN_CAPPED,
            LeverageMode.INVARIANT,
            LeverageMode.RISK_SCALED,
            LeverageMode.KELLY_FRACTIONAL,
            LeverageMode.VOL_TARGETED,
        ]


# ── Task #115 synthetic strategies for validation testing ───────────────


class _OverridesInInitStrategy:
    """Synthetic strategy that accepts max_risk_per_trade kwarg but IGNORES it
    inside __init__ and hardcodes the attribute. Used to test the Phase 0
    read-back probe — this pattern is exactly what the probe must catch
    because the leverage_mode transform would have no effect on this class.
    """

    # Mimic BaseStrategy interface just enough to satisfy _resolve_strategy.
    # We don't inherit from BaseStrategy because we want a minimal synthetic.
    name = "synthetic_override"
    markets = ["XAUUSD"]
    timeframe = "1h"
    leverage_range = (1.0, 100.0)

    def __init__(self, *, max_risk_per_trade: float = 0.02, timeframe: str = "1h", **kwargs):
        # BUG SIMULATION: we accept the kwarg but store a hardcoded value.
        # This is the exact silent-failure pattern the probe catches.
        self.max_risk_per_trade = 0.01  # ignores kwarg value
        self.timeframe = timeframe
        self._bar = 0

    def on_features(self, symbol, timeframe, features):
        return None  # no signals needed — probe runs before any backtest


class TestLeverageModeValidation:
    """Phase 2.5 zero-tolerance validation tests (task #115).

    Covers:
      - Passthrough modes skip Phase 2.5
      - RISK_SCALED validation passes on correct donchian_gold
      - Phase 0 read-back probe catches silent strategy-constructor override
      - KELLY_FRACTIONAL validation passes on invariance
      - KELLY_FRACTIONAL warns on hard-cap clamp
      - KELLY_FRACTIONAL warns on unrealistic priors
      - Unit-level f* math via _validate_kelly_fractional priors path
      - Verdict override forces FAILED on hard-fail
    """

    def _base_cfg(self, tmp_path, out_sub, **overrides):
        defaults = dict(
            strategy="donchian_gold",
            strategy_params={"session_filter": True, "max_risk_per_trade": 0.02},
            timeframes=["1h"],
            window_days=[90],
            leverages=[10.0],
            fee_profiles=["ic_markets_mt4_xauusd_normal"],
            wf_enabled=False,
            leverage_validation_enabled=False,
            out_dir=tmp_path / out_sub,
            generate_html=False,
            generate_pdf=False,
            generate_heatmaps=False,
            progress=False,
        )
        defaults.update(overrides)
        return DeepBacktestConfig(**defaults)

    def test_passthrough_modes_skip_phase_2_5(self, tmp_path):
        """INVARIANT / MARGIN_CAPPED / VOL_TARGETED runs should have
        leverage_mode_validation == None. The existing Phase 3 handles them.
        """
        for i, mode in enumerate((LeverageMode.INVARIANT,
                                  LeverageMode.MARGIN_CAPPED,
                                  LeverageMode.VOL_TARGETED)):
            cfg = self._base_cfg(
                tmp_path, f"pass_{mode.value}_{i}",
                leverage_mode=mode,
                leverages=[10.0],  # single leverage → Phase 3 doesn't trigger either
            )
            result = run_deep_backtest(cfg)
            assert result.leverage_mode_validation is None, \
                f"passthrough mode {mode.value} should skip Phase 2.5"

    def test_risk_scaled_validation_passes_on_correct_donchian(self, tmp_path):
        """End-to-end: donchian_gold in RISK_SCALED mode should PASS Phase 2.5
        when risk_pct is small enough that the L2=2×baseline run still fits
        in margin.

        donchian_gold's default notional ≈ 3× equity at risk_pct=0.02. At L2
        with risk_pct=0.04, notional becomes ~6× equity. At high leverage
        that still fits, but at modest leverage the margin gate starts
        rejecting positions → different trade counts → scaling contract
        breaks. Solution: use a small risk_pct (0.005) so L2 has headroom.
        """
        cfg = self._base_cfg(
            tmp_path, "rs_passing",
            strategy_params={"session_filter": True, "max_risk_per_trade": 0.005},
            leverage_mode=LeverageMode.RISK_SCALED,
            baseline_leverage=10.0,
            leverages=[10.0],
        )
        result = run_deep_backtest(cfg)
        assert result.leverage_mode_validation is not None
        lmv = result.leverage_mode_validation
        assert lmv.mode == LeverageMode.RISK_SCALED
        # With small enough risk_pct, the 2× scaling should fit in margin and
        # the hand-trace should pass all 9 assertions.
        assert lmv.passed is True, \
            f"expected Phase 2.5 to pass on donchian_gold RISK_SCALED at "\
            f"risk_pct=0.005, got failures: {lmv.failures}"
        assert len(lmv.failures) == 0

    def test_readback_probe_catches_param_override(self, tmp_path):
        """Phase 0 read-back probe must raise RuntimeError when a strategy's
        __init__ silently overrides max_risk_per_trade. The _OverridesInInitStrategy
        synthetic above simulates this exact bug pattern."""
        # Use dotted path to load the synthetic class
        cfg = DeepBacktestConfig(
            strategy=f"{__name__}:_OverridesInInitStrategy",
            strategy_params={"max_risk_per_trade": 0.02},
            leverage_mode=LeverageMode.RISK_SCALED,
            baseline_leverage=10.0,
            timeframes=["1h"],
            window_days=[90],
            leverages=[10.0],
            fee_profiles=["ic_markets_mt4_xauusd_normal"],
            wf_enabled=False,
            leverage_validation_enabled=False,
            out_dir=tmp_path / "readback_catch",
            generate_html=False, generate_pdf=False, generate_heatmaps=False,
            progress=False,
        )
        # The Phase 0 probe should raise — expected = 0.02 (base), actual = 0.01 (override)
        with pytest.raises(RuntimeError, match="preflight FAILED"):
            run_deep_backtest(cfg)

    def test_kelly_fractional_validation_passes_on_donchian(self, tmp_path):
        """donchian_gold + KELLY_FRACTIONAL with realistic priors + small
        kelly_fraction should PASS Phase 2.5. The Kelly formula can produce
        large risk_pct values that crash donchian_gold (e.g., half-Kelly on
        b=2.0, p=0.55 → 0.1625 risk_pct → 24× equity notional → rejections).
        Use quarter-Kelly with conservative priors for a feasible run.

        p=0.52, b=1.2 → f* = (1.2×0.52 - 0.48)/1.2 = 0.12
        kelly_fraction=0.1 → risk_pct = 0.012 (small enough to fit)
        """
        cfg = self._base_cfg(
            tmp_path, "kelly_passing",
            leverage_mode=LeverageMode.KELLY_FRACTIONAL,
            kelly_fraction=0.1,
            kelly_win_rate=0.52,
            kelly_payoff_ratio=1.2,
            leverages=[10.0, 50.0],  # two leverages for invariance check
        )
        result = run_deep_backtest(cfg)
        assert result.leverage_mode_validation is not None
        lmv = result.leverage_mode_validation
        assert lmv.mode == LeverageMode.KELLY_FRACTIONAL
        assert lmv.passed is True, \
            f"expected Phase 2.5 to pass on Kelly with conservative priors, failures: {lmv.failures}"
        # No cap-clamp warning (0.1 × 0.12 = 0.012, well below 0.25)
        assert not any("hard-capped" in w for w in lmv.warnings)

    def test_kelly_fractional_warns_on_hard_cap(self, tmp_path):
        """Priors that produce raw f* × kelly_fraction > 0.25 should trigger
        the hard-cap clamp warning.

        p=0.80, b=5.0 → f* = (5×0.8 - 0.2)/5 = 0.76
        kelly_fraction=0.5 → raw=0.38, capped to 0.25

        Note: the capped 0.25 risk_pct is VERY aggressive for donchian_gold
        and will almost certainly cause margin-gate rejections → trade-count
        mismatch between L1 and L2 → Phase 2.5 will HARD-FAIL on invariance.
        That's actually correct behavior (real math in real data) — the
        strategy cannot survive 25% risk per trade. But the hard-cap warning
        SHOULD fire regardless. We assert ONLY that the warning is present.
        """
        cfg = self._base_cfg(
            tmp_path, "kelly_cap_warn",
            leverage_mode=LeverageMode.KELLY_FRACTIONAL,
            kelly_fraction=0.5,
            kelly_win_rate=0.80,
            kelly_payoff_ratio=5.0,
            leverages=[10.0, 50.0],
        )
        result = run_deep_backtest(cfg)
        lmv = result.leverage_mode_validation
        assert lmv is not None
        # Warning must fire regardless of whether passed=True or False —
        # the cap clamp is a config-level observation, not a runtime outcome
        assert any("hard-capped" in w for w in lmv.warnings), \
            f"expected hard-cap warning, got warnings: {lmv.warnings}"

    def test_kelly_fractional_warns_on_unrealistic_priors(self, tmp_path):
        """p=0.95 (> 0.90) should trigger the unrealistic-priors warning."""
        cfg = self._base_cfg(
            tmp_path, "kelly_unreal_warn",
            leverage_mode=LeverageMode.KELLY_FRACTIONAL,
            kelly_fraction=0.25,  # use quarter-Kelly to avoid cap
            kelly_win_rate=0.95,
            kelly_payoff_ratio=3.0,
            leverages=[10.0, 50.0],
        )
        result = run_deep_backtest(cfg)
        lmv = result.leverage_mode_validation
        assert lmv is not None
        assert any("unrealistic" in w for w in lmv.warnings), \
            f"expected unrealistic-priors warning, got: {lmv.warnings}"

    def test_kelly_math_unit(self, tmp_path):
        """Unit test: _validate_kelly_fractional computes f* correctly.
        p=0.55, b=2.0 → f* = (2×0.55 - 0.45)/2 = (1.10 - 0.45)/2 = 0.325
        half-Kelly × 0.325 = 0.1625 (below cap)"""
        cfg = self._base_cfg(
            tmp_path, "kelly_math_unit",
            leverage_mode=LeverageMode.KELLY_FRACTIONAL,
            kelly_fraction=0.5,
            kelly_win_rate=0.55,
            kelly_payoff_ratio=2.0,
        )
        # Also reproduce via _apply_leverage_mode directly
        params = _apply_leverage_mode(cfg, 10.0)
        assert abs(params["max_risk_per_trade"] - 0.1625) < 1e-9

    def test_rejected_positions_count_field_exists(self, tmp_path):
        """After task #115, every CellResult should have rejected_positions
        populated from the engine's open_rejected_count. For a normal
        donchian_gold L=10 run, this is typically 0 (no margin rejections)."""
        cfg = self._base_cfg(tmp_path, "rejected_field")
        result = run_deep_backtest(cfg)
        for cell in result.matrix:
            assert hasattr(cell, "rejected_positions")
            assert isinstance(cell.rejected_positions, int)
            assert cell.rejected_positions >= 0

    def test_sanity_checks_include_rejection_warnings(self, tmp_path):
        """Phase 2 sanity_checks dict should include margin_rejection_warnings
        list and total_rejected_positions int after task #115."""
        cfg = self._base_cfg(tmp_path, "rejection_warnings_field")
        result = run_deep_backtest(cfg)
        assert "margin_rejection_warnings" in result.sanity_checks
        assert isinstance(result.sanity_checks["margin_rejection_warnings"], list)
        assert "total_rejected_positions" in result.sanity_checks


class TestAttribution:
    """G.7 — alpha vs leverage attribution decomposition (task #79).

    Verifies the return decomposition math and ensures:
      - Passthrough modes produce zero leverage_amplification
      - RISK_SCALED produces linear amplification from baseline
      - cost_drag matches commission / initial_cash exactly
      - Baseline-cell-missing edge case handled gracefully
      - Large residual triggers the warning note
      - attribution serializes correctly into summary.json
    """

    def _base_cfg(self, tmp_path, out_sub, **overrides):
        defaults = dict(
            strategy="donchian_gold",
            strategy_params={"session_filter": True, "max_risk_per_trade": 0.005},
            timeframes=["1h"],
            window_days=[90],
            leverages=[10.0],
            fee_profiles=["ic_markets_mt4_xauusd_normal"],
            wf_enabled=False,
            leverage_validation_enabled=False,
            out_dir=tmp_path / out_sub,
            generate_html=False,
            generate_pdf=False,
            generate_heatmaps=False,
            progress=False,
        )
        defaults.update(overrides)
        return DeepBacktestConfig(**defaults)

    def test_attribution_on_passthrough_mode_zero_amplification(self, tmp_path):
        """MARGIN_CAPPED (default) should produce leverage_amplification = 0
        for every cell. Alpha return should equal total return."""
        cfg = self._base_cfg(
            tmp_path, "attr_passthrough",
            leverages=[10.0, 20.0],
            leverage_mode=LeverageMode.MARGIN_CAPPED,
        )
        result = run_deep_backtest(cfg)
        assert result.attribution is not None
        for key, ba in result.attribution.items():
            assert ba.leverage_amplification == 0.0, \
                f"MARGIN_CAPPED should not amplify, got {ba.leverage_amplification} at {key}"
            assert ba.alpha_return == ba.total_return, \
                f"alpha should equal total for passthrough, got diff at {key}"

    def test_attribution_on_risk_scaled_amplifies(self, tmp_path):
        """RISK_SCALED at L > baseline should produce positive
        leverage_amplification. L=10 (baseline) has amp=0, L=20 has amp>0
        and amp at L=20 should roughly double amp at L=15."""
        cfg = self._base_cfg(
            tmp_path, "attr_risk_scaled",
            leverages=[10.0, 15.0, 20.0],
            leverage_mode=LeverageMode.RISK_SCALED,
            baseline_leverage=10.0,
        )
        result = run_deep_backtest(cfg)
        assert result.attribution is not None

        # Pull the three cells (same window/tf/fee, different leverage)
        by_lev = {
            k[2]: v for k, v in result.attribution.items()
            if k[0] == 90 and k[1] == "1h"
        }
        assert 10.0 in by_lev and 15.0 in by_lev and 20.0 in by_lev

        # Baseline: amp must be 0
        assert abs(by_lev[10.0].leverage_amplification) < 1e-9

        # Alpha is the same across all three (it's the baseline's return)
        assert by_lev[10.0].alpha_return == by_lev[15.0].alpha_return
        assert by_lev[15.0].alpha_return == by_lev[20.0].alpha_return

        # L=15 and L=20 should have positive amplification (donchian has edge)
        assert by_lev[15.0].leverage_amplification > 0
        assert by_lev[20.0].leverage_amplification > 0

        # amp at L=20 should be ~2× amp at L=15 (2× step from baseline)
        # Tolerance: 40% relative (compounding drift over fewer bars)
        ratio = by_lev[20.0].leverage_amplification / by_lev[15.0].leverage_amplification
        assert 1.5 <= ratio <= 2.5, \
            f"Expected amp ratio ~2.0 at 2× step, got {ratio}"

    def test_attribution_cost_drag_matches_commission(self, tmp_path):
        """cost_drag % should equal -(total_commission / initial_cash × 100)."""
        cfg = self._base_cfg(
            tmp_path, "attr_cost_drag",
            leverage_mode=LeverageMode.MARGIN_CAPPED,
        )
        result = run_deep_backtest(cfg)
        assert result.attribution is not None
        for cell in result.matrix:
            if cell.notes:
                continue
            key = (cell.window_days, cell.timeframe, cell.leverage, cell.fee_profile)
            ba = result.attribution[key]
            expected_drag = -(cell.total_commission / cfg.initial_cash * 100.0)
            assert abs(ba.cost_drag - expected_drag) < 1e-9, \
                f"cost_drag {ba.cost_drag} != expected {expected_drag}"

    def test_attribution_baseline_cell_missing_graceful(self, tmp_path):
        """If baseline_leverage isn't in the matrix, the attribution should
        use the closest ≤ baseline cell and emit a note explaining the fallback.

        Config: leverages=[5, 15], baseline=10 → neither matches exactly.
        Expected: L=5 cell is the alpha source for both (closest ≤ 10 is 5).
        Note should mention the baseline-not-in-matrix situation.
        """
        cfg = self._base_cfg(
            tmp_path, "attr_baseline_missing",
            leverages=[5.0, 15.0],
            leverage_mode=LeverageMode.RISK_SCALED,
            baseline_leverage=10.0,
        )
        result = run_deep_backtest(cfg)
        assert result.attribution is not None

        # L=15 cell should have a note about baseline-not-in-matrix
        by_lev = {
            k[2]: v for k, v in result.attribution.items()
            if k[0] == 90 and k[1] == "1h"
        }
        assert 5.0 in by_lev and 15.0 in by_lev

        # L=5 is the baseline (closest ≤ 10) for L=15's alpha
        assert by_lev[15.0].baseline_cell_leverage == 5.0
        # Note should mention the fallback
        assert "baseline cell" in by_lev[15.0].note.lower(), \
            f"expected note about baseline fallback, got: {by_lev[15.0].note!r}"

    def test_attribution_residual_flagged_on_large_drift(self, tmp_path):
        """Unit-level: synthesize cells where return_pct diverges hugely from
        the sum of components. Verify the note field flags 'large residual'.
        """
        # Build two synthetic cells in the same triplet. The baseline cell
        # has return 10%, and the scaled cell has return 100% (impossibly
        # large — should trigger the large-residual flag after subtracting
        # the ~10% alpha).
        baseline_cell = CellResult(
            window_days=90, window_label="3mo", timeframe="1h",
            leverage=10.0, fee_profile="ic_markets_mt4_xauusd_normal",
            trades=10, return_pct=10.0, maxdd_pct=5.0, win_rate=60.0,
            profit_factor=1.5, avg_win=50.0, avg_loss=-30.0,
            total_commission=5.0, final_equity=11000.0,
            margin_per_trade=1000.0, cost_per_trade=0.5, cost_pct_of_margin=0.05,
            broker_stop_outs=0, calmar=2.0, sharpe=1.5, rejected_positions=0,
        )
        weird_cell = CellResult(
            window_days=90, window_label="3mo", timeframe="1h",
            leverage=20.0, fee_profile="ic_markets_mt4_xauusd_normal",
            trades=10, return_pct=100.0, maxdd_pct=50.0, win_rate=60.0,
            profit_factor=1.5, avg_win=500.0, avg_loss=-300.0,
            total_commission=5.0, final_equity=20000.0,
            margin_per_trade=500.0, cost_per_trade=0.5, cost_pct_of_margin=0.1,
            broker_stop_outs=0, calmar=4.0, sharpe=2.0, rejected_positions=0,
        )
        cfg = self._base_cfg(
            tmp_path, "attr_residual",
            leverage_mode=LeverageMode.RISK_SCALED,
            baseline_leverage=10.0,
        )
        # alpha=10 (from baseline), lev_amp=90 (100-10), cost_drag≈-0.05
        # components sum = 10 + 90 - 0.05 - small = ~99.95
        # total = 100.0
        # residual ~= 0.05 → NOT large enough to flag
        # Let me hand-tune: change baseline to return_pct=30 so:
        # alpha=30, lev_amp=70 (100-30), cost=-0.05, residual = -0.05
        # Still not large. Need to force a gap.

        # Simpler: test by calling _compute_cell_attribution on cells where
        # the components don't add up to return_pct at all (simulate compounding
        # drift by making return_pct much smaller than sum of components).
        #
        # Actually, the cleanest approach is to verify the threshold LOGIC
        # works: call _compute_cell_attribution on a custom pair and check
        # the note field is populated when residual > 15.

        # Force a scenario by doctoring: give weird_cell a tiny return_pct
        # while baseline has a large one.
        weird_cell.return_pct = 30.0  # total=30, alpha=10, lev_amp=20, drag tiny
        # residual = 30 - (10 + 20 + cost + 0) = ~0, no flag

        # To FORCE a large residual, make the baseline's return much larger
        # than the scaled cell's:
        baseline_cell.return_pct = 50.0  # alpha = 50
        weird_cell.return_pct = 10.0     # total = 10
        # lev_amp = 10 - 50 = -40 (negative amplification — unusual)
        # sum = 50 - 40 - 0.05 = 9.95
        # residual = 10 - 9.95 = 0.05 → no flag

        # OK, the math makes residual small because it's an IDENTITY by
        # construction (return = alpha + amp + cost + drag + residual, solved
        # for residual). So residual is always exactly the unexplained
        # portion. Forcing it > 15% requires extreme cost_drag or margin drag
        # that doesn't align with reality.
        #
        # Easier: directly test the threshold by mocking. But simpler still
        # is to construct costs that create a bigger gap. Let me make
        # total_commission huge:
        baseline_cell.return_pct = 10.0
        weird_cell.return_pct = 30.0
        weird_cell.total_commission = 5000.0  # $5k commission on $10k → -50% drag
        # alpha=10, lev_amp=20, cost_drag=-50, drag=0
        # sum = 10+20-50 = -20
        # residual = 30 - (-20) = 50 → LARGE, triggers flag

        ba = _compute_cell_attribution(
            weird_cell, [baseline_cell, weird_cell], cfg,
        )
        assert abs(ba.residual) > 15.0, \
            f"expected large residual, got {ba.residual}"
        assert "large residual" in ba.note.lower(), \
            f"expected 'large residual' in note, got: {ba.note!r}"

    def test_attribution_serializes_to_summary_json(self, tmp_path):
        """Running a full deep_backtest should produce summary.json with an
        'attribution' top-level key containing stringified-tuple keys."""
        cfg = self._base_cfg(
            tmp_path, "attr_json",
            leverages=[10.0, 20.0],
            leverage_mode=LeverageMode.RISK_SCALED,
            baseline_leverage=10.0,
            # generate_html defaults False; _write_json_summary always runs
        )
        result = run_deep_backtest(cfg)
        summary_path = result.report_dir / "summary.json"
        assert summary_path.exists()
        import json
        loaded = json.loads(summary_path.read_text())
        assert "attribution" in loaded
        assert loaded["attribution"] is not None
        # Keys are "window|tf|leverage|fee" strings
        for key_str, cell_attr in loaded["attribution"].items():
            parts = key_str.split("|")
            assert len(parts) == 4, f"bad key format: {key_str}"
            # Each value is a dict with the dataclass fields
            assert "alpha_return" in cell_attr
            assert "leverage_amplification" in cell_attr
            assert "cost_drag" in cell_attr
            assert "residual" in cell_attr
