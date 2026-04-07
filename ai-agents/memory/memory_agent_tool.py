from __future__ import annotations

"""
Autonomous Memory Agent Tool (app_code: memory)

Refactored implementation: this file keeps the public tool surface
(`MemoryAgentTool`) while delegating responsibilities to modules in this
plugin package (API client, JSON utils, scoring, tagging, etc.).
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Set

from backend.services.app_agents.base_tool_agent import BaseToolAgent

from .api_client import MemoryApiClient
from .constants import (
    COMPANY_URL,
    IMPORTANCE_THRESHOLD,
    DUPLICATE_OVERLAP_THRESHOLD,
    POST_SUMMARY_DUPLICATE_THRESHOLD,
    CONTRADICTION_OVERLAP_MIN,
)
from .events import AgentEvent, event
from .json_utils import parse_json_response
from .llm_client import MemoryLlmClient
from .normalization import normalize_memories
from .scoring import (
    log_freq,
    count_similar_in_list,
    compute_retrieval_boost,
    compute_decay_factor,
    effective_threshold,
    compute_confidence,
    score_relevance,
)
from .tagging import extract_tags, KNOWN_TOOL_CODES

logger = logging.getLogger(__name__)


# ─── Tool ────────────────────────────────────────────────────────────────────


class MemoryAgentTool(BaseToolAgent):
    """
    Autonomous long-term memory backed by the PlumoAI Memory API.
    """

    TOOL_NAME = "Memory"
    APP_CODE = "memory"

    TOOL_DESCRIPTION = """Memory: Autonomous long-term memory that persists user-specific knowledge across sessions.

The tool reasons about the conversation on its own — the planner does NOT need to decide whether
to store or recall. Call with operation="auto" and pass the raw user message; the tool's internal
LLM will determine what (if anything) should be stored, recalled, or both.

USE THIS TOOL when the conversation involves:
  • Personal identity, profession, location, relationships, or contact/identifier details (e.g. email, phone, address) the user shares for future use
  • User goals, active projects, or long-term objectives
  • Stated preferences, habits, or style choices
  • Corrections or updates to previously stated facts
  • Requests to recall past information or personal context

DO NOT USE for:
  • Live data (schedules, databases, files) — use the appropriate data tool instead
  • General knowledge unrelated to this specific user
  • Arithmetic or date/time operations

USE THIS TOOL ALSO when the user says:
  • "Forget my X preference" / "Remove X from memory" → operation="forget"
  • "Change X to Y" / "Actually my name is Y" / "Correct that to Y" → operation="update"

OPERATIONS (tool_args.operation):
  auto     – [DEFAULT] The tool reads the message and conversation context, then decides
             autonomously whether to store, recall, update, forget, or do nothing.
               user_message     : the raw user turn text
               conversation     : recent turns as context  (optional but improves accuracy)
               active_goal      : current project/goal     (optional)
  store    – Explicitly persist a specific piece of text (use when the planner is certain)
               content, type_hint, active_goal, raw_context
  recall   – Explicitly retrieve memories for a query (use when the planner is certain)
               query, limit (default 10)
  update   – Replace the content of an existing memory with corrected/new information
               content_match (describes the OLD fact to locate), new_content (replacement)
               Optionally: memory_id (if the ID is known exactly)
  forget   – Permanently delete a memory by ID or description
               memory_id OR content_match (e.g. "urdu language preference", "name Hussain")
  reflect  – Periodic maintenance: prune stale, decay unused, merge duplicates, resolve conflicts
  evaluate – Dry-run importance scoring without storing
  list     – List all stored memories sorted by importance"""

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
        self.agent_id = agent_id or "default"
        self.token = token or ""
        self.company_id = company_id or ""
        self.user_id = user_id
        self.app_config = app_config or {}
        self.threshold = float(self.app_config.get("importance_threshold", IMPORTANCE_THRESHOLD))

        self._llm = MemoryLlmClient(llm_provider)
        self._api = MemoryApiClient(
            agent_id=self.agent_id,
            token=self.token,
            company_id=self.company_id,
            user_id=self.user_id,
        )

        # Dynamically extended tool codes from connected apps (merged with KNOWN_TOOL_CODES)
        self._connected_tool_codes: frozenset[str] = frozenset()

    def set_connected_tool_codes(self, codes: List[str]) -> None:
        if codes:
            self._connected_tool_codes = frozenset(c.strip().lower() for c in codes if c)

    def _effective_tool_codes(self) -> Set[str]:
        return set(KNOWN_TOOL_CODES) | set(self._connected_tool_codes)

    def _tool_codes_for_prompt(self) -> str:
        return ", ".join(sorted(self._effective_tool_codes()))

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION

    def get_description(self) -> str:
        return self.get_tool_responsibility()

    async def initialize(self) -> None:
        if not COMPANY_URL:
            logger.warning("Memory agent: COMPANY_URL not set — API calls will be no-ops")
        logger.info(
            "Memory agent initialised (agent_id=%s, user_id=%s, base_threshold=%.2f — thresholds set per-content by LLM)",
            self.agent_id,
            self.user_id,
            self.threshold,
        )

    async def cleanup(self) -> None:
        logger.debug("Memory agent cleaned up")

    # ─── Session-start context injection ──────────────────────────────────────

    async def get_session_context(self) -> str:
        import asyncio

        try:
            personal_data, shared_data = await asyncio.gather(
                self._api.list(limit=20, offset=0, mem_type=None),
                self._api.list_shared(limit=10, offset=0),
                return_exceptions=True,
            )
            personal_raw = personal_data.get("memories", []) if isinstance(personal_data, dict) else []
            shared_raw = shared_data.get("memories", []) if isinstance(shared_data, dict) else []
            if not personal_raw and not shared_raw:
                return ""

            seen: set[str] = set()
            merged: List[Dict[str, Any]] = []
            for m in personal_raw + shared_raw:
                fp = (m.get("content") or "").strip().lower()[:120]
                if fp and fp not in seen:
                    seen.add(fp)
                    merged.append(m)

            memories = normalize_memories(merged)
            memories.sort(key=lambda m: float(m.get("importance_score") or 0), reverse=True)
            candidates = memories[:20]

            numbered = "\n".join(
                f"{i + 1}. [scope={m.get('scope','personal')} type={m.get('type','unknown')}] {m.get('content', '').strip()}"
                for i, m in enumerate(candidates)
            )

            prompt = f"""You are reviewing stored memories for an AI assistant.

STORED MEMORIES:
{numbered}

TASK:
Build the MINIMAL "always-on" context the agent must carry into EVERY turn.
This is NOT a static category split — decide dynamically based on usefulness.

