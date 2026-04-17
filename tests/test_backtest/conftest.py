"""Test isolation for src/strategies/storage.py auto-capture hook.

`run_deep_backtest()` now writes into the strategy storage tables via a hook
in `src/backtest/deep_backtest.py`. The default DB path is `data/trades.db`
(the real file). When pytest fires backtest tests (via test_deep_backtest.py
etc.), the hook fires inside the test process and pollutes data/trades.db
with rows whose report_dir points at pytest's tmp_path.

Fix: an autouse fixture sets `ALGO_STRATEGY_DB` to a per-test tmp DB so the
storage layer routes writes there instead. The hook still fires (we want the
integration coverage), but the writes land in throwaway storage.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_strategy_storage_db(tmp_path, monkeypatch):
    """Point storage.record_deep_backtest_result at a per-test tmp DB so
    deep_backtest hook firings don't pollute the real data/trades.db."""
    db_path = tmp_path / "test_strategy_storage.db"
    monkeypatch.setenv("ALGO_STRATEGY_DB", str(db_path))
    yield
