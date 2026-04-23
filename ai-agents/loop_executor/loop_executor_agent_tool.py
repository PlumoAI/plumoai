from __future__ import annotations

"""
Loop Executor Agent Tool (app_code: loop_executor)

Planner-side fan-out: given N items (and optional shared context such as KB chunks)
from prior steps plus a natural-language goal, this tool asks the LLM which
operations to run per item (and optionally once in aggregate). It does **not**
invoke other tool agents. It returns `_expand_plan` + `router_steps` with
`top_level: true` so the main chat runner splices those steps into the plan and
executes them like any other plan step.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

from backend.services.ai_agents.base_tool_agent import BaseToolAgent

logger = logging.getLogger(__name__)


def _ev(event_type: str, content: Any) -> Dict:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "content": content,
    }


class LoopExecutorAgentTool(BaseToolAgent):
    """
    Meta-tool: turns items + shared context + a goal into concrete per-item (and
    optional aggregate) plan steps. The main runner executes those steps.
    """

    TOOL_NAME = "Loop Executor"
    APP_CODE = "loop_executor"

    TOOL_DESCRIPTION = """Loop Executor: expands a batch goal into normal plan steps the runner executes (per-item and optional aggregate).

USE when a previous step returned N items (and optional KB/search context) and you need the same workflow on each item and/or one final step after all items.
This tool does not run other tools; it returns expanded steps for the main runner.

