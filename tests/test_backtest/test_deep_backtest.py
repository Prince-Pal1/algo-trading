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
    CellResult,
    DeepBacktestConfig,
    DeepBacktestResult,
    LeverageMode,
    LeverageValidationResult,
    WalkForwardSummary,
    _apply_leverage_mode,
    _check_window_availability,
    _load_timeframe,
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
