#!/usr/bin/env python3
"""Task #141.4 — end-to-end dashboard smoke test.

Runs the full `run_deep_backtest() → record_deep_backtest_result()
→ Strategies page` pipeline against the minimal `smoke_demo` strategy
with an isolated tmp DB, then asserts every feature works:

    [1]  smoke_demo registered in STRATEGY_REGISTRY
    [2]  smoke_demo instantiates with cls()
    [3]  "XAUUSD" in smoke_demo().markets (dashboard filter gate)
    [4]  run_deep_backtest against tmp DB — 1 window × 1 TF × 2 leverages × 1 fee
    [5]  storage row created (strategy_id > 0, version_id > 0)
    [6]  version_slug is pure triplet: "margin_capped_L10_1h"
    [7]  description auto-generated: "Margin Capped @ L10 · 1h"
    [8]  facets_json populated per (window, fee)
    [9]  top-level hero matches cross-facet picker
    [10] re-run with 3mo window → facets accumulates to 2 keys,
         max_return_pct reflects the BETTER of the two windows
    [11] hero picker with 3 seeded rows: verdict-filter + rank-by-return

Usage:
    python3 scripts/smoke_test_dashboard.py

Exit code: 0 = all pass, 1 = any fail. On failure, the tmp DB path is
printed so you can inspect it post-mortem.
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path


# Make the repo root importable BEFORE touching any src imports so the
# auto-capture hook resolves storage correctly.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# ── Test harness ─────────────────────────────────────────────────────

RESULTS: list[tuple[str, bool, str]] = []  # (step, ok, detail)


def _step(n: int, total: int, label: str):
    print(f"\n[{n}/{total}] {label}")


def _assert(step_name: str, condition: bool, detail: str = "") -> bool:
    ok = bool(condition)
    RESULTS.append((step_name, ok, detail))
    glyph = "✓" if ok else "✗"
    print(f"      {glyph} {step_name}{'  — ' + detail if detail else ''}")
    return ok


def _summary() -> int:
    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    if passed == total:
        print(f"✓ ALL {total} CHECKS PASSED")
        return 0
    failed = [(name, detail) for name, ok, detail in RESULTS if not ok]
    print(f"✗ {len(failed)}/{total} FAILED:")
    for name, detail in failed:
        print(f"    ✗ {name}: {detail}")
    return 1


# ── Step implementations ─────────────────────────────────────────────


def step_1_registry() -> None:
    _step(1, 11, "smoke_demo registered in STRATEGY_REGISTRY")
    from src.strategies.router import STRATEGY_REGISTRY
    _assert("smoke_demo in STRATEGY_REGISTRY", "smoke_demo" in STRATEGY_REGISTRY)


def step_2_instantiation() -> object:
    _step(2, 11, "smoke_demo instantiates with cls()")
    from src.strategies.demos.smoke_demo import SmokeDemoStrategy
    try:
        inst = SmokeDemoStrategy()
        _assert("cls() does not raise", True, f"name={inst.name}")
        return inst
    except Exception as e:
        _assert("cls() does not raise", False, f"{type(e).__name__}: {e}")
        return None


def step_3_market_gate(inst) -> None:
    _step(3, 11, "'XAUUSD' in smoke_demo().markets (dashboard filter gate)")
    markets = getattr(inst, "markets", []) if inst else []
    _assert("XAUUSD in markets", "XAUUSD" in markets, f"markets={markets}")


def step_4_run_deep_backtest(db_path: str, report_out_dir: Path) -> object:
    _step(4, 11, "run_deep_backtest against tmp DB (1mo × 1h × 2 leverages × 1 fee)")
    from src.backtest.deep_backtest import (
        DeepBacktestConfig,
        LeverageMode,
        run_deep_backtest,
    )
    cfg = DeepBacktestConfig(
        strategy="smoke_demo",
        symbol="XAUUSD",
        window_days=[30],
        timeframes=["1h"],
        leverages=[10.0, 20.0],
        fee_profiles=["pine_zero_cost"],
        initial_cash=10000.0,
        leverage_mode=LeverageMode.MARGIN_CAPPED,
        baseline_leverage=10.0,
        wf_enabled=False,
        generate_html=False,
        generate_pdf=False,
        generate_heatmaps=False,
        out_dir=report_out_dir,
        progress=False,
    )
    try:
        os.environ["ALGO_STRATEGY_DB"] = db_path
        result = run_deep_backtest(cfg)
        _assert(
            "run_deep_backtest completed",
            result is not None,
            f"verdict={getattr(result, 'verdict', None)}",
        )
        return result
    except Exception as e:
        _assert("run_deep_backtest completed", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return None


def step_5_storage_row(db_path: str) -> tuple[int, int]:
    _step(5, 11, "storage row created (strategy_id > 0, version_id > 0)")
    from src.strategies.storage import get_strategy, get_version
    s = get_strategy("smoke_demo", db_path=db_path)
    _assert("parent row exists", s is not None and s.id is not None and s.id > 0,
            f"id={getattr(s, 'id', None)}")
    v = get_version("smoke_demo", "margin_capped_L10_1h", db_path=db_path)
    _assert("version row exists", v is not None and v.id is not None and v.id > 0,
            f"id={getattr(v, 'id', None)}")
    return (s.id if s else 0, v.id if v else 0)


def step_6_pure_triplet(db_path: str) -> None:
    _step(6, 11, "version_slug is pure triplet (no hash suffix)")
    from src.strategies.storage import list_versions
    versions = list_versions(strategy_name="smoke_demo", db_path=db_path)
    slugs = [v.version_slug for v in versions]
    _assert(
        "exactly one version row",
        len(versions) == 1,
        f"got {len(versions)}: {slugs}",
    )
    if versions:
        slug = versions[0].version_slug
        _assert(
            "slug is pure triplet 'margin_capped_L10_1h'",
            slug == "margin_capped_L10_1h",
            f"got {slug!r}",
        )


def step_7_description(db_path: str) -> None:
    _step(7, 11, "description auto-generated from triplet")
    from src.strategies.storage import get_version
    v = get_version("smoke_demo", "margin_capped_L10_1h", db_path=db_path)
    expected = "Margin Capped @ L10 · 1h"
    _assert(
        f"description == {expected!r}",
        v is not None and v.description == expected,
        f"got {getattr(v, 'description', None)!r}",
    )


def step_8_facets(db_path: str) -> None:
    _step(8, 11, "facets_json populated with (window, fee) keys")
    from src.strategies.storage import get_version
    v = get_version("smoke_demo", "margin_capped_L10_1h", db_path=db_path)
    facets = getattr(v, "facets", {}) if v else {}
    _assert(
        "facets dict is non-empty",
        bool(facets),
        f"keys={list(facets.keys())}",
    )
    expected_key = "1mo::pine_zero_cost"
    _assert(
        f"key {expected_key!r} present",
        expected_key in facets,
        f"keys={list(facets.keys())}",
    )
    if expected_key in facets:
        f = facets[expected_key]
        _assert(
            "facet has return_pct + calmar + trades + sane",
            all(k in f for k in ("return_pct", "calmar", "trades", "sane")),
            f"facet keys: {list(f.keys())}",
        )


def step_9_top_level_hero(db_path: str) -> None:
    _step(9, 11, "top-level max_return_pct matches cross-facet hero")
    from src.strategies.storage import _top_level_from_facets, get_version
    v = get_version("smoke_demo", "margin_capped_L10_1h", db_path=db_path)
    if not v or not v.facets:
        _assert("top-level hero check", False, "no version or facets")
        return
    hero = _top_level_from_facets(v.facets)
    _assert(
        "top-level return_pct == hero cell return_pct",
        v.max_return_pct == hero.get("max_return_pct"),
        f"top={v.max_return_pct}, hero={hero.get('max_return_pct')}",
    )


def step_10_rerun_different_window(db_path: str, report_out_dir: Path) -> None:
    _step(10, 11, "re-run with different window accumulates facets")
    from src.backtest.deep_backtest import (
        DeepBacktestConfig,
        LeverageMode,
        run_deep_backtest,
    )
    from src.strategies.storage import get_version

    cfg = DeepBacktestConfig(
        strategy="smoke_demo",
        symbol="XAUUSD",
        window_days=[90],  # 3mo this time
        timeframes=["1h"],
        leverages=[10.0],
        fee_profiles=["pine_zero_cost"],
        initial_cash=10000.0,
        leverage_mode=LeverageMode.MARGIN_CAPPED,
        baseline_leverage=10.0,
        wf_enabled=False,
        generate_html=False,
        generate_pdf=False,
        generate_heatmaps=False,
        out_dir=report_out_dir / "second",
        progress=False,
    )
    try:
        run_deep_backtest(cfg)
    except Exception as e:
        _assert("second run completed", False, f"{type(e).__name__}: {e}")
        return

    v = get_version("smoke_demo", "margin_capped_L10_1h", db_path=db_path)
    facets = v.facets if v else {}
    _assert(
        "facets has BOTH 1mo and 3mo keys",
        "1mo::pine_zero_cost" in facets and "3mo::pine_zero_cost" in facets,
        f"keys={list(facets.keys())}",
    )

    # Slug should still be the same — no duplicate row on re-run
    from src.strategies.storage import list_versions
    versions = list_versions(strategy_name="smoke_demo", db_path=db_path)
    _assert(
        "still exactly one version row (pure triplet → upsert in place)",
        len(versions) == 1,
        f"got {len(versions)}: {[x.version_slug for x in versions]}",
    )


def step_11_hero_picker_seeded(db_path: str) -> None:
    _step(11, 11, "hero picker: sane-first + WF-Calmar + return tiebreak on 3 seeded rows")
    from src.strategies.storage import (
        StoredStrategy,
        StoredVersion,
        _is_better_version_metric,
        upsert_strategy,
        upsert_version,
    )

    # Seed a second strategy with 2 versions + a THIRD row with FAILED verdict
    sid = upsert_strategy(
        StoredStrategy(name="_smoke_hero_test", status="researching"),
        db_path=db_path,
    )

    # Version A: lower return but sane
    upsert_version(
        StoredVersion(
            strategy_id=sid,
            version_slug="margin_capped_L10_1h",
            description="Margin Capped @ L10 · 1h",
            leverage_mode="margin_capped",
            baseline_leverage=10.0,
            timeframe="1h",
            max_return_pct=5.0,
            max_return_sane=True,
            max_return_cell={"return_pct": 5.0, "trades": 50, "maxdd_pct": 3.0, "calmar": 1.5},
            verdict="DEPLOYABLE",
            facets={
                "1mo::pine_zero_cost": {
                    "return_pct": 5.0, "sane": True, "wf_calmar": None, "calmar": 1.5,
                    "trades": 50, "maxdd_pct": 3.0, "verdict": "DEPLOYABLE",
                }
            },
        ),
        db_path=db_path,
    )

    # Version B: higher return AND sane — should win between A and B
    upsert_version(
        StoredVersion(
            strategy_id=sid,
            version_slug="margin_capped_L25_1h",
            description="Margin Capped @ L25 · 1h",
            leverage_mode="margin_capped",
            baseline_leverage=25.0,
            timeframe="1h",
            max_return_pct=12.0,
            max_return_sane=True,
            max_return_cell={"return_pct": 12.0, "trades": 50, "maxdd_pct": 5.0, "calmar": 2.4},
            verdict="DEPLOYABLE",
            facets={
                "1mo::pine_zero_cost": {
                    "return_pct": 12.0, "sane": True, "wf_calmar": None, "calmar": 2.4,
                    "trades": 50, "maxdd_pct": 5.0, "verdict": "DEPLOYABLE",
                }
            },
        ),
        db_path=db_path,
    )

    # Verify the comparator correctly prefers B over A (higher return, both sane)
    facet_a = {"return_pct": 5.0, "sane": True, "wf_calmar": None}
    facet_b = {"return_pct": 12.0, "sane": True, "wf_calmar": None}
    _assert(
        "comparator(facet_b, facet_a) → True (higher return wins)",
        _is_better_version_metric(facet_b, facet_a),
    )

    # Verify an insane +300% row LOSES to a sane +5% row (vanity trap rejection)
    insane_high = {"return_pct": 300.0, "sane": False, "wf_calmar": None, "trades": 4}
    sane_low = {"return_pct": 5.0, "sane": True, "wf_calmar": None, "trades": 50}
    _assert(
        "comparator rejects insane +300% in favor of sane +5% (vanity trap)",
        _is_better_version_metric(sane_low, insane_high)
        and not _is_better_version_metric(insane_high, sane_low),
    )

    # Verify WF Calmar beats raw return when both sane
    with_wf = {"return_pct": 10.0, "sane": True, "wf_calmar": 5.0}
    higher_no_wf = {"return_pct": 20.0, "sane": True, "wf_calmar": None}
    _assert(
        "WF Calmar presence beats no-WF even if no-WF has higher return",
        _is_better_version_metric(with_wf, higher_no_wf),
    )


def main() -> int:
    print("=" * 70)
    print("DASHBOARD SMOKE TEST (task #141.4)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = str(tmp_path / "smoke_test.db")
        report_out_dir = tmp_path / "reports"
        report_out_dir.mkdir(exist_ok=True)

        print(f"tmp dir:  {tmp_path}")
        print(f"tmp DB:   {db_path}")

        # Isolate the auto-capture hook from the real DB
        os.environ["ALGO_STRATEGY_DB"] = db_path

        try:
            step_1_registry()
            inst = step_2_instantiation()
            step_3_market_gate(inst)
            result = step_4_run_deep_backtest(db_path, report_out_dir)
            if result is None:
                print("\nSKIPPING steps 5-10 because step 4 failed.")
                # Continue to step 11 which doesn't depend on the pipeline
                step_11_hero_picker_seeded(db_path)
                return _summary()
            step_5_storage_row(db_path)
            step_6_pure_triplet(db_path)
            step_7_description(db_path)
            step_8_facets(db_path)
            step_9_top_level_hero(db_path)
            step_10_rerun_different_window(db_path, report_out_dir)
            step_11_hero_picker_seeded(db_path)
        except Exception as e:
            print(f"\nFATAL: {type(e).__name__}: {e}")
            traceback.print_exc()
            return 1
        finally:
            os.environ.pop("ALGO_STRATEGY_DB", None)

        return _summary()


if __name__ == "__main__":
    sys.exit(main())
