"""Historical aggregated-trade downloader — Binance Data Vision.

Binance publishes every aggregated trade as free daily ZIP dumps at
``data.binance.vision`` — no API key, no rate-limit budget, years of history.
Each row carries the aggressor flag, which is what makes it the one free
source complete enough to backtest order-flow signals (see ``src/data/cvd.py``).

Raw trades are enormous (a liquid pair runs to hundreds of MB per day
uncompressed), so ``download_delta_bars`` aggregates each day into delta bars
and discards the raw rows before fetching the next day. Only the bars are
ever held in memory or written to disk.

Usage::

    dl = AggTradesDownloader()
    bars = await dl.download_delta_bars("BTCUSDT", "5m", "2026-09-01", "2026-09-07")
    await dl.close()

CSV layout (positional — the dumps are headerless before ~2024):
    agg_trade_id, price, quantity, first_trade_id, last_trade_id,
    transact_time, is_buyer_maker[, is_best_match]

USD-M futures dumps omit the trailing ``is_best_match``, so columns are
assigned by count, not by name.
"""

from __future__ import annotations

import asyncio
import io
import ssl
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import certifi
import httpx
import pandas as pd

from src.data.cvd import BAR_COLUMNS, compute_delta_bars
from src.data.storage import ParquetStore
from src.utils.logger import get_logger

log = get_logger("agg_trades")

BINANCE_VISION_BASE = "https://data.binance.vision/data"
RATE_LIMIT_DELAY = 0.2  # seconds between day fetches

# Market → path segment under the Data Vision root.
MARKET_PATHS = {
    "spot": "spot",
    "um": "futures/um",     # USD-M perpetuals
    "cm": "futures/cm",     # COIN-M
}

_SPOT_COLUMNS = [
    "agg_trade_id", "price", "quantity", "first_trade_id",
    "last_trade_id", "timestamp", "is_buyer_maker", "is_best_match",
]

# Binance moved some Data Vision datasets to microsecond timestamps during
# 2025. Epoch ms is ~1.7e12 and epoch µs ~1.7e15, so this threshold separates
# them with several orders of magnitude to spare.
_MICROSECOND_THRESHOLD = 1e14


def daily_url(symbol: str, day: date, market: str = "spot") -> str:
    """Data Vision URL for one symbol-day of aggregated trades."""
    if market not in MARKET_PATHS:
        raise ValueError(f"unknown market: {market!r} (known: {sorted(MARKET_PATHS)})")
    sym = symbol.upper()
    seg = MARKET_PATHS[market]
    stamp = day.strftime("%Y-%m-%d")
    return f"{BINANCE_VISION_BASE}/{seg}/daily/aggTrades/{sym}/{sym}-aggTrades-{stamp}.zip"


