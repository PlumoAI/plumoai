from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Literal, Optional

from .indicators import IndicatorSnapshot


Signal = Literal["buy", "sell", "hold"]


@dataclass(frozen=True)
class StrategyDecision:
    signal: Signal
    confidence: float
    rationale: str
    meta: Dict[str, Any]


def sma_cross_strategy(s: IndicatorSnapshot) -> StrategyDecision:
    if s.sma_fast is None or s.sma_slow is None:
        return StrategyDecision(signal="hold", confidence=0.0, rationale="Insufficient SMA history.", meta={"strategy": "sma_cross"})
    if s.sma_fast > s.sma_slow:
        return StrategyDecision(signal="buy", confidence=0.60, rationale="SMA fast above SMA slow (uptrend).", meta={"strategy": "sma_cross"})
    if s.sma_fast < s.sma_slow:
        return StrategyDecision(signal="sell", confidence=0.55, rationale="SMA fast below SMA slow (downtrend).", meta={"strategy": "sma_cross"})
    return StrategyDecision(signal="hold", confidence=0.1, rationale="SMA fast equals SMA slow.", meta={"strategy": "sma_cross"})


def rsi_reversion_strategy(s: IndicatorSnapshot) -> StrategyDecision:
    if s.rsi14 is None:
        return StrategyDecision(signal="hold", confidence=0.0, rationale="Insufficient RSI history.", meta={"strategy": "rsi_reversion"})
    if s.rsi14 <= 30:
        return StrategyDecision(signal="buy", confidence=0.62, rationale="RSI <= 30 (oversold).", meta={"strategy": "rsi_reversion"})
    if s.rsi14 >= 70:
        return StrategyDecision(signal="sell", confidence=0.58, rationale="RSI >= 70 (overbought).", meta={"strategy": "rsi_reversion"})
    return StrategyDecision(signal="hold", confidence=0.2, rationale="RSI neutral range.", meta={"strategy": "rsi_reversion"})


def decision_to_dict(d: StrategyDecision) -> Dict[str, Any]:
    return asdict(d)

