from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from .config import TraderConfig
from .learning_bandit import Ucb1Bandit, bounded_reward
from .models import AgentState, ExecutionResult, OrderRequest, TradeIdea
from .market_data import MarketDataClient, candles_to_dict
from .indicators import latest_snapshot, snapshot_to_dict
from .risk import build_risk_plan, risk_plan_to_dict
from .strategy_rules import decision_to_dict, rsi_reversion_strategy, sma_cross_strategy


STRATEGY_KEYS = ["sma_cross", "rsi_reversion"]


def _mode_from_config(cfg: TraderConfig, requested: Optional[str]) -> str:
    m = (requested or cfg.default_mode or "paper").strip().lower()
    if m not in ("paper", "live"):
        return "paper"
    # Standalone agent: always paper
    return "paper"


def _to_order(idea: TradeIdea, quantity: float, notes: str) -> OrderRequest:
    return OrderRequest(
        symbol=idea.symbol,
        side=idea.side,
        quantity=float(quantity),
        order_type="market",
        market=idea.market,
        notes=notes,
        client_order_id=str(uuid.uuid4()),
    )


async def run_cycle(
    *,
    cfg: TraderConfig,
    state: AgentState,
    symbols: List[str],
    market: str,
    hint: str,
    requested_mode: Optional[str],
    quantity_per_order: float,
    price_by_symbol: Optional[Dict[str, float]] = None,
    account_value: float = 100000.0,
    risk_per_trade_pct: float = 1.0,
    stop_loss_pct: float = 2.0,
    take_profit_rr: float = 2.0,
    data_source: str = "stooq",
) -> Dict[str, Any]:
    """
    One autonomous cycle:
      choose strategy (bandit) -> generate ideas -> risk gate -> execute (paper/live) -> score -> update bandit.

    Persistence model:
      The updated state is returned as `next_state` and should be passed back in `provided_data`.
    """

    mode = _mode_from_config(cfg, requested_mode)
    bandit = Ucb1Bandit.from_dict(state.bandit)

    strategy_key = bandit.select(STRATEGY_KEYS)
    md = MarketDataClient()

    price_by_symbol = price_by_symbol or {}
    executions: List[ExecutionResult] = []
    if mode == "paper":
        ideas_out: List[Dict[str, Any]] = []
        for sym in symbols[: max(1, int(cfg.max_orders_per_cycle))]:
            candles = await md.fetch_ohlcv(symbol=sym, market=market, timeframe="1d", limit=200, source=data_source)
            snap = latest_snapshot(candles)
            if not snap:
                continue
            # Strategy decision
            if strategy_key == "rsi_reversion":
                decision = rsi_reversion_strategy(snap)
            else:
                decision = sma_cross_strategy(snap)
            entry = float(price_by_symbol.get(sym) or snap.close)
            plan = build_risk_plan(
                entry=entry,
                account_value=account_value,
                risk_per_trade_pct=risk_per_trade_pct,
                stop_loss_pct=stop_loss_pct,
                take_profit_rr=take_profit_rr,
                max_notional_per_order=cfg.max_notional_per_order,
            )
            # Build “paper execution” record
            idea = {
                "symbol": sym,
                "market": market,
                "strategy": strategy_key,
                "decision": decision_to_dict(decision),
                "indicators": snapshot_to_dict(snap),
                "risk_plan": risk_plan_to_dict(plan),
            }
            ideas_out.append(idea)
            # Only trade on buy/sell signals; hold => no execution
            if decision.signal == "hold" or plan.quantity <= 0:
                continue
            order = OrderRequest(
                symbol=sym,
                side=decision.signal,  # type: ignore[arg-type]
                quantity=plan.quantity if quantity_per_order == 1 else float(quantity_per_order),
                order_type="paper",
                limit_price=None,
                market=market,  # type: ignore[arg-type]
                notes=f"{decision.rationale} SL={plan.stop_loss:.2f} TP={plan.take_profit:.2f}",
                client_order_id=str(uuid.uuid4()),
            )
            executions.append(
                ExecutionResult(
                    success=True,
                    mode="paper",
                    broker="paper",
                    order=order,
                    broker_order_id=None,
                    raw={"simulated": True},
                )
            )

    # Reward is currently heuristic: success => small positive, failure => negative.
    # A real deployment should compute PnL-based rewards when fills and mark prices are available.
    reward = 0.0
    if executions:
        for ex in executions:
            reward += 0.1 if ex.success else -0.2
        reward /= len(executions)
    reward = bounded_reward(reward)
    bandit.update(strategy_key, reward)

    state.cycle_count += 1
    state.bandit = bandit.to_dict()
    state.last_actions = [
        {
            "mode": mode,
            "strategy": strategy_key,
            "reward": reward,
            "idea_count": len(ideas),
        }
    ]

    return {
        "mode": mode,
        "strategy": strategy_key,
        "ideas": ideas_out,
        "executions": [asdict(e) for e in executions],
        "reward": reward,
        "next_state": asdict(state),
    }

