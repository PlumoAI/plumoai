from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RiskPlan:
    entry: float
    stop_loss: float
    take_profit: float
    risk_per_unit: float
    quantity: float
    notional: float


def build_risk_plan(
    *,
    entry: float,
    account_value: float,
    risk_per_trade_pct: float,
    stop_loss_pct: float,
    take_profit_rr: float,
    max_notional_per_order: float = 0.0,
) -> RiskPlan:
    entry = float(entry)
    account_value = float(account_value)
    risk_per_trade_pct = float(risk_per_trade_pct)
    stop_loss_pct = float(stop_loss_pct)
    take_profit_rr = float(take_profit_rr)
    max_notional_per_order = float(max_notional_per_order or 0.0)

    stop_loss = entry * (1.0 - stop_loss_pct / 100.0)
    risk_per_unit = max(1e-12, entry - stop_loss)
    risk_budget = max(0.0, account_value * (risk_per_trade_pct / 100.0))
    qty = risk_budget / risk_per_unit if risk_budget > 0 else 0.0

    notional = qty * entry
    if max_notional_per_order and notional > max_notional_per_order and entry > 0:
        qty = max_notional_per_order / entry
        notional = qty * entry

    take_profit = entry + (entry - stop_loss) * take_profit_rr
    return RiskPlan(
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_per_unit=risk_per_unit,
        quantity=qty,
        notional=notional,
    )


def risk_plan_to_dict(p: RiskPlan) -> Dict[str, Any]:
    return asdict(p)

