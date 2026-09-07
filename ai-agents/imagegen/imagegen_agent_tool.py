"""
ImageGenAgentTool -- generate images from text prompts.

Delegates image generation to whatever llm_provider is injected by the
entrypoint (OpenAIProvider or OpenRouterProvider, pre-configured with the
correct API key).  No provider-specific logic lives here -- fully
provider-agnostic.

Prompt generation pipeline (real multi-call agentic design, kept deliberately
small so a single image request stays fast). It is deliberately generic --
nothing in it is hardcoded to a specific deliverable type (poster, banner,
social post, ad, thumbnail, invitation, packaging, etc.). Every judgement
call (what facts this design needs, what style fits, what canvas size is
conventional, whether it carries on-image text at all) is made dynamically
by the LLM per-request, the way a senior graphic designer would scope any
job -- not from a fixed checklist or lookup table baked into this file:

  1. Planner Agent       -- intent + requirement extraction + missing-field
                             check, reasoned fresh for whatever deliverable
                             this request actually is. NEVER invents
                             client-identity facts (shop name, phone, logo,
                             dates, prices). The pipeline never stops to ask
                             the user, though -- anything missing is simply
                             passed downstream as "not supplied" and the
                             Design Spec Agent omits that element from the
                             design rather than fabricating it.
  2. Design Spec Agent    -- one call that turns the planner's facts into a
                             structured Design Specification: literal
                             on-image copy, creative direction, layout,
                             typography, colors, assets, effects/decoration,
                             and canvas sizing -- all chosen using the
                             model's own design knowledge for this specific
                             brief, not a style library or platform/size
                             table in code. Brand-specific fields are only
                             requested when the planner flagged this as a
                             branded/business design.
  3. Prompt Generator Agent -- converts the neutral spec into a prompt
                             formatted for whichever image model is
                             configured, using the model's own knowledge of
                             that model family's prompting conventions.
                             Every literal copy string from the spec must
                             show up verbatim, plus a negative prompt.
  4. Validator (local, no LLM call) -- schema check against the structured
                             spec (not keyword-matching the final prose).
                             One retry of step 2 if required fields are
                             missing.
  5. Image Generation     -- unchanged, delegates to the injected provider.
                             Canvas size is snapped to the nearest API-valid
                             size purely by aspect-ratio math against
                             whatever dimensions the Design Spec Agent chose
                             -- no keyword/word-list guessing.
  6. Vision QA + single revision -- the generated image is shown to a
                             vision-capable model, scored against the spec
                             (including literal copy accuracy), and
                             regenerated once if it fails QA.

This is ~3-5 LLM calls end to end (plus at most one extra for a QA
revision), not 17+.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
from typing import Any, AsyncGenerator, Dict, List, Optional

from backend.services.ai_agents.base_tool_agent import BaseToolAgent

logger = logging.getLogger(__name__)

# The only two size enums OpenAI's image API actually accepts per model family --
# this is a hard technical constraint of the provider's API surface, not a design
# decision, so it's the one place a fixed set is unavoidable.
_GPT_IMAGE_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
_DALLE_SIZES = {"1024x1024", "1792x1024", "1024x1792"}

_DIM_RE = re.compile(r"\b(\d{2,5})\s*[xX×]\s*(\d{2,5})\b")


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of a JSON object from an LLM response."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _nearest_valid_size(dimension_text: str, model: str) -> Optional[str]:
    """
    Snap an arbitrary WxH (chosen dynamically by the Design Spec Agent for
    whatever platform/use-case it was given -- not a fixed lookup) to the
    nearest size the provider's API actually accepts, purely by comparing
    aspect ratios mathematically. Generic for any canvas the model proposes;
    no keyword/word-list guessing involved. Returns None if no dimensions
    could be parsed out of dimension_text at all.
    """
    m = _DIM_RE.search(dimension_text or "")
    if not m:
        return None
    width, height = float(m.group(1)), float(m.group(2))
    if width <= 0 or height <= 0:
        return None
    target_ratio = math.log(width / height)

    is_dalle = "dall-e" in (model or "").lower()
    valid_sizes = _DALLE_SIZES if is_dalle else _GPT_IMAGE_SIZES

    best_candidate: Optional[str] = None
    best_diff = float("inf")
    for candidate in valid_sizes:
        cm = _DIM_RE.match(candidate)
        if not cm:
            continue
        cw, ch = float(cm.group(1)), float(cm.group(2))
        diff = abs(target_ratio - math.log(cw / ch))
        if diff < best_diff:
            best_diff = diff
            best_candidate = candidate

    return best_candidate


# ---------------------------------------------------------------------------
# Stage 1 -- Planner Agent (Intent Detection + Requirement Extraction +
# Missing Info Detection, combined into one call). Never invents
# client-identity facts.
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM = (
    "You are the Planner Agent in an AI design pipeline built to handle ANY kind of "
    "visual deliverable a senior graphic designer might be asked for -- poster, banner, "
    "social media post, ad, thumbnail, invitation, flyer, packaging, app screen, "
    "wallpaper, concept art, or anything else not listed here. Never force the request "
    "into a fixed category; reason about THIS specific brief the way an experienced "
    "designer would. In one pass you must: "
    "(1) detect the design_type, platform, and goal of the request, described in "
    "whatever terms fit the request -- there is no predefined enum to match against; "
    "(2) extract every concrete fact already available -- from the user's own words AND "
    "from any \"DATA FROM PRIOR STEPS\" block (e.g. research/lookup results gathered "
    "earlier in this conversation) -- into extracted, keyed however makes sense for this "
    "particular request. Treat real facts found in prior-step data exactly like "
    "user-supplied facts (names, numbers, dates, contact info, products, offers, brand "
    "details, etc.) -- never re-derive or guess something that's already sitting in that "
    "data; "
    "(3) decide whether this is a BRANDED BUSINESS design (it represents a business or "
    "brand and its real identity facts materially affect whether the design works) "
    "versus a purely personal/creative design (it doesn't need any) -- judge this "
    "per-request from the content of the brief itself, not from a lookup table; "
    "(4) using your own expert design judgement -- as if you were the senior designer "
    "scoping this exact job -- decide which real-world facts this specific design "
    "genuinely needs to be effective and complete (e.g. an offer needs the offer terms "
    "and how to act on it; an event invite needs who/what/when/where; a product ad "
    "needs to know the product; a wallpaper needs none of this), then list whichever of "
    "those the user has NOT supplied for real in missing_required; "
    "(5) decide needs_on_image_text: whether a design of this kind conventionally has "
    "words rendered on it at all -- true for the great majority of communication/"
    "marketing visuals, false only for genuinely wordless pieces (an abstract "
    "wallpaper, pure concept art with no copy) -- this is your judgement call for this "
    "request, not a fixed list of types. "
    "\n\nCOLOR PALETTE IS NEVER A missing_required ITEM. Whether the user names "
    "specific colors or not, color choice is always the Design Spec Agent's job "
    "downstream -- if the user named colors (even informally, e.g. 'gold and "
    "soft pink', not hex codes), extract them into extracted.colors; if not, "
    "just leave that field out of extracted and let the next agent pick a "
    "fitting palette. Never ask the user for colors. "
    "\n\nPLACEHOLDER DETECTION (apply consistently to every field, not just some): "
    "treat bracketed text like [Complete Address Here], values like 'XXX', "
    "'TBD', 'N/A', repeated X's in a phone number, or obvious example.com-style "
    "addresses as NOT actually supplied -- list that field in missing_required "
    "even though the user typed something in that slot, and do not put the "
    "placeholder text in extracted. Apply this check to EVERY field (phone, "
    "address, email, website, etc.) -- do not catch some placeholders and miss "
    "others. "
    "\n\nCRITICAL RULE: never invent, guess, or fabricate a business-identity "
    "fact (shop name, phone number, address, discount amount, prices, dates, "
    "website, logo). If the user didn't give a real (non-placeholder) value and "
    "the design needs it, put it in missing_required and leave it out of "
    "extracted. Purely creative/stylistic choices (mood, layout, decorative "
    "motifs) are NOT missing_required items -- those are the Design Spec "
    "Agent's job to choose. "
    "\n\nReturn ONLY a single valid JSON object, no markdown fences, no commentary:\n"
    '{"design_type": str, "platform": str, "goal": str, '
    '"is_branded_business_design": bool, '
    '"needs_on_image_text": bool, '
    '"extracted": {"<fact_name>": "<value>", ...}, '
    '"missing_required": [str, ...]}'
)


def _build_request_context(
    step_query: str,
    user_input: Optional[str],
    agent_context: Optional[str],
    tool_args: Dict[str, Any],
    provided_data: Optional[Any] = None,
) -> str:
    user_input_block = ""
    if user_input and user_input.strip() and user_input.strip() != (step_query or "").strip():
        user_input_block = f"FULL USER REQUEST:\n{user_input.strip()}\n\n"

    tool_args_block = ""
    _skip_keys = {"session_id", "tool_args", "provided_data", "agent_context",
                  "agent_id", "token", "company_id", "user_id"}
    _extra_parts = []
    for k, v in tool_args.items():
        if k in _skip_keys:
            continue
        if isinstance(v, str) and len(v) > 20:
            _extra_parts.append(f"{k}: {v}")
        elif isinstance(v, dict):
            for kk, vv in v.items():
                if isinstance(vv, str) and len(vv) > 20 and kk not in _skip_keys:
                    _extra_parts.append(f"{kk}: {vv}")
    if _extra_parts:
        tool_args_block = "ADDITIONAL PARAMETERS:\n" + "\n".join(_extra_parts) + "\n\n"

    agent_context_block = ""
    if agent_context and agent_context.strip():
        _ctx = agent_context.strip()
        if len(_ctx) > 2000:
            _ctx = _ctx[:2000] + "…"
        agent_context_block = f"EXECUTION CONTEXT (prior steps / identity):\n{_ctx}\n\n"

    # Facts the orchestrator gathered in earlier steps (e.g. a web search result
    # with the real business's contact info, branding, etc.) and explicitly
    # projected for this exact tool call. This is real researched data, not
    # the user's own words -- treat anything concrete in here (names, colors,
    # contact details, URLs, descriptions) the same as user-supplied facts;
    # never ignore it just because it didn't come from the prompt text.
    provided_data_block = ""
    if provided_data:
        try:
            _pd_text = json.dumps(provided_data, ensure_ascii=False, default=str)
        except Exception:
            _pd_text = str(provided_data)
        if len(_pd_text) > 4000:
            _pd_text = _pd_text[:4000] + "…"
        if _pd_text.strip() and _pd_text.strip() not in ("{}", "[]", "null"):
            provided_data_block = f"DATA FROM PRIOR STEPS (use any relevant facts in here):\n{_pd_text}\n\n"

    return (
        f"GOAL FOR THIS IMAGE:\n{step_query or '(generate an image)'}\n\n"
        f"{user_input_block}{tool_args_block}{provided_data_block}{agent_context_block}"
    )


# ---------------------------------------------------------------------------
# Stage 2 -- Design Spec Agent: Creative Director + Layout + Typography +
# Color Harmony + Asset/Background/Subject planning + Effects/Decoration +
# canvas/platform sizing, all in one call producing one neutral JSON spec.
# ---------------------------------------------------------------------------

_DESIGN_SPEC_SYSTEM = (
    "You are the Design Spec Agent in an AI design pipeline built to handle ANY kind "
    "of visual deliverable -- poster, banner, social post, ad, thumbnail, invitation, "
    "flyer, packaging, app screen, wallpaper, concept art, or anything else. You "
    "receive the Planner Agent's output (design_type, platform, goal, extracted facts, "
    "is_branded_business_design, needs_on_image_text) and must produce ONE structured, "
    "neutral Design Specification covering literal on-image copy, creative direction, "
    "layout, typography, color, assets, and finishing effects -- a model-agnostic "
    "spec, not prose. Make every choice the way a senior graphic designer would for "
    "THIS specific brief, not from any fixed list or template.\n\n"
    "COPY IS THE MOST IMPORTANT PART OF THIS SPEC. The \"copy\" object holds "
    "the EXACT words that must appear rendered on the image. Build it directly "
    "from the Planner's extracted facts -- NEVER paraphrase away or drop a "
    "concrete number, percentage, price, date, or count (e.g. a '50% off' "
    "discount or 'first 1000 customers' limit MUST appear verbatim in "
    "copy.headline_text or copy.offer_text, not summarised as 'a discount' or "
    "omitted). copy.headline_text is required whenever the Planner's "
    "needs_on_image_text is true. Leave any other copy field out entirely when "
    "that fact wasn't provided -- do not invent filler text.\n\n"
    "Choose an overall creative style and direction that authentically fits this "
    "design_type, goal, audience, and mood -- draw on the full breadth of "
    "professional graphic design practice rather than picking from any fixed list "
    "of style names; decide what a senior designer would actually choose for this "
    "exact brief.\n\n"
    "Only populate the \"brand\" object when is_branded_business_design is true "
    "AND the planner extracted real brand facts -- otherwise omit it entirely "
    "(do not invent a brand for a birthday card or wedding invite).\n\n"
    "The Planner's missing_required list tells you which business-identity "
    "facts (logo, contact number, website, etc.) the user did NOT supply. "
    "NEVER invent a value for those -- instead simply leave that element out "
    "of the design altogether (no logo placeholder, no fake phone number, no "
    "blank contact block). The design must still be complete and polished "
    "using only what was actually provided; missing extras are an omission, "
    "not a blocker.\n\n"
    "Size the canvas using your own knowledge of standard, industry-conventional "
    "dimensions for the stated platform or use-case -- social posts, stories, "
    "covers, thumbnails, print formats, signage, etc. each have real-world "
    "conventions; reason this out per-request from your own design knowledge "
    "rather than any fixed table. If the platform is genuinely unclear, choose "
    "whichever standard size best fits the described use-case. Express canvas.size "
    "as literal pixel dimensions, e.g. \"1080x1080\".\n\n"
    "Return ONLY a single valid JSON object, no markdown fences, no commentary:\n"
    '{"copy": {"headline_text": str, "subheadline_text": str, "offer_text": str, '
    '"cta_text": str, "footer_text": str} '
    '(omit any key whose fact was not supplied -- never invent filler copy), '
    '"creative": {"theme": str, "mood": str, "style": str, "emotion": str}, '
    '"canvas": {"size": str, "platform": str}, '
    '"layout": {"grid": str, "hierarchy": [str, ...], "margins": str}, '
    '"typography": {"headline": {"font": str, "size": str}, "body": {"font": str}}, '
    '"colors": {"primary": str, "secondary": str, "accent": str}, '
    '"assets": {"background": str, "subjects": [{"type": str, "position": str}, ...], '
    '"icons": [str, ...]}, '
    '"style_effects": {"effects": [str, ...], "decorations": [str, ...]}, '
    '"brand": {"shop_name": str, "logo_placement": str, "brand_colors": [str, ...]} '
    '(omit this key entirely when not a branded business design)}'
)

_SPEC_REQUIRED_KEYS = ("creative", "canvas", "layout", "typography", "colors", "assets")


def _validate_design_spec(spec: Dict[str, Any], expects_copy: bool = True) -> List[str]:
    """Agent (Validator) -- schema check against the structured spec, not the prose prompt."""
    missing = []
    if expects_copy and not (spec.get("copy") or {}).get("headline_text"):
        missing.append("copy.headline_text")
    for key in _SPEC_REQUIRED_KEYS:
        if not spec.get(key):
            missing.append(key)
    assets = spec.get("assets") or {}
    if not assets.get("background"):
        missing.append("assets.background")
    if not assets.get("subjects"):
        missing.append("assets.subjects")
    typography = spec.get("typography") or {}
    if not typography.get("headline"):
        missing.append("typography.headline")
    return missing


# ---------------------------------------------------------------------------
# Stage 3 -- Prompt Generator Agent: neutral spec -> model-specific prompt.
# ---------------------------------------------------------------------------

_PROMPT_GENERATOR_SYSTEM = (
    "You are the Prompt Generator Agent in an AI design pipeline. You receive a "
    "neutral, structured Design Specification and the target image model name. "
    "\n\nCRITICAL: if the spec has a \"copy\" object, every text value in it "
    "(headline_text, subheadline_text, offer_text, cta_text, footer_text) MUST "
    "appear in the prompt as an exact, quoted instruction to render that exact "
    "text on the image -- verbatim, not summarised, not reworded, not dropped. "
    "A discount, price, date, or customer-count figure in copy is the single "
    "most important content in the whole image; losing it makes the output "
    "wrong even if everything else is perfect. Spell out explicitly which text "
    "goes in which position (e.g. 'large bold headline text reading exactly "
    "\"50% OFF\"').\n\n"
    "Format the prompt the way the named target model actually performs best with, "
    "using your own knowledge of that model's prompting conventions. As general "
    "starting points (override with whatever you actually know about the specific "
    "named model -- this is guidance, not a rule to apply blindly):\n"
    "- GPT Image / DALL-E-style models: a single rich, detailed natural-language "
    "paragraph covering canvas size, background, headline/typography, subjects, "
    "logo/CTA placement, style, lighting, effects, and rendering quality.\n"
    "- FLUX-style models: a scene-focused natural description, less parameter-listy, "
    "more visually narrative.\n"
    "- SDXL / Stable Diffusion-style models: a comma-separated tag-style positive "
    "prompt plus a strong separate negative prompt.\n"
    "- Midjourney-style models: a compact prompt with trailing parameters like "
    "--ar <ratio> --stylize <n> matching the canvas aspect ratio.\n"
    "If the model name doesn't clearly match any known family, default to the GPT "
    "Image / DALL-E style.\n\n"
    "Always also produce a negative prompt listing relevant exclusions (low "
    "quality, blurry, distorted, cropped, bad typography, duplicate objects, "
    "pixelated, watermark, logo distortion, poor alignment, bad anatomy, noise, "
    "oversaturated, extra hands, incorrect perspective -- trimmed to what's "
    "relevant).\n\n"
    'Return ONLY a single valid JSON object: {"master_prompt": str, '
    '"negative_prompt": str} -- no markdown fences, no commentary.'
)


# ---------------------------------------------------------------------------
# Vision QA Agent (after image generation) + single revision pass.
# ---------------------------------------------------------------------------

_VISION_QA_PROMPT_TMPL = """\
You are the Design Reviewer Agent. Look at the generated image and judge it \
against the design brief below.

