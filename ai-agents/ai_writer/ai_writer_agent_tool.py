from __future__ import annotations

"""
AI Writer Agent (app_code: ai_writer)

Full implementation lives in this plugin folder so the ai_writer tool
is completely self-contained. The legacy module llm_tools.ai_writer_agent_tool
simply re-exports this AIWriterAgentTool class.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentEvent:
    THOUGHT = "thought"
    PLAN = "plan"
    RESULT = "result"
    FINAL = "final"
    ERROR = "error"


def event(event_type: str, content: Any) -> Dict:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        "content": content,
    }


# Approximate chars per token for context truncation (conservative)
APPROX_CHARS_PER_TOKEN = 4
MAX_CONTEXT_TOKENS = 1800
MAX_ENRICHED_CONTEXT_CHARS = MAX_CONTEXT_TOKENS * APPROX_CHARS_PER_TOKEN

# Short-request guardrail: skip external context when request is tiny and has no context keywords
CONTEXT_REQUEST_MIN_WORDS = 15
CONTEXT_KEYWORDS = frozenset(
    {
        "email",
        "inbox",
        "thread",
        "message",
        "attach",
        "document",
        "last",
        "reply",
        "previous",
        "gmail",
        "knowledge",
        "base",
        "crm",
        "check",
        "read",
        "fetch",
        "context",
        "attached",
    }
)

# Optional template hints for common types (structure only; LLM still generates content)
EMAIL_TEMPLATE_HINTS = {
    "follow_up": "Structure: brief reference to previous contact, restate ask or update, single clear CTA, polite close.",
    "thank_you": "Structure: thank recipient, mention specific reason, offer next step or sign-off.",
    "leave_request": "Structure: greeting, leave duration and dates, reason, coverage if any, thank and close.",
}


from backend.services.ai_agents.base_tool_agent import BaseToolAgent


class AIWriterAgentTool(BaseToolAgent):
    """
    AI Writer Agent: end-to-end document drafting with intent classification,
    context enrichment, strategy planning, draft generation, and self-review.
    """

    TOOL_NAME = "AI Writer"
    APP_CODE = "ai_writer"

    TOOL_DESCRIPTION = """The AI Writer helps users create professional documents.

CAPABILITIES:
- Emails (follow-up, proposal, thank-you, escalation)
- Proposals, memos, reports, contract drafts
- Multiple tones: formal, friendly, persuasive, concise
- Structure: subject line, body, CTA, closing, signature

USER PROVIDES:
- Intent (e.g. "write follow-up email to client")
- Context (who, what, why)
- Tone preference (formal, friendly, persuasive)
- Optional: attachments refs, deadline

