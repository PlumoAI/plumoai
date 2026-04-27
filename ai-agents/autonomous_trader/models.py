from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


Market = Literal["us-stock", "crypto", "forex", "options", "futures"]
Side = Literal["buy", "sell", "short", "cover"]
Mode = Literal["paper", "live"]


@dataclass(frozen=True)
class TradeIdea:
    symbol: str
    market: Market = "us-stock"
    side: Side = "buy"
    confidence: float = 0.5
    rationale: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: float
    order_type: str = "market"
    limit_price: Optional[float] = None
    market: Market = "us-stock"
    notes: str = ""
    client_order_id: Optional[str] = None


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    mode: Mode
    broker: str
    order: OrderRequest
    broker_order_id: Optional[str] = None
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentState:
    """Serializable state blob meant to be fed back via provided_data."""

    version: str = "1.0"
    cycle_count: int = 0
    bandit: Dict[str, Any] = field(default_factory=dict)
    last_actions: List[Dict[str, Any]] = field(default_factory=list)