DESIGN BRIEF (what the image should contain):
{spec_json}

Score it 0-100 on: text ACCURACY (does the rendered text match the brief's \
"copy" fields verbatim, e.g. an exact discount/offer/number -- this is the \
single biggest factor, weight it heavily and fail the image if copy text is \
missing, wrong, or garbled), text visibility, color harmony, alignment, \
cropping, contrast, branding accuracy, and readability. status is "PASS" \
when score >= 85.

If status is "FAIL", write concrete, actionable revision instructions \
(e.g. "Increase headline size by 15%, move logo upward, improve text contrast").

Return ONLY a single valid JSON object, no markdown fences, no commentary:
{{"score": int, "status": "PASS" or "FAIL", "issues": [str, ...], "revision_prompt": str}}
"""

_QA_PASS_THRESHOLD = 85


class ImageGenAgentTool(BaseToolAgent):
    """
    Generates images from natural-language prompts.

    Config (from app_config):
      image_model  -- model identifier forwarded to the image provider
      size         -- image dimensions (default "auto")
      quality      -- quality hint (default "standard", dall-e-3 only)
    """

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return (
            "Generate images from text prompts using AI image models. "
            "Use when the user asks to create, draw, illustrate, or generate any image or visual."
        )

    def __init__(
        self,
        llm_provider: Any,
        app_config: Optional[Dict[str, Any]] = None,
        agent_id: str = "",
        token: str = "",
        company_id: Optional[str] = None,
        user_id: Optional[int] = None,
        chat_llm: Optional[Any] = None,
    ) -> None:
        self._llm = llm_provider          # image generation provider
        self._chat_llm = chat_llm         # text LLM for prompt synthesis (may be None)
        self._app_config = app_config or {}
        self._agent_id = agent_id
        self._token = token
        self._company_id = company_id
        self._user_id = user_id

        self._image_model: Optional[str] = self._app_config.get("image_model") or None
        self._size: str = str(self._app_config.get("size") or "auto")
        self._quality: str = str(self._app_config.get("quality") or "standard")

    async def initialize(self) -> None:
        logger.info(
            "ImageGenAgentTool ready: model=%s size=%s quality=%s chat_llm=%s",
            self._image_model or "(provider default)",
            self._size,
            self._quality,
            type(self._chat_llm).__name__ if self._chat_llm else "none",
        )

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        conversation_history: Optional[Any] = None,
        system_prompt: Optional[str] = None,
        agent_guidance: Optional[Any] = None,
        user_input: Optional[str] = None,
        agent_context: Optional[str] = None,
        **tool_args: Any,
    ) -> AsyncGenerator[Dict, None]:
        async for event in self._run(
            user_query=user_query,
            session_id=session_id,
            user_input=user_input,
            agent_context=agent_context,
            provided_data=provided_data,
            **tool_args,
        ):
            yield event

    # ------------------------------------------------------------------
    # Pipeline agents
    # ------------------------------------------------------------------

    async def _call_json_agent(
        self,
        system_prompt: str,
        user_msg: str,
        max_tokens: int,
        label: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            raw = await self._chat_llm.get_response(
                transcript=user_msg,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=0.3,
            )
            parsed = _extract_json_object(raw or "")
            if parsed is not None:
                return parsed
            logger.warning("%s: returned non-JSON output", label)
        except Exception as exc:
            logger.warning("%s: failed: %s", label, exc)
        return None

    async def _run_planner(self, context_block: str) -> Optional[Dict[str, Any]]:
        """Agent 1+2+3 combined -- Intent Detection + Requirement Extraction + Missing Info Detection."""
        return await self._call_json_agent(
            _PLANNER_SYSTEM, context_block, max_tokens=500, label="Planner Agent",
        )

    async def _run_design_spec(
        self,
        context_block: str,
        plan: Dict[str, Any],
        revision_hint: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Creative Director + Layout + Typography + Color + Assets + Effects/Decoration, combined."""
        user_msg = (
            f"{context_block}"
            f"PLANNER OUTPUT:\n{json.dumps(plan, ensure_ascii=False)}\n\n"
            f"{revision_hint}"
            f"Produce the Design Specification now."
        )
        return await self._call_json_agent(
            _DESIGN_SPEC_SYSTEM, user_msg, max_tokens=700, label="Design Spec Agent",
        )

    async def _run_prompt_generator(
        self,
        spec: Dict[str, Any],
        image_model: str,
    ) -> Optional[Dict[str, str]]:
        user_msg = (
            f"TARGET IMAGE MODEL: {image_model or '(unspecified -- assume GPT Image style)'}\n\n"
            f"DESIGN SPECIFICATION:\n{json.dumps(spec, ensure_ascii=False)}\n\n"
            f"Generate the formatted prompt and negative prompt now."
        )
        result = await self._call_json_agent(
            _PROMPT_GENERATOR_SYSTEM, user_msg, max_tokens=700, label="Prompt Generator Agent",
        )
        if not result or not result.get("master_prompt"):
            return None
        return {
            "master_prompt": str(result.get("master_prompt") or "").strip(),
            "negative_prompt": str(result.get("negative_prompt") or "").strip(),
        }

    async def _build_prompt_pipeline(
        self,
        step_query: str,
        user_input: Optional[str],
        agent_context: Optional[str],
        tool_args: Dict[str, Any],
        provided_data: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Runs Planner -> Design Spec (with one schema-validated retry) ->
        Prompt Generator. Never stops to ask the user: any business-identity
        fact the Planner couldn't find (logo, contact, website, etc.) is
        passed through to the Design Spec Agent as known-missing, which
        omits that element from the design entirely rather than inventing
        a fake value -- the pipeline always completes in one pass.

        Returns a dict with one of:
          {"master_prompt": str, "negative_prompt": str, "spec": dict}
          {"master_prompt": str (fallback raw text)}          -- pipeline unavailable/failed
        """
        fallback_text = (user_input or step_query or "").strip()
        if not self._chat_llm:
            return {"master_prompt": fallback_text}

        context_block = _build_request_context(
            step_query, user_input, agent_context, tool_args, provided_data,
        )

        plan = await self._run_planner(context_block)
        if not plan:
            return {"master_prompt": fallback_text}

        # Planner makes this judgement call per-request (not a static lookup);
        # default true since most communication/marketing visuals carry text.
        expects_copy = bool(plan.get("needs_on_image_text", True))

        spec = await self._run_design_spec(context_block, plan)
        if not spec:
            return {"master_prompt": fallback_text}

        missing_keys = _validate_design_spec(spec, expects_copy)
        if missing_keys:
            logger.info("ImageGenAgentTool: Design Spec missing %s -- retrying once", missing_keys)
            revision_hint = f"PREVIOUS ATTEMPT WAS MISSING: {', '.join(missing_keys)}. Cover these explicitly.\n\n"
            retried = await self._run_design_spec(context_block, plan, revision_hint)
            if retried and not _validate_design_spec(retried, expects_copy):
                spec = retried

        generated = await self._run_prompt_generator(spec, self._image_model or "")
        if not generated:
            return {"master_prompt": fallback_text}

        return {
            "master_prompt": generated["master_prompt"],
            "negative_prompt": generated["negative_prompt"],
            "spec": spec,
        }

    async def _run_vision_qa(
        self,
        spec: Optional[Dict[str, Any]],
        image_b64: Optional[str],
        image_url: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Design Reviewer Agent: scores the generated image, returns None if QA is unavailable."""
        vision_caller = self._chat_llm or self._llm
        if not vision_caller or not (image_b64 or image_url):
            return None

        qa_prompt = _VISION_QA_PROMPT_TMPL.format(
            spec_json=json.dumps(spec or {}, ensure_ascii=False),
        )
        try:
            raw = await vision_caller.analyze_image(
                image_b64=image_b64,
                image_url=image_url,
                prompt=qa_prompt,
                max_tokens=400,
            )
        except NotImplementedError:
            logger.info("ImageGenAgentTool: vision QA not supported by this provider -- skipping")
            return None
        except Exception as exc:
            logger.warning("ImageGenAgentTool: vision QA call failed: %s", exc)
            return None

        return _extract_json_object(raw or "")

    # ------------------------------------------------------------------
    # Upload helper
    # ------------------------------------------------------------------

    async def _upload_image(
        self,
        b64_data: str,
        prompt: str,
        session_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Upload a base64-encoded PNG to the platform files API."""
        import httpx

        company_url = (os.getenv("COMPANY_URL") or "").rstrip("/")
        if not company_url:
            logger.warning("ImageGenAgentTool: COMPANY_URL not set — skipping file upload")
            return None
        if not self._token:
            logger.warning("ImageGenAgentTool: no token available — skipping file upload")
            return None

        try:
            image_bytes = base64.b64decode(b64_data)
        except Exception as exc:
            logger.error("ImageGenAgentTool: failed to decode b64 image: %s", exc)
            return None

        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", prompt[:40]).strip("_") or "generated"
        filename = f"{safe_name}.png"

        fields: Dict[str, Any] = {
            "agentId": str(self._agent_id or ""),
            "title": prompt[:120],
            "description": f"AI-generated image using model {self._image_model or 'unknown'}",
        }
        if session_id:
            fields["sessionId"] = str(session_id)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{company_url}/aiagentchat/files",
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "companyids": str(self._company_id or ""),
                    },
                    files={"file": (filename, image_bytes, "image/png")},
                    data=fields,
                )
                resp.raise_for_status()
                body = resp.json()
        except Exception as exc:
            logger.error("ImageGenAgentTool: file upload failed: %s", exc)
            return None

        file_data = (body or {}).get("data")
        if not isinstance(file_data, dict):
            logger.warning("ImageGenAgentTool: unexpected upload response: %s", body)
            return None

        logger.info(
            "ImageGenAgentTool: uploaded image file _id=%s fileName=%s",
            file_data.get("_id"), file_data.get("fileName"),
        )
        return file_data

    async def _generate(
        self,
        prompt: str,
        spec: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Single call to the image provider. Size comes from the Design Spec
        Agent's own canvas.size choice (dynamically reasoned per request),
        snapped to the nearest size the provider's API accepts; falls back
        to parsing dimensions out of the prompt text itself, then to config.
        """
        _model_lower = (self._image_model or "").lower()
        canvas_size = ((spec or {}).get("canvas") or {}).get("size") or ""
        _size = (
            _nearest_valid_size(canvas_size, _model_lower)
            or _nearest_valid_size(prompt, _model_lower)
            or self._size
        )
        # quality is an OpenAI API parameter accepted only by dall-e-3 -- a
        # technical capability difference between model families, not a
        # design decision.
        _quality = self._quality if "dall-e" in _model_lower else None
        return await self._llm.generate_image(
            prompt=prompt,
            model=self._image_model,
            size=_size,
            quality=_quality,
        )

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    async def _run(
        self,
        user_query: str,
        session_id: Optional[str] = None,
        user_input: Optional[str] = None,
        agent_context: Optional[str] = None,
        provided_data: Optional[Any] = None,
        **tool_args: Any,
    ) -> AsyncGenerator[Dict, None]:
        step_query = (user_query or "").strip()
        if not step_query and not user_input:
            yield {
                "type": "error",
                "content": {"error": "No prompt provided for image generation."},
            }
            return

        yield {
            "type": "AGENT_REASONING",
            "content": "Planning the design (intent, requirements, available details)…",
        }

        pipeline_result = await self._build_prompt_pipeline(
            step_query=step_query,
            user_input=user_input,
            agent_context=agent_context,
            provided_data=provided_data,
            tool_args=tool_args,
        )

        prompt = pipeline_result.get("master_prompt") or step_query or user_input or ""
        negative_prompt = pipeline_result.get("negative_prompt") or ""
        spec = pipeline_result.get("spec")

        if not prompt:
            yield {
                "type": "error",
                "content": {"error": "No prompt provided for image generation."},
            }
            return

        logger.info(
            "ImageGenAgentTool: final prompt passed to image generator (model=%s): %s",
            self._image_model or "(provider default)", prompt,
        )
        if negative_prompt:
            logger.info("ImageGenAgentTool: negative prompt (not sent to provider): %s", negative_prompt)

        yield {
            "type": "AGENT_REASONING",
            "content": f"Generating image: {prompt[:120]}{'…' if len(prompt) > 120 else ''}",
        }

        try:
            result = await self._generate(prompt, spec)
        except NotImplementedError:
            yield {
                "type": "error",
                "content": {
                    "error": (
                        "The configured AI service does not support image generation. "
                        "Please connect an OpenAI or OpenRouter service."
                    )
                },
            }
            return
        except Exception as exc:
            logger.error("ImageGenAgentTool: generate_image raised: %s", exc)
            yield {
                "type": "error",
                "content": {"error": f"Image generation failed: {exc}"},
            }
            return

        if not result:
            yield {
                "type": "error",
                "content": {"error": "Image generation returned no result. Check your API key and model name."},
            }
            return

        image_url = result.get("url")
        b64 = result.get("b64_json")
        revised = result.get("revised_prompt") or ""

        if not image_url and not b64:
            yield {
                "type": "error",
                "content": {"error": "Unexpected response format from image provider."},
            }
            return

        # -- Vision QA + single revision pass --------------------------------
        if spec is not None:
            qa = await self._run_vision_qa(spec, b64, image_url)
            if qa and str(qa.get("status", "")).upper() == "FAIL" and qa.get("revision_prompt"):
                score = qa.get("score")
                logger.info("ImageGenAgentTool: vision QA failed (score=%s) -- revising once", score)
                yield {
                    "type": "AGENT_REASONING",
                    "content": f"Design review found issues (score {score}) — revising once…",
                }
                revised_prompt = f"{prompt}\n\nREVISION INSTRUCTIONS: {qa['revision_prompt']}"
                logger.info(
                    "ImageGenAgentTool: revised prompt passed to image generator: %s", revised_prompt,
                )
                try:
                    revised_result = await self._generate(revised_prompt, spec)
                except Exception as exc:
                    logger.warning("ImageGenAgentTool: revision generation failed: %s", exc)
                    revised_result = None
                if revised_result and (revised_result.get("url") or revised_result.get("b64_json")):
                    result = revised_result
                    image_url = result.get("url")
                    b64 = result.get("b64_json")
                    revised = result.get("revised_prompt") or revised

        file_meta: Optional[Dict[str, Any]] = None
        if b64:
            yield {
                "type": "AGENT_REASONING",
                "content": "Uploading generated image…",
            }
            file_meta = await self._upload_image(b64, prompt, session_id)

        if file_meta:
            placeholder_data = json.dumps({
                "_id": file_meta.get("_id", ""),
                "fileName": file_meta.get("fileName", ""),
                "mimeType": file_meta.get("mimeType", "image/png"),
                "size": file_meta.get("size", 0),
            })
            generated_output = f"[[FILE_PLACEHOLDER {placeholder_data}]]"
            if revised and revised != prompt:
                generated_output += f"\n\n*Revised prompt: {revised}*"
            result_payload = {
                "generated_output": generated_output,
                "file_ref_for_internal_use_only": {
                    "_id": file_meta.get("_id", ""),
                    "mimeType": file_meta.get("mimeType", "image/png"),
                },
            }
            yield {
                "type": "TOOL_RESULT",
                "content": result_payload,
            }
            yield {
                "type": "FINAL",
                "content": result_payload,
            }
        else:
            display_url = image_url or f"data:image/png;base64,{b64}"
            message = f"Here is the generated image:\n\n![Generated image]({display_url})"
            if revised and revised != prompt:
                message += f"\n\n*Revised prompt: {revised}*"
            yield {
                "type": "TOOL_RESULT",
                "content": {
                    "text": message,
                    "image_url": image_url,
                    "revised_prompt": revised,
                    "model": self._image_model,
                },
            }
            yield {
                "type": "FINAL",
                "content": {"text": message},
            }