USE WHEN:
- User asks to write, draft, compose an email, proposal, memo, or report
- User wants a follow-up, thank-you, or formal communication
- User says "write", "draft", "compose", "create email", "proposal", "memo\""""

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION

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
        # Identity from top agent (system prompt / employeeIdentity) — use when context does not provide author
        self._system_prompt = (app_config or {}).get("system_prompt") or ""
        self._employee_identity = (app_config or {}).get("employee_identity") or {}
        if not isinstance(self._employee_identity, dict):
            self._employee_identity = {}
        # Behavioral rules extracted from user_query each run; injected into every LLM system prompt
        self._behavioral_rules_str: str = ""

    def _extract_behavioral_rules(self, user_query: str) -> str:
        """
        Extract behavioral/preference rule blocks from user_query so they can be
        promoted to the system prompt of every LLM call instead of sitting in user context.

        Recognised block headers (injected by the runner):
          [Stored preferences / behavioral rules]
          [AI Writer preferences]

        Any content between a recognised header and the next header (or end of string)
        is captured. Returns a concatenated string, or "" when nothing is found.
        """
        text = user_query or ""
        _headers = [
            "[Stored preferences / behavioral rules]",
            "[AI Writer preferences]",
        ]
        collected: list[str] = []
        for header in _headers:
            idx = text.find(header)
            if idx < 0:
                continue
            # Content starts after the header line
            content_start = idx + len(header)
            # Find the next recognised header (or end of string)
            next_idx = len(text)
            for other in _headers:
                if other == header:
                    continue
                pos = text.find(other, content_start)
                if pos >= 0 and pos < next_idx:
                    next_idx = pos
            # Also stop at common runner section separators
            for sep in ("\n[", "\n---"):
                pos = text.find(sep, content_start)
                if pos >= 0 and pos < next_idx:
                    next_idx = pos
            block = text[content_start:next_idx].strip()
            if block:
                collected.append(block)
        return "\n".join(collected)

    def _get_identity_for_draft(self, tool_args: Optional[Dict]) -> Optional[str]:
        """
        Return a short identity string for writing on behalf of the agent.
        Used when context does not already provide author/signer. From employeeIdentity or system_prompt.
        """
        # If context explicitly provides author/signer, do not override
        if tool_args:
            for key in ("author_name", "signer", "from_name", "writer_identity", "sender_name", "written_by"):
                val = tool_args.get(key)
                if val and isinstance(val, str) and val.strip():
                    return None
        # Prefer employee_identity (roleName, companyName, departmentName)
        ei = self._employee_identity
        if ei:
            role = (ei.get("roleName") or "").strip()
            company = (ei.get("companyName") or "").strip()
            dept = (ei.get("departmentName") or "").strip()
            parts = []
            if role:
                parts.append(role)
            if company:
                parts.append(f"at {company}")
            if dept:
                parts.append(f"({dept})")
            if parts:
                return " ".join(parts)
        # Fallback: first 1–2 sentences of system prompt (e.g. "You are a {role}. You work for {company}.")
        sp = (self._system_prompt or "").strip()
        if sp:
            first = sp.split("\n")[0].strip()
            if first:
                return first[:400]
        return None

    def _get_signoff_name(self, tool_args: Optional[Dict]) -> str:
        """Best-effort signer name from explicit args or employee identity."""
        targs = tool_args or {}
        for key in ("author_name", "signer", "from_name", "sender_name", "written_by"):
            val = targs.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        ei = self._employee_identity if isinstance(self._employee_identity, dict) else {}
        for key in ("name", "fullName", "displayName", "firstName"):
            val = ei.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    def _is_valid_recipient_name(self, candidate: str) -> bool:
        if not candidate or not isinstance(candidate, str):
            return False
        c = candidate.strip()
        if len(c) < 3:
            return False
        lowered = c.lower()
        # Exclude common verbs/fragments frequently mis-extracted from natural language.
        blocked = {"go", "send", "write", "request", "leave", "email", "vacation", "country", "out", "auto"}
        if lowered in blocked:
            return False
        if any(ch.isdigit() for ch in c):
            return False
        return True

    async def _llm_generate(self, prompt: str, max_tokens: int = 1200) -> Optional[str]:
        """Call LLM and return single string output.

        Uses get_response() — the standard async method exposed by all AI providers
        in this codebase (openrouter, openai, etc.).  The old code incorrectly called
        a non-existent generate() method, causing every internal LLM call to return
        None immediately and trigger the fallback draft path.
        """
        if not self.llm_provider or not getattr(self.llm_provider, "get_response", None):
            return None
        try:
            _base_system = "You are a concise professional writing assistant. Follow instructions exactly."
            if self._behavioral_rules_str:
                _base_system = (
                    f"{_base_system}\n\n## Behavioral rules (always enforced — highest priority)\n"
                    f"{self._behavioral_rules_str}"
                )
            result = await self.llm_provider.get_response(
                transcript=prompt,
                system_prompt=_base_system,
                max_tokens=max_tokens,
            )
            if result is None:
                return None
            # get_response always returns a plain string
            return result.strip() or None
        except Exception as e:
            logger.debug("AI Writer LLM failed: %s", e)
        return None

    def _strip_wrappers(self, text: str) -> str:
        """Strip common markdown wrappers around JSON/text."""
        out = (text or "").strip().strip("`").strip()
        for prefix in ("json", "```"):
            if out.lower().startswith(prefix):
                out = out[len(prefix) :].lstrip()
        if out.endswith("```"):
            out = out[:-3].strip()
        return out

    def _parse_json_loose(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Parse JSON robustly from LLM output:
        - accepts fenced blocks
        - extracts first balanced JSON object if extra text exists
        - removes common trailing commas
        """
        if not text:
            return None
        raw = self._strip_wrappers(text)
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass

        first = raw.find("{")
        if first < 0:
            return None
        depth = 0
        in_string = False
        escape = False
        quote = ""
        for i in range(first, len(raw)):
            c = raw[i]
            if escape:
                escape = False
                continue
            if in_string:
                if c == "\\":
                    escape = True
                elif c == quote:
                    in_string = False
                continue
            if c in ('"', "'"):
                in_string = True
                quote = c
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[first : i + 1]
                    candidate = re.sub(r",\s*}", "}", candidate)
                    candidate = re.sub(r",\s*]", "]", candidate)
                    try:
                        obj = json.loads(candidate)
                        return obj if isinstance(obj, dict) else None
                    except Exception:
                        return None
        return None

    def _get_tone_preference(self, tool_args: Optional[Dict[str, Any]], classification: Optional[Dict[str, Any]] = None) -> str:
        """Resolve tone from tool args/classification with compatible key support."""
        targs = tool_args or {}
        tone = (
            targs.get("tone")
            or targs.get("tone_preference")
            or (classification or {}).get("tone_style")
            or "professional"
        )
        return str(tone).strip() or "professional"

    def _extract_request_profile_heuristic(self, user_query: str, tool_args: Optional[Dict[str, Any]]) -> Dict[str, str]:
        """Heuristic profile extraction fallback used when LLM extraction is unavailable."""
        text = (user_query or "").strip()
        targs = tool_args or {}

        recipient_email = ""
        recipient_name = ""
        purpose = ""
        reason = ""
        duration = ""

        # Explicit tool args always win
        if isinstance(targs.get("recipient_email"), str):
            recipient_email = targs.get("recipient_email", "").strip()
        if isinstance(targs.get("recipient_name"), str):
            recipient_name = targs.get("recipient_name", "").strip()

        # Try extract email and recipient from query
        email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
        if email_match and not recipient_email:
            recipient_email = email_match.group(0)
        if not recipient_name:
            to_name_match = re.search(r"\bto\s+([A-Z][a-zA-Z0-9_.-]{1,40})", text, flags=re.IGNORECASE)
            if to_name_match:
                recipient_name = to_name_match.group(1)
        if not self._is_valid_recipient_name(recipient_name):
            recipient_name = ""

        # Purpose extraction: avoid generic "for ..." capture that often produces broken phrases.
        purpose_match = re.search(r"\b(request(?:ed)?\s+for|regarding|about)\s+(.+)$", text, flags=re.IGNORECASE)
        if purpose_match:
            purpose = purpose_match.group(2).strip(" .")
        elif re.search(r"\bleave\b", text, flags=re.IGNORECASE):
            purpose = "request leave"
        elif re.search(r"\bfollow[- ]?up\b", text, flags=re.IGNORECASE):
            purpose = "follow up"
        elif text:
            purpose = text[:220]

        # Duration / reason
        dur_match = re.search(r"\bfor\s+(\d+\s+(?:day|days|week|weeks|month|months))\b", text, flags=re.IGNORECASE)
        if dur_match:
            duration = dur_match.group(1)
        reason_match = re.search(r"\b(?:because|as|that)\s+(.+)$", text, flags=re.IGNORECASE)
        if reason_match:
            reason = reason_match.group(1).strip(" .")

        return {
            "recipient_name": recipient_name,
            "recipient_email": recipient_email,
            "purpose": purpose,
            "reason": reason,
            "duration": duration,
        }

    async def _extract_request_profile(self, user_query: str, tool_args: Optional[Dict[str, Any]]) -> Dict[str, str]:
        """
        LLM-first profile extraction for better quality.
        Falls back to heuristic extraction when LLM output is missing/invalid.
        """
        heuristic = self._extract_request_profile_heuristic(user_query, tool_args)
        if not self.llm_provider:
            return heuristic

        prompt = f"""Extract writing-request profile as JSON only.
Keys: recipient_name, recipient_email, purpose, reason, duration
- recipient_name: ONLY real person names (e.g. Amber, John, Krishna). NEVER use fragments like "auto" (from "automatically"), "automation", or generic words — use empty string if unclear.
- Keep values short and concrete.
- Do not copy full user message into purpose.
- purpose should be an actionable phrase like "request leave", "send follow-up", "request approval".
- "reason" is STRICTLY for personal/leave requests only — it is WHY the person needs absence/time-off/medical leave.
  For marketing emails, proposals, introductions, reports, or any business communication, "reason" MUST be empty string "".
  NEVER set "reason" to a description of the email content, what the email describes, or the goal of the email.
  Examples of valid reason values: "medical emergency", "annual vacation", "family bereavement".
  Examples of INVALID reason values: "describe services", "market our products", "introduce the company", "send follow-up".
- If unknown, use empty string.

User query: {(user_query or "").strip()[:1000]}
Tool args: {json.dumps(tool_args or {}, default=str)[:1000]}

Return JSON only."""
        out = await self._llm_generate(prompt, max_tokens=220)
        parsed = self._parse_json_loose(out or "")
        if not parsed:
            return heuristic

        profile = {
            "recipient_name": str(parsed.get("recipient_name") or "").strip(),
            "recipient_email": str(parsed.get("recipient_email") or "").strip(),
            "purpose": str(parsed.get("purpose") or "").strip(),
            "reason": str(parsed.get("reason") or "").strip(),
            "duration": str(parsed.get("duration") or "").strip(),
        }
        # Fill missing fields from heuristic extraction for robustness.
        for k, v in heuristic.items():
            if not profile.get(k):
                profile[k] = v
        if not self._is_valid_recipient_name(profile.get("recipient_name", "")):
            profile["recipient_name"] = ""
        return profile

    async def _generate_body_from_context(self, context_block: str, max_sentences: int = 4) -> str:
        """
        Generate 2-4 substantive sentences for the email body from the given context (e.g.
        bullet points from main agent). Fully dynamic — no static phrases; LLM derives content from context.
        """
        if not (context_block or "").strip() or not self.llm_provider:
            return ""
        block = (context_block or "").strip()[:3000]
        prompt = f"""Using only the context below, write 2 to {max_sentences} short sentences for the middle of a professional email. No greeting, no sign-off. Use facts and details from the context to write real content (e.g. services, offerings, data). Output only the paragraph, no JSON, no quotes.

Context:
{block}

Paragraph only:"""
        out = await self._llm_generate(prompt, max_tokens=250)
        if not out:
            return ""
        paragraph = (out or "").strip().strip('"').strip()
        if len(paragraph) < 15:
            return ""
        return paragraph

    async def _generate_body_from_context(
        self,
        context_block: str,
        brief: str = "",
        max_sentences: int = 4,
    ) -> str:
        """
        Generate 2-4 substantive sentences for the email body from the full context block
        (bullets from previous steps, workflow vars). Dynamic: uses whatever context is
        provided; no static template or company name.
        """
        if not (context_block or "").strip() or not self.llm_provider:
            return ""
        ctx = (context_block or "").strip()[:2500]
        brief_line = f"\nBrief/task: {(brief or '').strip()[:300]}" if brief else ""
        prompt = f"""Using only the context below, write 2 to {max_sentences} short sentences for the middle of a professional email. Use facts from the context (company names, recipients, step outputs, workflow variables). Match the brief/task. No greeting, no sign-off, no bullet list—flowing sentences only.

Context:
{ctx}{brief_line}

Paragraph only (no JSON, no quotes):"""
        out = await self._llm_generate(prompt, max_tokens=280)
        if not out and self.llm_provider:
            await asyncio.sleep(1.0)
            out = await self._llm_generate(prompt, max_tokens=280)
        if not out:
            return ""
        paragraph = (out or "").strip().strip('"').strip()
        if len(paragraph) < 15:
            return ""
        return paragraph

    async def _generate_services_overview_paragraph(
        self,
        company_or_topic: str,
        source_text: str = "",
        max_sentences: int = 4,
    ) -> str:
        """
        When the brief is to describe services/offerings, generate 2-4 substantive sentences
        so the fallback email body has real content. Uses source_text (e.g. context excerpt) when provided.
        """
        if not self.llm_provider:
            return ""
        company = (company_or_topic or "").strip()[:100] or "the company"
        prompt = f"""Write 2 to {max_sentences} short sentences describing what {company} offers (e.g. products, services, solutions). Use a professional marketing tone. No greeting, no sign-off—just 2-4 flowing sentences for the middle of an email.
{f'Use details from this context where relevant: {source_text[:400]}' if source_text else ''}

Paragraph only (no quotes, no JSON):"""
        out = await self._llm_generate(prompt, max_tokens=220)
        if not out:
            return ""
        paragraph = (out or "").strip().strip('"').strip()
        if len(paragraph) < 20:
            return ""
        return paragraph

    async def _polish_fallback_with_llm(
        self,
        subject: str,
        body: str,
        profile: Dict[str, str],
        tone: str,
        source_text: str = "",
        context_excerpt: str = "",
    ) -> Dict[str, str]:
        """Try one LLM rewrite pass so fallback still sounds natural and specific. context_excerpt: optional bullet/step context to use when expanding body."""
        src = (source_text or "").lower()
        content_instruction = ""
        if any(x in src for x in ("market", "describ", "service", "overview", "offer", "all service")):
            content_instruction = "\n- The user requested substantive content (e.g. marketing email, describing services). The body MUST include 2-4 sentences of actual content about the topic (e.g. what services/offerings are), not just greeting and sign-off. Expand the draft body accordingly."
        context_section = ""
        if (context_excerpt or "").strip():
            context_section = f"\nContext from previous steps (use these facts in the body):\n{(context_excerpt or '').strip()[:800]}\n"
        prompt = f"""Improve this draft email and return JSON only with keys subject, body.
Requirements:
- Keep intent and facts unchanged.
- Professional, concise, human-like.
- Avoid awkward phrases like "I am writing to 5 days because...".
- If "source text" is a user instruction (e.g. "write marketing email describing X"), the subject must be a real subject line (e.g. "X Overview"), not the instruction; the body must fulfill the instruction with actual content, not "I am writing to [instruction]".{content_instruction}
- Keep recipient and leave duration/reason accurate when present.
- Do not add any new dates, days, timelines, or assumptions not explicitly in source text/profile.{context_section}

Tone: {tone}
Profile: {json.dumps(profile)}
Source text (user request – fulfill it, do not quote it): {(source_text or "")[:1000]}

Draft subject: {subject}
Draft body:
{body[:1800]}

Return only JSON."""
        out = await self._llm_generate(prompt, max_tokens=700)
        if not out and self.llm_provider and content_instruction:
            await asyncio.sleep(1.5)
            out = await self._llm_generate(prompt, max_tokens=700)
        parsed = self._parse_json_loose(out or "")
        if not parsed:
            return {"subject": subject, "body": body}
        new_subject = str(parsed.get("subject") or "").strip() or subject
        new_body = str(parsed.get("body") or "").strip() or body
        return {"subject": new_subject, "body": new_body}

    def _normalize_reason_text(self, reason: str) -> str:
        txt = (reason or "").strip()
        if not txt:
            return ""
        txt = txt.rstrip(".")
        if txt.lower().startswith(("i am ", "i'm ", "im ")):
            return txt
        return f"I am {txt}"

    # Verbs / phrases that indicate a profile "reason" is describing the writing task itself,
    # not a personal/leave reason that should be injected into the email body.
    # E.g. "describe all services", "market PlumoAi", "introduce our products".
    _TASK_REASON_INDICATORS = (
        "describ",
        "market",
        "introduc",
        "present",
        "promot",
        "explain",
        "all service",
        "our service",
        "our product",
        "our offer",
        "highlight",
        "overview",
        "proposal",
        "report",
        "inform",
        "notify",
        "write",
        "compose",
        "draft",
        "create",
        "send",
        "follow",
    )

    def _is_personal_reason(self, reason: str) -> bool:
        """
        Return True only when the reason looks like a genuine personal/leave context
        (sick, vacation, absence, medical, etc.) that should be injected into the body.
        Returns False for meta-descriptions of the writing task itself (e.g. "describe
        all services offered by PlumoAi") so they are never leaked into emails.
        """
        if not reason:
            return False
        r = reason.lower()
        # Reject task-description verbs — these are the AI's own purpose, not a personal reason
        if any(ind in r for ind in self._TASK_REASON_INDICATORS):
            return False
        # Accept if it mentions genuine personal/leave context
        _PERSONAL_INDICATORS = (
            "sick",
            "ill",
            "medical",
            "health",
            "hospital",
            "leav",
            "vacat",
            "absence",
            "absent",
            "personal",
            "emergency",
            "family",
            "bereavement",
            "annual leave",
            "urgent",
            "surgery",
            "recovery",
            "maternity",
            "paternity",
        )
        return any(p in r for p in _PERSONAL_INDICATORS)

    def _apply_fact_guard(self, body: str, profile: Dict[str, str], source_text: str) -> str:
        """
        Guard against fabricated specifics in rewritten drafts.
        - Remove weekday mentions if not present in source input.
        - Inject reason ONLY for personal/leave requests — not marketing or business emails.
        - Preserve paragraph formatting (newlines are never collapsed to spaces).
        """
        out = (body or "").strip()
        src = (source_text or "").lower()

        # Block fabricated weekdays/dates (e.g. "starting Monday") unless user/source mentions them.
        weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        if not any(day in src for day in weekdays):
            out = re.sub(
                r"\b(starting|from|beginning)\s+(?:the\s+)?(?:upcoming|next|this)?\s*(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
                "",
                out,
                flags=re.IGNORECASE,
            )
            # Collapse only HORIZONTAL whitespace (spaces/tabs), never newlines
            out = re.sub(r"[^\S\n]{2,}", " ", out).replace(" .", ".").strip()
            out = re.sub(r"\n{3,}", "\n\n", out)  # normalise excessive blank lines

        # Inject "Reason:" ONLY for personal/leave context — never for marketing/proposal emails
        reason = self._normalize_reason_text(profile.get("reason", ""))
        if reason and self._is_personal_reason(reason) and reason.lower() not in out.lower():
            addition = f"Reason: {reason}."
            if "Kind regards" in out:
                out = out.replace("Kind regards,", f"{addition}\n\nKind regards,", 1)
            elif "Best regards" in out:
                out = out.replace("Best regards,", f"{addition}\n\nBest regards,", 1)
            else:
                out = f"{out}\n\n{addition}"

        # Last-resort safety strip: at this point _resolve_placeholders should already have
        # filled every placeholder via LLM.  If any survived (e.g. LLM skipped them), replace
        # with sensible professional defaults rather than just deleting the tokens, which
        # would leave broken text like "Dear ," or "Best regards,\n".
        def _replace_placeholder(m: re.Match) -> str:  # type: ignore[type-arg]
            token = m.group(0).strip("[]{}").lower()
            if any(k in token for k in ("recipient", "name", "first name", "last name")):
                return "Valued Partner"
            if any(k in token for k in ("your name", "sender", "sign", "signature", "agent")):
                return ""  # will be cleaned up by sign-off logic above
            if any(k in token for k in ("company", "organisation", "organization")):
                return "our company"
            if any(k in token for k in ("position", "title", "role", "designation")):
                return ""
            if any(k in token for k in ("date", "time", "day")):
                return ""
            return ""  # generic fallback: remove unknown placeholder

        out = re.sub(r"\[[^\]\[]{1,80}\]", _replace_placeholder, out)
        out = re.sub(r"\{[^}{]{1,80}\}", _replace_placeholder, out)
        # Repair orphaned salutations left by removed name tokens, e.g. "Dear ," → "Dear Valued Partner,"
        out = re.sub(r"\b(Dear|Hi|Hello)\s*,", r"\1 Valued Partner,", out)
        out = re.sub(r"\b(Dear|Hi|Hello)\s+Valued Partner\s*,", r"\1 Valued Partner,", out)
        # Final cleanup: collapse only horizontal whitespace (preserve \n paragraph breaks)
        out = re.sub(r"[^\S\n]{2,}", " ", out).replace(" .", ".").replace(",\n\n\n", ",\n\n").strip()
        out = re.sub(r"\n{3,}", "\n\n", out)  # max two consecutive blank lines
        return out

    async def _classify_intent(self, user_query: str, tool_args: Optional[Dict]) -> Dict[str, Any]:
        """Intent & task classification: document type, objective, audience, urgency, tone."""
        context = ""
        if tool_args:
            parts = []
            if tool_args.get("intent"):
                parts.append("Intent: " + str(tool_args.get("intent")))
            if tool_args.get("context"):
                parts.append("Context: " + str(tool_args.get("context"))[:300])
            tone_pref = self._get_tone_preference(tool_args)
            if tone_pref:
                parts.append("Tone: " + tone_pref)
            if tool_args.get("audience"):
                parts.append("Audience: " + str(tool_args.get("audience")))
            if parts:
                context = "\n".join(parts)
        prompt = f"""Classify this writing request into structured fields. Output a single JSON object (no markdown).

Keys: document_type, objective, audience_type, urgency, tone_style
- document_type: one of email, proposal, memo, report, contract_draft, thank_you, follow_up
- objective: one of inform, persuade, follow_up, escalate, summarize, request
- audience_type: one of client, internal_team, executive, vendor, general
- urgency: one of low, medium, high
- tone_style: one of formal, friendly, persuasive, concise, neutral

{context}

User request: {(user_query or "").strip()[:500]}

JSON:"""
        out = await self._llm_generate(prompt, max_tokens=200)
        if not out:
            return {
                "document_type": "email",
                "objective": "inform",
                "audience_type": "general",
                "urgency": "medium",
                "tone_style": "professional",
            }
        out = out.strip().strip("`").strip()
        for prefix in ("json", "```"):
            if out.lower().startswith(prefix):
                out = out[len(prefix) :].lstrip()
        if out.endswith("```"):
            out = out[:-3].strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {
                "document_type": "email",
                "objective": "inform",
                "audience_type": "general",
                "urgency": "medium",
                "tone_style": "professional",
            }

    def _should_skip_external_context(self, user_query: str) -> bool:
        """
        Guardrail: skip context detection for very short requests with no context-related keywords.
        Avoids over-calling Gmail/KB for simple prompts like "write a thank you email".
        """
        text = (user_query or "").strip()
        words = [w for w in text.split() if w]
        if len(words) >= CONTEXT_REQUEST_MIN_WORDS:
            return False
        lowered = set(w.lower() for w in words)
        if lowered & CONTEXT_KEYWORDS:
            return False
        return True

    async def _needs_external_context(self, user_query: str, tool_args: Optional[Dict]) -> bool:
        """
        Ask LLM whether this writing request needs external data (emails, KB, etc.).
        No hardcoded keywords; returns True only when LLM says external data is needed.
        Short-request guardrail: skip LLM call when request is under 15 words and has no context keywords.
        """
        if self._should_skip_external_context(user_query):
            return False
        if not self.llm_provider:
            return False
        step = (tool_args or {}).get("step_action") or ""
        prompt = f"""Does this writing request need external data (e.g. from emails, inbox, knowledge base, documents, CRM) to be completed well? Answer only YES or NO.

Request: {(user_query or "").strip()[:300]}
{(("Step: " + step[:200]) if step else "")}

Answer (YES or NO):"""
        out = await self._llm_generate(prompt, max_tokens=10)
        if not out:
            return False
        return "yes" in (out or "").strip().lower()

    def _brief_needs_substantive_detail(self, user_query: str) -> bool:
        """True when the brief implies we need factual detail (e.g. describe company services) that may be missing from context."""
        if not user_query or not isinstance(user_query, str):
            return False
        brief = self._get_brief_from_user_message(user_query)
        text = (brief or "").lower()
        triggers = (
            "describe",
            "marketing",
            "service",
            "overview",
            "offer",
            "all service",
            "product",
            "company info",
            "what we do",
            "capabilities",
        )
        return any(t in text for t in triggers)

    async def _detect_context_needs(
        self,
        user_query: str,
        tool_args: Optional[Dict],
        already_fetched_summary: str = "",
    ) -> List[Dict[str, str]]:
        """
        Ask LLM which context steps would help. When the message already has "Complete context from main agent"
        (bullets), still suggest steps if the brief needs specific details (e.g. company services) not present.
        Steps can be Memory (natural-language question) or Web Search (query for company/product info).

        already_fetched_summary: plain-text digest of data already in provided_data (from previous context
        steps). If this summary answers the brief's key questions, the LLM should return [].
        """
        if not self.llm_provider:
            return []
        msg = (user_query or "").strip()
        # Always pin the actual brief at the top of the excerpt.
        # When context is prepended (workflow steps, bullets), the brief can be buried
        # past the 1200-char window and the LLM would never see the task instruction.
        brief = self._get_brief_from_user_message(msg)
        if brief and brief not in msg[:300]:
            # Brief is buried deep — surface it explicitly
            msg_excerpt = f"Task brief: {brief}\n\n{msg[:1000]}"
        else:
            msg_excerpt = msg[:1200]

        already_section = ""
        if already_fetched_summary and already_fetched_summary.strip():
            already_section = (
                f"\n\nData ALREADY FETCHED from previous context steps (do NOT request this again):\n"
                f"{already_fetched_summary[:1500]}\n"
            )

        prompt = f"""For this writing request, decide if the writer needs MORE context to write a detailed email. The message may already contain "Complete context from main agent" with bullets (contacts, recipients, workflow vars). If the brief asks for specific content (e.g. "describe all services offered by X", "marketing email about X") and that detail is NOT clearly present in the bullets OR the already-fetched data below, suggest one step to get it.

Return a JSON array of steps ONLY when the needed facts are genuinely absent from BOTH the message bullets AND the already-fetched data.

Return [] when:
- The request is simple or can be written from the text and existing context alone.
- The existing context (bullets or already-fetched data) already contains enough detail to fulfill the brief.
- The same query was already fetched and the data is present in the already-fetched section.

Available tools (use exact "name"): Memory, Web Search, Gmail, Knowledgebase.
- Memory: for stored/user knowledge. {{ "tool_name": "Memory", "query": "What are PlumoAi's main product offerings?" }}
- Web Search: for current company/product/service info. {{ "tool_name": "Web Search", "query": "PlumoAi services and offerings" }}
- Gmail/Knowledgebase: only if the request explicitly needs email or KB.

Suggest at most 1-2 steps. Each step: tool_name (string), query (string). Keep queries short.
{already_section}
User request (with any existing context):
{msg_excerpt}

JSON array of steps, or []:"""
        out = await self._llm_generate(prompt, max_tokens=280)
        if not out:
            return []
        out = out.strip().strip("`").strip()
        for prefix in ("json", "```"):
            if out.lower().startswith(prefix):
                out = out[len(prefix) :].lstrip()
        if out.endswith("```"):
            out = out[:-3].strip()
        try:
            arr = json.loads(out)
            if not isinstance(arr, list):
                return []
            steps = []
            for item in arr:
                if isinstance(item, dict) and item.get("tool_name") and item.get("query"):
                    steps.append({"tool_name": str(item["tool_name"]).strip(), "query": str(item["query"]).strip()})
            return steps[:5]
        except json.JSONDecodeError:
            return []

    async def _summarize_context(self, raw_context: str) -> str:
        """
        Summarize long context (e.g. Gmail From/Subject/Snippet list) into a short thread summary
        to save tokens and focus the draft. E.g. "Client asked about pricing last week."
        """
        if not raw_context or len(raw_context.strip()) < 300:
            return (raw_context or "").strip()
        if not self.llm_provider:
            return raw_context[:800]
        prompt = f"""Summarize this email/thread context in 1-3 short sentences for a writer drafting a reply. Capture: who, what they asked/said, and key facts. No bullet list.

Context:
{raw_context[:2000]}

Thread summary:"""
        out = await self._llm_generate(prompt, max_tokens=150)
        return (out or raw_context[:800]).strip()

    def _limit_context_tokens(self, text: str, max_chars: int = MAX_ENRICHED_CONTEXT_CHARS) -> str:
        """Truncate or cap context string to avoid prompt token explosion."""
        if not text:
            return ""
        s = (text or "").strip()
        if len(s) <= max_chars:
            return s
        return s[: max_chars - 50] + "\n\n[... context truncated for length ...]"

    def _format_provided_data_by_source(self, provided_data: Any) -> str:
        """Format provided_data from other apps (Gmail, KB, MCP, KB-chunks) for the draft prompt.

        Handles all known shapes dynamically:
          - Gmail messages list
          - Knowledgebase answer/data
          - KB search results wrapper (results:[{chunk_text, title, ...}])
          - KB chunk rows (chunk_text + title)
          - MCP / generic response
          - Nested result wrappers
          - Fallback key-value subset
        """
        if not provided_data or not isinstance(provided_data, list):
            return ""
        parts = []
        for i, item in enumerate(provided_data[:15]):
            if not isinstance(item, dict):
                continue

            # Gmail-style: messages list
            if "messages" in item and isinstance(item["messages"], list):
                emails = item["messages"][:5]
                lines = ["Past emails (from Gmail):"]
                for m in emails:
                    subj = m.get("subject") or ""
                    fr = m.get("from") or ""
                    snip = (m.get("snippet") or "")[:200]
                    lines.append(f"  From: {fr} | Subject: {subj} | {snip}")
                parts.append("\n".join(lines))
                logger.debug("_format: item[%d] → Gmail messages", i)
                continue

            # KB structured extraction output (highest-priority for AI Writer).
            # Shape: {"structured_data": {"items": [...], "categories": {...}}}
            # Produced by KnowledgebaseSearchTool._extract_structured_data after
            # retrieval.  Render as a clean named list the LLM can directly enumerate.
            if "structured_data" in item and isinstance(item.get("structured_data"), dict):
                sd = item["structured_data"]
                items = sd.get("items") or []
                categories = sd.get("categories") or {}
                if items:
                    lines = ["Knowledge base — extracted items:"]
                    if isinstance(categories, dict) and categories:
                        for cat, cat_items in categories.items():
                            if isinstance(cat_items, list) and cat_items:
                                lines.append(f"  {cat}:")
                                for it in cat_items:
                                    lines.append(f"    • {it}")
                    else:
                        for it in items:
                            lines.append(f"  • {it}")
                    parts.append("\n".join(lines))
                    logger.debug("_format: item[%d] → KB structured_data (%d items)", i, len(items))
                    continue

            # Knowledgebase wrapper with nested results list (chunk-based KB search response)
            # Shape: {"success": True, "results": [{"chunk_text": "...", "title": "...", ...}]}
            if "results" in item and isinstance(item.get("results"), list):
                chunks = item["results"]
                if chunks and isinstance(chunks[0], dict) and (
                    "chunk_text" in chunks[0] or "text" in chunks[0] or "content" in chunks[0]
                ):
                    lines = ["Knowledge base (search results):"]
                    for idx, chunk in enumerate(chunks[:8]):
                        title = (chunk.get("title") or chunk.get("document_title") or "").strip()
                        text = (
                            chunk.get("chunk_text") or chunk.get("text") or
                            chunk.get("content") or chunk.get("answer") or ""
                        ).strip()
                        if text:
                            header = f"  [{idx + 1}] {title}" if title else f"  [{idx + 1}]"
                            lines.append(header)
                            lines.append(f"  {text}")
                    if len(lines) > 1:
                        parts.append("\n".join(lines))
                        logger.debug("_format: item[%d] → KB search results wrapper (%d chunks)", i, len(chunks))
                    continue

            # KB chunk row (direct chunk dict without wrapper)
            # Shape: {"chunk_text": "...", "title": "...", "relevance_score": 0.9, ...}
            if "chunk_text" in item or ("text" in item and "relevance_score" in item):
                text = (item.get("chunk_text") or item.get("text") or "").strip()
                title = (item.get("title") or item.get("document_title") or "").strip()
                if text:
                    header = f"Knowledge base chunk — {title}:" if title else "Knowledge base chunk:"
                    parts.append(f"{header}\n{text}")
                    logger.debug("_format: item[%d] → KB chunk row (title=%s, len=%d)", i, title, len(text))
                continue

            # Knowledgebase answer/data (flat)
            if "answer" in item or "data" in item:
                src = "Knowledge base" if "answer" in item else "Data"
                text = item.get("answer") or item.get("data")
                if isinstance(text, str) and text.strip():
                    parts.append(f"{src}: {text}")
                    logger.debug("_format: item[%d] → KB answer/data", i)
                elif isinstance(text, list):
                    parts.append(f"{src}: " + " | ".join(str(x) for x in text[:10]))
                    logger.debug("_format: item[%d] → KB answer/data list", i)
                continue

            # Generic MCP / previous-step response
            if "response" in item and item.get("response"):
                parts.append(f"Context from previous step: {item.get('response') or ''}")
                logger.debug("_format: item[%d] → generic response", i)
                continue

            # Unpack nested result wrapper
            inner = item.get("result") if isinstance(item.get("result"), dict) else None
            if inner and isinstance(inner, dict):
                if "messages" in inner:
                    emails = inner["messages"][:5] if isinstance(inner["messages"], list) else []
                    lines = ["Past emails (from Gmail):"]
                    for m in emails:
                        if isinstance(m, dict):
                            lines.append(
                                f"  From: {m.get('from','')} | Subject: {m.get('subject','')} | {(m.get('snippet') or '')[:200]}"
                            )
                    if len(lines) > 1:
                        parts.append("\n".join(lines))
                elif inner.get("answer"):
                    parts.append("Knowledge base: " + str(inner["answer"]))
                elif inner.get("results") and isinstance(inner["results"], list):
                    # Nested search results
                    lines = ["Knowledge base (nested results):"]
                    for idx, chunk in enumerate(inner["results"][:8]):
                        if isinstance(chunk, dict):
                            text = (chunk.get("chunk_text") or chunk.get("text") or chunk.get("content") or "").strip()
                            title = (chunk.get("title") or "").strip()
                            if text:
                                lines.append(f"  [{idx + 1}] {title}" if title else f"  [{idx + 1}]")
                                lines.append(f"  {text}")
                    if len(lines) > 1:
                        parts.append("\n".join(lines))
                logger.debug("_format: item[%d] → nested result wrapper", i)
                continue

            # Fallback: surface any non-empty text-like values
            text_vals = {
                k: v for k, v in item.items()
                if k in (
                    "to", "from", "subject", "body", "summary", "content",
                    "response", "answer", "text", "description", "detail",
                )
                and v and isinstance(v, str)
            }
            if text_vals:
                parts.append("Context: " + json.dumps(text_vals))
                logger.debug("_format: item[%d] → fallback text fields %s", i, list(text_vals.keys()))

        result = "\n\n".join(parts) if parts else ""
        logger.info("_format_provided_data_by_source: %d items → %d sections → %d chars", len(provided_data), len(parts), len(result))
        return result

    def _extract_context_block_from_message(self, user_query: str, max_chars: int = 3500) -> str:
        """
        Extract the main-agent context block (bullets, step outputs) from the full message
        for use in dynamic body generation. Returns "" if no such block.
        Handles all casing variants produced by different message builders.
        """
        if not user_query or not isinstance(user_query, str):
            return ""
        text = user_query.strip()
        start = -1
        for marker in (
            "Complete context from main agent",
            "Context From Previous Steps",  # new workflow format (capital F/S)
            "Context from previous steps",  # legacy lowercase
        ):
            idx = text.find(marker)
            if idx >= 0:
                start = idx
                break
        if start < 0:
            return ""
        # Stop before the task instruction so it doesn't leak into the context block
        for stop_marker in ("Current step:", "Do this step"):
            end = text.find(stop_marker, start)
            if end > start:
                return text[start:end].strip()[:max_chars]
        return text[start : start + max_chars].strip()

    def _get_brief_from_user_message(self, user_query: str) -> str:
        """
        Extract only the task/brief from the full message.

        Format priority (newest first):
        1. "Do this step without any confirmation just perform this:" — new workflow format.
           Extract the task text that follows the header, stopping before "Note For Tool to use:"
           so the tool note never leaks into the generated content.
        2. Legacy "Current step:" — old workflow format.  Return everything from that marker.
        3. Context block without a step marker — return the text that comes before the context
           block (the real user request), or fall back to the post-context remainder.
        """
        if not user_query or not isinstance(user_query, str):
            return user_query or ""
        text = user_query.strip()

        # Dynamic context prepend format: "--- Writing task ---\n<task>" (runner prepends
        # agent identity + step bullets before this marker; extract only what follows).
        if "--- Writing task ---" in text:
            idx = text.find("--- Writing task ---")
            task_text = text[idx + len("--- Writing task ---"):].strip()
            if task_text:
                return task_text

        # New workflow step format: extract task between header and optional tool note
        if "Do this step" in text:
            idx = text.find("Do this step")
            step_block = text[idx:]
            # Skip the "Do this step...:" header line to reach the actual task text
            first_nl = step_block.find("\n")
            if first_nl >= 0:
                rest = step_block[first_nl:].strip()
                # Remove "Note For Tool to use:" and everything after it
                note_idx = rest.find("Note For Tool to use:")
                task_only = rest[:note_idx].strip() if note_idx >= 0 else rest.strip()
                if task_only:
                    return task_only
            return step_block.strip()

        # Legacy workflow step message: contains "Current step:" with Task/Input JSON
        if "Current step:" in text:
            idx = text.find("Current step:")
            return text[idx:].strip()

        # Context block without a step marker: use the text that precedes the context block
        if "Complete context from main agent" in text:
            idx = text.find("Complete context from main agent")
            before = text[:idx].strip()
            if before:
                return before
            after = text[idx:]
            end_marker = "\n\n"
            if end_marker in after:
                rest = after.split(end_marker, 1)[1].strip()
                if rest:
                    return rest
            return "Draft the email based on the context provided above."
        if "Context from previous steps" in text:
            idx = text.find("Context from previous steps")
            before = text[:idx].strip()
            if before:
                return before
        return text

    def _enrich_context(
        self,
        user_query: str,
        provided_data: Any,
        tool_args: Optional[Dict],
        pre_formatted_context: Optional[str] = None,
    ) -> str:
        """Build enriched context from provided_data (other apps), tool_args, and any main-agent bullet context in the message."""
        brief = self._get_brief_from_user_message(user_query or "")
        # Full brief — no independent truncation here. The overall enriched_context
        # already gets a single, token-budget-aware cap at the end of this method
        # (_limit_context_tokens), so an extra fixed char cutoff on just this piece
        # only risks silently cutting off the brief's own instructions/rules.
        parts = [f"User request (task to fulfill): {brief}"]
        # Main agent may pass complete context as bullets in the message (workflow step)
        main_ctx = (user_query or "").strip()
        if (
            "Complete context from main agent" in main_ctx
            or "Context from previous steps" in main_ctx
            or "Context From Previous Steps" in main_ctx
        ):
            for marker in (
                "Complete context from main agent",
                "Context from previous steps",
                "Context From Previous Steps",
            ):
                idx = main_ctx.find(marker)
                if idx >= 0:
                    break
            if idx >= 0:
                raw_block = main_ctx[idx : idx + 4000]
                # Strip the "Do this step" task section from the context block so it never
                # appears inside the bullet context sent to the LLM as data.
                do_step_idx = raw_block.find("Do this step")
                if do_step_idx >= 0:
                    raw_block = raw_block[:do_step_idx]
                # Also stop before the "gathered by main agent" block (that goes in its own section)
                gathered_idx = raw_block.find("--- Context gathered by main agent")
                if gathered_idx >= 0:
                    raw_block = raw_block[:gathered_idx]
                bullet_block = raw_block.strip()
                if bullet_block:
                    parts.append("--- Complete context from main agent (use these bullets for the draft) ---")
                    parts.append(bullet_block)
                    logger.info(
                        "   📋 _enrich_context: context block extracted from user_query → %d chars:\n%s",
                        len(bullet_block),
                        bullet_block,
                    )
        # Pre-flight context answered by main agent (sender identity, company/product facts, etc.)
        if "Context gathered by main agent for AI Writer" in main_ctx:
            g_idx = main_ctx.find("--- Context gathered by main agent for AI Writer ---")
            if g_idx >= 0:
                gathered_block = main_ctx[g_idx : g_idx + 2000].strip()
                if gathered_block:
                    parts.append(
                        "--- Answers from main agent (use these to personalise and sign off the draft) ---"
                    )
                    parts.append(gathered_block)
        if tool_args and tool_args.get("context_bullets"):
            parts.append("--- Context (bullets from main agent) ---")
            parts.append(str(tool_args["context_bullets"])[:3000])
        # Write on behalf of top-agent identity when context does not provide author
        identity = self._get_identity_for_draft(tool_args)
        if identity:
            parts.append(
                "--- Write on behalf of (use this identity for sign-off and voice; do not invent a different author) ---"
            )
            parts.append(f"Identity: {identity}")
        # Real data from other apps: use pre_formatted (e.g. thread summary) when provided, else format by source
        if pre_formatted_context:
            parts.append("--- Context from other apps (use this to personalize and align the draft) ---")
            parts.append(pre_formatted_context)
        else:
            from_apps = self._format_provided_data_by_source(provided_data)
            if from_apps:
                parts.append("--- Context from other apps (use this to personalize and align the draft) ---")
                parts.append(from_apps)
        if tool_args:
            if tool_args.get("step_action"):
                parts.append("Step / full request: " + str(tool_args["step_action"])[:500])
            if tool_args.get("context"):
                parts.append("Context (user): " + str(tool_args["context"])[:400])
            if tool_args.get("recipient_name") or tool_args.get("recipient_email"):
                parts.append(
                    "Recipient: "
                    + (tool_args.get("recipient_name") or tool_args.get("recipient_email") or "")
                )
            if tool_args.get("deadline"):
                parts.append("Deadline: " + str(tool_args["deadline"]))
            if tool_args.get("company_name") or tool_args.get("brand_notes"):
                parts.append(
                    "Brand/Company: "
                    + (tool_args.get("company_name") or "")
                    + " "
                    + (tool_args.get("brand_notes") or "")[:200]
                )
        return self._limit_context_tokens("\n".join(parts))

    async def _plan_strategy_structured(
        self, user_query: str, classification: Dict, enriched_context: str
    ) -> Dict[str, Any]:
        """
        Writing strategy as structured JSON for consistent drafts: greeting, reference_previous_email,
        value_proposition, call_to_action, closing_style. Increases draft consistency.
        """
        if not self.llm_provider:
            return {
                "greeting": True,
                "reference_previous_email": False,
                "value_proposition": "",
                "call_to_action": "Please let me know if you need anything.",
                "closing_style": "polite",
            }
        doc_type = classification.get("document_type") or "email"
        obj_key = (classification.get("objective") or "").replace(" ", "_").replace("-", "_")
        template_hint = EMAIL_TEMPLATE_HINTS.get(obj_key) or EMAIL_TEMPLATE_HINTS.get(doc_type, "")
        prompt = f"""Given the writing request and classification, output a single JSON object (no markdown) with these exact keys:
- greeting (boolean): whether to include a greeting
- reference_previous_email (boolean): whether to reference a previous email/thread
- value_proposition (string): one sentence on the main value or ask
- call_to_action (string): one short CTA phrase
- closing_style (string): one of polite, warm, formal, concise

Classification: {json.dumps(classification)}
{f"Template hint: {template_hint}" if template_hint else ""}

{self._limit_context_tokens(enriched_context, max_chars=800)}

Output only valid JSON."""
        out = await self._llm_generate(prompt, max_tokens=280)
        parsed = self._parse_json_loose(out or "")
        if not parsed:
            return {
                "greeting": True,
                "reference_previous_email": False,
                "value_proposition": "",
                "call_to_action": "Please let me know if you need anything.",
                "closing_style": (classification.get("tone_style") or "polite").lower(),
            }
        return {
            "greeting": bool(parsed.get("greeting", True)),
            "reference_previous_email": bool(parsed.get("reference_previous_email", False)),
            "value_proposition": str(parsed.get("value_proposition") or "").strip(),
            "call_to_action": str(parsed.get("call_to_action") or "Please let me know if you need anything.").strip(),
            "closing_style": str(parsed.get("closing_style") or "polite").strip().lower(),
        }

    async def _plan_strategy(self, user_query: str, classification: Dict, enriched_context: str) -> Dict[str, Any]:
        """Writing strategy as structured JSON; also used for display as short summary."""
        return await self._plan_strategy_structured(user_query, classification, enriched_context)

    async def _sensitive_document_detection(self, user_query: str, classification: Dict) -> Optional[str]:
        """
        Detect sensitive document types (legal, HR complaint, termination, escalation).
        Returns a short compliance note to add to strategy/prompt, or None.
        """
        if not self.llm_provider:
            return None
        text = (user_query or "").strip().lower()
        sensitive_terms = (
            "legal",
            "lawyer",
            "sue",
            "hr complaint",
            "termination",
            "fire",
            "escalat",
            "complaint",
            "discriminat",
            "harassment",
        )
        if not any(t in text for t in sensitive_terms):
            return None
        prompt = f"""This writing request may involve sensitive content. Classification: {json.dumps(classification)}

Request (excerpt): {text[:400]}

If the request is clearly sensitive (legal, HR, termination, escalation, discrimination, etc.), reply with one short compliance note (e.g. "Suggest formal tone and avoid commitment; recommend legal/HR review."). Otherwise reply with exactly: NONE"""
        out = await self._llm_generate(prompt, max_tokens=80)
        if not out or "none" in (out or "").strip().lower():
            return None
        return (out or "").strip()[:300]

    async def _fallback_draft_from_context(
        self,
        user_query: str,
        tool_args: Optional[Dict],
        classification: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build a contextual draft when LLM output is empty/invalid.
        Never use generic boilerplate like "Dear Sir/Madam".
        """
        step_action = (tool_args or {}).get("step_action") or ""
        # Use only the task/brief so we don't use context block as purpose (would leak into email)
        text = self._get_brief_from_user_message(user_query or "") or (step_action or "").strip() or "Request"
        profile = await self._extract_request_profile(text, tool_args)
        tone = self._get_tone_preference(tool_args, classification)

        recipient = profile.get("recipient_name") or "there"
        greeting = f"Hi {recipient}," if recipient != "there" else "Hello,"
        purpose = profile.get("purpose") or text[:220]
        duration = profile.get("duration")
        reason = profile.get("reason")

        # Subject: use a short descriptor, never the user's instruction verbatim (e.g. not "write marketing email describing...")
        subject = "Request"
        if duration and "leave" in (purpose or "").lower():
            subject = f"Leave Request for {duration}"
        elif "follow" in (purpose or "").lower():
            subject = "Follow-up on Previous Request"
        elif "market" in (purpose or "").lower() or "service" in (purpose or "").lower():
            subject = "Services Overview"
        elif "thank" in (purpose or "").lower():
            subject = "Thank You"
        else:
            first_line = (purpose or "").split("\n")[0].strip()
            if first_line and len(first_line) < 50 and not first_line.lower().startswith(
                ("write", "draft", "compose", "create", "send")
            ):
                subject = first_line[:70]
            else:
                subject = "Message"

        # Body: do not forward the user's order. When brief is a content request (marketing, describe, services), add a line so polish can expand into substantive content.
        body_lines = [
            greeting,
            "",
            "I hope you are doing well.",
        ]
        if purpose and not re.search(r"^\s*(write|draft|compose|create|send)\s+", purpose.strip().lower()):
            body_lines.append(f" {purpose.rstrip('.')}.")
        else:
            # Brief is an instruction (e.g. "write marketing email describing all services") — generate substantive paragraph dynamically from context
            purpose_lower = (purpose or text or "").lower()
            if any(x in purpose_lower for x in ("market", "service", "describ", "overview", "offer")):
                context_block = self._extract_context_block_from_message(user_query or "")
                overview = ""
                if (context_block or "").strip():
                    overview = await self._generate_body_from_context(context_block, text, max_sentences=4)
                if not overview:
                    company = (profile.get("company_name") or (tool_args.get("company_name") if tool_args else "")) or ""
                    if not company and isinstance(text, str):
                        for word in re.findall(r"[A-Z][a-zA-Z0-9]+", text):
                            if len(word) > 2 and word.lower() not in (
                                "write",
                                "draft",
                                "email",
                                "professional",
                                "marketing",
                                "describing",
                                "services",
                                "offered",
                                "please",
                                "overview",
                            ):
                                company = word
                                break
                    if not company and user_query:
                        for word in re.findall(r"[A-Z][a-zA-Z0-9]+", (user_query or "")[:500]):
                            if len(word) > 2 and word.lower() not in (
                                "write",
                                "draft",
                                "email",
                                "professional",
                                "marketing",
                                "describing",
                                "services",
                                "offered",
                                "complete",
                                "context",
                                "main",
                                "agent",
                                "step",
                                "trigger",
                            ):
                                company = word
                                break
                    if not company:
                        company = "our company"
                    overview = await self._generate_services_overview_paragraph(
                        company, (context_block or text or "")[:500], max_sentences=4
                    )
                if overview:
                    body_lines.append("")
                    body_lines.append(overview)
                else:
                    body_lines.append(" We would be glad to share more information when you need it.")
        body_lines.append("")
        if duration:
            body_lines.append(f"This request is for {duration}.")
        if reason:
            body_lines.append(f"Reason: {reason}.")
        body_lines.extend(
            [
                "Please let me know if you need any additional details.",
                "",
                "Kind regards,",
            ]
        )
        signoff_name = self._get_signoff_name(tool_args)
        if signoff_name:
            body_lines.append(signoff_name)
        if (
            tone.lower() in ("friendly", "neutral")
            and purpose
            and not re.search(r"^\s*(write|draft|compose|create|send)\s+", purpose.strip().lower())
        ):
            body_lines[2] = f"I wanted to reach out to {purpose.rstrip('.')}."
        body = "\n".join(body_lines)
        context_excerpt = self._extract_context_block_from_message(user_query or "")[:800] if user_query else ""
        polished = await self._polish_fallback_with_llm(
            subject, body[:2200], profile, tone, text, context_excerpt=context_excerpt
        )
        return {
            "subject": polished.get("subject", subject),
            "body": polished.get("body", body[:2200]),
            "draft_source": "fallback",
        }

    async def _generate_draft(
        self,
        user_query: str,
        classification: Dict,
        strategy: Any,
        enriched_context: str,
        tool_args: Optional[Dict],
        sensitive_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Draft generation: subject (if email), body, CTA, closing. Strategy can be structured dict or string. Retries with simpler prompt if LLM returns nothing."""
        tone = self._get_tone_preference(tool_args, classification)
        identity_note = ""
        if self._get_identity_for_draft(tool_args):
            identity_note = " Write the email on behalf of the identity given in context (use that identity for sign-off and voice; do not invent a different author)."
        brief = self._get_brief_from_user_message(user_query or "")
        profile = await self._extract_request_profile(brief, tool_args or {})
        # Structured plan increases consistency
        if isinstance(strategy, dict):
            strategy_text = json.dumps(strategy)
            plan_instruction = f'Follow this structure: greeting={strategy.get("greeting", True)}, reference_previous={strategy.get("reference_previous_email", False)}, value_proposition="{strategy.get("value_proposition", "")}", CTA="{strategy.get("call_to_action", "")}", closing_style={strategy.get("closing_style", "polite")}.'
        else:
            strategy_text = (strategy or "")[:300]
            plan_instruction = f"Strategy: {strategy_text}"
        doc_type = classification.get("document_type") or "email"
        obj_key = (classification.get("objective") or "").replace(" ", "_").replace("-", "_")
        template_hint = EMAIL_TEMPLATE_HINTS.get(obj_key) or EMAIL_TEMPLATE_HINTS.get(doc_type, "")
        compliance_note = ""
        if sensitive_note:
            compliance_note = f"\nCompliance note (follow this): {sensitive_note}"
        style_profile = (tool_args or {}).get("writing_style_profile") or (tool_args or {}).get("style_preferences")
        style_instruction = ""
        if style_profile:
            if isinstance(style_profile, str):
                style_instruction = f"\nWriter style preference: {style_profile[:300]}"
            elif isinstance(style_profile, dict):
                style_instruction = "\nWriter style preferences: " + json.dumps(style_profile)[:300]
        # User request is the *brief*: we fulfill it, not quote it. Subject/body must be real content, not the instruction text.
        brief_rule = """- CRITICAL – Brief vs. content: The user's request below is the *instruction* for what to write. Fulfill it; do not quote or forward it into the email.
  * Subject: Write a proper email subject line that describes the email's topic (e.g. "PlumoAi Services Overview"). Do NOT use the user's instruction text as the subject (e.g. never use "write marketing email describing..." as subject).
  * Body: Write the actual email content that fulfills the request. Do NOT write "I am writing to [instruction]" or put the request in order form. If the request is "describe PlumoAi services", the body must actually describe the services; do not repeat the request.
  * NEVER include in the email body any "Complete context from main agent" section, bullet points (•), or step context — use that only as reference; the body must be a normal email only."""
        # When brief asks to describe services/offerings, require listing EVERY distinct service by name
        brief_lower = (brief or "").lower()
        services_rule = ""
        if any(
            x in brief_lower
            for x in ("market", "describ", "service", "overview", "offer", "all service", "product")
        ):
            services_rule = (
                "\n- When the brief asks to describe a company's services or offerings: you MUST list EVERY distinct service,"
                " product, or offering found by name in the context/provided data, with a 1–2 sentence description of each."
                " Do NOT summarise into 2-4 generic sentences — each service must appear by its actual name and key benefit."
                " If the context does not mention specific services, write substantive marketing paragraphs using all available facts."
            )
        # Require using ALL context data — including recipient names — dynamically
        context_use_rule = (
            "\n- RECIPIENT PERSONALISATION (autonomous): Scan the context below for any contact names, 'name' fields,"
            " contacts lists, contacts_item, or email_recipients. If real names are found, use them in the greeting"
            " (e.g. 'Dear Krishna Kumar,' for one person; 'Dear Krishna and Hussain,' for two; suitable professional"
            " group greeting for many). Only fall back to 'Dear Sir/Madam' when NO names exist anywhere in the context."
            "\n- DATA COMPLETENESS: Every fact, data point, service name, product, or offering found in the context"
            " MUST be reflected in the email body. Do not omit available information — the email should be as complete"
            " and specific as the data allows. Generic filler sentences are only acceptable when no specific data exists."
        )
        if enriched_context and (
            "Complete context from main agent" in enriched_context
            or "• From step" in enriched_context
            or "Context from previous steps" in enriched_context
            or "Context From Previous" in enriched_context
        ):
            context_use_rule += (
                "\n- The context contains step outputs and workflow data (bullets, JSON outputs). Parse those values and"
                " incorporate them directly — company names, contact names, step outputs, workflow variables, service lists."
            )
        # The enriched_context already begins with "User request (task to fulfill): <brief>"
        # (added by _enrich_context). Strip that leading line so the brief only appears
        # once in the prompt — in the dedicated labeled section below — preventing the LLM
        # from echoing it verbatim into the email body.
        _ec = enriched_context.strip()
        if _ec.startswith("User request (task to fulfill):"):
            _first_break = _ec.find("\n")
            _ec = _ec[_first_break:].strip() if _first_break >= 0 else _ec

        # Determine token budget: richer content (services/marketing) needs more room
        _brief_lower_check = (brief or "").lower()
        _is_rich_content = any(
            x in _brief_lower_check
            for x in (
                "market",
                "describ",
                "service",
                "overview",
                "offer",
                "all service",
                "product",
                "report",
                "proposal",
            )
        )
        _draft_max_tokens = 1800 if _is_rich_content else 1200

        prompt = f"""Generate a single JSON object for a professional draft.

Rules:
- For email: include "subject" and "body". Body: greeting, main content, clear call-to-action, polite closing. Use a brief sign-off consistent with the identity in context when provided.{identity_note}
- Write on behalf of your own identity (the writer/digital employee in context). The email is from you, not from the user; do not forward the user's order as the message.
{brief_rule}{services_rule}{context_use_rule}
- For memo/report: include "subject" (title) and "body" (sections as needed).
- Be thorough and complete — do not cut the body short. All services, facts, and data points from context must appear.
- Use concrete details from the request/context (recipient, purpose, duration, reason). Avoid generic placeholders.
- Never output unresolved placeholders such as [start date], [end date], [Your Name], {{name}}.
- Never start with "Dear Sir/Madam" when a real recipient name is available in the context. Tone: {tone}.
- Do NOT invent recipient names — use only names explicitly found in the context. Never use "auto", "automation", or similar fragments as a name.
- Do NOT write that the email will be sent, delivered, or automatically processed. You only draft; do not describe future delivery or sending.
{plan_instruction}
{f"Template hint: {template_hint}" if template_hint else ""}{compliance_note}{style_instruction}

Classification: {json.dumps(classification)}
Derived request profile: {json.dumps(profile)}
User request (this is the brief to fulfill; do not paste it into subject or body): {brief}

{_ec[:5000]}

Output JSON only with keys: subject, body. No markdown."""
        out = await self._llm_generate(prompt, max_tokens=_draft_max_tokens)
        if not out and self.llm_provider:
            # Transient empty response (e.g. OpenRouter) — retry once after short delay
            await asyncio.sleep(2.0)
            out = await self._llm_generate(prompt, max_tokens=1000)
        if not out:
            # Retry once with minimal prompt so we don't fail on transient LLM issues
            fallback_prompt = f"""Write a professional email as JSON. Keys: "subject", "body".
The following is the user's *instruction* (brief) to fulfill. Do NOT use it as the subject or as the body text. Produce a real subject line (e.g. "Services Overview") and a body that fulfills the instruction. Do NOT include any "Complete context" or bullet-point context in the body — normal email only.
Request (brief to fulfill): {brief}
Output only valid JSON, no markdown."""
            out = await self._llm_generate(fallback_prompt, max_tokens=800)
        if not out:
            return await self._fallback_draft_from_context(user_query, tool_args, classification)

        parsed = self._parse_json_loose(out)
        if not parsed:
            # Repair pass: force the model to convert malformed output into JSON.
            repair_prompt = f"""Convert this text into valid JSON with keys "subject" and "body" only.
Text:
{self._strip_wrappers(out)[:1800]}

Return only JSON."""
            repaired = await self._llm_generate(repair_prompt, max_tokens=700)
            parsed = self._parse_json_loose(repaired or "")

        if parsed:
            subj = str(parsed.get("subject") or "").strip()
            body = str(parsed.get("body") or "").strip()
            if subj or body:
                return {"subject": subj, "body": body, "draft_source": "llm"}
        return await self._fallback_draft_from_context(user_query, tool_args, classification)

    async def _resolve_placeholders(
        self,
        draft: Dict[str, Any],
        enriched_context: str,
        user_query: str = "",
    ) -> Dict[str, Any]:
        """
        Autonomously detect and fill every unresolved placeholder in the draft
        ([Recipient Name], [Your Name], {company}, etc.) using LLM reasoning
        over the full enriched context (recipients, sender identity, company,
        workflow outputs, etc.).

        The LLM decides:
          - what real value to substitute for each placeholder (from context), or
          - what professional default to use when context has no explicit value.

        Only runs when at least one placeholder pattern is actually present in the
        draft — otherwise returns the draft unchanged (zero extra LLM cost).
        """
        subject = (draft.get("subject") or "").strip()
        body = (draft.get("body") or "").strip()
        full_text = subject + " " + body

        # Detect both bracket/brace placeholders AND generic salutations that may
        # be resolvable from context (e.g. "Sir/Madam", "Valued Partner", "To Whom It May Concern")
        has_brackets = bool(re.search(r"\[[^\]\[]{1,80}\]|\{[^}{]{1,80}\}", full_text))
        # Generic salutations are "resolvable" only when context likely contains real names
        _generic_salutation_pattern = re.compile(
            r"\b(Sir/Madam|Sir or Madam|Valued Partner|To Whom It May Concern|Dear Colleague)\b",
            re.IGNORECASE,
        )
        has_generic_salutation = bool(_generic_salutation_pattern.search(body))
        _ctx_has_names = bool(
            re.search(
                r'"name"\s*:\s*"[A-Za-z]|contacts\s*:|contacts_item|email_recipients',
                enriched_context,
                re.IGNORECASE,
            )
        )
        needs_resolution = has_brackets or (has_generic_salutation and _ctx_has_names)

        if not needs_resolution:
            return draft

        # Build appropriate instruction based on what needs fixing
        salutation_instruction = ""
        if has_generic_salutation and _ctx_has_names:
            salutation_instruction = (
                "\n6. SALUTATION FIX: The draft uses a generic salutation (e.g. 'Sir/Madam', 'Valued Partner',"
                " 'To Whom It May Concern'). Check the Context block for actual recipient names (look inside"
                " contacts, contacts_item, name fields, email_recipients). If real names are found, replace the"
                " generic salutation with a personalised one (e.g. 'Dear Krishna Kumar,' or 'Dear Krishna and Hussain,')."
                " If no names are found, keep the generic salutation but make it professional."
            )

        prompt = (
            "The email draft below may contain: (a) unresolved placeholders in [brackets] or {braces},"
            " or (b) generic salutations that can be personalised using the Context.\n\n"
            "Your job: resolve ALL issues and return the complete improved draft.\n\n"
            "Rules:\n"
            "0. NEVER use 'auto', 'automation', or similar fragments as recipient names — only real person names from Context.\n"
            "1. Use values explicitly found in the Context block (recipient names, sender "
            "identity, company name, product names, titles, etc.).\n"
            "2. For any placeholder where context provides NO explicit value, substitute a "
            "professional non-placeholder default — e.g. 'Valued Partner' for [Recipient Name], "
            "the agent's first name or 'Your Name' written out for [Your Name], "
            "'Our Company' for [Company Name].\n"
            "3. NEVER leave any [placeholder] or {placeholder} in the output — resolve ALL.\n"
            "4. Keep every other sentence, paragraph, and formatting exactly as-is. "
            "Only replace the placeholder tokens and fix the salutation; do not rewrite other content.\n"
            "5. Output a single JSON object with exactly two keys: \"subject\" and \"body\". "
            f"No markdown fences.{salutation_instruction}\n\n"
            f"Context:\n{enriched_context[:3200]}\n\n"
            f"Draft:\nSubject: {subject}\nBody:\n{body}\n\n"
            "Output JSON:"
        )

        out = await self._llm_generate(prompt, max_tokens=2000)
        if not out:
            return draft
        data = self._parse_json_loose(out)
        if not data or not isinstance(data, dict):
            return draft
        new_subject = (data.get("subject") or subject).strip()
        new_body = (data.get("body") or body).strip()
        # Sanity: if LLM returned something shorter than 20 chars for the body, keep original
        if len(new_body) < 20:
            return draft
        return {"subject": new_subject, "body": new_body}

    async def _self_review(
        self,
        draft: Dict,
        classification: Dict,
        source_text: str = "",
        profile: Optional[Dict[str, str]] = None,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Self-review & optimization: grammar, tone consistency, redundancy, readability."""
        body_for_review = draft.get("body", "")
        # Use up to 5000 chars so long marketing emails are reviewed in full, not silently truncated
        prompt = f"""Review this draft for: 1) Grammar and clarity, 2) Tone consistency ({classification.get('tone_style', 'professional')}), 3) Redundancy removal, 4) Business appropriateness.

If the draft is already good, return it with minimal changes. If you improve it, return the improved version.
Do not add facts not present in the draft/request (no invented day/date/time/location/details).
Do not invent recipient names (e.g. "Dear Auto" from "automatically") — use only names from the draft or context.
Do not add language about sending, delivery, or automatic processing — this is a draft only.
Preserve key facts such as leave duration and reason if present.
IMPORTANT: Return the COMPLETE body — do not cut or shorten the content. All paragraphs and sections must be present in your output.

Draft:
Subject: {draft.get('subject', '')}
Body:
{body_for_review[:5000]}

Output a single JSON object with keys: subject, body (the final improved draft). No markdown."""
        # Increase max_tokens proportionally so the full body can be returned
        _body_len = len(body_for_review)
        _review_max_tokens = max(1000, min(4000, _body_len // 2 + 500))
        out = await self._llm_generate(prompt, max_tokens=_review_max_tokens)
        if not out:
            return draft
        data = self._parse_json_loose(out)
        if not data:
            return draft
        reviewed_body = self._apply_fact_guard(
            (data.get("body") or draft.get("body") or "").strip(),
            profile or {},
            source_text,
        )
        signoff_name = self._get_signoff_name(tool_args)
        if signoff_name and not re.search(rf"\b{re.escape(signoff_name)}\b", reviewed_body, flags=re.IGNORECASE):
            if "Kind regards," in reviewed_body:
                reviewed_body = reviewed_body.replace("Kind regards,", f"Kind regards,\n{signoff_name}", 1)
            elif "Best regards," in reviewed_body:
                reviewed_body = reviewed_body.replace("Best regards,", f"Best regards,\n{signoff_name}", 1)
        return {
            "subject": (data.get("subject") or draft.get("subject") or "").strip(),
            "body": reviewed_body,
        }

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict, None]:
        """
        Run the AI Writer pipeline. When provided_data is empty, can request context from
        other apps (Gmail, Knowledgebase, etc.); executor runs those as new steps and re-invokes
        with provided_data. Then: classify -> enrich context -> plan -> generate -> self-review.
        """
        try:
            # ── Incoming data diagnostics ─────────────────────────────────────
            # Logs exactly what the AI Writer receives from the runner so you can
            # see which keys/shapes come from MCP tools, Gmail, KB, etc.
            logger.info(
                "📝 AI Writer run() called | query_len=%d | tool_args_keys=%s | "
                "provided_data_type=%s | provided_data_len=%s",
                len(user_query or ""),
                list((tool_args or {}).keys()),
                type(provided_data).__name__,
                len(provided_data) if isinstance(provided_data, (list, dict)) else "n/a",
            )
            if isinstance(provided_data, list):
                for _i, _item in enumerate(provided_data):
                    if isinstance(_item, dict):
                        _top_keys = list(_item.keys())
                        logger.info("   📦 provided_data[%d]: keys=%s", _i, _top_keys)
                        # Log every non-empty value in full (no truncation)
                        for _k, _v in _item.items():
                            if _v is not None and _v != "" and _v != [] and _v != {}:
                                logger.info("      [%s] = %s", _k, _v)
                    else:
                        logger.info("   📦 provided_data[%d]: type=%s val=%s", _i, type(_item).__name__, _item)
            if tool_args:
                for _k, _v in (tool_args or {}).items():
                    logger.info("   🔧 tool_arg[%s] = %s", _k, _v)
            # Context markers present in the message
            _ctx_markers = [
                m for m in (
                    "Complete context from main agent",
                    "Context From Previous Steps",
                    "Context from previous steps",
                    "Do this step",
                    "Current step:",
                )
                if m in (user_query or "")
            ]
            if _ctx_markers:
                logger.info("   📋 Context markers in user_query: %s", _ctx_markers)
            # ─────────────────────────────────────────────────────────────────

            # Promote behavioral rules from user_query into system prompt for all downstream LLM calls.
            # This ensures constraints like "never mention X" are enforced at the instruction level,
            # not buried as soft context in the user message.
            self._behavioral_rules_str = self._extract_behavioral_rules(user_query or "")
            if self._behavioral_rules_str:
                logger.info(
                    "   🔒 Behavioral rules extracted → %d chars (will be injected into every LLM system prompt)",
                    len(self._behavioral_rules_str),
                )

            # Main agent may have given context as bullets; we may still need more detail for the email
            # Check all casing variants: workflow uses "Context From Previous Steps" (capitals) or
            # "Context from previous steps" (lower) or "Complete context from main agent"
            _uq = user_query or ""
            has_main_agent_context = (
                "Complete context from main agent" in _uq
                or "Context from previous steps" in _uq
                or "Context From Previous Steps" in _uq
            )
            has_prior_data = bool(provided_data and isinstance(provided_data, list) and len(provided_data) > 0)
            # If we were re-invoked after context steps and got empty list, generate from available info only (don't ask again)
            context_round_empty = (
                provided_data is not None
                and isinstance(provided_data, list)
                and len(provided_data) == 0
                and ((tool_args or {}).get("step_action") or (tool_args or {}).get("context"))
            )
            # Build a compact plain-text digest of what's already in provided_data so the
            # context-need detector can see that information is already present and avoid
            # requesting it again (prevents infinite web-search loops).
            _already_fetched_summary = ""
            if has_prior_data:
                _already_fetched_summary = self._format_provided_data_by_source(provided_data)[:2000]

            # Ask for context when: (1) no prior tool data, or (2) we have main-agent bullets but brief needs more detail.
            # Do NOT enter the context loop when provided_data already has substantive external content — the
            # LLM in _detect_context_needs will see the already-fetched summary and return [] anyway, but
            # this fast-path avoids the extra LLM call entirely.
            _prior_has_external_content = has_prior_data and bool(_already_fetched_summary.strip())
            should_check_context = not context_round_empty and not _prior_has_external_content and (
                not has_prior_data
                or (has_main_agent_context and self._brief_needs_substantive_detail(user_query or ""))
            )
            # Secondary safety: if external data IS present but the brief still somehow needs ONE
            # specific thing not in it, allow a single pass through _detect_context_needs (with the
            # already-fetched summary so the LLM can make an informed decision).
            if not should_check_context and _prior_has_external_content and not has_main_agent_context:
                if self._brief_needs_substantive_detail(user_query or ""):
                    needs_external = await self._needs_external_context(user_query, tool_args)
                    if needs_external:
                        suggested = await self._detect_context_needs(user_query, tool_args, _already_fetched_summary)
                        if suggested:
                            yield event(
                                AgentEvent.THOUGHT,
                                "Requesting supplementary context; the main agent will gather it and re-invoke the writer.",
                            )
                            result = {
                                "success": False,
                                "need_context": True,
                                "suggested_context_steps": suggested,
                                "response": "Context will be gathered by the suggested step(s). The main agent will run them and call AI Writer again with the new context.",
                                "result": {"need_context": True, "suggested_context_steps": suggested},
                            }
                            yield event(AgentEvent.RESULT, result)
                            yield event(AgentEvent.FINAL, {**result, "result": result})
                            return
            if should_check_context:
                # When we have main context, still ask LLM if more detail is needed (e.g. company services not in bullets)
                needs_external = await self._needs_external_context(user_query, tool_args) if not has_main_agent_context else True
                if needs_external or has_main_agent_context:
                    suggested = await self._detect_context_needs(user_query, tool_args, _already_fetched_summary)
                else:
                    suggested = []
                if suggested:
                    yield event(
                        AgentEvent.THOUGHT,
                        "Requesting more context from the main agent (e.g. Memory or Web Search) so the email can include specific details; the main agent will run the suggested step(s) and then re-invoke the writer.",
                    )
                    result = {
                        "success": False,
                        "need_context": True,
                        "suggested_context_steps": suggested,
                        "response": "Context will be gathered by the suggested step(s). The main agent will run them and call AI Writer again with the new context.",
                        "result": {
                            "need_context": True,
                            "suggested_context_steps": suggested,
                        },
                    }
                    yield event(AgentEvent.RESULT, result)
                    yield event(AgentEvent.FINAL, {**result, "result": result})
                    return

            if context_round_empty or (not has_prior_data and not has_main_agent_context):
                yield event(AgentEvent.THOUGHT, "Generating draft from available information only.")

            yield event(AgentEvent.THOUGHT, "Classifying your writing request and planning the draft...")

            # Use only the task/brief so context block is never treated as the request (would leak into email)
            brief = self._get_brief_from_user_message(user_query or "")
            source_text = " ".join(
                str(x).strip()
                for x in [
                    brief,
                    (tool_args or {}).get("step_action") or "",
                    (tool_args or {}).get("context") or "",
                ]
                if x
            ).strip()

            # Parallel: profile + classification to cut latency (use brief so profile/purpose is the task, not context block)
            profile_fut = self._extract_request_profile(brief, tool_args)
            classification_fut = self._classify_intent(brief, tool_args)
            profile, classification = await asyncio.gather(profile_fut, classification_fut)
            # Backfill missing profile fields from broader step context when needed
            if source_text and any(not (profile.get(k) or "").strip() for k in ("reason", "duration", "purpose")):
                ctx_profile = await self._extract_request_profile(source_text, tool_args)
                for k in ("reason", "duration", "purpose"):
                    if not (profile.get(k) or "").strip() and (ctx_profile.get(k) or "").strip():
                        profile[k] = ctx_profile[k]

            # Optional context summarization to save tokens (thread summary instead of raw Gmail dump).
            # Skip summarization when data contains knowledge base chunks or search results —
            # summarization collapses specific service/product lists into vague prose, losing the
            # exact items the LLM needs to enumerate in the draft. Detection is structural: any item
            # with chunk_text / relevance_score / chunk_id keys, or a results list containing those
            # keys, signals reference/factual content that must reach the LLM verbatim.
            _kb_content_keys = {"chunk_text", "relevance_score", "chunk_id", "chunk_index"}
            def _has_kb_content(items: list) -> bool:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if _kb_content_keys & set(item.keys()):
                        return True
                    nested = item.get("results")
                    if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                        if _kb_content_keys & set(nested[0].keys()):
                            return True
                return False

            pre_formatted = None
            if provided_data and isinstance(provided_data, list) and len(provided_data) > 0:
                from_apps = self._format_provided_data_by_source(provided_data)
                if from_apps and len(from_apps.strip()) > 600:
                    if _has_kb_content(provided_data):
                        # KB / search content: pass directly so all service names survive intact.
                        # _limit_context_tokens inside _enrich_context handles the final size cap.
                        pre_formatted = from_apps
                        logger.info("   🔍 KB content detected — skipping summarization, using raw %d chars", len(from_apps))
                    else:
                        pre_formatted = await self._summarize_context(from_apps)
                elif from_apps:
                    pre_formatted = from_apps
                logger.info(
                    "   🔍 _format_provided_data_by_source → %d chars | pre_formatted → %d chars",
                    len(from_apps or ""),
                    len(pre_formatted or ""),
                )
            enriched_context = self._enrich_context(
                user_query, provided_data, tool_args, pre_formatted_context=pre_formatted
            )
            logger.info("   📄 enriched_context built → %d chars", len(enriched_context))
            logger.info("   📄 enriched_context FULL CONTENT:\n%s", enriched_context)

            yield event(
                AgentEvent.THOUGHT,
                f"Document type: {classification.get('document_type', 'email')}, tone: {classification.get('tone_style', 'professional')}. Planning structure...",
            )

            strategy = await self._plan_strategy(user_query, classification, enriched_context)
            yield event(AgentEvent.PLAN, strategy if isinstance(strategy, str) else json.dumps(strategy, indent=2))

            sensitive_note = await self._sensitive_document_detection(user_query, classification)

            yield event(AgentEvent.THOUGHT, "Generating the draft...")
            draft = await self._generate_draft(
                user_query, classification, strategy, enriched_context, tool_args or {}, sensitive_note=sensitive_note
            )

            # If still empty (should not happen after fallback), use fallback from context so we never stop
            if not draft.get("body") and not draft.get("subject"):
                draft = await self._fallback_draft_from_context(user_query, tool_args or {}, classification)

            # Dynamically fill any remaining [placeholder] / {placeholder} tokens using LLM + context.
            # The LLM autonomously decides what value each placeholder should receive, drawing from
            # enriched_context (recipient names, sender identity, company/product info, workflow outputs).
            # This step is skipped entirely when no placeholder patterns exist (zero extra LLM cost).
            draft = await self._resolve_placeholders(draft, enriched_context, user_query)

            yield event(AgentEvent.THOUGHT, "Running quality check and optimization...")
            final = await self._self_review(
                draft,
                classification,
                source_text=source_text,
                profile=profile,
                tool_args=tool_args or {},
            )
            # Safety: strip any context block that leaked into the body (must not appear in the email)
            body = final.get("body") or ""
            original_body = body
            for leak_marker in (
                "Complete context from main agent",
                "Context From Previous Steps",  # new workflow format (capital F/S)
                "Context from previous steps",  # legacy lowercase
                "• From step",
                "--- Context gathered by main agent",
                "--- Answers from main agent",
            ):
                if leak_marker in body:
                    body = body[: body.find(leak_marker)].rstrip()
                    break  # one strip is enough; re-check below
            # Second pass in case multiple markers appeared
            for leak_marker in ("• From step", "--- Context"):
                if leak_marker in body:
                    body = body[: body.find(leak_marker)].rstrip()
                    break
            # If stripping gutted the body, keep the original and let it show — it's
            # better to show a slightly polluted body than a completely empty one.
            if len(body.strip()) < 30 and len(original_body.strip()) > 30:
                body = original_body
            if body != (final.get("body") or ""):
                final = {**final, "body": body}

            output_mode = (tool_args or {}).get("output_mode") or "draft"
            draft_source = str(draft.get("draft_source") or "llm")
            result = {
                "success": True,
                "response": f"Subject: {final.get('subject', '')}\n\n{final.get('body', '')}",
                "subject": final.get("subject", ""),
                "body": final.get("body", ""),
                "classification": classification,
                "output_mode": output_mode,
                "document_type": classification.get("document_type", "email"),
                "draft_source": draft_source,
            }
            if (tool_args or {}).get("variations"):
                # Optional: generate short and long versions
                short_prompt = (
                    f"Rewrite this in 2-3 short sentences (same meaning, concise).\n\n{final.get('body', '')[:800]}"
                )
                long_prompt = (
                    f"Expand this professionally into a fuller paragraph (same tone).\n\n{final.get('body', '')[:800]}"
                )
                short_body = await self._llm_generate(short_prompt, max_tokens=200)
                long_body = await self._llm_generate(long_prompt, max_tokens=400)
                result["variations"] = [
                    {"label": "Short", "body": (short_body or final.get("body", ""))[:500]},
                    {"label": "Long", "body": (long_body or final.get("body", ""))[:1500]},
                ]

            yield event(AgentEvent.RESULT, result)
            yield event(
                AgentEvent.FINAL,
                {
                    "success": True,
                    "response": result["response"],
                    "result": result,
                },
            )
        except Exception as e:
            logger.exception("AI Writer agent failed")
            yield event(AgentEvent.ERROR, {"error": str(e)})
            yield event(AgentEvent.FINAL, {"success": False, "error": str(e), "response": None})

    async def initialize(self) -> None:
        logger.debug("AI Writer agent initialized")

    async def cleanup(self) -> None:
        logger.debug("AI Writer agent cleaned up")


__all__ = ["AIWriterAgentTool"]

