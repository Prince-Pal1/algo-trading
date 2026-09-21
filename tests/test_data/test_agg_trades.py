"""Tests for src/data/agg_trades.py — Binance Data Vision dump parsing.

The dumps are not a stable format: headerless before ~2024, headered after,
8 columns on spot and 7 on futures, and some 2025-onward datasets switched to
microsecond timestamps. Every one of those variants is covered here because
each silently corrupts CVD rather than raising.

No test touches the network — archives are built in memory.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pandas as pd
import pytest

from src.data.agg_trades import (
    AggTradesDownloader,
    _daterange,
    _parse_date,
    daily_url,
    parse_agg_trades,
)

_BASE_MS = 1_700_000_000_000


def _zip_csv(body: str, name: str = "trades.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, body)
    return buf.getvalue()


def _spot_row(trade_id: int, price: float, qty: float, ts: int, maker: str) -> str:
    return f"{trade_id},{price},{qty},{trade_id},{trade_id},{ts},{maker},true"


class TestDailyUrl:
    def test_spot_path(self):
        url = daily_url("btcusdt", date(2026, 9, 1))
        assert url.endswith("/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-09-01.zip")

    def test_futures_um_path(self):
        url = daily_url("BTCUSDT", date(2026, 9, 1), market="um")
        assert "/futures/um/daily/aggTrades/" in url

    def test_futures_cm_path(self):
        assert "/futures/cm/" in daily_url("BTCUSD_PERP", date(2026, 9, 1), market="cm")

    def test_unknown_market_raises(self):
        with pytest.raises(ValueError, match="unknown market"):
            daily_url("BTCUSDT", date(2026, 9, 1), market="options")


class TestParseAggTrades:
    def test_headerless_spot_dump(self):
        body = "\n".join([
            _spot_row(1, 100.5, 2.0, _BASE_MS, "true"),
            _spot_row(2, 100.6, 3.0, _BASE_MS + 1000, "false"),
        ])
        df = parse_agg_trades(_zip_csv(body))
        assert len(df) == 2
        assert df["price"].tolist() == [100.5, 100.6]
        assert df["quantity"].tolist() == [2.0, 3.0]
        assert df["is_buyer_maker"].tolist() == [True, False]
        assert df["timestamp"].tolist() == [_BASE_MS, _BASE_MS + 1000]

    def test_headered_dump(self):
        body = (
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker,is_best_match\n"
            + _spot_row(1, 100.5, 2.0, _BASE_MS, "true")
        )
        df = parse_agg_trades(_zip_csv(body))
        assert len(df) == 1
        assert df["timestamp"].iloc[0] == _BASE_MS
        assert bool(df["is_buyer_maker"].iloc[0]) is True

    def test_seven_column_futures_layout(self):
        body = f"1,100.5,2.0,1,1,{_BASE_MS},true"
        df = parse_agg_trades(_zip_csv(body))
        assert len(df) == 1
        assert df["price"].iloc[0] == 100.5
        assert bool(df["is_buyer_maker"].iloc[0]) is True

    def test_microsecond_timestamps_downscaled(self):
        body = _spot_row(1, 100.0, 1.0, _BASE_MS * 1000, "false")
        df = parse_agg_trades(_zip_csv(body))
        assert df["timestamp"].iloc[0] == _BASE_MS

    def test_millisecond_timestamps_untouched(self):
        body = _spot_row(1, 100.0, 1.0, _BASE_MS, "false")
        df = parse_agg_trades(_zip_csv(body))
        assert df["timestamp"].iloc[0] == _BASE_MS

    def test_numeric_maker_flag(self):
        body = "\n".join([
            f"1,100.0,1.0,1,1,{_BASE_MS},1,1",
            f"2,100.0,1.0,2,2,{_BASE_MS + 1},0,1",
        ])
        df = parse_agg_trades(_zip_csv(body))
        assert df["is_buyer_maker"].tolist() == [True, False]

    def test_timestamp_dtype_is_int(self):
        body = _spot_row(1, 100.0, 1.0, _BASE_MS, "false")
        df = parse_agg_trades(_zip_csv(body))
        assert df["timestamp"].dtype == "int64"

    def test_empty_body_returns_empty_frame(self):
        df = parse_agg_trades(_zip_csv(""))
        assert df.empty
        assert list(df.columns) == ["timestamp", "price", "quantity", "is_buyer_maker"]

    def test_empty_archive_raises(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        with pytest.raises(ValueError, match="archive is empty"):
            parse_agg_trades(buf.getvalue())

    def test_missing_columns_raises(self):
        body = "some_col,other_col\n1,2"
        with pytest.raises(ValueError, match="missing columns"):
            parse_agg_trades(_zip_csv(body))


class TestDateHelpers:
    def test_parse_date(self):
        assert _parse_date("2026-09-01") == date(2026, 9, 1)

    def test_daterange_inclusive(self):
        days = list(_daterange(date(2026, 9, 1), date(2026, 9, 3)))
        assert days == [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]

    def test_daterange_single_day(self):
        assert list(_daterange(date(2026, 9, 1), date(2026, 9, 1))) == [date(2026, 9, 1)]


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Serves crafted archives keyed by the date in the requested URL."""

    def __init__(self, by_day: dict[str, bytes]):
        self.by_day = by_day
        self.requested: list[str] = []

    async def get(self, url: str) -> _FakeResponse:
        self.requested.append(url)
        for stamp, payload in self.by_day.items():
            if stamp in url:
                return _FakeResponse(payload)
        return _FakeResponse(b"", status_code=404)

    async def aclose(self) -> None:
        pass


