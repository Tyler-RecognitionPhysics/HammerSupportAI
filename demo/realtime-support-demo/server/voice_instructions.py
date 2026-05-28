"""Voice agent instructions for SIP/phone (ported from web/src pen + hammer prompts)."""

from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _web_src_dir() -> Path:
    env_root = os.environ.get("REALTIME_SALES_REPO_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root) / "demo" / "realtime-sales-demo" / "web" / "src"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent / "web" / "src"


_WEB_SRC = _web_src_dir()


def _extract_ts_backtick_const(path: Path, const_name: str) -> str:
    text = path.read_text(encoding="utf-8")
    marker = f"export const {const_name} = `"
    start = text.find(marker)
    if start < 0:
        raise ValueError(f"{const_name} not found in {path}")
    start += len(marker)
    end = text.find("`;", start)
    if end < 0:
        raise ValueError(f"unterminated template for {const_name} in {path}")
    return text[start:end]


@dataclass(frozen=True)
class PenOpenerBase:
    angle: str
    question: str


@dataclass(frozen=True)
class PenOpening:
    angle: str
    question: str
    greeting: str
    bridge: str


def _pen_challenge_ts_text() -> str:
    return (_WEB_SRC / "pen-challenge-instructions.ts").read_text(encoding="utf-8")


def _parse_ts_string_array(const_name: str) -> tuple[str, ...]:
    text = _pen_challenge_ts_text()
    block_m = re.search(
        rf"export const {const_name}\s*=\s*\[(.*?)\]\s*as const",
        text,
        re.DOTALL,
    )
    if not block_m:
        return ()
    return tuple(re.findall(r'"([^"]+)"', block_m.group(1)))


@lru_cache(maxsize=1)
def _cached_pen_opener_bases() -> tuple[PenOpenerBase, ...]:
    text = _pen_challenge_ts_text()
    block_m = re.search(
        r"export const PEN_CHALLENGE_OPENERS.*?=\s*\[(.*?)\]\s*as const",
        text,
        re.DOTALL,
    )
    if not block_m:
        return (
            PenOpenerBase(
                "failure",
                "When was the last time a pen let you down in front of someone who was watching?",
            ),
        )
    block = block_m.group(1)
    pairs = re.findall(r'angle:\s*"([^"]+)".*?question:\s*"([^"]+)"', block, re.DOTALL)
    return tuple(PenOpenerBase(a, q) for a, q in pairs)


@lru_cache(maxsize=1)
def _cached_pen_greetings() -> tuple[str, ...]:
    items = _parse_ts_string_array("PEN_CHALLENGE_GREETINGS")
    if items:
        return items
    return (
        "Hey, it's Hannah with Hammer AI — just saw your lead come in for a pen, so I figured I'd give you a call!",
    )


@lru_cache(maxsize=1)
def _cached_pen_bridges() -> tuple[str, ...]:
    items = _parse_ts_string_array("PEN_CHALLENGE_BRIDGES")
    if items:
        return items
    return ("How are you doing today?",)


def pick_pen_opening() -> PenOpening:
    bases = _cached_pen_opener_bases()
    base = random.choice(bases) if bases else PenOpenerBase(
        "failure",
        "When was the last time a pen let you down in front of someone who was watching?",
    )
    return PenOpening(
        angle=base.angle,
        question=base.question,
        greeting=random.choice(_cached_pen_greetings()),
        bridge=random.choice(_cached_pen_bridges()),
    )


def format_pen_opening_spoken_line(opening: PenOpening) -> str:
    """Greeting only — discovery question follows after caller responds."""
    return opening.greeting.rstrip(".!?")


def format_phone_opening_response_create(opening: PenOpening) -> str:
    """Instructions for the first response.create on SIP — must match browser speak_opening."""
    return (
        "TURN ONE — Greeting only, then stop and wait for their reply:\n"
        f'"{opening.greeting}"\n'
        "Do **not** ask the discovery question on this turn. Deliver the greeting, then silence."
    )


def format_phone_call_greeting(opening: PenOpening) -> str:
    return (
        "── PHONE CALL — THREE-TURN OPENER (MANDATORY: greeting → premise ask → discovery) ──\n"
        "TURN ONE — Greeting only, then stop and wait:\n"
        f'"{opening.greeting}"\n\n'
        "Stop completely after the greeting and wait until they respond.\n"
        "Do **not** ask the premise question and do **not** ask the discovery question on turn one.\n\n"
        "TURN TWO — PREMISE ASK (only after they respond and give their name). Use their name right away, be direct "
        "and confident. Do NOT mention price. Examples (rephrase naturally):\n"
        "- \"So [name] — you agree, right? If I can sell you a pen, I can sell your customers a car?\"\n"
        "- \"So [name], real quick — if I can close you on a pen, that proves I can close your shoppers on a car, yeah?\"\n"
        "- \"[name] — fair to say: if I can sell you a pen today, you'd have to agree I can sell your customers a car?\"\n"
        "If no name yet, lead without it: \"Quick — you agree, right? If I can sell you a pen, I can sell your customers a car?\"\n\n"
        "Accept any clear yes, playful skepticism (\"we'll see,\" \"prove it,\" \"good luck\"), or "
        "direct ask back as agreement.\n\n"
        "TURN THREE — Only after the premise lands: ask the discovery question **conversationally** "
        "(with the natural lead-in in the assigned line), then stop:\n"
        f'"{opening.question}"\n\n'
        "Never strip the lead-in (e.g. \"Now let me ask you —\"). Never open cold with \"When was…\" alone.\n\n"
        "**Forbidden:** greeting + premise + discovery collapsed into one or two turns; pen "
        "features, price, or \"ten dollars\" on turn one or two; jumping cold into discovery without the premise ask in between. "
        "Price is NEVER volunteered — only answered if the caller specifically asks."
    )


def build_phone_accept_instructions() -> str:
    """Slim prompt for SIP accept (faster pickup). Full pen-challenge rules pushed on sideband."""
    return (
        f"{voice_anti_narration_rules()}\n\n"
        f"{format_phone_opening_overrides()}\n\n"
        "── PHONE CALL — OPENING ONLY (full rules load during connect) ──\n"
        "First spoken turn: greeting only — then stop and wait. "
        "Do NOT ask the discovery question on turn one.\n"
        "Never repeat this opening later in the call."
    )


def format_phone_opening_overrides() -> str:
    from outbound_telephony import voice_phone_disclosure_enabled

    disclosure_note = (
        "- Twilio already played the legal recording and AI-voice notice before you connected. "
        "**Do not** open with any recording or AI disclaimer. Turn one is the sales greeting only.\n"
        if voice_phone_disclosure_enabled()
        else ""
    )
    return (
        "── PHONE OPENING OVERRIDES (wins over generic SESSION OPENING above) ──\n"
        "- On this **phone call**, the opener is THREE turns: greeting → premise ask → discovery.\n"
        "- Turn one: greeting only — stop and wait; **never** add the premise or discovery question on this turn.\n"
        "- Turn two: PREMISE ASK — pitch the if-I-sell-a-pen-I-can-sell-a-car promise and get verbal agreement. "
        "Do NOT mention price. Then stop. **Never** add the discovery question on this turn. **Never** pitch pen features on this turn.\n"
        "- **No real transaction:** there is no payment, no shipping, no checkout. The caller is not actually buying a pen. "
        "Never mention the pen's price unless the caller specifically asks what it costs. Price is reactive, never proactive.\n"
        "- Turn three: discovery question — only after they respond to the premise ask. Then stop and listen.\n"
        "- Deliver the phone opener **exactly once** per call. **Never** restart from turn one if interrupted.\n"
        "- **Do not** repeat the premise ask or the discovery question on a later turn if you already asked them.\n"
        "- **Never** ask dealership name, store name, rooftop, website, or phone during the pen challenge "
        "(before begin_hammer_signup or skip_pen_challenge unlocks Hammer signup). Email and lot size are collected only "
        "AFTER pen victory in the assumptive close.\n"
        f"{disclosure_note}"
    )


def format_pen_session_opening(opening: PenOpening, *, after_phone_greeting: bool = False) -> str:
    premise_examples = (
        "Use their name right away and be direct. Do NOT mention price.\n"
        "- \"So [name] — you agree, right? If I can sell you a pen, I can sell your customers a car?\"\n"
        "- \"So [name], real quick — if I can close you on a pen, that proves I can close your shoppers on a car, yeah?\"\n"
        "- \"[name] — fair to say: if I can sell you a pen, you'd agree I can sell your customers a car?\"\n"
        "If no name yet: \"Quick — you agree, right? If I can sell you a pen, I can sell your customers a car?\"\n"
    )
    if after_phone_greeting:
        return (
            "── PHONE OPENING ALREADY DELIVERED — CONTINUE PEN CHALLENGE (premise ask → discovery) ──\n"
            f"Assigned angle: {opening.angle}.\n"
            f'Greeting already spoken: "{opening.greeting}"\n\n'
            "Next: wait for them to respond to the greeting, then deliver the PREMISE ASK in your own voice "
            "and stop and listen. Examples:\n"
            f"{premise_examples}\n"
            "Accept any clear yes, playful skepticism (\"we'll see,\" \"prove it\"), or direct ask back as agreement.\n\n"
            "Only after the premise lands, ask the discovery question:\n"
            f'"{opening.question}" — then stop and listen.\n\n'
            "After the opener is complete: sell the Hammer Pen **discovery-first** (questions > pitching, never volunteer "
            "a feature they didn't bring up). When they ask a question, answer it fully and stop — do not pivot to selling "
            "on the same turn. Selling turns: 15–25 words; question-answer turns: 25–45 words.\n"
            "Forbidden: dealership/store/website/phone questions before Hammer signup tools unlock. "
            "(Email and lot size are collected only AFTER pen victory in the assumptive close.)"
        )
    return (
        "── SESSION OPENING — THREE-TURN OPENER (MANDATORY: greeting → premise ask → discovery) ──\n"
        f"Assigned angle: {opening.angle}.\n\n"
        f'TURN ONE — Greeting then bridge in the same turn, then stop:\n'
        f'"{opening.greeting}" then immediately "{opening.bridge}"\n\n'
        "Do not stop after the greeting alone. Do not ask the premise question or the discovery question on turn one.\n\n"
        "TURN TWO — After they respond to the bridge, PREMISE ASK in your own voice (pitch + verbal agreement ask), "
        "then stop and listen. Examples:\n"
        f"{premise_examples}\n"
        "Accept any clear yes, playful skepticism, or direct ask back as agreement. Do NOT pitch pen features on this turn.\n\n"
        f'TURN THREE — Only after the premise lands, discovery question with conversational lead-in, then stop:\n'
        f'"{opening.question}"\n\n'
        "Use the assigned line as written — include the lead-in. Do not add discovery on turn one or two. "
        "Wait for the bridge answer before turn two, and the premise answer before turn three."
    )


@lru_cache(maxsize=1)
def voice_anti_narration_rules() -> str:
    return _extract_ts_backtick_const(
        _WEB_SRC / "voice-anti-narration.ts", "VOICE_ANTI_NARRATION_RULES"
    )


def _prompt_override(key: str) -> str | None:
    # On Vercel/serverless we never honor dashboard prompt overrides — the file in this
    # repo is the single source of truth so deploys can't be silently shadowed by a
    # stale SQLite row left in /tmp from a prior editing session.
    try:
        from voice_dashboard_store import _is_serverless  # type: ignore

        if _is_serverless():
            return None
    except Exception:
        pass
    try:
        from voice_dashboard_store import get_setting

        val = get_setting(key)
        if isinstance(val, str) and val.strip():
            return val
    except Exception:
        pass
    return None


@lru_cache(maxsize=1)
def _pen_challenge_instructions_default() -> str:
    return _extract_ts_backtick_const(
        _WEB_SRC / "pen-challenge-instructions.ts", "PEN_CHALLENGE_INSTRUCTIONS"
    )


def pen_challenge_instructions() -> str:
    override = _prompt_override("pen_prompt")
    if override:
        return override
    return _pen_challenge_instructions_default()


@lru_cache(maxsize=1)
def _hammer_sales_instructions_default() -> str:
    return _extract_ts_backtick_const(
        _WEB_SRC / "hammer-sales-instructions.ts", "HAMMER_SALES_INSTRUCTIONS"
    )


def hammer_sales_instructions() -> str:
    override = _prompt_override("hammer_prompt")
    if override:
        return override
    return _hammer_sales_instructions_default()


@lru_cache(maxsize=1)
def voice_contact_readback_rules() -> str:
    return _extract_ts_backtick_const(
        _WEB_SRC / "voice-contact-readback.ts", "VOICE_CONTACT_READBACK_RULES"
    )


@lru_cache(maxsize=1)
def _hammer_voice_close_rules_default() -> str:
    raw = _extract_ts_backtick_const(_WEB_SRC / "pen-challenge-close.ts", "HAMMER_VOICE_CLOSE_RULES")
    placeholder = "${VOICE_CONTACT_READBACK_RULES}"
    if placeholder in raw:
        raw = raw.replace(placeholder, voice_contact_readback_rules().strip())
    return raw


def hammer_voice_close_rules() -> str:
    override = _prompt_override("pen_close_prompt")
    if override:
        return override
    return _hammer_voice_close_rules_default()


def clear_instruction_cache() -> None:
    """Bust cached prompt files after dashboard settings change."""
    _pen_challenge_instructions_default.cache_clear()
    _hammer_sales_instructions_default.cache_clear()
    _hammer_voice_close_rules_default.cache_clear()
    voice_anti_narration_rules.cache_clear()
    voice_contact_readback_rules.cache_clear()
    hammer_browser_pricing_rules.cache_clear()
    hammer_product_boundaries_rules.cache_clear()
    _cached_pen_opener_bases.cache_clear()
    _cached_pen_greetings.cache_clear()
    _cached_pen_bridges.cache_clear()


def get_default_prompts() -> dict[str, str]:
    """File-backed prompt defaults (ignores dashboard overrides)."""
    return {
        "pen_prompt": _pen_challenge_instructions_default(),
        "hammer_prompt": _hammer_sales_instructions_default(),
        "pen_close_prompt": _hammer_voice_close_rules_default(),
    }


def warm_instruction_cache() -> None:
    """Load static prompt files once so the first inbound SIP call is not blocked on disk I/O."""
    _cached_pen_opener_bases()
    _cached_pen_greetings()
    _cached_pen_bridges()
    voice_anti_narration_rules()
    voice_contact_readback_rules()
    _pen_challenge_instructions_default()
    _hammer_sales_instructions_default()
    _hammer_voice_close_rules_default()
    hammer_browser_pricing_rules()
    hammer_product_boundaries_rules()


@lru_cache(maxsize=1)
def hammer_product_boundaries_rules() -> str:
    """Authoritative Hammer product boundaries — what each product DOES and does NOT cover.

    Sourced from the canonical PRODUCTS + HARD RULES sections of HAMMER_SALES_INSTRUCTIONS.
    This block wins over wiki/PRODUCT CONTEXT when they disagree (wiki occasionally lags
    when a feature moves between products — e.g. Marketplace messaging is Hammer Connect
    only, never Hammer Drive).
    """
    return (
        "── HAMMER PRODUCT BOUNDARIES (AUTHORITATIVE — wins over wiki/PRODUCT CONTEXT when they disagree) ──\n"
        "Four Hammer products. Stay precise on which product covers which lead source — dealers will catch a wrong answer.\n\n"
        "**Hammer Drive** — core A-I agent for internet and integrated lead-source response and follow-up. "
        "Covers: website leads, web chat (included), C-R-M / I-L-M leads, Facebook A-I-A ad leads, and Craigslist posting "
        "($5.99 per post; no free Craigslist postings; dealer fully controls posting cadence).\n"
        "**Does NOT cover:** Facebook Marketplace messages or Marketplace inbox leads. Drive does not text or follow up on "
        "Marketplace shoppers — that is Hammer Connect only.\n\n"
        "**Hammer Connect** — the only Hammer product for Facebook Marketplace lead/message engagement. Marketplace "
        "messages route into Hammer; first reply goes out as S-M-S/text. Bundled with MarketPoster at no extra monthly "
        "charge. Standalone (Connect only, no MarketPoster): $99/mo only — never quote another Connect price.\n\n"
        "**MarketPoster** — Chrome extension that POSTS inventory to Facebook Marketplace. Hammer Connect is bundled in "
        "(no extra monthly fee on top of seat tiers). Note: posting inventory is NOT the same as engaging Marketplace "
        "messages — message replies need Connect (which MarketPoster includes).\n\n"
        "**Facebook A-I-A** — runs the dealer's inventory as sponsored Meta ads on both Facebook and Instagram (cross-app, "
        "not Facebook only). Hammer responds to every lead the ads generate. A-I-A ad leads flow through **Hammer Drive** — "
        "they are NOT Marketplace messages. Pricing: $299/mo Hammer fee (flat at every lot size) PLUS $15/day minimum Meta "
        "ad spend (separate, covers full inventory).\n\n"
        "**Inbound phone calls:** Hammer does NOT answer the dealership's phone — no A-I receptionist, no picking up live "
        "rings, same answer for every store. What we DO: transcribe missed calls and voicemail, then text back, update "
        "C-R-M, and take steps from what was said.\n\n"
        "**Trials:** Hammer does not offer trials. Month-to-month subscription only — no setup, signup, activation, or "
        "trial fees on the monthly tiers. The only $5 figure outside monthly tiers is $5.99 per Craigslist post.\n\n"
        "**Minimum lot size:** Ten or more vehicles on the lot to sign up — applies to Drive, A-I-A, MarketPoster, and "
        "Connect. Nine or fewer is not a fit."
    )


@lru_cache(maxsize=1)
def hammer_browser_pricing_rules() -> str:
    """Authoritative pricing for browser voice — sourced from hammer_agreement.py constants."""
    from hammer_agreement import (
        FACEBOOK_AIA_HAMMER_MONTHLY_USD,
        FACEBOOK_AIA_META_DAILY_MIN_USD,
        HAMMER_CONNECT_MONTHLY_USD,
        HAMMER_DRIVE_CAD_BANDS,
        HAMMER_DRIVE_USD_BANDS,
        MARKETPOSTER_ADDITIONAL_USER_MONTHLY_USD,
    )

    def _drive_lines(bands: tuple[tuple[int, int, int], ...], currency: str) -> str:
        lines: list[str] = []
        for lo, hi, price in bands:
            label = f"{lo}–{hi} cars" if hi < 10_000 else f"{lo}+ cars"
            suffix = f" {currency}" if currency != "USD" else ""
            lines.append(f"- {label}: ${price}/mo{suffix}")
        return "\n".join(lines)

    return (
        "── PRICING (AUTHORITATIVE — quote exactly; this block wins over wiki/PRODUCT CONTEXT) ──\n"
        "Month-to-month only. No trials, setup fees, signup fees, activation fees, or long-term contracts.\n"
        "Minimum 10 vehicles on lot to sign up (exactly 10 qualifies; 9 or fewer = not a fit).\n\n"
        "Hammer Drive (USD, lot-tiered):\n"
        f"{_drive_lines(HAMMER_DRIVE_USD_BANDS, 'USD')}\n\n"
        "Hammer Drive (Canada, CAD, lot-tiered):\n"
        f"{_drive_lines(HAMMER_DRIVE_CAD_BANDS, 'CAD')}\n\n"
        f"Facebook AIA: ${FACEBOOK_AIA_HAMMER_MONTHLY_USD}/mo Hammer fee — flat at every lot size, NOT lot-tiered.\n"
        f"Plus ${FACEBOOK_AIA_META_DAILY_MIN_USD}/day minimum Meta ad spend (separate from the "
        f"${FACEBOOK_AIA_HAMMER_MONTHLY_USD}/mo; covers full inventory).\n\n"
        "MarketPoster (USD, by seat count — Hammer Connect included at no extra monthly charge):\n"
        "- 1 user: $199/mo\n"
        "- 2 users: $249/mo\n"
        "- 3 users: $299/mo\n"
        "- 4 users: $349/mo\n"
        "- 5 users: $599/mo\n"
        f"- 6+ users: $599/mo + ${MARKETPOSTER_ADDITIONAL_USER_MONTHLY_USD}/mo per user above 5\n\n"
        f"Hammer Connect standalone (Marketplace messaging only, no MarketPoster): "
        f"${HAMMER_CONNECT_MONTHLY_USD}/mo only — never quote another Connect price.\n\n"
        "Craigslist (via Hammer Drive): $5.99 per vehicle post — no free Craigslist postings.\n"
        "Never invent discounts or fees not listed here."
    )


def _product_context_block(wiki_context: str) -> str:
    if wiki_context.strip():
        return (
            "── PRODUCT CONTEXT ──\n"
            "Answer from here when covered; only call search_wiki when the fact is missing here.\n\n"
            f"{wiki_context.strip()}"
        )
    return "── PRODUCT CONTEXT ──\nUse search_wiki for Hammer product facts."


def hammer_browser_no_handoff_rules() -> str:
    """Top-priority guardrail for the website live voice demo — wins over every other rule."""
    return (
        "── ABSOLUTE RULE — NO HUMAN HANDOFF EVER (overrides EVERYTHING below) ──\n"
        "This is the Hammer website live voice AI demo. There is NO live sales rep on the other end of this call. "
        "No human is going to reach out, call back, follow up, finish their account, walk them through their dashboard, "
        "complete signup, take their card, or do ANY part of onboarding for them. That path does not exist here.\n"
        "\n"
        "YOU — Hannah, the AI on this call — are the ONLY signup channel. You complete the entire signup yourself: "
        "agreement email → confirm I approve → collect Phase B account fields → create the Hammer account → "
        "guide them through Welcome → Activate → password → card on screen.\n"
        "\n"
        "STRICTLY FORBIDDEN PHRASES (never say any of these, in any wording — applies for the entire call, including "
        "right after capture_lead, right after I approve, mid signup, and at close):\n"
        "- 'a live sales rep will reach out'\n"
        "- 'a live rep will reach out / call / follow up'\n"
        "- 'a Hammer rep will reach out / call / follow up'\n"
        "- 'someone from Hammer will reach out / call / follow up'\n"
        "- 'our team will reach out / call you'\n"
        "- 'a rep will finish your signup / walk you through your dashboard / complete your account'\n"
        "- 'we'll have someone reach out to finish this'\n"
        "- 'if you want to move forward, a rep will…'\n"
        "- Any phrase that suggests a human will contact them later to complete signup or onboarding.\n"
        "\n"
        "If they ASK whether someone will reach out: one line — 'No need — I can finish your signup right now on this call. "
        "Want me to keep going?' Then continue the flow.\n"
        "\n"
        "The ONLY time a human phone number is mentioned: if the visitor EXPLICITLY asks to speak to a human on the phone, "
        "you may give (512) 883-1336 and immediately add that you can still finish signup on this call if they prefer.\n"
        "\n"
        "This rule overrides any other 'rep,' 'walkthrough,' or 'handoff' phrasing anywhere else in the prompt, in tool "
        "result messages, or in wiki context. If a tool result accidentally mentions a rep, ignore that part and follow this rule.\n"
    )


def build_hammer_browser_prompt(wiki_context: str) -> str:
    """Browser ElevenLabs demo — full sales instructions for factual accuracy."""
    mode = (
        "── BROWSER LIVE DEMO MODE (authoritative) ──\n"
        "This is the Hammer website live voice demo — NOT the Sell Me a Pen phone challenge.\n"
        "You handle the FULL signup on this call: agreement email → I approve → account fields → "
        "create_hammer_account → activate → password → card guidance.\n"
        "After the agreement email is sent, NEVER tell the visitor a live sales rep will reach out, "
        "call back, or complete their account — that will not happen. YOU finish signup on this call.\n"
        "Do NOT tell the visitor a live sales rep will reach out, finish signup, or walk them through "
        "the dashboard unless they explicitly ask for a human callback.\n"
        "Do NOT call capture_lead more than once per email unless resend_agreement=true.\n"
    )
    return (
        f"{hammer_browser_no_handoff_rules()}\n"
        f"{mode}\n"
        f"{voice_anti_narration_rules()}\n\n"
        f"{hammer_sales_instructions()}\n\n"
        f"{hammer_product_boundaries_rules()}\n\n"
        f"{hammer_browser_pricing_rules()}\n\n"
        f"{voice_contact_readback_rules()}\n\n"
        f"{_product_context_block(wiki_context)}\n\n"
        f"{format_austin_session_clock()}\n\n"
        f"{hammer_browser_no_handoff_rules()}"
    )


def build_pen_challenge_prompt(wiki_context: str) -> str:
    """Pen-challenge phase — pen-selling persona, plus authoritative Hammer
    product boundaries, pricing, and prefetched product context so dealer
    questions about Hammer products are answered with the same precision as
    the live browser demo (e.g. Marketplace messaging is Connect only, never
    Drive). The visitor takes over to Hammer mode the moment they ask a
    substantive product question (HAMMER ENGAGEMENT in the pen instructions).

    The wiki context is already prefetched at executor warm time, so injecting
    it here adds prompt tokens but no I/O latency.
    """
    return (
        f"{voice_anti_narration_rules()}\n\n"
        f"{pen_challenge_instructions()}\n\n"
        f"{hammer_product_boundaries_rules()}\n\n"
        f"{hammer_browser_pricing_rules()}\n\n"
        f"{_product_context_block(wiki_context)}\n\n"
        f"{format_austin_session_clock()}"
    )


def build_micro_pitch_guidance(hammer_product: str) -> str:
    product = hammer_product.strip() or "Hammer Drive"
    return (
        "── MICRO-PITCH (2 sentences aloud, then close — no third sentence) ──\n"
        f"Product: {product}\n"
        "1) Shoppers hit other rooftops the second the lead fires.\n"
        f"2) {product} texts them in that window and keeps following up.\n"
        "Then: price in one clause if needed, then **email** — do not re-ask product or lot if already clear."
    )


def build_hammer_knowledge_handoff(wiki_context: str) -> str:
    rules = hammer_voice_close_rules()
    wiki_block = (
        f"── HAMMER KNOWLEDGE ──\nAnswer ALL Hammer product, pricing, feature, and integration questions "
        f"from the facts below. Only call search_wiki when the specific fact is NOT found here.\n{wiki_context.lstrip()}"
        if wiki_context.strip()
        else "── HAMMER KNOWLEDGE ──\nUse search_wiki before Hammer product claims."
    )
    return (
        "skip_pen_challenge: OK — Hammer knowledge and signup tools are live.\n\n"
        "- Hannah — answer from knowledge block and search_wiki.\n"
        "- **Want to sign up** → assumptive close below; skip extra discovery.\n"
        "- No checkout URLs.\n\n"
        f"{hammer_product_boundaries_rules()}\n\n"
        f"{hammer_browser_pricing_rules()}\n\n"
        f"{rules}\n\n"
        f"{wiki_block}"
    )


def hammer_browser_close_rules() -> str:
    return (
        "── HAMMER BROWSER LIVE CLOSE (SELF-SERVE SIGNUP) ──\n"
        "You are still Hannah. On this browser call, you handle the full signup yourself. "
        "Do NOT mention a live rep, and do NOT offer/schedule a walkthrough. The visitor will "
        "complete signup 100% self-serve on this call. "
        "Your task is to send the agreement, verify their 'I approve' reply, and walk them through "
        "account creation step-by-step.\n\n"
        "The full flow is just six beats:\n"
        "1. **Confirm Product & Lot size**, then ask for **email** (never read/spell back proactively) and **dealership name**.\n"
        "2. **Call capture_lead** with email + dealership_name + selected_plan + lot_size. Wait for 'ok —' before moving on.\n"
        "3. **Ask 'Got the agreement at that same email?'** on the same turn. Tell them to open it and reply 'I approve'.\n"
        "4. **Wait and poll for approval**: the moment they say they replied, speak the confirming-wait line, then call check_agreement_approval with just_replied=true while asking the next question.\n"
        "5. **On approval**: silently call open_hammer_account_form if needed, then ask the remaining Phase B fields "
        "one-by-one (legal business structure, phone, website, physical address). Call fill_hammer_account_field "
        "after each field. Never ask more than one field per turn. Never assume fields.\n"
        "6. **On account created**: ask if they received the 'Welcome to Hammer' email, answer final questions, and close warmly."
    )


def build_hammer_signup_handoff(
    hammer_product_interest: str,
    wiki_context: str,
    *,
    awaiting_hammer_product: bool = True,
    is_browser: bool = False,
) -> str:
    hammer_product = hammer_product_interest.strip()
    if hammer_product:
        pivot = (
            f'Product: "{hammer_product}". set_buyer_product if needed, 2-sentence micro-pitch, then **email** — assumptive close.'
        )
    elif awaiting_hammer_product:
        pivot = (
            "Go straight to Hammer for dealers — one question: Drive, Facebook AIA, MarketPoster, or Connect? "
            "No pen recap."
        )
    else:
        pivot = ""
    wiki_block = (
        f"── HAMMER KNOWLEDGE ──\nAnswer ALL Hammer product, pricing, feature, and integration questions "
        f"from the facts below. Only call search_wiki when the specific fact is NOT found here.\n{wiki_context.lstrip()}"
        if wiki_context.strip()
        else "── HAMMER KNOWLEDGE ──\nUse search_wiki before Hammer product claims."
    )
    close_rules = hammer_browser_close_rules() if is_browser else hammer_voice_close_rules()
    return (
        "begin_hammer_signup: OK — signup tools live. Assumptive close — process the deal, no overexplaining.\n\n"
        f"{pivot}\n\n"
        f"{hammer_product_boundaries_rules()}\n\n"
        f"{hammer_browser_pricing_rules()}\n\n"
        f"{close_rules}\n\n"
        f"{wiki_block}"
    )


def format_austin_session_clock() -> str:
    fallback = (
        "── CURRENT TIME (AUSTIN / CENTRAL) ──\n"
        "Hammer HQ is Austin, Texas — US Central Time (America/Chicago). Live reps: Monday–Friday 9 a.m.–5 p.m. Central; "
        "nights and weekends the floor is not staffed. Live Hammer line for humans who want our floor: (512) 883-1336 — "
        "say five one two, eight eight three, one three three six. You can still sign them up on this call yourself. "
        "Do not invent a specific timestamp."
    )
    try:
        # Hour-only resolution — minute-level precision busts OpenAI's prefix cache on
        # every request. GPT doesn't need exact minutes for "are you open right now?" logic.
        now = datetime.now(ZoneInfo("America/Chicago"))
        stamp = now.strftime("%A, %B %-d, %Y, %-I %p %Z")
    except Exception:
        try:
            now = datetime.now(ZoneInfo("America/Chicago"))
            stamp = now.strftime("%A, %B %d, %Y, %I %p %Z")
        except Exception:
            return fallback
    return (
        "── CURRENT TIME IN AUSTIN (AUTHORITATIVE FOR THIS CALL) ──\n"
        f"Captured when this voice session connected: {stamp}.\n"
        "When they ask what time it is in Austin, whether Hammer is open now, when live reps answer the phone, "
        "when to call back for a human, or **Hammer's phone number for a live rep**: combine this timestamp with "
        "Monday–Friday 9 a.m.–5 p.m. Central — nights and weekends the floor is not staffed — and give **(512) 883-1336** "
        "when they want our live team (speak digits clearly: five one two — eight eight three — one three three six). "
        "During the **pen challenge** (before begin_hammer_signup or skip_pen_challenge): do **not** offer Hammer signup "
        "or ask dealership/store/lot/email/phone fields — pen sale only.\n"
        "After Hammer signup tools unlock: you may sign them up on this call if they prefer.\n"
        "Never invent a different clock time or weekday; do not reconstruct \"now\" from memory beyond this line."
    )


def build_pen_call_instructions(opening: PenOpening | None = None) -> str:
    opening = opening or pick_pen_opening()
    return (
        f"{voice_anti_narration_rules()}\n\n"
        f"{pen_challenge_instructions()}\n\n"
        f"{format_phone_opening_overrides()}\n\n"
        f"{format_phone_call_greeting(opening)}\n\n"
        f"{format_pen_session_opening(opening, after_phone_greeting=True)}\n\n"
        f"{format_austin_session_clock()}"
    )


def prefetch_wiki_context(retriever, queries: list[str], *, max_chars: int = 14000) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    total = 0
    for q in queries:
        for chunk, _score in retriever.top_k(q, k=6):
            key = f"{chunk.doc_id}#{chunk.chunk_id}"
            if key in seen:
                continue
            seen.add(key)
            piece = re.sub(r"\s+", " ", chunk.text.strip())
            if not piece:
                continue
            if total + len(piece) > max_chars:
                return _format_wiki_block(lines)
            lines.append(piece)
            total += len(piece)
    return _format_wiki_block(lines)


def _format_wiki_block(lines: list[str]) -> str:
    if not lines:
        return ""
    return (
        "\n\n── PRODUCT CONTEXT ──\n"
        "Answer ALL Hammer product, pricing, feature, and integration questions directly from "
        "the facts below. Only call search_wiki when the specific fact is NOT found here.\n\n"
        + "\n---\n".join(lines)
    )


WIKI_PREFETCH_QUERIES = [
    # Core product features — most-asked topics first
    "Hammer AI lead response speed features",
    "Hammer metrics appointments conversion rates",
    "Hammer integrations CRM Facebook AIA platforms",
    "Facebook AIA Instagram Meta inventory ads placements",
    "Hammer Connect MarketPoster Hammer Drive products",
    "Hammer Drive Facebook Marketplace not included Connect only",
    "Hammer Drive website web chat included",
    "Hammer Drive Craigslist posting per post fee",
    "Hammer inbound phone calls transcription missed voicemail not answering",
    # Pricing, contracts, trial — frequently asked in close phase
    "Hammer pricing cost per rooftop monthly contract",
    "Hammer trial period free approval renewal",
    "Hammer setup onboarding 72 hours account manager",
    "Hammer Welcome email activate password card next screen account",
    # After-hours, 24/7, response time — speed is Hammer's core pitch
    "Hammer after hours 24 7 response time seconds",
    "Hammer Drive agentic AI sales rep conversations",
    # Reporting and dashboard
    "Hammer dashboard reporting analytics fire metrics",
    # Trade-in, credit app, Spanish language
    "Hammer trade-in credit application Spanish multilingual",
    # Competitor comparisons, uniqueness
    "Hammer competitors comparison automotive BDC",
    # How it works end-to-end
    "Hammer how it works lead fires text call follow up",
]

# Top queries only — faster wiki warm for browser ElevenLabs on cold serverless starts.
EL_BROWSER_WIKI_PREFETCH = WIKI_PREFETCH_QUERIES[:8]