def parse_agg_trades(raw: bytes) -> pd.DataFrame:
    """Parse one Data Vision aggTrades ZIP into a trade DataFrame.

    Handles both headered and headerless dumps, the 7-column futures layout,
    and microsecond timestamps.

    Returns:
        DataFrame with ``timestamp`` (Unix ms, int64), ``price``,
        ``quantity``, ``is_buyer_maker`` (bool).
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        if not names:
            raise ValueError("aggTrades archive is empty")
        with zf.open(names[0]) as fh:
            body = fh.read()

    if not body.strip():
        return pd.DataFrame(columns=["timestamp", "price", "quantity", "is_buyer_maker"])

    first_field = body.split(b"\n", 1)[0].split(b",", 1)[0].strip()
    has_header = not first_field.lstrip(b"-").isdigit()

    df = pd.read_csv(
        io.BytesIO(body),
        header=0 if has_header else None,
        names=None if has_header else _SPOT_COLUMNS[: _column_count(body)],
    )
    if has_header:
        df.columns = [str(c).strip().lower() for c in df.columns]
        # Headered dumps name the time column transact_time or transact_id-era
        # variants; normalize whichever one is present.
        for candidate in ("transact_time", "timestamp", "time"):
            if candidate in df.columns:
                df = df.rename(columns={candidate: "timestamp"})
                break

    missing = {"timestamp", "price", "quantity", "is_buyer_maker"} - set(df.columns)
    if missing:
        raise ValueError(f"aggTrades dump missing columns: {sorted(missing)}")

    out = pd.DataFrame({
        "timestamp": _normalize_timestamps(df["timestamp"]),
        "price": df["price"].astype(float),
        "quantity": df["quantity"].astype(float),
        "is_buyer_maker": _to_bool(df["is_buyer_maker"]),
    })
    return out


def _column_count(body: bytes) -> int:
    """Field count of the first data row — 8 for spot, 7 for futures."""
    first_line = body.split(b"\n", 1)[0]
    return len(first_line.split(b","))


def _normalize_timestamps(series: pd.Series) -> pd.Series:
    """Coerce to int64 Unix milliseconds, downscaling microsecond dumps."""
    ts = pd.to_numeric(series, errors="coerce").astype("float64")
    if ts.notna().any() and ts.max() > _MICROSECOND_THRESHOLD:
        ts = ts // 1000
    return ts.fillna(0).astype("int64")


def _to_bool(series: pd.Series) -> pd.Series:
    """Parse the aggressor flag — dumps use bools, 0/1, or 'true'/'false'."""
    if series.dtype == bool:
        return series
    if pd.api.types.is_numeric_dtype(series):
        return series.astype(int).astype(bool)
    return (
        series.astype(str).str.strip().str.lower()
        .map({"true": True, "false": False, "1": True, "0": False})
        .fillna(False)
        .astype(bool)
    )


def _daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).date()


class AggTradesDownloader:
    """Fetches aggregated-trade dumps and folds them straight into delta bars."""

    def __init__(self, timeout: float = 120.0):
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self._client = httpx.AsyncClient(verify=ssl_ctx, timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def download_day(
        self,
        symbol: str,
        day: date,
        market: str = "spot",
    ) -> pd.DataFrame | None:
        """Fetch one symbol-day of raw trades. ``None`` when not published."""
        url = daily_url(symbol, day, market)
        resp = await self._client.get(url)
        if resp.status_code == 404:
            log.warning("aggtrades_missing", symbol=symbol, day=str(day), url=url)
            return None
        resp.raise_for_status()
        return parse_agg_trades(resp.content)

    async def download_delta_bars(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str | None = None,
        market: str = "spot",
    ) -> pd.DataFrame:
        """Download a date range and aggregate it into one continuous bar series.

        Each day is fetched, folded into delta bars, and released before the
        next day is requested, so peak memory is one day of raw trades.
        CVD carries across day boundaries — the result is a single running
        series, not per-day segments.

        Args:
            symbol: e.g. "BTCUSDT"
            timeframe: any key of ``src.data.candle_builder.TF_MS``
            start_date / end_date: "YYYY-MM-DD" (end defaults to start)
            market: "spot", "um", or "cm"

        Returns:
            DataFrame of delta bars ascending by timestamp. Missing days are
            skipped with a warning rather than failing the range.
        """
        start = _parse_date(start_date)
        end = _parse_date(end_date) if end_date else start
        if end < start:
            raise ValueError(f"end_date {end} is before start_date {start}")

        log.info(
            "aggtrades_download_start",
            symbol=symbol, timeframe=timeframe,
            start=str(start), end=str(end), market=market,
        )

        frames: list[pd.DataFrame] = []
        running_cvd = 0.0
        missing_days = 0

        for day in _daterange(start, end):
            trades = await self.download_day(symbol, day, market)
            if trades is None or trades.empty:
                missing_days += 1
                await asyncio.sleep(RATE_LIMIT_DELAY)
                continue

            bars = compute_delta_bars(trades, timeframe, symbol=symbol, initial_cvd=running_cvd)
            if not bars.empty:
                running_cvd = float(bars["cvd"].iloc[-1])
                frames.append(bars)

            log.info(
                "aggtrades_day_done",
                symbol=symbol, day=str(day),
                trades=len(trades), bars=len(bars), cvd=round(running_cvd, 4),
            )
            del trades
            await asyncio.sleep(RATE_LIMIT_DELAY)

        if not frames:
            log.warning("aggtrades_empty", symbol=symbol, start=str(start), end=str(end))
            out = pd.DataFrame(columns=BAR_COLUMNS)
            out["symbol"] = pd.Series(dtype="object")
            return out

        result = pd.concat(frames, ignore_index=True)
        result = result.sort_values("timestamp").reset_index(drop=True)
        log.info(
            "aggtrades_download_complete",
            symbol=symbol, timeframe=timeframe, bars=len(result),
            missing_days=missing_days,
            first=int(result["timestamp"].iloc[0]),
            last=int(result["timestamp"].iloc[-1]),
        )
        return result

    async def download_and_save(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str | None = None,
        market: str = "spot",
    ) -> Path:
        """Download delta bars and persist them via ``ParquetStore``.

        Stored under the pseudo-timeframe ``<tf>_cvd`` so flow bars sit beside
        OHLCV caches without colliding with them.
        """
        bars = await self.download_delta_bars(symbol, timeframe, start_date, end_date, market)
        if bars.empty:
            raise ValueError(f"No aggTrades downloaded for {symbol} {timeframe}")

        store = ParquetStore()
        tf_key = f"{timeframe}_cvd"
        store.save(bars, symbol, tf_key)
        path = store._path(symbol, tf_key)
        log.info("aggtrades_saved", path=str(path), rows=len(bars))
        return path
