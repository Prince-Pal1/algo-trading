"""Historical data downloader — fetches OHLCV candles from Binance REST API.

Downloads historical kline data in paginated batches and saves to Parquet files
via the existing ParquetStore.

Usage:
    downloader = BinanceDownloader()
    df = await downloader.download("BTCUSDT", "1m", "2024-01-01", "2026-01-01")
    await downloader.download_and_save("BTCUSDT", "1m", "2024-01-01")
"""

from __future__ import annotations

import asyncio
import ssl
from datetime import datetime, timezone
from pathlib import Path

import certifi
import httpx
import pandas as pd

from src.data.storage import ParquetStore
from src.utils.logger import get_logger

log = get_logger("downloader")

# Binance REST API
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
MAX_CANDLES_PER_REQUEST = 1000
RATE_LIMIT_DELAY = 0.3  # seconds between requests


def _parse_date(date_str: str) -> int:
    """Convert date string to Unix milliseconds."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _parse_klines(raw: list[list]) -> pd.DataFrame:
    """Parse Binance kline response into a DataFrame."""
    rows = []
    for k in raw:
        rows.append({
            "timestamp": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return pd.DataFrame(rows)


class BinanceDownloader:
    """Downloads historical OHLCV data from Binance REST API."""

    def __init__(self):
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self._client = httpx.AsyncClient(verify=ssl_ctx, timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def download(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """Download historical candles from Binance.

        Args:
            symbol: e.g. "BTCUSDT"
            timeframe: e.g. "1m", "5m", "15m", "1h", "4h", "1d"
            start_date: "YYYY-MM-DD" format
            end_date: "YYYY-MM-DD" format (default: now)

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
        """
        start_ms = _parse_date(start_date)
        end_ms = _parse_date(end_date) if end_date else int(datetime.now(timezone.utc).timestamp() * 1000)

        all_frames: list[pd.DataFrame] = []
        current_start = start_ms
        request_count = 0

        log.info(
            "download_start",
            symbol=symbol,
            timeframe=timeframe,
            start=start_date,
            end=end_date or "now",
        )

        while current_start < end_ms:
            params = {
                "symbol": symbol.upper(),
                "interval": timeframe,
                "startTime": current_start,
                "endTime": end_ms,
                "limit": MAX_CANDLES_PER_REQUEST,
            }

            resp = await self._client.get(BINANCE_KLINES_URL, params=params)
            resp.raise_for_status()
            raw = resp.json()

            if not raw:
                break

            df = _parse_klines(raw)
            all_frames.append(df)
            request_count += 1

            # Progress logging every 50 requests
            if request_count % 50 == 0:
                pct = min(100, (current_start - start_ms) / max(1, end_ms - start_ms) * 100)
                total_rows = sum(len(f) for f in all_frames)
                log.info("download_progress", requests=request_count, rows=total_rows, pct=round(pct, 1))

            # Move start to after the last candle
            last_ts = int(raw[-1][0])
            if last_ts <= current_start:
                break  # no progress, avoid infinite loop
            current_start = last_ts + 1

            # Rate limiting
            await asyncio.sleep(RATE_LIMIT_DELAY)

        if not all_frames:
            log.warning("download_empty", symbol=symbol, timeframe=timeframe)
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        result = pd.concat(all_frames, ignore_index=True)
        result = result.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

        log.info(
            "download_complete",
            symbol=symbol,
            timeframe=timeframe,
            rows=len(result),
            requests=request_count,
            first=result["timestamp"].iloc[0],
            last=result["timestamp"].iloc[-1],
        )
        return result

    async def download_and_save(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str | None = None,
    ) -> Path:
        """Download and save to Parquet file."""
        df = await self.download(symbol, timeframe, start_date, end_date)
        if df.empty:
            raise ValueError(f"No data downloaded for {symbol} {timeframe}")

        store = ParquetStore()
        store.save(df, symbol, timeframe)
        path = store._path(symbol, timeframe)
        log.info("saved_to_parquet", path=str(path), rows=len(df))
        return path


# ── Binance Futures funding rate downloader (Strategy A) ──────────────────


# Binance Futures USD-M REST API
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
BINANCE_FUNDING_URL = f"{BINANCE_FUTURES_BASE}/fapi/v1/fundingRate"
MAX_FUNDING_PER_REQUEST = 1000  # Binance hard limit
FUNDING_RATE_DELAY = 0.3        # seconds between requests


def _parse_funding(raw: list[dict]) -> pd.DataFrame:
    """Parse Binance funding rate response into a DataFrame.

    Response schema per item:
        {
            "symbol": "BTCUSDT",
            "fundingRate": "0.00010000",   # string, per 8h epoch
            "fundingTime": 1698220800000,  # epoch ms at settlement
            "markPrice": "34820.5"         # optional, string
        }
    """
    rows = []
    for item in raw:
        rows.append({
            "timestamp": int(item["fundingTime"]),
            "funding_rate": float(item["fundingRate"]),
            "mark_price": float(item.get("markPrice", 0.0) or 0.0),
            "symbol": str(item["symbol"]),
        })
    return pd.DataFrame(rows)


class BinanceFundingDownloader:
    """Downloads historical funding rate data from Binance Futures REST API.

    Endpoint: `GET /fapi/v1/fundingRate`
    - Rate limit: ~500 req / 5 min per IP (shared with fundingInfo)
    - Returns 8h epoch settlement data (00:00, 08:00, 16:00 UTC)
    - Max 1000 rows per request

    Usage:
        dl = BinanceFundingDownloader()
        df = await dl.download("BTCUSDT", "2020-09-01", "2026-04-13")
        await dl.download_and_save("BTCUSDT", "2020-09-01")
    """

    def __init__(self):
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self._client = httpx.AsyncClient(verify=ssl_ctx, timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def download(
        self,
        symbol: str,
        start_date: str,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """Download historical funding rates from Binance Futures.

        Args:
            symbol: e.g. "BTCUSDT"
            start_date: "YYYY-MM-DD"
            end_date: "YYYY-MM-DD" (default: now)

        Returns:
            DataFrame with columns: timestamp (ms, UTC), funding_rate (float,
            per-epoch, e.g. 0.0001 = 0.01%/8h), mark_price (float), symbol.
            Sorted by timestamp ascending, duplicates removed.
        """
        start_ms = _parse_date(start_date)
        end_ms = (
            _parse_date(end_date)
            if end_date
            else int(datetime.now(timezone.utc).timestamp() * 1000)
        )

        all_frames: list[pd.DataFrame] = []
        current_start = start_ms
        request_count = 0

        log.info(
            "funding_download_start",
            symbol=symbol,
            start=start_date,
            end=end_date or "now",
        )

        while current_start < end_ms:
            params = {
                "symbol": symbol.upper(),
                "startTime": current_start,
                "endTime": end_ms,
                "limit": MAX_FUNDING_PER_REQUEST,
            }
            resp = await self._client.get(BINANCE_FUNDING_URL, params=params)
            resp.raise_for_status()
            raw = resp.json()

            if not raw:
                break

            df = _parse_funding(raw)
            all_frames.append(df)
            request_count += 1

            if request_count % 20 == 0:
                rows_so_far = sum(len(f) for f in all_frames)
                log.info(
                    "funding_download_progress",
                    requests=request_count,
                    rows=rows_so_far,
                )

            last_ts = int(raw[-1]["fundingTime"])
            if last_ts <= current_start:
                break
            current_start = last_ts + 1
            await asyncio.sleep(FUNDING_RATE_DELAY)

        if not all_frames:
            log.warning("funding_download_empty", symbol=symbol)
            return pd.DataFrame(
                columns=["timestamp", "funding_rate", "mark_price", "symbol"]
            )

        result = pd.concat(all_frames, ignore_index=True)
        result = (
            result.drop_duplicates(subset=["timestamp"])
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        log.info(
            "funding_download_complete",
            symbol=symbol,
            rows=len(result),
            requests=request_count,
            first_ts=int(result["timestamp"].iloc[0]),
            last_ts=int(result["timestamp"].iloc[-1]),
        )
        return result

    async def download_and_save(
        self,
        symbol: str,
        start_date: str,
        end_date: str | None = None,
    ) -> Path:
        """Download funding rates and save to data/historical/funding/<SYMBOL>.parquet."""
        df = await self.download(symbol, start_date, end_date)
        if df.empty:
            raise ValueError(f"No funding data downloaded for {symbol}")

        funding_dir = Path("data/historical/funding")
        funding_dir.mkdir(parents=True, exist_ok=True)
        path = funding_dir / f"{symbol.upper()}_8h.parquet"

        # Append-and-dedupe pattern matching ParquetStore.save()
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pandas(df)
        if path.exists():
            existing = pq.read_table(path)
            table = pa.concat_tables([existing, table])
            df_combined = (
                table.to_pandas()
                .drop_duplicates(subset=["timestamp"], keep="last")
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
            table = pa.Table.from_pandas(df_combined)
        pq.write_table(table, path, compression="snappy")
        log.info("funding_saved_to_parquet", path=str(path), rows=table.num_rows)
        return path
