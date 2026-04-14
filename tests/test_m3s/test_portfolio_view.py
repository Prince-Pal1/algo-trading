from __future__ import annotations

import threading

import pytest

from src.m3s.portfolio_view import VersionedPortfolioView


def _publish(view: VersionedPortfolioView, equity: float, ts_ms: int = 0):
    return view.publish(
        ts_ms=ts_ms,
        equity=equity,
        cash=equity,
        floating_pnl=0.0,
        used_margin=0.0,
        aggregate_leverage=0.0,
        open_position_count=0,
        institutional_equity=equity * 0.7,
        aggressive_equity=equity * 0.3,
    )


class TestVersionedPortfolioView:
    def test_initial_version_zero(self):
        v = VersionedPortfolioView()
        assert v.version() == 0
        snap = v.read()
        assert snap.equity == 0.0
        assert snap.version == 0

    def test_publish_increments_version(self):
        v = VersionedPortfolioView()
        _publish(v, equity=10_000.0)
        assert v.version() == 1
        _publish(v, equity=10_500.0)
        assert v.version() == 2

    def test_read_returns_latest_snapshot(self):
        v = VersionedPortfolioView()
        _publish(v, equity=10_000.0)
        _publish(v, equity=10_500.0, ts_ms=100)
        snap = v.read()
        assert snap.equity == 10_500.0
        assert snap.version == 2
        assert snap.ts_ms == 100
        assert snap.institutional_equity == pytest.approx(7_350.0)
        assert snap.aggressive_equity == pytest.approx(3_150.0)

    def test_read_is_consistent_snapshot(self):
        v = VersionedPortfolioView()
        _publish(v, equity=10_000.0)
        # Capture a snapshot, then publish, then re-read — the captured snapshot
        # must be unchanged (frozen dataclass + RCU)
        snap = v.read()
        assert snap.equity == 10_000.0
        _publish(v, equity=99_999.0)
        assert snap.equity == 10_000.0  # still the old snapshot


class TestConcurrentReadWrite:
    def test_readers_see_consistent_snapshot(self):
        """10 readers × 1 writer: readers should never see a half-written snapshot.

        The RCU pattern is to assemble a fully-constructed PortfolioViewSnapshot
        object and atomically assign it to `_current`. Under CPython's GIL this
        assignment is a single bytecode op; readers that read `_current` get
        either the old or the new snapshot, never a torn one.
        """
        v = VersionedPortfolioView()
        _publish(v, equity=10_000.0)
        stop = threading.Event()

        seen_equities: list[float] = []
        lock = threading.Lock()

        def writer() -> None:
            eq = 10_000.0
            while not stop.is_set():
                eq += 1.0
                _publish(v, equity=eq)

        def reader() -> None:
            while not stop.is_set():
                snap = v.read()
                # equity == cash == institutional/0.7 (by _publish convention)
                expected_institutional = snap.equity * 0.7
                expected_aggressive = snap.equity * 0.3
                assert abs(snap.institutional_equity - expected_institutional) < 1e-6
                assert abs(snap.aggressive_equity - expected_aggressive) < 1e-6
                with lock:
                    seen_equities.append(snap.equity)

        writer_thread = threading.Thread(target=writer)
        reader_threads = [threading.Thread(target=reader) for _ in range(10)]

        writer_thread.start()
        for t in reader_threads:
            t.start()

        # Let them spin briefly
        import time as _time
        _time.sleep(0.1)
        stop.set()

        writer_thread.join(timeout=1.0)
        for t in reader_threads:
            t.join(timeout=1.0)

        # Readers saw many different equities — no torn snapshots (no asserts
        # fired inside the reader loop)
        assert len(set(seen_equities)) > 1