DO NOT use for single-item work unless you explicitly want step expansion."""

    # Maximum items to process in one invocation (safety cap)
    MAX_ITEMS = 50

    def __init__(
        self,
        llm_provider: Any,
        agent_id: Optional[str] = None,
        token: Optional[str] = None,
        company_id: Optional[str] = None,
        user_id: Optional[int] = None,
        app_config: Optional[Dict[str, Any]] = None,
    ):
        self.llm_provider = llm_provider
        self.agent_id = agent_id or ""
        self.token = token
        self.company_id = company_id
        self.user_id = user_id
        self.app_config = app_config or {}

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        conversation_history: Optional[Any] = None,
        system_prompt: Optional[str] = None,
        agent_guidance: Optional[Any] = None,
        # injected by runner — name-keyed dict of live agent instances
        tool_registry: Optional[Dict[str, Any]] = None,
        # injected by runner — list of {name, app_code, custom_name} metadata
        available_tools: Optional[List[Dict]] = None,
        **tool_args: Any,
    ) -> AsyncGenerator[Dict, None]:
        # Runner may inject these for other tools; unused here (plan expansion only).
        _ = (tool_registry, session_id, conversation_history, system_prompt, agent_guidance, tool_args)

        yield _ev("thought", f"Loop Executor starting — operation: {user_query[:120]}")

        # 1. Split provided_data into items-to-iterate and shared context -----
        items, shared_context = self._split_items_and_context(provided_data)
        if not items:
            yield _ev("thought", "No items found in provided_data — nothing to loop over")
            yield _ev("final", {
                "success": False,
                "response": "Loop Executor received no items to process. "
                            "Ensure a previous step fetches the list first.",
                "items_processed": 0,
            })
            return

        items = items[: self.MAX_ITEMS]
        logger.info(
            "🔁 Loop Executor: %d items to process | %d shared-context objects",
            len(items), len(shared_context),
        )
        yield _ev("thought", f"Processing {len(items)} items sequentially")

        # 2. Ask LLM to plan all operations (per-item + aggregate) --------
        plan = await self._plan_operations(
            user_query=user_query,
            items=items,
            shared_context=shared_context,
            available_tools=available_tools or [],
        )
        if plan is None:
            yield _ev("thought", "Could not generate operations plan — falling back")
            yield _ev("final", {
                "success": False,
                "response": "Loop Executor could not determine what tool calls to make for the items.",
                "items_processed": 0,
            })
            return

        per_item_ops: List[Dict] = plan.get("per_item_operations", [])
        aggregate_ops: List[Dict] = plan.get("aggregate_operations", [])

        logger.info(
            "🔁 Loop Executor plan: %d per-item groups, %d aggregate calls",
            len(per_item_ops), len(aggregate_ops),
        )

        router_steps = self._build_router_steps(
            user_query=user_query,
            items=items,
            shared_context=shared_context,
            per_item_ops=per_item_ops,
            aggregate_ops=aggregate_ops,
        )
        if not router_steps:
            yield _ev("thought", "No executable steps produced from plan")
            yield _ev("final", {
                "success": False,
                "response": "Loop Executor produced no tool steps to run.",
                "items_processed": len(items),
            })
            return

        yield _ev("final", {
            "success": True,
            "_expand_plan": True,
            "router_steps": router_steps,
            "response": (
                f"Unrolled batch into {len(router_steps)} plan step(s); "
                "the runner will execute each like a normal step."
            ),
            "items_processed": len(items),
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _describe_item(self, item: Any, fallback: str) -> str:
        """
        Derive the most human-readable identity string from an item dict.
        Fully dynamic — scans all keys, scores them by how identifier-like they are,
        picks the best 1-2 fields. No field names hardcoded.
        """
        if not isinstance(item, dict):
            return fallback or str(item)[:80]

        # Score each key: prefer keys whose names suggest human identity
        # (name, title, email, label, subject, company, etc.) over IDs and booleans.
        # Scoring is based on common patterns, not hardcoded field lists.
        def _score(k: str, v: Any) -> int:
            if not isinstance(v, (str, int, float)) or not str(v).strip():
                return -1
            kl = k.lower()
            # Skip raw ID-looking values (long random strings, UUIDs)
            vs = str(v).strip()
            if len(vs) > 60:
                return -1
            if len(vs) > 30 and all(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in vs) and "_" in vs:
                return -1  # Looks like an internal ID
            score = 0
            # Human-identity signals
            if any(x in kl for x in ("name", "title", "full", "display")):
                score += 10
            if any(x in kl for x in ("email", "mail")):
                score += 8
            if any(x in kl for x in ("phone", "mobile", "contact")):
                score += 6
            if any(x in kl for x in ("label", "subject", "company", "org")):
                score += 5
            # Penalise ID fields
            if kl.endswith("_id") or kl == "id" or kl.endswith("id"):
                score -= 5
            # Penalise numeric-only values for non-numeric fields
            if str(v).isdigit() and not any(x in kl for x in ("number", "num", "count", "index", "key")):
                score -= 2
            return score

        scored = sorted(
            [(k, v, _score(k, v)) for k, v in item.items()],
            key=lambda t: t[2],
            reverse=True,
        )
        top = [(k, str(v).strip()) for k, v, s in scored if s > 0][:2]
        if not top:
            return fallback or str(item)[:80]
        return " | ".join(v for _, v in top)

    # Keys that structurally identify KB/search content — shared context, not items to loop
    _CONTEXT_KEYS = frozenset({"chunk_text", "relevance_score", "chunk_id", "chunk_index"})
    _SEARCH_WRAPPER_KEYS = frozenset({"query", "results", "search_depth", "confidence"})

    def _split_items_and_context(self, provided_data: Any) -> tuple[List[Any], List[Any]]:
        """
        Split provided_data into:
          items         — entity rows to iterate over (CRM records, contacts, etc.)
          shared_context — knowledge/reference data passed to every iteration (KB chunks, search results)

        Detection is purely structural — no field names are hardcoded except the
        universal KB shape signals (chunk_text, relevance_score, chunk_id).
        """
        if provided_data is None:
            return [], []

        # Normalise to a flat list first
        flat: List[Any] = []
        if isinstance(provided_data, list):
            if len(provided_data) == 1 and isinstance(provided_data[0], list):
                flat = provided_data[0]
            else:
                flat = list(provided_data)
        elif isinstance(provided_data, dict):
            for key in ("data", "records", "items", "results", "list"):
                val = provided_data.get(key)
                if isinstance(val, list):
                    flat = val
                    break
            if not flat:
                flat = [provided_data]

        items: List[Any] = []
        shared_context: List[Any] = []

        for obj in flat:
            if not isinstance(obj, dict):
                items.append(obj)
                continue
            keys = set(obj.keys())
            # KB chunk row
            if keys & self._CONTEXT_KEYS:
                shared_context.append(obj)
            # Search result wrapper (has both query + results list)
            elif ("query" in keys and "results" in keys) or (keys & self._SEARCH_WRAPPER_KEYS and "results" in keys):
                shared_context.append(obj)
                # Also flatten the individual chunks inside results into shared_context
                inner = obj.get("results")
                if isinstance(inner, list):
                    for chunk in inner:
                        if isinstance(chunk, dict) and set(chunk.keys()) & self._CONTEXT_KEYS:
                            shared_context.append(chunk)
            # Pure-ID/metadata dict with no entity fields — IDs dict from runner
            elif all(
                isinstance(v, str) and ("_id" in k.lower() or k.lower().endswith("id"))
                for k, v in obj.items()
                if v
            ) and len(obj) <= 8:
                shared_context.append(obj)
            else:
                # Default: treat as an entity item to loop over
                items.append(obj)

        logger.info(
            "🔁 _split_items_and_context: %d items | %d shared-context objects",
            len(items), len(shared_context),
        )
        return items, shared_context

    def _build_router_steps(
        self,
        *,
        user_query: str,
        items: List[Any],
        shared_context: List[Any],
        per_item_ops: List[Dict],
        aggregate_ops: List[Dict],
    ) -> List[Dict[str, Any]]:
        """Build `router_steps` with top_level for the runner's plan expansion."""
        router_steps: List[Dict[str, Any]] = []
        n_items = max(len(items), 1)

        for op in per_item_ops:
            item_index = int(op.get("item_index") or 0)
            item_label = op.get("item_label", f"item {item_index}")
            calls = op.get("calls", []) or []
            current_item = items[item_index] if 0 <= item_index < len(items) else {}
            item_identity = self._describe_item(current_item, item_label)

            for call in calls:
                tool_name = (call.get("tool_name") or "").strip()
                call_query = call.get("query", user_query)
                if not tool_name:
                    continue
                seed: List[Any] = [
                    {
                        "_loop_item_index": item_index,
                        "_loop_item_label": item_label,
                        "_loop_item_identity": item_identity,
                    }
                ]
                # Provide shared context + only this item's data (no other items).
                # Keep dynamic: shared_context is derived structurally (KB chunks, search results, id maps).
                if shared_context:
                    seed.extend([x for x in shared_context[:40] if isinstance(x, (dict, list, str, int, float, bool)) or x is None])
                if isinstance(current_item, dict) and current_item:
                    seed.append(current_item)
                extra = call.get("tool_args") if isinstance(call.get("tool_args"), dict) else {}
                arguments: Dict[str, Any] = dict(extra)
                arguments["_provided_data_seed"] = seed
                router_steps.append({
                    "top_level": True,
                    "tool_name": tool_name,
                    "action": f"[{item_index + 1}/{n_items}] {item_identity} — {tool_name}",
                    "query": call_query,
                    "arguments": arguments,
                })

        for agg_call in aggregate_ops or []:
            tool_name = (agg_call.get("tool_name") or "").strip()
            call_query = agg_call.get("query", user_query)
            if not tool_name:
                continue
            seed = [
                {"_loop_aggregate": True, "items_count": len(items)},
                {"items": items, "shared_context": shared_context},
            ]
            extra = agg_call.get("tool_args") if isinstance(agg_call.get("tool_args"), dict) else {}
            arguments = dict(extra)
            arguments["_provided_data_seed"] = seed
            router_steps.append({
                "top_level": True,
                "tool_name": tool_name,
                "action": f"[aggregate] {tool_name}",
                "query": call_query,
                "arguments": arguments,
            })

        return router_steps

    async def _plan_operations(
        self,
        user_query: str,
        items: List[Any],
        shared_context: List[Any],
        available_tools: List[Dict],
    ) -> Optional[Dict]:
        """
        Ask the LLM to generate a concrete execution plan with two phases:
          - per_item_operations: runs for EACH item (fan-out)
          - aggregate_operations: runs ONCE after all items (fan-in), optional

        Returns:
          {
            "per_item_operations": [
              {
                "item_index": 0,
                "item_label": "Muhmaad Hussain",
                "calls": [
                  {"tool_name": "Gmail New Agent", "query": "Send email to m@x.com..."},
                  {"tool_name": "PlumoAI - Sales CRM", "query": "Update record EKAd... to Contacted"},
                ]
              },
              ...
            ],
            "aggregate_operations": [
              {"tool_name": "Slack", "query": "Post summary: N emails sent..."}
            ]
          }

        Either phase can be empty [] if not needed.
        """
        tool_names = [t.get("name") or t.get("custom_name") or t.get("app_code", "") for t in available_tools]
        tool_list_str = ", ".join(f'"{n}"' for n in tool_names if n)

        # Detect any tool that self-declares as a content composer (COMPOSE_BEFORE_DELIVER keyword).
        # Fully dynamic — no tool names hardcoded here; the tool owns its own rule via description.
        _compose_tools = [
            t for t in available_tools
            if (t.get("description") or "").strip().upper().startswith("COMPOSE_BEFORE_DELIVER:")
        ]
        _compose_rule = ""
        if _compose_tools:
            _cnames = ", ".join(f'"{t.get("name") or t.get("app_code", "")}"' for t in _compose_tools)
            _compose_rule = (
                f"\nCOMPOSE BEFORE DELIVER RULE (MANDATORY):\n"
                f"Tool(s) {_cnames} declare they must run BEFORE any call that sends, delivers, "
                f"or transmits content to recipients (email, message, document, report).\n"
                f"For every call whose tool description indicates it sends/delivers content externally:\n"
                f"  - Insert a prior call to {_cnames} to compose the content first\n"
                f"  - The delivery call's query must reference the composed content\n"
                f"Base this decision on tool descriptions only — never infer roles from tool names.\n"
            )

        # Compact item representation — include all fields so the LLM can inject real IDs
        items_json = json.dumps(items[:20], default=str, ensure_ascii=False)

        # Shared context summary (first 2 KB chunks text + structured_data if present)
        _shared_context_summary = ""
        if shared_context:
            _sc_lines = []
            _chunk_count = 0
            for sc in shared_context:
                if not isinstance(sc, dict):
                    continue
                # Structured data summary (service list)
                if "structured_data" in sc and isinstance(sc.get("structured_data"), dict):
                    sd = sc["structured_data"]
                    items_list = sd.get("items") or []
                    cats = sd.get("categories") or {}
                    if items_list:
                        _sc_lines.append("Extracted knowledge items:")
                        if cats:
                            for cat, cat_items in cats.items():
                                _sc_lines.append(f"  {cat}: {', '.join(str(x) for x in cat_items[:20])}")
                        else:
                            for it in items_list[:20]:
                                _sc_lines.append(f"  • {it}")
                # Raw chunk text (first 2 only to keep prompt lean)
                elif "chunk_text" in sc and _chunk_count < 2:
                    _sc_lines.append(f"Knowledge chunk: {str(sc.get('chunk_text', ''))[:600]}")
                    _chunk_count += 1
            _shared_context_summary = "\n".join(_sc_lines)

        _shared_ctx_section = ""
        if _shared_context_summary:
            _shared_ctx_section = f"""
SHARED CONTEXT (available to every per-item call — do NOT re-fetch; reference it in queries):
{_shared_context_summary}
"""

        prompt = f"""You are an operation planner for a loop executor agent.
{_compose_rule}
TASK: {user_query}

ITEMS TO PROCESS:
{items_json}
{_shared_ctx_section}
AVAILABLE TOOLS: [{tool_list_str}]

Generate a concrete two-phase execution plan:

PHASE 1 — per_item_operations: operations that run for EACH item individually (fan-out).
PHASE 2 — aggregate_operations: operations that run ONCE after ALL items are done (fan-in). Leave empty [] if not needed.

IMPORTANT — ordering within one item's "calls" array:
The main runner executes expanded steps in list order. Each step gets the item row plus
shared context above in provided_data, and later steps also see prior steps' results the
same way as in any normal multi-step plan. List tools in dependency order (e.g. compose,
then send, then CRM update); you do not need to paste full composed bodies into a send
query when the compose step is listed immediately before it.

Use real field values from the items (IDs, emails, names) — never placeholders.

Return ONLY valid JSON, no markdown, no explanation:
{{
  "per_item_operations": [
    {{
      "item_index": 0,
      "item_label": "<name or identifier of this item>",
      "calls": [
        {{
          "tool_name": "<exact tool name from AVAILABLE TOOLS>",
          "query": "<complete, self-contained query for this item with real values injected>"
        }}
      ]
    }}
  ],
  "aggregate_operations": [
    {{
      "tool_name": "<exact tool name from AVAILABLE TOOLS>",
      "query": "<complete query that summarizes or acts on all results — include counts, summaries, real values>"
    }}
  ]
}}

Rules:
- item_label: use Name, email, title, or any human-readable identifier from the item
- tool_name: must exactly match one of the AVAILABLE TOOLS
- query: must be a complete standalone instruction with real IDs/values, not references like "the record"
- per_item_operations: one entry per item, in order. Empty [] if the task only needs an aggregate step.
- aggregate_operations: only include if the task requires a post-loop action (e.g. send one summary, create a report). Otherwise []
- If the task requires multiple tools per item (e.g. compose + send + update CRM), list all calls under that item's "calls" array in the correct order"""

        try:
            response = await asyncio.wait_for(
                self.llm_provider.get_response(prompt, max_tokens=3000),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error("⏱️ Loop Executor: LLM plan timed out")
            return None
        except Exception as e:
            logger.error("❌ Loop Executor: LLM plan failed: %s", e)
            return None

        # Parse LLM response
        try:
            text = (response or "").strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]).strip()
            parsed = json.loads(text)
            # Validate structure — both keys are required; allow empty lists
            if isinstance(parsed.get("per_item_operations"), list) or isinstance(parsed.get("aggregate_operations"), list):
                parsed.setdefault("per_item_operations", [])
                parsed.setdefault("aggregate_operations", [])
                logger.info(
                    "🔁 Loop Executor plan: %d per-item groups, %d aggregate calls",
                    len(parsed["per_item_operations"]),
                    len(parsed["aggregate_operations"]),
                )
                return parsed
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error("❌ Loop Executor: failed to parse LLM plan: %s | raw: %s", e, (response or "")[:300])

        return None