Include only what must be applied globally and continuously, such as:
  - Standing behavioral rules (tone, language, address style, strict constraints)
  - Safety/authority rules (who the agent should obey, access constraints)
  - Critical identity facts that would break the conversation if forgotten
    (e.g. user's name IF the user consistently expects it, accessibility needs)

Exclude:
  - One-off episodic details
  - Long lists or anything that should be recalled on demand instead

If you include facts, write them as short, standalone statements.

Respond with JSON only:
{{
  "inject": true | false,
  "context": "A concise, ready-to-use ALWAYS-ON context block (plain text). Empty string if none.",
  "reason": "one sentence explaining why this must be always-on"
}}"""

            raw = await self._llm.generate_json(prompt, max_tokens=300)
            if not raw:
                return ""

            parsed = parse_json_response(
                raw,
                required_keys=["inject", "context", "reason"],
                defaults={"inject": False, "context": "", "reason": ""},
            )
            if not parsed or not parsed.get("inject"):
                return ""

            ctx_text = (parsed.get("context") or "").strip()
            if not ctx_text:
                return ""

            logger.info("🧠 [Memory] Always-on context injected (%d chars): %s", len(ctx_text), ctx_text[:120])
            return (
                "\n\n🚨 ALWAYS-ON MEMORY CONTEXT — apply to EVERY response without exception:\n"
                f"{ctx_text}\n"
                "(This block is dynamically derived from long-term memory. "
                "Treat it as mandatory unless the user explicitly changes it.)"
            )
        except Exception as exc:
            logger.warning("get_session_context failed: %s", exc)
            return ""

    # ─── Internal helpers ─────────────────────────────────────────────────────

    def _scores_for_api(self, eval_result: Dict[str, Any]) -> Dict[str, float]:
        identity = float(eval_result.get("identity_score") or 0)
        goal = float(eval_result.get("goal_relevance") or 0)
        freq = float(eval_result.get("frequency") or 0)
        emotional = float(eval_result.get("emotional_weight") or 0)
        return {
            "recency": round(emotional, 4),
            "relevance": round((identity + goal) / 2, 4),
            "frequency": round(freq, 4),
        }

    async def _safe_patch(self, memory_id: Optional[str], updates: Dict[str, Any]) -> bool:
        if not memory_id:
            return False
        try:
            await self._api.patch(memory_id=memory_id, updates=updates)
            return True
        except Exception as exc:
            logger.warning("Memory PATCH failed for id=%s: %s", memory_id, exc)
            return False

    async def _safe_delete(self, memory_id: Optional[str]) -> bool:
        if not memory_id:
            return False
        try:
            return await self._api.delete(memory_id=memory_id)
        except Exception as exc:
            logger.warning("Memory DELETE failed for id=%s: %s", memory_id, exc)
            return False

    async def _expand_recall_queries(self, user_query: str) -> List[str]:
        prompt = f"""A user asked: "{(user_query or '').strip()[:200]}"

We need to search stored memories. The exact phrase might not match.
Suggest 1–3 short search queries (different phrasings or concepts) that could match relevant stored memory content (e.g. identity, name, preferences, facts about the user).

Respond with JSON only:
{{
  "queries": ["...", "..."]
}}"""
        raw = await self._llm.generate_json(prompt, max_tokens=150)
        parsed = parse_json_response(raw or "", required_keys=["queries"], defaults={"queries": []}) or {}
        qs = parsed.get("queries") or []
        out: List[str] = []
        for q in qs:
            if isinstance(q, str) and q.strip():
                out.append(q.strip()[:120])
        return out[:3]

    async def _detect_contradiction(self, content: str, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not candidates or not self.llm_provider:
            return None
        candidates = normalize_memories(candidates)

        cw = set(re.findall(r"\b\w{3,}\b", content.lower()))
        related: List[Dict[str, Any]] = []
        for m in candidates:
            mw = set(re.findall(r"\b\w{3,}\b", (m.get("content") or "").lower()))
            union = len(cw | mw)
            if union and len(cw & mw) / union >= CONTRADICTION_OVERLAP_MIN:
                related.append(m)
        if not related:
            return None

        existing_block = "\n".join(f"[{(m.get('memory_id') or '')[:8]}] {m.get('content','')}" for m in related[:10])
        prompt = f"""You are a belief-consistency checker for an AI agent's memory system.

New information:
"{content}"

Existing memories (ID prefix shown):
{existing_block}

Does the new information DIRECTLY CONTRADICT any existing memory?
Contradiction means the new info states the OPPOSITE or REPLACES a specific fact
(e.g. old: "prefers short answers", new: "wants detailed responses").
Minor additions or elaborations are NOT contradictions.

Output ONLY valid JSON:
{{
  "contradicts": true,
  "conflicting_id_prefix": "a1b2c3d4",
  "reason": "one sentence explaining the contradiction"
}}
OR if no contradiction:
{{
  "contradicts": false,
  "conflicting_id_prefix": null,
  "reason": "no conflict found"
}}"""
        raw = await self._llm.generate_json(prompt, max_tokens=300)
        parsed = parse_json_response(
            raw or "",
            required_keys=["contradicts", "conflicting_id_prefix", "reason"],
            defaults={"contradicts": False, "conflicting_id_prefix": None, "reason": ""},
        )
        if not parsed or not parsed.get("contradicts"):
            return None

        prefix = (parsed.get("conflicting_id_prefix") or "").strip()
        if not prefix or len(prefix) < 4:
            return None

        for m in related:
            mid = (m.get("memory_id") or m.get("_id") or "").strip()
            if mid and mid.startswith(prefix):
                m["_contradiction_reason"] = parsed.get("reason") or "Contradiction detected"
                return m
        return None

    async def _evaluate_importance(self, content: str, type_hint: Optional[str], existing_memories: List[Dict[str, Any]]) -> Dict[str, Any]:
        similarity_count = count_similar_in_list(content, existing_memories)

        similar = [
            m
            for m in existing_memories
            if (lambda cw, mw: len(cw & mw) / len(cw | mw) > 0.35 if (cw | mw) else False)(
                set(re.findall(r"\b\w{3,}\b", content.lower())),
                set(re.findall(r"\b\w{3,}\b", (m.get("content") or "").lower())),
            )
        ]
        avg_access = (sum(float(m.get("access_count") or 0) for m in similar) / len(similar)) if similar else 0
        log_freq_score = log_freq(similarity_count)
        type_str = f" (Type hint: {type_hint})" if type_hint else ""

        prompt = f"""You are a memory importance evaluator for an autonomous AI agent.
Score the following information on four dimensions (0.0 – 1.0 each).

Information to evaluate{type_str}:
"{content}"

Context:
- {similarity_count} semantically similar memories already stored
- Average retrieval count of similar memories: {avg_access:.1f}
- Log-scaled frequency signal: {log_freq_score:.2f}  ← use this as your frequency score

━━━ DIMENSION DEFINITIONS ━━━

1. identity_score  — How permanently and broadly does this shape all future interactions?
   Score 0.7–1.0 for ANY of:
     • Direct identity: name, job title, location, relationships, or contact/identifier details (e.g. email, phone) the user shares for future use
     • Behavioral identity: communication language/script ("always in urdu roman"),
       address style ("call me sir"), tone preference, response format instructions,
       authority rules ("only answer to me"), role definitions
     • Anything the agent MUST remember and apply in EVERY future response
   Score 0.3–0.6 for context-specific preferences (apply in some situations)
   Score 0.0–0.2 for transient or one-off statements

2. goal_relevance  — Reveals active goals or strategic direction?

3. frequency       — Use the log-scaled frequency signal ({log_freq_score:.2f}) provided above.

4. emotional_weight — Emphasised, corrected, or emotionally significant?

━━━ THRESHOLD GUIDANCE ━━━
Set `threshold` to the minimum importance needed to store this content (0.0–1.0).
  • Behavioral rules / communication preferences that affect ALL responses → 0.25–0.35
  • Stable identity facts (name, role) → 0.30–0.40
  • Goal or preference statements → 0.40–0.55
  • Transient or low-value events → 0.60–0.75

━━━ ALSO PROVIDE ━━━
  type            : a short free-form label for the memory category (no fixed list).
                    TOOL-SCOPE RULE: If the content is a preference, rule, or setting that
                    applies specifically to a single AI tool (not all responses), PREFIX the
                    type with the tool code followed by a colon.
                    Connected tool codes: {self._tool_codes_for_prompt()}.
  summary         : 1-2 sentence storage-ready concise summary (no filler words)
  store           : true / false
  already_covered : true if ANY candidate already captures the same fact (even with different wording)
  reason          : one sentence explaining the storage decision
  scope           : "personal" | "shared" (when in doubt, choose personal)

━━━ FORMULA ━━━
importance = 0.4·identity + 0.3·goal + 0.2·frequency + 0.1·emotional

Output ONLY valid JSON:
{{
  "identity_score": 0.0,
  "goal_relevance": 0.0,
  "frequency": 0.0,
  "emotional_weight": 0.0,
  "importance": 0.0,
  "type": "fact",
  "threshold": 0.5,
  "summary": "...",
  "store": false,
  "already_covered": false,
  "reason": "...",
  "scope": "personal"
}}"""

        raw = await self._llm.generate_json(prompt, max_tokens=600)
        parsed = parse_json_response(
            raw or "",
            required_keys=[
                "identity_score",
                "goal_relevance",
                "frequency",
                "emotional_weight",
                "importance",
                "type",
                "threshold",
                "summary",
                "store",
                "already_covered",
                "reason",
                "scope",
            ],
            defaults={
                "identity_score": 0.0,
                "goal_relevance": 0.0,
                "frequency": log_freq_score,
                "emotional_weight": 0.0,
                "importance": 0.0,
                "type": type_hint or "fact",
                "threshold": self.threshold,
                "summary": content[:300],
                "store": True,
                "already_covered": False,
                "reason": "LLM evaluated",
                "scope": "personal",
            },
        )
        if not parsed:
            # Lightweight fallback when LLM is unreachable: preserve type_hint and log_freq signal.
            return {
                "identity_score": 0.0,
                "goal_relevance": 0.0,
                "frequency": log_freq_score,
                "emotional_weight": 0.0,
                "importance": 0.0,
                "type": (type_hint or "unknown").strip().lower(),
                "threshold": self.threshold,
                "summary": content[:300],
                "store": True,
                "already_covered": False,
                "reason": "LLM unavailable",
                "scope": "personal",
                "similarity_count": similarity_count,
            }

        identity = max(0.0, min(1.0, float(parsed.get("identity_score") or 0)))
        goal = max(0.0, min(1.0, float(parsed.get("goal_relevance") or 0)))
        freq = max(0.0, min(log_freq_score + 0.1, float(parsed.get("frequency") or log_freq_score)))
        emotional = max(0.0, min(1.0, float(parsed.get("emotional_weight") or 0)))
        importance = round(0.4 * identity + 0.3 * goal + 0.2 * freq + 0.1 * emotional, 4)

        raw_threshold = parsed.get("threshold")
        try:
            llm_threshold = round(max(0.05, min(0.99, float(raw_threshold))), 4)
        except (TypeError, ValueError):
            llm_threshold = self.threshold

        parsed["identity_score"] = identity
        parsed["goal_relevance"] = goal
        parsed["frequency"] = round(freq, 4)
        parsed["emotional_weight"] = emotional
        parsed["importance"] = importance
        parsed["threshold"] = llm_threshold
        parsed["similarity_count"] = similarity_count
        parsed["store"] = True
        raw_scope = str(parsed.get("scope") or "personal").strip().lower()
        parsed["scope"] = "shared" if raw_scope == "shared" else "personal"
        return parsed

    async def _decompose_content(self, content: str, type_hint: Optional[str]) -> List[Dict[str, Any]]:
        if not content or len(content.split()) < 4:
            return [{"content": content, "type_hint": type_hint, "scope_hint": "personal"}]

        type_str = f" (category hint: {type_hint})" if type_hint else ""
        prompt = f"""You are a memory extraction engine for an AI assistant.
Your job: read the message and extract EVERY distinct, independently-useful
memory unit it contains — there may be one, or there may be many.

Message{type_str}:
"{content}"

Each memory unit must be ATOMIC — one fact, one rule, one attribute.
Assign scope per unit: personal vs shared.
Each content must be self-contained (avoid "I/me/my" pronouns).

Respond with JSON only:
{{
  "decompose": true | false,
  "reasoning": "...",
  "pieces": [
    {{
      "content": "...",
      "type_hint": "...",
      "scope_hint": "personal" | "shared"
    }}
  ]
}}"""
        raw = await self._llm.generate_json(prompt, max_tokens=600)
        if not raw:
            return [{"content": content, "type_hint": type_hint, "scope_hint": "personal"}]

        parsed = parse_json_response(
            raw,
            required_keys=["decompose", "pieces"],
            defaults={"decompose": False, "pieces": []},
        )
        if not parsed or not parsed.get("decompose") or not parsed.get("pieces"):
            return [{"content": content, "type_hint": type_hint, "scope_hint": "personal"}]

        pieces: List[Dict[str, Any]] = []
        for p in parsed.get("pieces") or []:
            if not isinstance(p, dict):
                continue
            piece_content = (p.get("content") or "").strip()
            if not piece_content:
                continue
            raw_scope = str(p.get("scope_hint") or "personal").strip().lower()
            pieces.append(
                {
                    "content": piece_content,
                    "type_hint": (p.get("type_hint") or type_hint or "").strip() or None,
                    "scope_hint": "shared" if raw_scope == "shared" else "personal",
                }
            )
        return pieces if pieces else [{"content": content, "type_hint": type_hint, "scope_hint": "personal"}]

    async def _evaluate_piece(
        self,
        *,
        content: str,
        type_hint: Optional[str],
        raw_context: Optional[str],
        active_goal: Optional[str],
        scope_hint: Optional[str],
    ) -> Dict[str, Any]:
        raw_candidates = await self._api.recall(query=content, limit=8)
        candidates = normalize_memories(raw_candidates)

        content_words = set(re.findall(r"\b\w{4,}\b", content.lower()))
        _MIN_CONTENT_WORDS = 3
        if len(content_words) >= _MIN_CONTENT_WORDS:
            for existing in candidates:
                existing_words = set(re.findall(r"\b\w{4,}\b", (existing.get("content") or "").lower()))
                union = len(content_words | existing_words)
                if union and len(content_words & existing_words) / union > DUPLICATE_OVERLAP_THRESHOLD:
                    mem_id = existing.get("_id") or existing.get("memory_id")
                    new_access = int(existing.get("access_count") or 0) + 1
                    retrieval_boost = compute_retrieval_boost({**existing, "access_count": new_access})
                    new_score = round(min(1.0, float(existing.get("importance_score") or 0) + retrieval_boost), 4)
                    await self._safe_patch(
                        mem_id,
                        {
                            "access_count": new_access,
                            "importance_score": new_score,
                            "last_accessed_at": datetime.utcnow().isoformat() + "Z",
                        },
                    )
                    return {
                        "ready": False,
                        "stored": False,
                        "duplicate": True,
                        "reason": "Duplicate (raw Jaccard) — updated retrieval boost on existing memory",
                        "existing_memory_id": mem_id,
                        "existing_content": existing.get("content"),
                        "updated_importance_score": new_score,
                    }

        conflicting = await self._detect_contradiction(content, candidates)
        overwritten_id: Optional[str] = None
        if conflicting:
            overwritten_id = conflicting.get("memory_id") or conflicting.get("_id")
            if overwritten_id:
                await self._safe_delete(overwritten_id)

        eval_type_hint = type_hint
        if scope_hint in ("personal", "shared") and type_hint:
            eval_type_hint = f"{type_hint} [scope: {scope_hint}]"
        elif scope_hint in ("personal", "shared"):
            eval_type_hint = f"scope: {scope_hint}"
        eval_result = await self._evaluate_importance(content, eval_type_hint, candidates)

        if eval_result.get("already_covered"):
            if candidates:
                best = candidates[0]
                mem_id = best.get("_id") or best.get("memory_id")
                if mem_id:
                    new_access = int(best.get("access_count") or 0) + 1
                    retrieval_boost = compute_retrieval_boost({**best, "access_count": new_access})
                    new_score = round(min(1.0, float(best.get("importance_score") or 0) + retrieval_boost), 4)
                    await self._safe_patch(
                        mem_id,
                        {
                            "access_count": new_access,
                            "importance_score": new_score,
                            "last_accessed_at": datetime.utcnow().isoformat() + "Z",
                        },
                    )
            return {
                "ready": False,
                "stored": False,
                "duplicate": True,
                "reason": "LLM identified as already covered by existing memory",
                "existing_content": candidates[0].get("content") if candidates else None,
            }

        inferred_type = eval_result.get("type") or type_hint or "fact"
        threshold = effective_threshold(base_threshold=self.threshold, active_goal=active_goal, eval_result=eval_result)
        importance = float(eval_result.get("importance") or 0)

        if importance < threshold:
            return {
                "ready": False,
                "stored": False,
                "importance_score": importance,
                "threshold_used": threshold,
                "type": inferred_type,
                "reason": eval_result.get("reason", "Below importance threshold"),
                "scores": {
                    "identity": eval_result.get("identity_score"),
                    "goal_relevance": eval_result.get("goal_relevance"),
                    "frequency": eval_result.get("frequency"),
                    "emotional_weight": eval_result.get("emotional_weight"),
                },
            }

        summary = eval_result.get("summary") or content[:300]

        summary_words = set(re.findall(r"\b\w{4,}\b", summary.lower()))
        if len(summary_words) >= _MIN_CONTENT_WORDS:
            for existing in candidates:
                existing_words = set(re.findall(r"\b\w{4,}\b", (existing.get("content") or "").lower()))
                union = len(summary_words | existing_words)
                if union and len(summary_words & existing_words) / union > POST_SUMMARY_DUPLICATE_THRESHOLD:
                    mem_id = existing.get("_id") or existing.get("memory_id")
                    new_access = int(existing.get("access_count") or 0) + 1
                    retrieval_boost = compute_retrieval_boost({**existing, "access_count": new_access})
                    new_score = round(min(1.0, float(existing.get("importance_score") or 0) + retrieval_boost), 4)
                    await self._safe_patch(
                        mem_id,
                        {
                            "access_count": new_access,
                            "importance_score": new_score,
                            "last_accessed_at": datetime.utcnow().isoformat() + "Z",
                        },
                    )
                    return {
                        "ready": False,
                        "stored": False,
                        "duplicate": True,
                        "reason": "Post-summary Jaccard duplicate — updated existing memory",
                        "existing_memory_id": mem_id,
                        "existing_content": existing.get("content"),
                        "updated_importance_score": new_score,
                    }

        api_scores = self._scores_for_api(eval_result)
        tags = extract_tags(summary, inferred_type, effective_tool_codes=self._effective_tool_codes())

        raw_scope = str(eval_result.get("scope") or scope_hint or "personal").strip().lower()
        memory_scope = "shared" if raw_scope == "shared" else "personal"

        bulk_payload: Dict[str, Any] = {
            "agent_id": self.agent_id,
            "user_id": str(self.user_id) if self.user_id is not None else "",
            "content": summary,
            "type": inferred_type,
            "importance_score": float(importance),
            "scores": api_scores,
            "scope": memory_scope,
            "tags": tags,
        }
        if raw_context:
            bulk_payload["raw_context"] = raw_context[:500]

        return {
            "ready": True,
            "bulk_payload": bulk_payload,
            "overwritten_id": overwritten_id,
            "conflicting": conflicting,
            "threshold": threshold,
            "eval_result": eval_result,
            "memory_scope": memory_scope,
            "inferred_type": inferred_type,
            "importance": float(importance),
        }

    async def _store_piece(
        self,
        content: str,
        type_hint: Optional[str],
        raw_context: Optional[str],
        active_goal: Optional[str],
        *,
        scope_hint: Optional[str],
    ) -> Dict[str, Any]:
        prepared = await self._evaluate_piece(
            content=content,
            type_hint=type_hint,
            raw_context=raw_context,
            active_goal=active_goal,
            scope_hint=scope_hint,
        )
        if not prepared.get("ready"):
            return prepared

        payload = prepared["bulk_payload"]
        stored = await self._api.store(
            content=payload["content"],
            mem_type=payload["type"],
            importance_score=float(payload["importance_score"]),
            scores=payload.get("scores") or {},
            tags=payload.get("tags") or [],
            raw_context=payload.get("raw_context"),
            scope=payload.get("scope", "personal"),
        )
        if not stored:
            return {"stored": False, "reason": "API storage failed — check server logs"}

        result: Dict[str, Any] = {
            "stored": True,
            "memory_id": stored.get("_id"),
            "content": stored.get("content"),
            "type": stored.get("type"),
            "scope": stored.get("scope", payload.get("scope")),
            "importance_score": stored.get("importance_score"),
            "threshold_used": prepared["threshold"],
            "scores": stored.get("scores"),
            "created_at": stored.get("createdAt"),
            "reason": prepared["eval_result"].get("reason", "Meets importance threshold"),
        }
        if prepared.get("overwritten_id"):
            result["overwrote_memory_id"] = prepared["overwritten_id"]
            result["overwrite_reason"] = (prepared.get("conflicting") or {}).get("_contradiction_reason", "")
        return result

    async def _op_store(self, content: str, type_hint: Optional[str], raw_context: Optional[str], active_goal: Optional[str]) -> Dict[str, Any]:
        import asyncio

        pieces = await self._decompose_content(content, type_hint)
        logger.info(
            "🧠 [Memory] Decomposed input into %d piece(s): %s",
            len(pieces),
            " | ".join(f"[{p.get('scope_hint','?')}] {p.get('type_hint','?')}: {p['content'][:60]}" for p in pieces),
        )

        if len(pieces) == 1:
            piece = pieces[0]
            return await self._store_piece(
                piece["content"],
                piece.get("type_hint") or type_hint,
                raw_context,
                active_goal,
                scope_hint=piece.get("scope_hint"),
            )

        eval_tasks = [
            self._evaluate_piece(
                content=p["content"],
                type_hint=p.get("type_hint"),
                raw_context=raw_context,
                active_goal=active_goal,
                scope_hint=p.get("scope_hint"),
            )
            for p in pieces
        ]
        eval_results = await asyncio.gather(*eval_tasks, return_exceptions=True)

        ready_indices: List[int] = []
        bulk_payloads: List[Dict[str, Any]] = []
        safe_results: List[Optional[Dict[str, Any]]] = []

        for i, ev in enumerate(eval_results):
            if isinstance(ev, Exception):
                safe_results.append({"stored": False, "error": str(ev)})
                continue
            ev = ev  # type: ignore[assignment]
            if ev.get("ready"):
                ready_indices.append(i)
                bulk_payloads.append(ev["bulk_payload"])
                safe_results.append(None)
            else:
                safe_results.append(ev)

        bulk_stored: List[Optional[Dict[str, Any]]] = []
        if bulk_payloads:
            bulk_stored = await self._api.store_bulk(memories=bulk_payloads)

        for list_pos, orig_idx in enumerate(ready_indices):
            ev = eval_results[orig_idx]  # type: ignore[assignment]
            stored = bulk_stored[list_pos] if list_pos < len(bulk_stored) else None
            if stored and isinstance(stored, dict):
                result: Dict[str, Any] = {
                    "stored": True,
                    "memory_id": stored.get("_id"),
                    "content": stored.get("content"),
                    "type": stored.get("type"),
                    "scope": stored.get("scope", ev["memory_scope"]),
                    "importance_score": stored.get("importance_score"),
                    "threshold_used": ev["threshold"],
                    "scores": stored.get("scores"),
                    "created_at": stored.get("createdAt"),
                    "reason": ev["eval_result"].get("reason", "Meets importance threshold"),
                }
                if ev.get("overwritten_id"):
                    result["overwrote_memory_id"] = ev["overwritten_id"]
                    result["overwrite_reason"] = (ev.get("conflicting") or {}).get("_contradiction_reason", "")
            else:
                result = {"stored": False, "reason": "API storage failed — check server logs", "importance_score": ev["importance"]}
            safe_results[orig_idx] = result

        stored_count = sum(1 for r in safe_results if r and r.get("stored"))
        logger.info("🧠 [Memory] Bulk multi-store: %d/%d pieces stored (bulk API call: %d payloads)", stored_count, len(pieces), len(bulk_payloads))
        return {
            "stored_multiple": True,
            "count": len(pieces),
            "pieces_stored": stored_count,
            "results": [r or {"stored": False, "reason": "unknown"} for r in safe_results],
        }

    async def _op_recall(self, query: str, limit: int = 10, *, update_access: bool = True) -> Dict[str, Any]:
        import asyncio

        fetch_limit = min(limit * 3, 50)

        def _bulk_query_list(q: str) -> List[Dict[str, Any]]:
            return [
                {"agent_id": self.agent_id, "user_id": str(self.user_id) if self.user_id is not None else "", "query": q, "limit": fetch_limit},
                {"agent_id": self.agent_id, "scope": "shared", "query": q, "limit": fetch_limit},
            ]

        bulk_results = await self._api.recall_bulk(queries=_bulk_query_list(query))
        raw_personal = bulk_results[0] if len(bulk_results) > 0 else []
        raw_shared = bulk_results[1] if len(bulk_results) > 1 else []
        raw_shared = [m for m in raw_shared if (m.get("scope") or "personal") == "shared"]

        seen_content: set[str] = set()
        merged_raw: List[Dict[str, Any]] = []
        for m in raw_personal + raw_shared:
            fingerprint = (m.get("content") or "").strip().lower()[:120]
            if fingerprint and fingerprint not in seen_content:
                seen_content.add(fingerprint)
                merged_raw.append(m)

        _query_stripped = (query or "").strip()
        if not merged_raw and _query_stripped and not _query_stripped.startswith("tool_scope:"):
            expanded = await self._expand_recall_queries(query)
            extra_queries: List[Dict[str, Any]] = []
            for eq in expanded:
                if (eq or "").strip() and (eq or "").strip().lower() != _query_stripped.lower():
                    extra_queries.extend(_bulk_query_list(eq))
            if extra_queries:
                retry_results = await self._api.recall_bulk(queries=extra_queries)
                for result_list in retry_results:
                    if isinstance(result_list, list):
                        for m in result_list:
                            fingerprint = (m.get("content") or "").strip().lower()[:120]
                            if fingerprint and fingerprint not in seen_content:
                                seen_content.add(fingerprint)
                                merged_raw.append(m)

        memories = normalize_memories(merged_raw)
        if not memories:
            return {"memories": [], "total": 0, "message": "No memories found"}

        scored: List[Tuple[Dict[str, Any], float]] = [(m, score_relevance(query, m)) for m in memories]
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:limit]

        if update_access:
            now_iso = datetime.utcnow().isoformat() + "Z"
            patch_tasks = []
            for mem, _ in top:
                mem_id = mem.get("_id") or mem.get("memory_id")
                if not mem_id:
                    continue
                new_access = int(mem.get("access_count") or 0) + 1
                retrieval_boost = compute_retrieval_boost({**mem, "access_count": new_access})
                decay_score = compute_decay_factor(mem)
                base = float(mem.get("importance_score") or 0)
                new_importance = round(
                    max(0.0, min(1.0, (decay_score if decay_score is not None else base) + retrieval_boost)),
                    4,
                )
                mem["access_count"] = new_access
                mem["importance_score"] = new_importance
                patch_tasks.append(
                    self._safe_patch(
                        mem_id,
                        {"last_accessed_at": now_iso, "access_count": new_access, "importance_score": new_importance},
                    )
                )
            if patch_tasks:
                await asyncio.gather(*patch_tasks, return_exceptions=True)

        return {
            "query": query,
            "memories": [
                {
                    "memory_id": mem.get("memory_id"),
                    "content": mem.get("content"),
                    "type": mem.get("type"),
                    "scope": mem.get("scope", "personal"),
                    "importance_score": mem.get("importance_score"),
                    "relevance_score": round(score, 3),
                    "confidence": compute_confidence(mem),
                    "scores": mem.get("scores"),
                    "access_count": mem.get("access_count", 0),
                    "created_at": mem.get("createdAt"),
                    "last_accessed_at": mem.get("last_accessed_at"),
                }
                for mem, score in top
            ],
            "total": len(top),
        }

    async def recall_for_tool(self, app_code: str, tool_name: str = "", limit: int = 8) -> List[Dict[str, Any]]:
        if not app_code:
            return []
        code = app_code.strip().lower()
        tag_query = f"tool_scope:{code}"

        seen: set[str] = set()
        results: List[Dict[str, Any]] = []
        try:
            data = await self._op_recall(tag_query, limit=limit * 2, update_access=False)
            for mem in data.get("memories") or []:
                key = (mem.get("content") or "").strip().lower()[:120]
                if not key or key in seen:
                    continue
                mem_tags = [str(t).lower() for t in (mem.get("tags") or [])]
                if tag_query in mem_tags:
                    seen.add(key)
                    results.append(mem)
        except Exception:
            pass

        results.sort(key=lambda m: float(m.get("importance_score") or 0), reverse=True)
        matched = results[:limit]

        if matched:
            import asyncio as _asyncio

            now_iso = datetime.utcnow().isoformat() + "Z"
            patch_tasks = []
            for m in matched:
                mid = m.get("memory_id") or m.get("_id")
                if mid:
                    patch_tasks.append(
                        self._safe_patch(
                            mid,
                            {
                                "last_accessed_at": now_iso,
                                "access_count": (m.get("access_count") or 0) + 1,
                                "importance_score": float(m.get("importance_score") or 0),
                            },
                        )
                    )
            if patch_tasks:
                await _asyncio.gather(*patch_tasks, return_exceptions=True)
        return matched

    async def _op_forget(self, memory_id: Optional[str] = None, content_match: Optional[str] = None) -> Dict[str, Any]:
        if not memory_id and not content_match:
            return {"success": False, "error": "Provide memory_id or content_match"}

        if memory_id:
            deleted = await self._api.delete(memory_id=memory_id)
            return {"success": deleted, "removed_count": 1 if deleted else 0, "removed": [{"memory_id": memory_id}] if deleted else []}

        candidates = await self._api.recall(query=content_match or "", limit=10)
        match_words = set(re.findall(r"\b\w{4,}\b", (content_match or "").lower()))
        to_delete: List[str] = []
        for mem in candidates:
            mem_words = set(re.findall(r"\b\w{4,}\b", (mem.get("content") or "").lower()))
            union = len(match_words | mem_words)
            if match_words and union and len(match_words & mem_words) / union > 0.45:
                mid = mem.get("_id") or mem.get("id")
                if mid:
                    to_delete.append(mid)

        if not to_delete:
            return {"success": True, "removed_count": 0, "message": "No memories matched the content"}

        result = await self._api.bulk_delete(memory_ids=to_delete)
        return {
            "success": True,
            "removed_count": result.get("deleted_count", 0),
            "requested_count": result.get("requested_count", 0),
            "removed_ids": to_delete,
        }

    async def _op_update(
        self,
        *,
        memory_id: Optional[str],
        content_match: Optional[str],
        new_content: Optional[str],
        type_hint: Optional[str],
    ) -> Dict[str, Any]:
        if not new_content:
            return {"success": False, "error": "new_content is required for update"}
        if not memory_id and not content_match:
            return {"success": False, "error": "Provide memory_id or content_match to locate the memory"}

        target_id = memory_id
        original_content: Optional[str] = None
        if not target_id and content_match:
            candidates = await self._api.recall(query=content_match, limit=8)
            match_words = set(re.findall(r"\b\w{4,}\b", content_match.lower()))
            for mem in candidates:
                mem_words = set(re.findall(r"\b\w{4,}\b", (mem.get("content") or "").lower()))
                union = len(match_words | mem_words)
                if match_words and union and len(match_words & mem_words) / union > 0.35:
                    target_id = mem.get("_id") or mem.get("id")
                    original_content = mem.get("content")
                    break

        if not target_id:
            return {"success": False, "error": f"No memory matched '{(content_match or '')[:80]}' — nothing updated"}

        eval_result = await self._evaluate_importance(new_content, type_hint, [])
        new_importance = float(eval_result.get("importance") or 0)
        summary = eval_result.get("summary") or new_content[:300]
        inferred_type = eval_result.get("type") or type_hint or "fact"
        raw_scope = str(eval_result.get("scope") or "personal").strip().lower()
        memory_scope = "shared" if raw_scope == "shared" else "personal"
        tags = extract_tags(summary, inferred_type, effective_tool_codes=self._effective_tool_codes())
        api_scores = self._scores_for_api(eval_result)

        patch_payload: Dict[str, Any] = {
            "content": summary,
            "type": inferred_type,
            "scope": memory_scope,
            "importance_score": new_importance,
            "scores": api_scores,
            "tags": tags,
            "last_accessed_at": datetime.utcnow().isoformat() + "Z",
        }

        logger.info("🧠 [Memory API] PATCH (update) %s | old: %s | new: %s | importance: %.4f", target_id, (original_content or "?")[:60], summary[:60], new_importance)
        success = await self._safe_patch(target_id, patch_payload)

        return {
            "success": success,
            "memory_id": target_id,
            "original_content": original_content,
            "updated_content": summary,
            "importance_score": new_importance,
            "type": inferred_type,
            "scope": memory_scope,
        }

    async def _op_reflect(self) -> Dict[str, Any]:
        import asyncio

        _REFLECT_FETCH_LIMIT = 200
        _REFLECT_LLM_CHUNK = 60
        _MERGED_TAG = "__merged__"

        data = await self._api.list(limit=_REFLECT_FETCH_LIMIT, offset=0, mem_type=None)
        raw_memories = data.get("memories", [])
        memories = normalize_memories(raw_memories)
        original_count = len(memories)
        if not memories:
            return {"success": True, "message": "No memories to consolidate", "before_count": 0, "after_count": 0}

        memories.sort(key=lambda m: compute_confidence(m), reverse=True)

        decay_patches = 0
        decay_tasks = []
        for mem in memories:
            new_score = compute_decay_factor(mem)
            if new_score is not None:
                mid = mem.get("memory_id")
                if mid:
                    mem["importance_score"] = new_score
                    decay_patches += 1
                    decay_tasks.append(self._safe_patch(mid, {"importance_score": new_score}))
        if decay_tasks:
            await asyncio.gather(*decay_tasks, return_exceptions=True)

        prune_threshold = max(0.05, self.threshold * 0.5)
        prune_ids = [m["memory_id"] for m in memories if float(m.get("importance_score") or 0) < prune_threshold and m.get("memory_id")]
        pruned_count = 0
        if prune_ids:
            r = await self._api.bulk_delete(memory_ids=prune_ids)
            pruned_count = r.get("deleted_count", 0)

        pruned_set = set(prune_ids)
        surviving = [m for m in memories if m.get("memory_id") not in pruned_set]

        merged_groups = 0
        outdated_count = 0

        if len(surviving) >= 3 and self.llm_provider:
            candidates_for_llm = [m for m in surviving if _MERGED_TAG not in (m.get("tags") or [])][:_REFLECT_LLM_CHUNK]
            if candidates_for_llm:
                block = "\n".join(
                    f"[{(m.get('memory_id') or '')[:8]}] ({m.get('type')}, score={float(m.get('importance_score') or 0):.2f}) {m.get('content', '')}"
                    for m in candidates_for_llm
                )
                prompt = f"""You are a memory consolidator for an AI agent.
Review these stored memories and identify:
1. Groups of 2+ memories describing the same topic → merge into one richer entry.
2. Memories clearly outdated or contradicted by newer entries → mark outdated.

Memories:
{block}

Rules:
- Use only the first 8 characters of the ID shown in brackets.
- merged_content must be a concise, information-dense 1-3 sentence summary.
- Only include merge groups with 2 or more IDs.
- Do not re-merge memories that already contain identical information.

Output ONLY valid JSON:
{{
  "merge_groups": [
    {{"ids": ["a1b2c3d4", "e5f6a7b8"], "merged_content": "...", "type": "preference"}}
  ],
  "outdated_ids": ["c9d0e1f2"]
}}"""
                raw = await self._llm.generate_json(prompt, max_tokens=900)
                consolidation = parse_json_response(
                    raw or "",
                    required_keys=["merge_groups", "outdated_ids"],
                    defaults={"merge_groups": [], "outdated_ids": []},
                ) or {"merge_groups": [], "outdated_ids": []}

                outdated_prefixes = set(consolidation.get("outdated_ids") or [])
                outdated_ids = [
                    m["memory_id"]
                    for m in surviving
                    if m.get("memory_id") and any(str(m["memory_id"]).startswith(p) for p in outdated_prefixes)
                ]
                if outdated_ids:
                    r2 = await self._api.bulk_delete(memory_ids=outdated_ids)
                    outdated_count = r2.get("deleted_count", 0)
                outdated_set = set(outdated_ids)
                surviving = [m for m in surviving if m.get("memory_id") not in outdated_set]

                merged_source_ids: List[str] = []
                for group in (consolidation.get("merge_groups") or []):
                    prefixes = group.get("ids") or []
                    merged_content = (group.get("merged_content") or "").strip()
                    merged_type_hint = group.get("type") or None
                    if len(prefixes) < 2 or not merged_content:
                        continue
                    source_mems = [m for m in surviving if m.get("memory_id") and any(str(m["memory_id"]).startswith(p) for p in prefixes)]
                    if len(source_mems) < 2:
                        continue

                    merged_eval = await self._evaluate_importance(
                        merged_content,
                        merged_type_hint or source_mems[0].get("type"),
                        surviving,
                    )
                    merged_importance = float(merged_eval.get("importance") or 0)
                    max_src_importance = max(float(m.get("importance_score") or 0) for m in source_mems)
                    merged_importance = round(max(0.0, min(1.0, max(merged_importance, max_src_importance))), 4)
                    merged_api_scores = self._scores_for_api(merged_eval)
                    inferred_type = merged_eval.get("type") or merged_type_hint or "fact"

                    merged_tags = list(dict.fromkeys(t for m in source_mems for t in (m.get("tags") or [])))
                    if _MERGED_TAG not in merged_tags:
                        merged_tags.append(_MERGED_TAG)

                    stored = await self._api.store(
                        content=merged_content,
                        mem_type=inferred_type,
                        importance_score=merged_importance,
                        scores=merged_api_scores,
                        tags=merged_tags,
                        raw_context=None,
                        scope="personal",
                    )
                    if stored:
                        merged_groups += 1
                        merged_source_ids.extend(m["memory_id"] for m in source_mems if m.get("memory_id"))

                if merged_source_ids:
                    await self._api.bulk_delete(memory_ids=merged_source_ids)

        after_count = max(0, original_count - pruned_count - outdated_count)
        return {
            "success": True,
            "before_count": original_count,
            "after_count": after_count,
            "pruned_count": pruned_count,
            "decay_patches": decay_patches,
            "outdated_removed": outdated_count,
            "merged_groups": merged_groups,
            "message": (
                f"Consolidated from {original_count} → {after_count} memories. "
                f"Pruned {pruned_count}, decayed {decay_patches} scores, "
                f"merged {merged_groups} groups (re-scored), removed {outdated_count} outdated."
            ),
        }

    async def _op_evaluate(self, content: str, type_hint: Optional[str], active_goal: Optional[str]) -> Dict[str, Any]:
        recent = await self._api.recall(query=content, limit=5)
        eval_result = await self._evaluate_importance(content, type_hint, recent)
        inferred_type = eval_result.get("type") or type_hint or "fact"
        threshold = effective_threshold(base_threshold=self.threshold, active_goal=active_goal, eval_result=eval_result)
        importance = float(eval_result.get("importance") or 0)
        return {
            "content": content,
            "would_store": importance > threshold,
            "importance_score": importance,
            "threshold_used": threshold,
            "type": inferred_type,
            "summary": eval_result.get("summary"),
            "scores": {
                "identity": eval_result.get("identity_score"),
                "goal_relevance": eval_result.get("goal_relevance"),
                "frequency": eval_result.get("frequency"),
                "emotional_weight": eval_result.get("emotional_weight"),
            },
            "api_scores_preview": self._scores_for_api(eval_result),
            "active_goal_applied": bool(active_goal),
            "reason": eval_result.get("reason"),
        }

    async def _op_list(self, limit: int = 50, mem_type: Optional[str] = None) -> Dict[str, Any]:
        data = await self._api.list(limit=limit, offset=0, mem_type=mem_type)
        return {
            "total": data.get("total", 0),
            "showing": len(data.get("memories", [])),
            "memories": [
                {
                    "memory_id": m.get("_id") or m.get("id"),
                    "content": m.get("content"),
                    "type": m.get("type"),
                    "importance_score": m.get("importance_score"),
                    "confidence": compute_confidence(m),
                    "scores": m.get("scores"),
                    "access_count": m.get("access_count", 0),
                    "created_at": m.get("createdAt"),
                    "last_accessed_at": m.get("last_accessed_at"),
                }
                for m in data.get("memories", [])
            ],
        }

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        conversation_history: Optional[Any] = None,
        system_prompt: Optional[str] = None,
        agent_guidance: Optional[Any] = None,
        **tool_args: Any,
    ) -> AsyncGenerator[Dict, None]:
        """
        Dispatch to the correct memory operation.
        Accepts `active_goal` in tool_args for contextual threshold coupling.
        """
        try:
            args = dict(tool_args or {})

            operation = (args.get("operation") or "").strip().lower() or "auto"

            content = (args.get("content") or args.get("text") or user_query or "").strip()
            query = (args.get("query") or args.get("search") or user_query or "").strip()
            memory_id = args.get("memory_id") or args.get("id")
            content_match = args.get("content_match") or args.get("match")
            type_hint = args.get("type_hint") or args.get("type")
            raw_context = args.get("raw_context")
            active_goal = (args.get("active_goal") or "").strip() or None
            mem_type_filter = args.get("type_filter")
            try:
                limit = int(args.get("limit") or 10)
            except (TypeError, ValueError):
                limit = 10

            if operation == "auto":
                raw_conv = args.get("conversation") or args.get("context") or conversation_history or ""
                if isinstance(raw_conv, list):
                    parts = []
                    for item in raw_conv:
                        if isinstance(item, dict):
                            role = item.get("role", "unknown")
                            text = item.get("content") or item.get("text") or ""
                            if isinstance(text, str) and text.strip():
                                parts.append(f"{role}: {text.strip()}")
                        elif isinstance(item, str) and item.strip():
                            parts.append(item.strip())
                    conversation = "\n".join(parts).strip()
                else:
                    conversation = (raw_conv or "").strip() if isinstance(raw_conv, str) else ""

                user_message = (args.get("user_message") or content or user_query or "").strip()
                yield event(AgentEvent.THOUGHT, "Autonomously analysing conversation for memory relevance…")
                result = await self._op_auto(user_message=user_message, conversation=conversation, active_goal=active_goal)
                actions = result.get("actions_taken") or []
                yield event(
                    AgentEvent.THOUGHT,
                    f"Auto decision: {result.get('decision')} — {', '.join(actions) if actions else 'no action'}",
                )
                out = {"success": True, "operation": "auto", "result": result}

            elif operation == "store":
                short = content[:80] + ("…" if len(content) > 80 else "")
                yield event(AgentEvent.THOUGHT, f"Evaluating importance: '{short}'")
                if active_goal:
                    yield event(
                        AgentEvent.THOUGHT,
                        f"Active goal context: '{active_goal[:60]}' — threshold may be reduced",
                    )
                result = await self._op_store(content, type_hint, raw_context, active_goal)
                if result.get("stored"):
                    score = result.get("importance_score") or 0
                    thresh = result.get("threshold_used") or self.threshold
                    yield event(AgentEvent.THOUGHT, f"Stored (importance={score:.2f} > threshold={thresh:.2f})")
                    if result.get("overwrote_memory_id"):
                        yield event(AgentEvent.THOUGHT, f"Overwrote conflicting memory: {result['overwrote_memory_id']}")
                else:
                    yield event(AgentEvent.THOUGHT, f"Not stored: {result.get('reason')}")
                out = {"success": True, "operation": "store", "result": result}

            elif operation == "recall":
                yield event(AgentEvent.THOUGHT, f"Recalling memories for: '{query[:60]}'")
                result = await self._op_recall(query, limit)
                yield event(AgentEvent.THOUGHT, f"Recalled {result.get('total', 0)} memories with confidence scores")
                out = {"success": True, "operation": "recall", "result": result}

            elif operation == "update":
                new_content = (args.get("new_content") or args.get("content") or "").strip()
                match_phrase = (content_match or args.get("match") or args.get("update_match") or "").strip()
                yield event(AgentEvent.THOUGHT, f"Locating memory matching '{match_phrase[:60]}' to update…")
                result = await self._op_update(
                    memory_id=memory_id,
                    content_match=match_phrase or None,
                    new_content=new_content or content or None,
                    type_hint=type_hint,
                )
                if result.get("success"):
                    yield event(
                        AgentEvent.THOUGHT,
                        f"Updated: '{(result.get('original_content') or '?')[:50]}' → '{(result.get('updated_content') or '?')[:50]}'",
                    )
                else:
                    yield event(AgentEvent.THOUGHT, f"Update failed: {result.get('error', '?')}")
                out = {"success": bool(result.get("success", False)), "operation": "update", "result": result}

            elif operation == "forget":
                yield event(AgentEvent.THOUGHT, "Locating memory to remove…")
                result = await self._op_forget(memory_id=memory_id, content_match=content_match or (content if not memory_id else None))
                yield event(AgentEvent.THOUGHT, f"Removed {result.get('removed_count', 0)} memory entries")
                out = {"success": bool(result.get("success", False)), "operation": "forget", "result": result}

            elif operation == "reflect":
                yield event(AgentEvent.THOUGHT, "Starting memory consolidation…")
                result = await self._op_reflect()
                yield event(AgentEvent.THOUGHT, result.get("message", "Done"))
                out = {"success": bool(result.get("success", False)), "operation": "reflect", "result": result}

            elif operation == "evaluate":
                yield event(AgentEvent.THOUGHT, f"Evaluating (dry-run): '{content[:60]}'")
                result = await self._op_evaluate(content, type_hint, active_goal)
                verdict = "WOULD store" if result.get("would_store") else "would NOT store"
                score = float(result.get("importance_score") or 0)
                thresh = float(result.get("threshold_used") or self.threshold)
                yield event(AgentEvent.THOUGHT, f"Decision: {verdict} (importance={score:.2f}, threshold={thresh:.2f})")
                out = {"success": True, "operation": "evaluate", "result": result}

            elif operation == "list":
                yield event(AgentEvent.THOUGHT, "Fetching stored memories…")
                result = await self._op_list(limit, mem_type=mem_type_filter)
                yield event(AgentEvent.THOUGHT, f"Total in store: {result.get('total', 0)}")
                out = {"success": True, "operation": "list", "result": result}

            else:
                out = {
                    "success": False,
                    "operation": operation or "unknown",
                    "error": (
                        f"Unknown operation '{operation}'. "
                        "Valid: auto, store, recall, forget, reflect, evaluate, list"
                    ),
                }

            yield event(AgentEvent.RESULT, out)
            yield event(
                AgentEvent.FINAL,
                {"success": out.get("success", False), "response": json.dumps(out, default=str), "result": out},
            )
        except Exception as exc:
            logger.exception("Memory agent failed")
            yield event(AgentEvent.ERROR, {"error": str(exc)})
            yield event(AgentEvent.FINAL, {"success": False, "error": str(exc), "response": None})

    async def _op_auto(self, user_message: str, conversation: str = "", active_goal: Optional[str] = None) -> Dict[str, Any]:
        if not user_message:
            return {"decision": "none", "reason": "No user message provided", "actions_taken": []}

        trimmed_conversation = (conversation or "").strip()[-800:] if conversation else ""
        context_block = f"\nRecent conversation (last ~800 chars for reference):\n{trimmed_conversation}\n" if trimmed_conversation else ""
        goal_block = f"\nCurrent active goal / project: {active_goal}" if active_goal else ""

        classifier_prompt = f"""You are a strict memory gatekeeper for an AI assistant.
Your job: decide the MINIMUM necessary memory operations for the current user message.
Default to NONE. Only deviate when there is a clear, specific reason.{context_block}{goal_block}

Current user message:
\"\"\"{user_message}\"\"\"

━━━ OPERATION RULES ━━━

STORE → choose ONLY when the message reveals a NEW, STABLE, REUSABLE personal fact:
  ✅ Name, job title, location, relationships, or contact/identifier details (e.g. email address, phone number) the user discloses for future use
  ✅ An explicit goal or long-term project the user states
  ✅ A clear preference, authority rule, or correction to a past belief
  ✅ Behavioural instructions: address style, role definitions, access rules
  ❌ NOT for: questions, requests, greetings, commands, opinions on general topics,
     or anything already visible in the conversation context above.

  ▶ store_content rule:
    • Single atomic fact  → copy only that fact verbatim.
    • Compound message with MULTIPLE independent facts (name + role + authority +
      address style + access rules etc.) → set store_content to the COMPLETE
      original message verbatim. A downstream decomposition engine will split it.
    • NEVER summarise or paraphrase a compound message; preserve every word so
      no facts are silently dropped.
    • store_type → a short free-form label for the PRIMARY nature of the content
      (e.g. "identity", "authority_rule", "address_preference", "role_definition",
      "compound_statement"). Choose the most accurate label — do not restrict to
      these examples.
      TOOL-SCOPE PREFIX RULE: If the content is a preference/rule/setting that applies
      specifically to ONE AI tool, prefix the store_type with that tool's code, e.g.:
      "ai_writer:preference", "gmail:label_rule", "call:retry_preference",
      "scheduler:reminder_style", "chartmaker:style_preference".
      Connected tool codes available: {self._tool_codes_for_prompt()}.
      Meaning guide — ai_writer: writing/email/document generation; gmail: inbox/sending;
      call: voice calls; scheduler: reminders/schedules; chartmaker: charts/graphs;
      websearch: web search; memory: memory operations.
      Only prefix when the user's statement is CLEARLY scoped to one specific tool.

UPDATE → choose when the user CORRECTS or REPLACES a previously stated fact:
  ✅ "Actually my name is Ahmad" (overrides stored name)
  ✅ "Change language to English" / "no longer talk in Urdu" (overrides preference)
  ✅ "My new job title is CTO" / "I moved to London"
  ✅ Any explicit correction to a previously stored belief
  ❌ NOT for brand-new facts (use STORE instead)
  ▶ update_match  → describe the OLD fact to locate (e.g. "language preference", "user name")
  ▶ update_content → the NEW replacement fact (verbatim or paraphrased clearly)

FORGET → choose when the user explicitly asks to DELETE a stored memory:
  ✅ "Forget my Urdu preference", "Remove my name from memory", "Clear all preferences"
  ✅ "Stop remembering X", "Delete that", "Don't remember this"
  ❌ NOT for corrections (use UPDATE instead)
  ▶ forget_match → describe WHAT to delete (e.g. "Urdu language preference", "name Hussain")

RECALL → choose ONLY when ALL of the following are true:
  1. The answer REQUIRES previously stored personal information (name, prefs, goals).
  2. That information is NOT already present in the conversation context above.
  3. The user explicitly references past context ("you mentioned", "do you remember",
     "what did I say", "based on what I told you", "my name/goal/preference") OR
     the question cannot be answered at all without stored facts.
  ❌ NOT for: general questions, simple tasks, clarifications, or anything answerable
     from the current message and conversation context alone.
  ❌ Do NOT recall just because memory access is available.

NONE → use when:
  • The message is a question answerable from general knowledge or the current context.
  • The message is a command, task, or action request with no personal-memory dependency.
  • No new stable personal fact is being disclosed and no correction/deletion is requested.

━━━ COST REMINDER ━━━
Recall triggers a database query. Prefer NONE over RECALL when uncertain.
Only add STORE if you can state exactly what fact to save in store_content.

Respond ONLY with valid JSON (no markdown, no comments):
{{
  "operations": [],
  "recall_query": null,
  "store_content": null,
  "store_type": null,
  "update_match": null,
  "update_content": null,
  "forget_match": null,
  "reason": "one sentence explaining why each chosen operation is strictly necessary"
}}"""

        raw = await self._llm.generate_json(classifier_prompt, max_tokens=600)
        decision_json = parse_json_response(
            raw or "",
            required_keys=[
                "operations",
                "recall_query",
                "store_content",
                "store_type",
                "update_match",
                "update_content",
                "forget_match",
                "reason",
            ],
            defaults={
                "operations": [],
                "recall_query": None,
                "store_content": None,
                "store_type": None,
                "update_match": None,
                "update_content": None,
                "forget_match": None,
                "reason": "LLM decision",
            },
        ) or {}

        ops: List[str] = [o.lower() for o in (decision_json.get("operations") or []) if isinstance(o, str)]

        recall_query_raw = (decision_json.get("recall_query") or "").strip()
        store_content_raw = (decision_json.get("store_content") or "").strip()
        update_match_raw = (decision_json.get("update_match") or "").strip()
        update_content_raw = (decision_json.get("update_content") or "").strip()
        forget_match_raw = (decision_json.get("forget_match") or "").strip()

        if "recall" in ops and (not recall_query_raw or recall_query_raw == user_message.strip()):
            ops = [o for o in ops if o != "recall"]
        if "store" in ops and not store_content_raw:
            ops = [o for o in ops if o != "store"]
        if "update" in ops and (not update_match_raw or not update_content_raw):
            ops = [o for o in ops if o != "update"]
        if "forget" in ops and not forget_match_raw:
            ops = [o for o in ops if o != "forget"]

        recall_query = recall_query_raw or user_message[:200]
        store_content = store_content_raw or user_message
        store_type: Optional[str] = decision_json.get("store_type")
        reason = decision_json.get("reason") or "LLM decision"

        if not ops:
            return {"decision": "none", "reason": reason, "actions_taken": [], "classifier_output": decision_json}

        recall_result: Optional[Dict[str, Any]] = None
        store_result: Optional[Dict[str, Any]] = None
        update_result: Optional[Dict[str, Any]] = None
        forget_result: Optional[Dict[str, Any]] = None
        actions_taken: List[str] = []

        if "recall" in ops:
            recall_result = await self._op_recall(recall_query, limit=10)
            actions_taken.append(f"recalled ({recall_result.get('total', 0)} memories)")

        if "store" in ops:
            store_result = await self._op_store(store_content, store_type, raw_context=None, active_goal=active_goal)
            if store_result.get("stored") or store_result.get("stored_multiple"):
                actions_taken.append("stored new memory")
            else:
                actions_taken.append(f"not stored: {store_result.get('reason', '?')}")

        if "update" in ops:
            update_result = await self._op_update(
                memory_id=None,
                content_match=update_match_raw or None,
                new_content=update_content_raw or None,
                type_hint=None,
            )
            if update_result.get("success"):
                actions_taken.append("updated memory")
            else:
                actions_taken.append(f"update failed: {update_result.get('error', '?')}")

        if "forget" in ops:
            forget_result = await self._op_forget(content_match=forget_match_raw)
            removed = forget_result.get("removed_count", 0)
            if removed:
                actions_taken.append(f"forgot {removed} memory/memories matching '{forget_match_raw[:50]}'")
            else:
                actions_taken.append(f"forget: no matching memory found for '{forget_match_raw[:50]}'")

        decision_label = "+".join(ops) if ops else "none"
        return {
            "decision": decision_label,
            "reason": reason,
            "actions_taken": actions_taken,
            "recall_result": recall_result,
            "store_result": store_result,
            "update_result": update_result,
            "forget_result": forget_result,
            "classifier_output": decision_json,
        }