def _day_archive(day_index: int, buys: int, sells: int) -> bytes:
    """One archive with `buys` aggressive buys and `sells` aggressive sells."""
    base = _BASE_MS + day_index * 86_400_000
    rows = []
    tid = 1
    for i in range(buys):
        rows.append(_spot_row(tid, 100.0, 1.0, base + i, "false"))
        tid += 1
    for i in range(sells):
        rows.append(_spot_row(tid, 100.0, 1.0, base + 1000 + i, "true"))
        tid += 1
    return _zip_csv("\n".join(rows))


class TestDownloadDeltaBars:
    def _downloader(self, by_day: dict[str, bytes]) -> tuple[AggTradesDownloader, _FakeClient]:
        dl = AggTradesDownloader()
        fake = _FakeClient(by_day)
        dl._client = fake  # type: ignore[assignment]
        return dl, fake

    async def test_end_before_start_raises(self):
        dl, _ = self._downloader({})
        with pytest.raises(ValueError, match="before start_date"):
            await dl.download_delta_bars("BTCUSDT", "1m", "2026-09-05", "2026-09-01")

    async def test_single_day_aggregates(self):
        dl, _ = self._downloader({"2026-09-01": _day_archive(0, buys=5, sells=2)})
        bars = await dl.download_delta_bars("BTCUSDT", "1h", "2026-09-01")
        assert len(bars) == 1
        assert bars["buy_volume"].iloc[0] == 5.0
        assert bars["sell_volume"].iloc[0] == 2.0
        assert bars["delta"].iloc[0] == 3.0
        assert bars["symbol"].iloc[0] == "BTCUSDT"

    async def test_cvd_is_continuous_across_days(self):
        dl, _ = self._downloader({
            "2026-09-01": _day_archive(0, buys=5, sells=0),   # +5
            "2026-09-02": _day_archive(1, buys=0, sells=3),   # -3
        })
        bars = await dl.download_delta_bars("BTCUSDT", "1h", "2026-09-01", "2026-09-02")
        assert len(bars) == 2
        # Day 2's CVD continues from day 1 rather than restarting at its own delta.
        assert bars["cvd"].tolist() == [5.0, 2.0]

    async def test_missing_day_skipped_not_fatal(self):
        dl, _ = self._downloader({
            "2026-09-01": _day_archive(0, buys=4, sells=0),
            # 2026-09-02 absent → 404
            "2026-09-03": _day_archive(2, buys=1, sells=0),
        })
        bars = await dl.download_delta_bars("BTCUSDT", "1h", "2026-09-01", "2026-09-03")
        assert len(bars) == 2
        assert bars["cvd"].tolist() == [4.0, 5.0]

    async def test_all_days_missing_returns_empty(self):
        dl, _ = self._downloader({})
        bars = await dl.download_delta_bars("BTCUSDT", "1m", "2026-09-01", "2026-09-02")
        assert bars.empty
        assert "cvd" in bars.columns

    async def test_result_sorted_by_timestamp(self):
        dl, _ = self._downloader({
            "2026-09-01": _day_archive(0, buys=2, sells=1),
            "2026-09-02": _day_archive(1, buys=2, sells=1),
        })
        bars = await dl.download_delta_bars("BTCUSDT", "1h", "2026-09-01", "2026-09-02")
        assert bars["timestamp"].is_monotonic_increasing

    async def test_requests_one_url_per_day(self):
        dl, fake = self._downloader({"2026-09-01": _day_archive(0, buys=1, sells=0)})
        await dl.download_delta_bars("BTCUSDT", "1h", "2026-09-01", "2026-09-03")
        assert len(fake.requested) == 3

    async def test_download_and_save_rejects_empty(self):
        dl, _ = self._downloader({})
        with pytest.raises(ValueError, match="No aggTrades downloaded"):
            await dl.download_and_save("BTCUSDT", "1m", "2026-09-01")

    async def test_download_and_save_writes_parquet(self, tmp_path, monkeypatch):
        # ParquetStore defaults to a relative "data/historical", so chdir is
        # enough to sandbox the write — no patching needed.
        monkeypatch.chdir(tmp_path)
        dl, _ = self._downloader({"2026-09-01": _day_archive(0, buys=3, sells=1)})
        path = await dl.download_and_save("BTCUSDT", "1h", "2026-09-01")
        assert path.exists()
        saved = pd.read_parquet(path)
        assert saved["delta"].iloc[0] == 2.0
