from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, AsyncGenerator, Dict, List, Optional

from backend.services.ai_agents.base_tool_agent import BaseToolAgent

from .config import load_config
from .events import event
from .models import AgentState
from .orchestrator import run_cycle
from .playbooks import build_workflow_steps, get_playbook, list_playbooks, list_workflows

logger = logging.getLogger(__name__)


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return []


def _extract_args(user_query: str, tool_args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    tool_args = tool_args or {}
    if tool_args:
        return tool_args
    s = (user_query or "").strip()
    if s.startswith("{") and "}" in s:
        try:
            obj = json.loads(s[:5000])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


class AutonomousTraderAgentTool(BaseToolAgent):
    TOOL_NAME = "Self-Driving Trader"
    APP_CODE = "autonomous_trader"

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return (
            "Self-Driving Trader: (1) curated trading playbooks, and (2) a self-driven trading cycle with a learning loop.\n\n"
            "This tool is production-oriented but SAFE by default:\n"
            "- Default mode is paper trading.\n"
            "- This standalone version does not require (or use) OpenAlgo or AI-Trader.\n\n"
            "Actions (tool_args.action):\n"
            "- run_cycle: pick strategy -> propose ideas -> execute (paper/live) -> score -> update state\n"
            "- list_playbooks: list curated playbooks\n"
            "- get_playbook: fetch a playbook by code\n"
            "- list_workflows: list workflow templates\n"
            "- recommend_workflow: return workflow steps\n\n"
            "Inputs (tool_args):\n"
            "- action: 'run_cycle' | 'list_playbooks' | 'get_playbook' | 'list_workflows' | 'recommend_workflow'\n"
            "- symbols: ['AAPL','MSFT'] or 'AAPL,MSFT'\n"
            "- market: 'us-stock' | 'crypto' (default: us-stock)\n"
            "- mode: 'paper' (live ignored in standalone mode)\n"
            "- quantity_per_order: number (default: 1)\n"
            "- hint: optional text for the strategy\n\n"
            "State / self-learning:\n"
            "- Provide prior `next_state` back via provided_data.state to continue learning across runs.\n"
        )

    def __init__(
        self,
        *,
        llm_provider: Any,
        agent_id: str,
        token: str,
        company_id: Optional[str],
        user_id: Optional[int],
        app_config: Dict[str, Any],
    ):
        self.llm_provider = llm_provider
        self.agent_id = agent_id
        self.token = token
        self.company_id = company_id
        self.user_id = user_id
        self.app_config = app_config or {}

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict, None]:
        try:
            cfg = load_config(self.app_config)
            args = _extract_args(user_query, tool_args)
            action = str(args.get("action") or "run_cycle").strip().lower()

            if action == "list_playbooks":
                out = {"success": True, "response": "Playbooks listed.", "result": {"playbooks": list_playbooks()}}
                yield event("result", out)
                yield event("final", out)
                return

            if action == "get_playbook":
                code = str(args.get("playbook_code") or args.get("skill_code") or args.get("code") or "").strip()
                if not code:
                    out = {
                        "success": False,
                        "need_discovery": True,
                        "missing_info": [{"parameter": "playbook_code", "reason": "No playbook_code provided."}],
                        "response": "Provide tool_args.playbook_code.",
                        "result": {"playbooks": list_playbooks()},
                    }
                    yield event("result", out)
                    yield event("final", out)
                    return
                pb = get_playbook(code)
                if not pb:
                    out = {
                        "success": False,
                        "execution_issue": True,
                        "issue": {
                            "code": "unknown_playbook_code",
                            "message": f"Unknown playbook_code: {code}",
                            "suggested_fix": "ask_user",
                            "fix_hint": "Ask the user to pick a playbook from the list.",
                        },
                        "response": f"Unknown playbook '{code}'.",
                        "result": {"playbooks": list_playbooks()},
                    }
                    yield event("result", out)
                    yield event("final", out)
                    return
                out = {"success": True, "response": "Playbook ready.", "result": {"playbook_code": code, "playbook": pb}}
                yield event("result", out)
                yield event("final", out)
                return

            if action == "list_workflows":
                out = {"success": True, "response": "Workflows listed.", "result": {"workflows": list_workflows()}}
                yield event("result", out)
                yield event("final", out)
                return

            if action == "recommend_workflow":
                workflow_code = str(args.get("workflow_code") or args.get("workflow") or "").strip() or "daily_market_monitoring"
                topic = str(args.get("topic") or args.get("query") or args.get("ticker") or "").strip()
                steps = build_workflow_steps(workflow_code, topic=topic)
                if not steps:
                    out = {
                        "success": False,
                        "execution_issue": True,
                        "issue": {
                            "code": "unknown_workflow_code",
                            "message": f"Unknown workflow_code: {workflow_code}",
                            "suggested_fix": "ask_user",
                            "fix_hint": "Ask the user to pick a workflow from the list.",
                        },
                        "response": f"Unknown workflow '{workflow_code}'.",
                        "result": {"workflows": list_workflows()},
                    }
                    yield event("result", out)
                    yield event("final", out)
                    return
                out = {"success": True, "response": "Workflow recommended.", "result": {"workflow_code": workflow_code, "steps": steps}}
                yield event("result", out)
                yield event("final", out)
                return

            if action != "run_cycle":
                out = {
                    "success": False,
                    "execution_issue": True,
                    "issue": {
                        "code": "unknown_action",
                        "message": f"Unknown action: {action}",
                        "suggested_fix": "ask_user",
                        "fix_hint": "Use action=run_cycle|list_playbooks|get_playbook|list_workflows|recommend_workflow",
                    },
                    "response": "Unsupported action. Use action=run_cycle.",
                    "result": {
                        "supported_actions": [
                            "run_cycle",
                            "list_playbooks",
                            "get_playbook",
                            "list_workflows",
                            "recommend_workflow",
                        ]
                    },
                }
                yield event("result", out)
                yield event("final", out)
                return

            symbols = _as_list(args.get("symbols")) or _as_list(args.get("tickers")) or []
            if not symbols:
                out = {
                    "success": False,
                    "need_discovery": True,
                    "missing_info": [
                        {"parameter": "symbols", "reason": "No symbols provided.", "original_query": (user_query or "")[:500]}
                    ],
                    "response": "Provide tool_args.symbols (e.g. ['AAPL','MSFT']).",
                    "result": None,
                }
                yield event("result", out)
                yield event("final", out)
                return

            market = str(args.get("market") or "us-stock").strip().lower()
            mode = str(args.get("mode") or "").strip().lower() or None
            hint = str(args.get("hint") or args.get("query") or "").strip()
            quantity_per_order = float(args.get("quantity_per_order") or 1)
            price_by_symbol: Dict[str, float] = {}
            raw_prices = self.app_config.get("paper_price_by_symbol")
            if isinstance(raw_prices, dict):
                price_by_symbol = {str(k): float(v) for k, v in raw_prices.items() if k is not None and v is not None}
            elif isinstance(raw_prices, str) and raw_prices.strip():
                try:
                    obj = json.loads(raw_prices)
                    if isinstance(obj, dict):
                        price_by_symbol = {str(k): float(v) for k, v in obj.items() if k is not None and v is not None}
                except Exception:
                    price_by_symbol = {}

            account_value = float(self.app_config.get("account_value") or 100000)
            risk_per_trade_pct = float(self.app_config.get("risk_per_trade_pct") or 1.0)
            stop_loss_pct = float(self.app_config.get("stop_loss_pct") or 2.0)
            take_profit_rr = float(self.app_config.get("take_profit_rr") or 2.0)
            data_source = str(self.app_config.get("data_source") or "stooq").strip().lower() or "stooq"

            prior_state = None
            if isinstance(provided_data, dict):
                prior_state = provided_data.get("state") or provided_data.get("next_state")
            state = AgentState()
            if isinstance(prior_state, dict):
                state.version = str(prior_state.get("version") or state.version)
                state.cycle_count = int(prior_state.get("cycle_count") or 0)
                state.bandit = prior_state.get("bandit") or {}
                state.last_actions = prior_state.get("last_actions") or []

            yield event("thought", {"mode_requested": mode, "symbols": symbols[:10], "market": market, "cycle": state.cycle_count + 1})

            result = await run_cycle(
                cfg=cfg,
                state=state,
                symbols=symbols,
                market=market,
                hint=hint,
                requested_mode=mode,
                quantity_per_order=quantity_per_order,
                price_by_symbol=price_by_symbol,
                account_value=account_value,
                risk_per_trade_pct=risk_per_trade_pct,
                stop_loss_pct=stop_loss_pct,
                take_profit_rr=take_profit_rr,
                data_source=data_source,
            )

            out = {
                "success": True,
                "response": f"Cycle complete (mode={result['mode']}, strategy={result['strategy']}, reward={result['reward']}).",
                "result": result,
            }
            yield event("result", out)
            yield event("final", out)

        except Exception as e:
            logger.exception("AutonomousTraderAgentTool failed")
            yield event("error", {"error": str(e)})
            yield event("final", {"success": False, "error": str(e), "response": None})

    async def initialize(self) -> None:
        logger.debug("AutonomousTraderAgentTool initialized")

