from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class TraderConfig:
    # Safety gates
    allow_live_trading: bool = False
    default_mode: str = "paper"  # "paper" | "live"
    max_orders_per_cycle: int = 3
    max_notional_per_order: float = 0.0  # 0 => disabled (requires caller sizing)


def _parse_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off", ""):
            return False
    return default


def _parse_int(v: Any, default: int) -> int:
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        return int(v)
    except Exception:
        return default


def _parse_float(v: Any, default: float) -> float:
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        return float(v)
    except Exception:
        return default


def load_config(app_config: Dict[str, Any]) -> TraderConfig:
    app_config = app_config or {}
    return TraderConfig(
        allow_live_trading=_parse_bool(app_config.get("allow_live_trading", False), default=False),
        default_mode=str(app_config.get("default_mode") or "paper").strip().lower(),
        max_orders_per_cycle=_parse_int(app_config.get("max_orders_per_cycle"), default=3),
        max_notional_per_order=_parse_float(app_config.get("max_notional_per_order"), default=0.0),
    )

