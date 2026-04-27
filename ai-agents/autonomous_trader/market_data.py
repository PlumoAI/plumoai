from __future__ import annotations

import csv
import io
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx


Timeframe = Literal["1d", "1h"]


@dataclass(frozen=True)
class Candle:
    ts: str  # ISO8601 date or datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def candles_to_dict(candles: List[Candle]) -> List[Dict[str, Any]]:
    return [asdict(c) for c in candles]


class MarketDataClient:
    def __init__(self, *, timeout_s: float = 20.0):
        self.timeout_s = timeout_s

    async def fetch_ohlcv(
        self,
        *,
        symbol: str,
        market: str = "us-stock",
        timeframe: Timeframe = "1d",
        limit: int = 300,
        source: str = "stooq",
    ) -> List[Candle]:
        source = (source or "").strip().lower() or "stooq"
        market = (market or "").strip().lower() or "us-stock"
        if market == "crypto" and source == "binance":
            return await self._binance_klines(symbol=symbol, interval="1d" if timeframe == "1d" else "1h", limit=limit)
        # Default: stooq daily for stocks/etfs
        if timeframe != "1d":
            raise ValueError("stooq source supports timeframe=1d only")
        return await self._stooq_daily(symbol=symbol, limit=limit)

    async def _stooq_daily(self, *, symbol: str, limit: int) -> List[Candle]:
        # Stooq expects e.g. aapl.us, msft.us. If user passes AAPL we assume US.
        s = (symbol or "").strip().lower()
        if "." not in s:
            s = f"{s}.us"
        url = f"https://stooq.com/q/d/l/?s={s}&i=d"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.get(url)
            r.raise_for_status()
            text = r.text

        f = io.StringIO(text)
        reader = csv.DictReader(f)
        out: List[Candle] = []
        for row in reader:
            try:
                dt = (row.get("Date") or "").strip()
                if not dt:
                    continue
                out.append(
                    Candle(
                        ts=dt,
                        open=float(row.get("Open") or 0),
                        high=float(row.get("High") or 0),
                        low=float(row.get("Low") or 0),
                        close=float(row.get("Close") or 0),
                        volume=float(row.get("Volume") or 0),
                    )
                )
            except Exception:
                continue
        # Stooq returns oldest->newest; keep last N
        out = out[-max(1, int(limit)) :]
        return out

    async def _binance_klines(self, *, symbol: str, interval: str, limit: int) -> List[Candle]:
        # symbol like BTCUSDT; if user gives BTC-USD we try to normalize.
        s = (symbol or "").upper().replace("-", "").replace("/", "")
        if s.endswith("USD") and not s.endswith("USDT"):
            s = s[:-3] + "USDT"
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": s, "interval": interval, "limit": int(limit)}
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        out: List[Candle] = []
        for item in data or []:
            try:
                # [ openTime, open, high, low, close, volume, closeTime, ... ]
                open_ms = int(item[0])
                ts = datetime.utcfromtimestamp(open_ms / 1000).isoformat() + "Z"
                out.append(
                    Candle(
                        ts=ts,
                        open=float(item[1]),
                        high=float(item[2]),
                        low=float(item[3]),
                        close=float(item[4]),
                        volume=float(item[5]),
                    )
                )
            except Exception:
                continue
        return out

