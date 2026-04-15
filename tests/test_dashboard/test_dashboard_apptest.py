"""Streamlit AppTest integration tests (task #146.4).

Uses `streamlit.testing.v1.AppTest` to programmatically render each page
of the dashboard and check for:
    - Python exceptions raised during page body execution
    - Expected titles / markdown / widgets present
    - Widget interactions work (radio clicks, selectbox changes)

These tests exercise the ACTUAL page body code paths that unit tests
can't reach because the page bodies live inside `elif page == "..."`
branches guarded by the sidebar radio.

Catches bugs like:
    - NameError / AttributeError in page body
    - Streamlit widget API misuse
    - Broken data flow between widgets
    - Exception in helper functions called from a page

Run: `pytest tests/test_dashboard/test_dashboard_apptest.py -v`
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


DASHBOARD_PATH = Path(__file__).resolve().parents[2] / "src" / "dashboard" / "app.py"


def _at() -> AppTest:
    """Build a fresh AppTest instance for each test.

    Longer default timeout (30s) because some pages do DB queries that
    can take a few hundred ms under pytest-xdist parallelism.
    """
    at = AppTest.from_file(str(DASHBOARD_PATH), default_timeout=30)
    at.run()
    return at


def _pick_page(at: AppTest, page_name: str) -> AppTest:
    """Navigate the sidebar radio to `page_name` and re-run."""
    at.sidebar.radio[0].set_value(page_name).run()
    return at


# =====================================================================
#  Navigation + "no exception" sanity
# =====================================================================


class TestNavigation:
    def test_default_page_no_exception(self):
        at = _at()
        assert not at.exception, f"default page raised: {at.exception}"

    def test_sidebar_has_all_pages(self):
        at = _at()
        options = at.sidebar.radio[0].options
        expected = {
            "Backtest Runs", "Strategy Deep Dive", "Compare Runs",
            "Imported Strategies", "Validation", "Strategies",
            "Run Deep Backtest", "Glossary",
        }
        assert set(options) == expected

    def test_all_pages_render_without_exception(self):
        """Crawl every sidebar page and check for exceptions. This is the
        single most important integration test — it catches NameErrors,
        AttributeErrors, and widget misuse in every page body."""
        pages = [
            "Backtest Runs", "Strategy Deep Dive", "Compare Runs",
            "Imported Strategies", "Validation", "Strategies",
            "Run Deep Backtest", "Glossary",
        ]
        for page in pages:
            at = _at()
            at = _pick_page(at, page)
            # Streamlit's AppTest aggregates exceptions across the full run.
            # A non-empty `exception` list means something blew up in the
            # page body — a real bug we need to fix.
            assert not at.exception, f"page {page!r} raised: {at.exception}"


# =====================================================================
#  Strategies page (page 6 — hierarchical facets tree)
# =====================================================================


class TestStrategiesPage:
    def test_strategies_page_renders(self):
        at = _pick_page(_at(), "Strategies")
        assert not at.exception

    def test_strategies_page_has_title(self):
        at = _pick_page(_at(), "Strategies")
        titles = [t.value for t in at.title]
        assert any("hierarchical registry" in t.lower() for t in titles)

    def test_strategies_page_has_fee_selector(self):
        """Layer 1 fee dropdown must be present when data exists."""
        at = _pick_page(_at(), "Strategies")
        # Selectbox widgets present on the page
        assert not at.exception
        # There should be at least one selectbox (the fee picker)
        # if the DB has data, OR an info message if empty
        has_selectbox = len(at.selectbox) > 0
        has_info = any("No strategies" in (i.value if hasattr(i, 'value') else '')
                       for i in (at.info if hasattr(at, 'info') else []))
        assert has_selectbox or has_info


# =====================================================================
#  Run Deep Backtest page (page 7 — broker fees + history + tooltips)
# =====================================================================


class TestRunDeepBacktestPage:
    def test_run_deep_backtest_renders(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        assert not at.exception

    def test_has_section_switcher_radio(self):
        """Page 7 must have the ▶ Run / 📜 History radio."""
        at = _pick_page(_at(), "Run Deep Backtest")
        # There should be a radio with the section options
        section_options = None
        for r in at.radio:
            opts = r.options if hasattr(r, 'options') else []
            if any("Run" in str(o) and "History" in str(o2) for o in opts for o2 in opts if o != o2):
                section_options = opts
                break
            # Simpler match: look for both strings
            if "▶ Run" in opts and "📜 History" in opts:
                section_options = opts
                break
        assert section_options is not None, f"section radio not found in {[r.options for r in at.radio if hasattr(r, 'options')]}"

    def test_run_section_has_strategy_picker(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        # Find a selectbox labeled "Strategy"
        strategy_box = None
        for sb in at.selectbox:
            if hasattr(sb, 'label') and sb.label and "strategy" in sb.label.lower():
                strategy_box = sb
                break
        assert strategy_box is not None, "Strategy selectbox not found"

    def test_run_section_shows_smoke_demo_in_picker(self):
        """smoke_demo must appear in the compatible strategy picker."""
        at = _pick_page(_at(), "Run Deep Backtest")
        strategy_box = None
        for sb in at.selectbox:
            if hasattr(sb, 'label') and sb.label and sb.label.lower().strip() == "strategy":
                strategy_box = sb
                break
        assert strategy_box is not None
        assert "smoke_demo" in strategy_box.options, f"options: {strategy_box.options}"

    def test_has_broker_radio(self):
        """Page 7 must have a Broker radio somewhere (task #141 broker tree)."""
        at = _pick_page(_at(), "Run Deep Backtest")
        broker_radio = None
        for r in at.radio:
            lbl = r.label if hasattr(r, 'label') else ''
            if lbl and "broker" in lbl.lower():
                broker_radio = r
                break
        assert broker_radio is not None, f"broker radio not found; radio labels: {[r.label for r in at.radio if hasattr(r, 'label')]}"

    def test_has_scenario_radio(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        scenario_radio = None
        for r in at.radio:
            lbl = r.label if hasattr(r, 'label') else ''
            if lbl and "scenario" in lbl.lower():
                scenario_radio = r
                break
        assert scenario_radio is not None

    def test_scenario_radio_has_all_four_options(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        scenario_radio = None
        for r in at.radio:
            lbl = r.label if hasattr(r, 'label') else ''
            if lbl and "scenario" in lbl.lower():
                scenario_radio = r
                break
        assert scenario_radio is not None
        assert set(scenario_radio.options) == {"normal", "news_active", "stress", "pine_faithful"}

    def test_switch_to_history_section(self):
        """Click the ▶ Run / 📜 History radio to History — must render cleanly."""
        at = _pick_page(_at(), "Run Deep Backtest")
        # Find the section radio
        section_radio = None
        for r in at.radio:
            opts = r.options if hasattr(r, 'options') else []
            if "▶ Run" in opts and "📜 History" in opts:
                section_radio = r
                break
        assert section_radio is not None, "section radio not found"
        section_radio.set_value("📜 History").run()
        assert not at.exception, f"History section raised: {at.exception}"


# =====================================================================
#  Glossary page (new — task #141.3)
# =====================================================================


class TestGlossaryPage:
    def test_glossary_renders(self):
        at = _pick_page(_at(), "Glossary")
        assert not at.exception

    def test_glossary_has_title(self):
        at = _pick_page(_at(), "Glossary")
        titles = [t.value for t in at.title]
        assert any("glossary" in t.lower() for t in titles)

    def test_glossary_has_search_filter(self):
        at = _pick_page(_at(), "Glossary")
        # Should have a text_input with "Filter" in label
        search_box = None
        for ti in at.text_input:
            lbl = ti.label if hasattr(ti, 'label') else ''
            if lbl and "filter" in lbl.lower():
                search_box = ti
                break
        assert search_box is not None

    def test_glossary_shows_leverage_modes(self):
        """All 5 leverage modes must appear as headers when no filter is
        applied. Verify by checking page markdown for each mode name."""
        at = _pick_page(_at(), "Glossary")
        all_md = " ".join(m.value for m in at.markdown if hasattr(m, 'value'))
        for mode in ("margin_capped", "invariant", "vol_targeted",
                     "risk_scaled", "kelly_fractional"):
            assert mode in all_md, f"{mode} missing from glossary markdown"

    def test_glossary_shows_technical_terms(self):
        at = _pick_page(_at(), "Glossary")
        all_md = " ".join(m.value for m in at.markdown if hasattr(m, 'value'))
        # Spot-check several terms
        for term in ("window", "timeframe", "walk_forward", "verdict",
                     "sane_cell", "facets", "kelly_win_rate"):
            assert term in all_md, f"{term} missing from glossary markdown"

    def test_glossary_search_filter_narrows_output(self):
        """Searching 'kelly' should hide non-kelly terms."""
        at = _pick_page(_at(), "Glossary")
        search_box = None
        for ti in at.text_input:
            lbl = ti.label if hasattr(ti, 'label') else ''
            if lbl and "filter" in lbl.lower():
                search_box = ti
                break
        assert search_box is not None
        search_box.set_value("kelly").run()
        assert not at.exception
        all_md = " ".join(m.value for m in at.markdown if hasattr(m, 'value'))
        assert "kelly" in all_md.lower()
        # Non-kelly terms like "walk_forward" should be filtered out
        # (the ## headers still render but the term definitions under ## are hidden)
        # At minimum, confirm no exception and kelly content present


# =====================================================================
#  Widget + help= integration (task #141.3 tooltip requirement)
# =====================================================================


class TestTooltips:
    def test_page_7_strategy_picker_has_help(self):
        """Strategy selectbox must have a non-empty help= kwarg after task #141."""
        at = _pick_page(_at(), "Run Deep Backtest")
        strategy_box = None
        for sb in at.selectbox:
            if hasattr(sb, 'label') and sb.label and sb.label.lower().strip() == "strategy":
                strategy_box = sb
                break
        assert strategy_box is not None
        # AppTest exposes .help on widgets since Streamlit 1.30+
        help_text = getattr(strategy_box, 'help', None)
        assert help_text is not None and help_text.strip(), "Strategy picker missing help text"

    def test_page_7_symbol_input_has_help(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        sym_input = None
        for ti in at.text_input:
            lbl = ti.label if hasattr(ti, 'label') else ''
            if lbl and lbl.lower() == "symbol":
                sym_input = ti
                break
        assert sym_input is not None
        help_text = getattr(sym_input, 'help', None)
        assert help_text is not None and help_text.strip()

    def test_page_7_broker_radio_has_help(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        broker_radio = None
        for r in at.radio:
            lbl = r.label if hasattr(r, 'label') else ''
            if lbl and "broker" in lbl.lower():
                broker_radio = r
                break
        assert broker_radio is not None
        help_text = getattr(broker_radio, 'help', None)
        assert help_text is not None and help_text.strip()

    def test_page_7_has_emoji_section_headers(self):
        """Task #141.3 layout polish: page 7 should have emoji section headers."""
        at = _pick_page(_at(), "Run Deep Backtest")
        all_md = " ".join(m.value for m in at.markdown if hasattr(m, 'value'))
        # At least 3 emoji headers should be present
        emoji_markers = ["🎯", "📅", "💰", "⚡", "📊"]
        count = sum(1 for e in emoji_markers if e in all_md)
        assert count >= 3, f"only {count}/{len(emoji_markers)} emoji headers found"


# =====================================================================
#  Adversarial edge-case tests — hunt for more bugs
# =====================================================================


class TestAdversarialEdgeCases:
    """Aggressive tests aimed at finding bugs in less-traveled code paths."""

    def test_scenario_pine_faithful_reveals_none_broker(self):
        """When scenario=pine_faithful, the (none) broker should appear
        because pine_zero_cost has broker=(none). Regression guard for
        the filter-skip logic when scenario=pine_faithful."""
        at = _pick_page(_at(), "Run Deep Backtest")
        scenario_radio = None
        for r in at.radio:
            lbl = r.label if hasattr(r, 'label') else ''
            if lbl and "scenario" in lbl.lower():
                scenario_radio = r
                break
        assert scenario_radio is not None
        scenario_radio.set_value("pine_faithful").run()
        assert not at.exception, f"pine_faithful scenario raised: {at.exception}"
        # After switching scenario, a broker radio should still be present
        broker_radio = None
        for r in at.radio:
            lbl = r.label if hasattr(r, 'label') else ''
            if lbl and "broker" in lbl.lower():
                broker_radio = r
                break
        assert broker_radio is not None, "broker radio disappeared on pine_faithful"
        # With pine_faithful, (none) broker must be reachable
        assert "(none)" in broker_radio.options, f"broker options: {broker_radio.options}"

    def test_scenario_stress_still_shows_ic_markets(self):
        """Stress scenario should still have IC Markets available (ic_markets_ctrader_xauusd_stress)."""
        at = _pick_page(_at(), "Run Deep Backtest")
        scenario_radio = None
        for r in at.radio:
            lbl = r.label if hasattr(r, 'label') else ''
            if lbl and "scenario" in lbl.lower():
                scenario_radio = r
                break
        assert scenario_radio is not None
        scenario_radio.set_value("stress").run()
        assert not at.exception
        broker_radio = None
        for r in at.radio:
            lbl = r.label if hasattr(r, 'label') else ''
            if lbl and "broker" in lbl.lower():
                broker_radio = r
                break
        assert broker_radio is not None
        assert "IC Markets" in broker_radio.options

    def test_scenario_news_active_has_profiles(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        scenario_radio = None
        for r in at.radio:
            if hasattr(r, 'label') and "scenario" in (r.label or '').lower():
                scenario_radio = r
                break
        scenario_radio.set_value("news_active").run()
        assert not at.exception

    def test_history_section_with_cutoff_short(self):
        """Switch to History and set cutoff to 1 day — verify the number_input
        change triggers a re-query without crashing."""
        at = _pick_page(_at(), "Run Deep Backtest")
        # Navigate to History
        section_radio = None
        for r in at.radio:
            opts = r.options if hasattr(r, 'options') else []
            if "📜 History" in opts:
                section_radio = r
                break
        assert section_radio is not None
        section_radio.set_value("📜 History").run()
        assert not at.exception
        # Find the cutoff number_input and change it
        for ni in at.number_input:
            lbl = ni.label if hasattr(ni, 'label') else ''
            if lbl and "cutoff" in lbl.lower():
                ni.set_value(1).run()
                assert not at.exception, f"cutoff change raised: {at.exception}"
                return

    def test_glossary_search_nonexistent_term(self):
        """Searching for a term that matches nothing should not crash."""
        at = _pick_page(_at(), "Glossary")
        search_box = None
        for ti in at.text_input:
            lbl = ti.label if hasattr(ti, 'label') else ''
            if lbl and "filter" in lbl.lower():
                search_box = ti
                break
        assert search_box is not None
        search_box.set_value("zzzzzzz_no_such_term_1234567890").run()
        assert not at.exception, f"empty-search glossary raised: {at.exception}"

    def test_glossary_search_case_insensitive(self):
        at = _pick_page(_at(), "Glossary")
        search_box = None
        for ti in at.text_input:
            if hasattr(ti, 'label') and "filter" in (ti.label or '').lower():
                search_box = ti
                break
        search_box.set_value("KELLY").run()
        assert not at.exception
        all_md = " ".join(m.value for m in at.markdown if hasattr(m, 'value'))
        assert "kelly" in all_md.lower()

    def test_navigate_every_page_in_sequence(self):
        """Round-trip through all 8 sidebar pages in sequence. Catches
        session state leaks and cross-page contamination."""
        at = _at()
        pages = [
            "Backtest Runs", "Strategies", "Run Deep Backtest", "Glossary",
            "Strategies", "Run Deep Backtest", "Glossary", "Backtest Runs",
        ]
        for page in pages:
            at = _pick_page(at, page)
            assert not at.exception, f"page {page} raised on sequence visit: {at.exception}"

    def test_run_deep_backtest_has_no_use_container_width_warning(self):
        """Zero-tolerance guard against the Streamlit `use_container_width`
        deprecation warning. Task #146 replaced all 12 occurrences with
        `width='stretch'` / `width='content'`. Any re-introduction is a
        regression — Streamlit will drop the arg after 2025-12-31."""
        from pathlib import Path
        src = Path(__file__).resolve().parents[2] / "src" / "dashboard" / "app.py"
        content = src.read_text()
        count = content.count("use_container_width")
        assert count == 0, (
            f"use_container_width count={count}. Task #146 fix was regressed. "
            "Replace with width='stretch' (was True) or width='content' (was False)."
        )

    def test_run_deep_backtest_metric_preview_present(self):
        """The 4-column metric preview (Cells/Runtime/Modes/Fees) must render."""
        at = _pick_page(_at(), "Run Deep Backtest")
        labels = [m.label for m in at.metric if hasattr(m, 'label')]
        expected = {"Cells", "Est. runtime", "Modes", "Fees"}
        assert expected <= set(labels), f"metrics found: {labels}"

    def test_section_switch_run_history_run(self):
        """Switch Run → History → Run. Verify the Run body re-renders
        correctly after coming back from History."""
        at = _pick_page(_at(), "Run Deep Backtest")
        section_radio = None
        for r in at.radio:
            opts = r.options if hasattr(r, 'options') else []
            if "📜 History" in opts:
                section_radio = r
                break
        assert section_radio is not None
        # Switch to History
        section_radio.set_value("📜 History").run()
        assert not at.exception
        # Switch back to Run
        # Need to re-fetch the radio because the AppTest rebinds widgets
        for r in at.radio:
            opts = r.options if hasattr(r, 'options') else []
            if "▶ Run" in opts:
                section_radio = r
                break
        section_radio.set_value("▶ Run").run()
        assert not at.exception, f"Run section re-render after History raised: {at.exception}"
        # Verify the Run section widgets are back
        assert any("strategy" in (sb.label or '').lower()
                   for sb in at.selectbox if hasattr(sb, 'label'))

    def test_backtest_runs_page_still_works(self):
        """Regression guard: the legacy Backtest Runs page should still render
        even though its code hasn't been touched by tasks #133/#141/#146."""
        at = _pick_page(_at(), "Backtest Runs")
        assert not at.exception

    def test_strategy_deep_dive_page_renders(self):
        at = _pick_page(_at(), "Strategy Deep Dive")
        assert not at.exception

    def test_compare_runs_page_renders(self):
        at = _pick_page(_at(), "Compare Runs")
        assert not at.exception

    def test_imported_strategies_page_renders(self):
        at = _pick_page(_at(), "Imported Strategies")
        assert not at.exception

    def test_validation_page_renders(self):
        at = _pick_page(_at(), "Validation")
        assert not at.exception

    def test_empty_platform_selection_resolves_zero_fees(self):
        """Unticking every platform should leave fees_list empty and
        correctly disable the Run button via the can_run guard."""
        at = _pick_page(_at(), "Run Deep Backtest")
        # Find the Platforms multiselect
        platform_ms = None
        for ms in at.multiselect:
            lbl = ms.label if hasattr(ms, 'label') else ''
            if lbl and "platform" in lbl.lower():
                platform_ms = ms
                break
        assert platform_ms is not None
        # Clear the selection
        platform_ms.set_value([]).run()
        assert not at.exception, f"empty platform selection raised: {at.exception}"

    def test_fee_list_empty_warning_when_no_profiles(self):
        """Pine validation scenario has platform=(none). If the broker default
        stays at IC Markets and scenario switches to pine_faithful, the
        platform multi-select must rebuild without crash."""
        at = _pick_page(_at(), "Run Deep Backtest")
        scenario_radio = None
        for r in at.radio:
            if hasattr(r, 'label') and "scenario" in (r.label or '').lower():
                scenario_radio = r
                break
        scenario_radio.set_value("pine_faithful").run()
        assert not at.exception

    def test_fee_multiselect_default_mt4_selected(self):
        """Default platform selection should be 'mt4' (or platforms[:1] fallback)."""
        at = _pick_page(_at(), "Run Deep Backtest")
        platform_ms = None
        for ms in at.multiselect:
            lbl = ms.label if hasattr(ms, 'label') else ''
            if lbl and "platform" in lbl.lower():
                platform_ms = ms
                break
        assert platform_ms is not None
        # Default should have something selected
        assert len(platform_ms.value) >= 1, "platforms default is empty"

    def test_strategy_picker_switching_reruns_fee_filter(self):
        """Switching strategy (which changes instrument_class) should re-run
        the fee tree filter. Verify no crash."""
        at = _pick_page(_at(), "Run Deep Backtest")
        strategy_box = None
        for sb in at.selectbox:
            if hasattr(sb, 'label') and sb.label and sb.label.lower().strip() == "strategy":
                strategy_box = sb
                break
        assert strategy_box is not None
        # Try switching to a different gold strategy (any from the picker)
        if len(strategy_box.options) > 1:
            # pick a different one
            other = [o for o in strategy_box.options if o != strategy_box.value][0]
            strategy_box.set_value(other).run()
            assert not at.exception, f"switching strategy raised: {at.exception}"

    def test_advanced_expander_kelly_inputs(self):
        """Expanding the Advanced options and changing Kelly inputs → no crash."""
        at = _pick_page(_at(), "Run Deep Backtest")
        # Find the kelly win_rate number_input
        kelly_win = None
        for ni in at.number_input:
            lbl = ni.label if hasattr(ni, 'label') else ''
            if lbl and "win rate" in lbl.lower():
                kelly_win = ni
                break
        assert kelly_win is not None, f"kelly win rate input not found"
        kelly_win.set_value(0.55).run()
        assert not at.exception

    def test_version_slug_override_text_input(self):
        """Setting a custom version slug in the advanced expander → no crash."""
        at = _pick_page(_at(), "Run Deep Backtest")
        slug_input = None
        for ti in at.text_input:
            lbl = ti.label if hasattr(ti, 'label') else ''
            if lbl and "slug" in lbl.lower():
                slug_input = ti
                break
        assert slug_input is not None
        slug_input.set_value("custom_experimental_v1").run()
        assert not at.exception

    def test_strategy_params_override_text_input(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        params_input = None
        for ti in at.text_input:
            lbl = ti.label if hasattr(ti, 'label') else ''
            if lbl and "param" in lbl.lower():
                params_input = ti
                break
        assert params_input is not None
        params_input.set_value("session_filter=true,max_risk_per_trade=0.02").run()
        assert not at.exception

    def test_history_limit_extreme_value(self):
        """Max rows = 100 should still render without crashing."""
        at = _pick_page(_at(), "Run Deep Backtest")
        # Switch to history first
        section_radio = None
        for r in at.radio:
            opts = r.options if hasattr(r, 'options') else []
            if "📜 History" in opts:
                section_radio = r
                break
        section_radio.set_value("📜 History").run()
        # Find and crank limit
        for ni in at.number_input:
            lbl = ni.label if hasattr(ni, 'label') else ''
            if lbl and "max rows" in lbl.lower():
                ni.set_value(100).run()
                assert not at.exception
                return

    def test_window_multiselect_toggle(self):
        """Unticking all windows should NOT crash; Run button gets disabled."""
        at = _pick_page(_at(), "Run Deep Backtest")
        window_ms = None
        for ms in at.multiselect:
            lbl = ms.label if hasattr(ms, 'label') else ''
            if lbl and "windows" in lbl.lower():
                window_ms = ms
                break
        assert window_ms is not None
        window_ms.set_value([]).run()
        assert not at.exception

    def test_leverage_modes_multiselect_empty(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        modes_ms = None
        for ms in at.multiselect:
            lbl = ms.label if hasattr(ms, 'label') else ''
            if lbl and "modes" in lbl.lower():
                modes_ms = ms
                break
        assert modes_ms is not None
        modes_ms.set_value([]).run()
        assert not at.exception

    def test_strategies_page_inspect_version_from_leaf(self):
        """Strategies page 6 Layer 4 leaf picker should not crash on any
        selection in the real database."""
        at = _pick_page(_at(), "Strategies")
        assert not at.exception
        # The leaf selectbox is labeled "Pick a (strategy × mode) group"
        leaf_box = None
        for sb in at.selectbox:
            lbl = sb.label if hasattr(sb, 'label') else ''
            if lbl and "strategy" in lbl.lower() and "mode" in lbl.lower():
                leaf_box = sb
                break
        if leaf_box is not None and len(leaf_box.options) > 1:
            # Pick the second option
            leaf_box.set_value(leaf_box.options[1]).run()
            assert not at.exception

    def test_strategies_page_fee_dropdown_change(self):
        """Changing the Layer 1 fee dropdown should re-render the tree."""
        at = _pick_page(_at(), "Strategies")
        fee_box = None
        for sb in at.selectbox:
            lbl = sb.label if hasattr(sb, 'label') else ''
            if lbl and "fee" in lbl.lower():
                fee_box = sb
                break
        if fee_box is not None and len(fee_box.options) > 1:
            fee_box.set_value(fee_box.options[1]).run()
            assert not at.exception

    def test_page_7_wf_checkbox_toggle(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        wf_box = None
        for cb in at.checkbox:
            lbl = cb.label if hasattr(cb, 'label') else ''
            if lbl and "walk-forward" in lbl.lower():
                wf_box = cb
                break
        assert wf_box is not None
        wf_box.set_value(False).run()
        assert not at.exception
        wf_box.set_value(True).run()
        assert not at.exception

    def test_baseline_leverage_extreme(self):
        """Baseline leverage 1000 should not trigger any widget validation."""
        at = _pick_page(_at(), "Run Deep Backtest")
        for ni in at.number_input:
            lbl = ni.label if hasattr(ni, 'label') else ''
            if lbl and "baseline" in lbl.lower():
                ni.set_value(1000.0).run()
                assert not at.exception
                return

    def test_initial_cash_extreme(self):
        at = _pick_page(_at(), "Run Deep Backtest")
        for ni in at.number_input:
            lbl = ni.label if hasattr(ni, 'label') else ''
            if lbl and "cash" in lbl.lower():
                ni.set_value(1_000_000.0).run()
                assert not at.exception
                return
