from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .market_data import Candle


def sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if period <= 0 or len(closes) < period + 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(0.0, ch)
        losses += max(0.0, -ch)
    avg_gain = gains / period
    avg_loss = losses / period
    rs = avg_gain / avg_loss if avg_loss > 0 else 999999.0
    out[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gain = max(0.0, ch)
        loss = max(0.0, -ch)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 999999.0
        out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


@dataclass(frozen=True)
class IndicatorSnapshot:
    close: float
    sma_fast: Optional[float]
    sma_slow: Optional[float]
    rsi14: Optional[float]


def latest_snapshot(candles: List[Candle], fast: int = 20, slow: int = 50) -> Optional[IndicatorSnapshot]:
    if not candles:
        return None
    closes = [c.close for c in candles]
    sma_fast = sma(closes, fast)
    sma_slow = sma(closes, slow)
    rsi14 = rsi(closes, 14)
    i = len(closes) - 1
    return IndicatorSnapshot(close=closes[i], sma_fast=sma_fast[i], sma_slow=sma_slow[i], rsi14=rsi14[i])


def snapshot_to_dict(s: IndicatorSnapshot) -> Dict[str, Any]:
    return asdict(s)

