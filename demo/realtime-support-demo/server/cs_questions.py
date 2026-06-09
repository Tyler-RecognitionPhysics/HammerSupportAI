"""CS Questions Database — mine the top customer questions/reasons from HubSpot tickets.

Reads synced HubSpot resolved-ticket subjects, normalizes and aggregates them,
then uses an LLM (map + reduce) to cluster them into the top ~100 canonical
customer questions with volume counts. Results are cached so the AI and the
support team can learn from the most common reasons customers reach out.
"""

from __future__ import annotations

import json
import logging
import os
import re
import hashlib
import sqlite3
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

_log = logging.getLogger(__name__)

TOP_N = 100
_BUILD_VERSION = "csq-customer-initiated-v5-cancellation"
# How many distinct (normalized) subjects to feed the LLM, highest-volume first.
# 0 (default) = analyze ALL unique subjects. Override with CS_QUESTIONS_MAX_SUBJECTS.
MAX_INPUT_SUBJECTS = int(os.environ.get("CS_QUESTIONS_MAX_SUBJECTS", "0") or "0")
# Skip the long tail of one-off subjects to cut build time. 1 (default) keeps all;
# set to 2+ to only cluster subjects seen at least N times.
MIN_SUBJECT_COUNT = max(1, int(os.environ.get("CS_QUESTIONS_MIN_COUNT", "1") or "1"))
# Concurrent OpenAI requests during map/reduce. Higher = faster, watch rate limits.
LLM_CONCURRENCY = max(1, int(os.environ.get("CS_QUESTIONS_CONCURRENCY", "8") or "8"))
# Ticket context snippets are longer than subjects; keep map prompts compact.
MAP_BATCH_SIZE = max(20, int(os.environ.get("CS_QUESTIONS_MAP_BATCH_SIZE", "60") or "60"))
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"

_rebuild_lock = threading.Lock()
_rebuild_running = False

_RE_PREFIX = re.compile(r"^\s*(re|fwd|fw|ticket)\s*:\s*", re.I)
_RE_TICKET_NUM = re.compile(r"#\s*\d+|\bticket\s*#?\s*\d+\b", re.I)
_RE_REDACTION = re.compile(r"\[?(?:phone|email|vin)-redacted\]?", re.I)
_RE_TEL = re.compile(r"\btel\s*:?", re.I)
_RE_BRACKETS = re.compile(r"[\[\]\(\)\{\}\"'`]+")
_RE_WS = re.compile(r"\s+")
_RE_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_HEADING = re.compile(r"^#{1,6}\s+")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _model() -> str:
    return os.environ.get(
        "SUPPORT_CHAT_MODEL", os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    ).strip()


def _openai_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


def cs_questions_configured() -> bool:
    return bool(_openai_key())


# ── Data source ─────────────────────────────────────────────────────────────

# Max distinct real tickets to keep per normalized subject (for example links).
_EXAMPLES_PER_SUBJECT = 5
# Max example tickets to surface per clustered question in the UI.
_EXAMPLES_PER_QUESTION = 20


def _portal_id() -> str:
    return os.environ.get("HUBSPOT_PORTAL_ID", "3355079").strip()


def hubspot_ticket_url(ticket_id: str) -> str:
    portal = _portal_id()
    tid = str(ticket_id or "").strip()
    if portal and tid:
        return f"https://app.hubspot.com/contacts/{portal}/ticket/{tid}"
    return ""


def _raw_hubspot_tickets_dir() -> Path | None:
    try:
        from hubspot_tickets_sync import _raw_hubspot_tickets_dir as raw_dir

        return raw_dir()
    except Exception:
        return None


def _ticket_markdown_path(row: dict[str, str]) -> Path | None:
    root = _raw_hubspot_tickets_dir()
    if not root:
        return None
    file_name = str(row.get("file_name") or "").strip()
    if file_name:
        path = root / file_name
        if path.is_file():
            return path
    ticket_id = str(row.get("ticket_id") or "").strip()
    if not ticket_id or not root.is_dir():
        return None
    try:
        return next(root.glob(f"{ticket_id}-*.md"), None)
    except OSError:
        return None


_PRODUCT_SIGNAL_RE = re.compile(
    r"\b("
    r"lead|leads|source|provider|cars\.com|autotrader|carfax|cargurus|dealercenter|"
    r"dashboard|prospect|inbox|notification|text|sms|email|ai|agent|training|account|"
    r"question|answer|facebook|tiktok|google|ad|ads|aia|marketposter|marketplace|"
    r"inventory|vehicle|vin|crm|integration|dealertrack|tekion|cdk|login|password|"
    r"billing|invoice|payment|profile|hours|settings|setup|tracking|"
    r"connect|widget|website|chat"
    r")\b",
    re.I,
)
_STRONG_ISSUE_RE = re.compile(
    r"\b("
    r"lead|leads|source|provider|cars\.com|autotrader|carfax|cargurus|dealercenter|"
    r"dashboard|prospect|inbox|notification|sms|ai|training|account|"
    r"question|answer|facebook|tiktok|google|ad|ads|aia|marketposter|marketplace|"
    r"inventory|vehicle|vin|crm|integration|dealertrack|tekion|cdk|login|password|"
    r"billing|invoice|payment|profile|hours|settings|setup|tracking|connect|widget|website"
    r")\b",
    re.I,
)
_EMAIL_TEXT_ISSUE_RE = re.compile(r"\b(email|text|sms)\b.*\b(not|can't|cannot|issue|problem|send|sent|receive|receiving|support|available)\b", re.I)
_VAGUE_SUBJECTS = {
    "question", "questions", "few questions", "have a few questions", "support",
    "help", "issue", "issues", "followup", "follow up", "call", "callback",
    "check in", "checking in", "account review", "ticket form submission",
}
_BOILERPLATE_RE = re.compile(
    r"\b("
    r"best regards|thanks,|thank you|customer success|hammer corp|a call took place|"
    r"thread status change|assignment|book a strategy call|need help\? submit a support ticket|"
    r"wasn.t able to connect|one last time regarding the request|reply directly to this email|"
    r"you.re welcome to reply|mark this as resolved|recent call attempt|specific question or request|"
    r"representative from our team will reach out|authorized user on the account|discuss next steps|"
    r"prefer to connect by phone"
    r")\b",
    re.I,
)
_TIMELINE_META_RE = re.compile(r"^-\s*(status|direction)\s*:", re.I)
_CALL_DISPOSITION_RE = re.compile(
    r"\b(agent answered the call|na/lvm|na\s*x?\d*|lvm|left voicemail|sent text|"
    r"ob call|ib call|ib support email|support email|missed call|fwds to same vm|"
    r"unable to connect|no response to txt|no answer)\b",
    re.I,
)


def _clean_text_for_issue(text: str) -> str:
    text = _RE_MD_LINK.sub(r"\1", text or "")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b[\w.+-]+@\w[\w.-]+\.\w+\b", "[email-redacted]", text)
    text = _RE_HEADING.sub("", text.strip())
    text = re.sub(r"[*_`>|]+", " ", text)
    text = _RE_WS.sub(" ", text).strip(" -:•\t")
    return text


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip()
    return meta, text[end + 4 :].lstrip()


def _section(md_body: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ims)^##\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(md_body)
    return (match.group("body") if match else "").strip()


def _subject_is_vague(norm_subject: str) -> bool:
    s = norm_subject.strip()
    if looks_like_noise(s):
        return True
    if s in _VAGUE_SUBJECTS:
        return True
    words = s.split()
    if len(words) <= 3 and not _PRODUCT_SIGNAL_RE.search(s):
        return True
    # Dealer/domain/person-name subjects tend to hide the actual issue in the timeline.
    if re.fullmatch(r"[a-z0-9.-]+\s*(com|net|org)?", s) and not _PRODUCT_SIGNAL_RE.search(s):
        return True
    return False


def _useful_issue_lines(text: str, *, max_lines: int = 8) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = _clean_text_for_issue(raw)
        if len(line) < 12:
            continue
        if _TIMELINE_META_RE.match(line):
            continue
        if line.lower().startswith("status:"):
            continue
        if _BOILERPLATE_RE.search(line):
            continue
        if line.lower().startswith(("dealership name:", "first name:", "last name:", "contact phone", "email:")):
            continue
        has_strong_issue = bool(_STRONG_ISSUE_RE.search(line) or _EMAIL_TEXT_ISSUE_RE.search(line))
        if _CALL_DISPOSITION_RE.search(line) and not has_strong_issue:
            continue
        if _PRODUCT_SIGNAL_RE.search(line) and has_strong_issue:
            lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


def _extract_ticket_issue_snippet(row: dict[str, str], *, max_chars: int = 650) -> str:
    path = _ticket_markdown_path(row)
    if not path:
        return ""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    _, body = _split_frontmatter(raw)

    parts: list[str] = []
    description = _section(body, "Description")
    if description:
        parts.extend(_useful_issue_lines(description, max_lines=3))

    for heading in ("Resolution", "Resolution (Resolution)", "Resolution (Hs Resolution)"):
        res = _section(body, heading)
        if res:
            parts.extend(_useful_issue_lines(res, max_lines=3))

    timeline = _section(body, "Help Desk Timeline")
    if timeline:
        # Prefer summaries, customer messages, notes, and concise agent notes over
        # lifecycle noise.
        priority_lines = []
        for raw_line in timeline.splitlines():
            line = raw_line.strip()
            if line.lower().startswith("summary:") or "ticket description:" in line.lower():
                priority_lines.append(line)
            elif _PRODUCT_SIGNAL_RE.search(line):
                priority_lines.append(line)
        parts.extend(_useful_issue_lines("\n".join(priority_lines), max_lines=8))
        if len(parts) < 4:
            parts.extend(_useful_issue_lines(timeline, max_lines=8 - len(parts)))

    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(part)

    snippet = " ".join(deduped)
    if len(snippet) > max_chars:
        snippet = snippet[: max_chars - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return snippet


# ── Customer-initiated (inbound) message extraction ─────────────────────────
# Top CS questions should reflect what DEALERSHIP CUSTOMERS actually ask, not
# Hammer-initiated work (account reviews, onboarding, proactive sales calls, QC
# follow-ups). Those are outbound/internal and dominate the raw ticket volume.
# We therefore mine the customer's own words from inbound channels only:
#   - Support-form submissions   (Conversation: Message — Incoming — … — Ticket Form Submission)
#   - Inbound emails             (Email: Incoming_Email)
#   - Inbound chat messages      (Conversation: Message — Incoming)
#   - Inbound phone calls         (Call … Direction: INBOUND — summary describes the ask)
# Set CS_QUESTIONS_CUSTOMER_INITIATED_ONLY=0 to also include the legacy
# subject/timeline snippet fallback for tickets with no inbound customer content.
_CUSTOMER_INITIATED_ONLY = (
    os.environ.get("CS_QUESTIONS_CUSTOMER_INITIATED_ONLY", "1").strip().lower()
    not in ("0", "false", "no")
)

# Quoted-reply / forwarded-history boundaries inside inbound emails.
_QUOTED_REPLY_RE = re.compile(
    r"(?im)^\s*(_{5,}|-{3,}\s*original message|from:\s|on .*wrote:\s*$|sent:\s|get outlook)"
)
# Inline lifecycle metadata that leaks into activity bodies.
_INLINE_META_RE = re.compile(r"(?i)\b(status|direction)\s*:\s*[a-z_]+", re.I)
_SUMMARY_LEAD_RE = re.compile(r"(?i)^\s*summary\s*:\s*(summary\b\s*)?")
# Email signatures, app footers, tracking pixels, and "sent from" lines.
_SIGNATURE_LINE_RE = re.compile(
    r"(?i)(!function\(|\[image:|customer success|connect with us|hammertime\.com|"
    r"monday\s*-\s*friday|sent from (my )?\w|get outlook|"
    r"ask me about getting more leads|account manager$)"
)
# Call-summary scaffolding ("Summary: Summary …", "Key notes") and lifecycle bullets.
_SUMMARY_INLINE_RE = re.compile(r"(?i)^summary\s*:\s*(summary\b\s*)?")
_CALL_SCAFFOLD_LINES = {"summary:", "summary: summary", "key notes", "key notes:", "notes:"}
# Support-form fields that are contact metadata (drop) vs. the actual request (keep value).
_FORM_META_FIELD_RE = re.compile(
    r"(?i)^(dealership name|first name|last name|full name|name|email|"
    r"contact (phone )?number|phone( number)?|company url|company|website|url|category)\s*:"
)
_FORM_VALUE_FIELD_RE = re.compile(
    r"(?i)^(ticket description|describe your request|description|message|"
    r"reason for your cancellation request|product\(s\) you are requesting to cancel)\s*:\s*(.*)$"
)


def _split_timeline_activities(body: str) -> list[tuple[str, str]]:
    """Split the Help Desk Timeline into (header, body) activity blocks."""
    timeline = _section(body, "Help Desk Timeline")
    if not timeline:
        return []
    acts: list[tuple[str, str]] = []
    header: str | None = None
    buf: list[str] = []
    for line in timeline.splitlines():
        if line.startswith("### "):
            if header is not None:
                acts.append((header, "\n".join(buf).strip()))
            header = line[4:].strip()
            buf = []
        elif header is not None:
            buf.append(line)
    if header is not None:
        acts.append((header, "\n".join(buf).strip()))
    return acts


def _clean_customer_text(text: str) -> str:
    text = _INLINE_META_RE.sub(" ", text or "")
    text = _SUMMARY_LEAD_RE.sub("", text)
    return _clean_text_for_issue(text)


def _extract_customer_inbound_text(
    body: str, *, max_chars: int = 600
) -> tuple[str, str | None]:
    """Return (customer's own words from inbound channels, form category or None).

    Only inbound/customer-initiated activities are read; outbound agent messages,
    notes, and proactive calls are ignored so the result is what the customer asked.
    """
    pieces: list[str] = []
    seen: set[str] = set()
    category: str | None = None
    for header, content in _split_timeline_activities(body):
        h = header.lower()
        inc_msg = "conversation: message" in h and "incoming" in h
        inc_email = "incoming_email" in h
        is_form = "ticket form submission" in h
        inbound_call = "call" in h and bool(
            re.search(r"direction:\s*inbound", content, re.I)
        )
        if not (inc_msg or inc_email or is_form or inbound_call):
            continue
        text = content
        if inc_email:
            match = _QUOTED_REPLY_RE.search(text)
            if match:
                text = text[: match.start()]
        kept: list[str] = []
        for raw in text.splitlines():
            s = raw.strip()
            if not s:
                continue
            if _TIMELINE_META_RE.match(s):  # "- Status: …" / "- Direction: …"
                continue
            if s.lower() in _CALL_SCAFFOLD_LINES:
                continue
            s = _SUMMARY_INLINE_RE.sub("", s).strip()
            if not s:
                continue
            if _SIGNATURE_LINE_RE.search(s):
                continue
            cat_match = re.match(r"(?i)^category\s*:\s*(.+)$", s)
            if cat_match:
                category = category or cat_match.group(1).strip()
                continue
            val_match = _FORM_VALUE_FIELD_RE.match(s)
            if val_match:
                val = val_match.group(2).strip()
                if val:
                    kept.append(val)
                continue
            if _FORM_META_FIELD_RE.match(s):
                continue
            kept.append(s)
        joined = _clean_customer_text(" ".join(kept))
        if len(joined) < 10 or looks_like_noise(joined.lower()):
            continue
        key = joined.lower()
        if key in seen:
            continue
        seen.add(key)
        pieces.append(joined)

    if not pieces:
        return "", category
    text = _RE_WS.sub(" ", " ".join(pieces)).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rsplit(" ", 1)[0].rstrip() + "…"
    return text, category


# Ticket pipeline stages whose tickets are NOT a useful source for top CS questions
# (e.g. "Spam", "Junk"). HubSpot marks the Spam stage as CLOSED, so it otherwise
# gets synced alongside genuinely resolved tickets. Stage IDs are resolved by label
# from the HubSpot pipelines API; override/extend with CS_QUESTIONS_EXCLUDE_STAGE_IDS
# (comma-separated stage IDs) for environments without a live HubSpot token.
_EXCLUDE_STAGE_LABEL_KEYWORDS = ("spam", "junk", "trash")
_excluded_stage_ids_cache: set[str] | None = None


def _hubspot_token() -> str:
    return (
        os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "").strip()
        or os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()
    )


def excluded_stage_ids() -> set[str]:
    """Stage IDs to drop from CS Questions (spam/junk). Resolved once per process."""
    global _excluded_stage_ids_cache
    if _excluded_stage_ids_cache is not None:
        return _excluded_stage_ids_cache

    env_ids = os.environ.get("CS_QUESTIONS_EXCLUDE_STAGE_IDS", "").strip()
    if env_ids:
        _excluded_stage_ids_cache = {s.strip() for s in env_ids.split(",") if s.strip()}
        return _excluded_stage_ids_cache

    ids: set[str] = set()
    token = _hubspot_token()
    if token:
        try:
            from hubspot_budget import consume as _consume_hubspot_budget

            _consume_hubspot_budget(1)
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(
                    "https://api.hubapi.com/crm/v3/pipelines/tickets",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if resp.status_code == 200:
                for pipeline in resp.json().get("results", []):
                    for stage in pipeline.get("stages", []):
                        label = str(stage.get("label") or "").lower()
                        if any(kw in label for kw in _EXCLUDE_STAGE_LABEL_KEYWORDS):
                            sid = str(stage.get("id") or "").strip()
                            if sid:
                                ids.add(sid)
            else:
                _log.warning(
                    "cs_questions: pipelines lookup for spam stages returned HTTP %s",
                    resp.status_code,
                )
        except Exception as exc:
            _log.warning("cs_questions: could not resolve spam stage ids: %s", exc)

    _excluded_stage_ids_cache = ids
    return ids


def read_ticket_rows() -> list[dict[str, str]]:
    """Synced HubSpot resolved tickets (id + subject), excluding spam/junk stages."""
    try:
        from hubspot_tickets_sync import _state_db_path
    except Exception:
        return []

    path = _state_db_path()
    try:
        if not path.is_file():
            return []
    except Exception:
        return []

    excluded = excluded_stage_ids()
    rows_out: list[dict[str, str]] = []
    try:
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute(
                "SELECT ticket_id, subject, stage_id, file_name FROM hubspot_tickets"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            subject = str(row[1] or "").strip()
            stage_id = str(row[2] or "").strip()
            if not subject:
                continue
            if excluded and stage_id in excluded:
                continue
            rows_out.append(
                {
                    "ticket_id": str(row[0] or "").strip(),
                    "subject": subject,
                    "stage_id": stage_id,
                    "file_name": str(row[3] or "").strip(),
                }
            )
        if excluded:
            _log.info("cs_questions: excluding spam/junk stages %s", sorted(excluded))
    except sqlite3.Error as exc:
        _log.warning("cs_questions: could not read ticket rows: %s", exc)
    return rows_out


def read_ticket_subjects() -> list[str]:
    """All synced HubSpot resolved-ticket subjects (kept for backward compatibility)."""
    return [r["subject"] for r in read_ticket_rows()]


def normalize_subject(subject: str) -> str:
    s = subject.strip().lower()
    # Strip repeated Re:/Fwd: prefixes
    while True:
        new = _RE_PREFIX.sub("", s)
        if new == s:
            break
        s = new
    s = _RE_TICKET_NUM.sub(" ", s)
    s = _RE_REDACTION.sub(" ", s)
    s = _RE_TEL.sub(" ", s)
    s = _RE_BRACKETS.sub(" ", s)
    s = _RE_WS.sub(" ", s).strip(" -:|·")
    return s


# Conservative noise filter — clear telephony / call-log / junk artifacts that are
# NOT customer support questions. The LLM is_support classifier handles the rest
# (e.g. business/person names). Kept narrow to avoid dropping real questions.
_EXACT_NOISE = {
    "", "n/a", "na", "ticket", "tickets", "test", "testing", "unknown", "none",
    "null", "new ticket", "untitled", "no subject", "fyi", "follow up", "follow-up",
    "no name", "noname", "dealer name", "dealership name", "name", "caller",
    "toll free call", "toll free", "automations inbox", "ticket form submission",
}
_NOISE_RE = re.compile(
    r"\b(missed call|inbound call|outbound call|ib\s*/?\s*ob call|ob\s*/?\s*ib call|"
    r"voicemail|new ib txt|new ob txt|toll[- ]?free call|in[_ ]?progress (?:in|out)bound|"
    r"(?:in|out)bound call (?:completed|in[_ ]?progress|cancell?ed|abandoned|ringing|connecting))\b",
    re.I,
)
_PHONE_ONLY_RE = re.compile(r"^[\d\s\-\+\(\)\.x]+$")
_IBOB_ONLY_RE = re.compile(r"^(ib|ob)(\s*[/&]\s*(ib|ob))?$", re.I)
# Date-only subjects (e.g. "12/02/25", "11-18-2025") — these are call/log artifacts.
_DATE_ONLY_RE = re.compile(r"^\d{1,2}[/\-.]\d{1,2}([/\-.]\d{2,4})?$")


def looks_like_noise(norm_subject: str) -> bool:
    s = norm_subject.strip()
    if s in _EXACT_NOISE:
        return True
    if len(s) < 3:
        return True
    if _PHONE_ONLY_RE.match(s):
        return True
    if _DATE_ONLY_RE.match(s):
        return True
    if _IBOB_ONLY_RE.match(s):
        return True
    if _NOISE_RE.search(s):
        return True
    return False


def _ticket_issue_text(row: dict[str, str]) -> tuple[str, bool, bool]:
    """Return (issue_text, customer_initiated, used_ticket_body).

    Prefers the customer's own inbound words. When CS_QUESTIONS_CUSTOMER_INITIATED_ONLY
    is set (default), tickets with no inbound customer content return empty text so they
    are dropped from the top-questions analysis (account reviews, proactive sales, etc.).
    """
    raw = str(row.get("subject") or "").strip()
    path = _ticket_markdown_path(row)
    body = ""
    if path:
        try:
            _, body = _split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            body = ""

    if body:
        customer_text, form_category = _extract_customer_inbound_text(body)
        if customer_text:
            prefix = f"[{form_category}] " if form_category else ""
            return f"{prefix}{customer_text}", True, True

    if _CUSTOMER_INITIATED_ONLY:
        return "", False, False

    # Legacy fallback: derive an issue snippet from the subject + timeline.
    norm = normalize_subject(raw)
    if _subject_is_vague(norm):
        snippet = _extract_ticket_issue_snippet(row)
        if snippet:
            return f"Ticket subject: {raw or 'No subject'}. Issue context: {snippet}", False, True
    return raw, False, False


def aggregate_subjects(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Collapse to unique normalized customer questions with counts + example tickets."""
    counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, str]]] = {}
    seen_ids: dict[str, set[str]] = {}
    display_text: dict[str, str] = {}
    stats = {
        "customer_initiated": 0,
        "excluded_internal": 0,
        "ticket_text_enriched": 0,
    }
    for row in rows:
        raw = str(row.get("subject") or "")
        ticket_id = str(row.get("ticket_id") or "").strip()
        issue_text, customer_initiated, used_body = _ticket_issue_text(row)
        if customer_initiated:
            stats["customer_initiated"] += 1
        if used_body:
            stats["ticket_text_enriched"] += 1

        norm = normalize_subject(issue_text)
        if not norm or len(norm) < 3:
            stats["excluded_internal"] += 1
            continue
        counts[norm] += 1
        display_text.setdefault(norm, issue_text)
        bucket = examples.setdefault(norm, [])
        ids = seen_ids.setdefault(norm, set())
        if len(bucket) < _EXAMPLES_PER_SUBJECT and (not ticket_id or ticket_id not in ids):
            if ticket_id:
                ids.add(ticket_id)
            bucket.append(
                {
                    "ticket_id": ticket_id,
                    "subject": raw.strip(),
                    "issue": issue_text.strip(),
                    "url": hubspot_ticket_url(ticket_id),
                }
            )
    out = [
        {
            "subject": display_text.get(norm, norm),
            "normalized_subject": norm,
            "example": examples[norm][0]["subject"] if examples.get(norm) else norm,
            "tickets": examples.get(norm, []),
            "count": n,
        }
        for norm, n in counts.most_common()
    ]
    return out, stats


# ── LLM clustering ──────────────────────────────────────────────────────────

def _openai_json(messages: list[dict], *, max_tokens: int = 4000) -> dict[str, Any]:
    key = _openai_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    with httpx.Client(timeout=90.0) as client:
        resp = client.post(
            _OPENAI_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": _model(),
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Best-effort: extract the first {...} block
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(content[start : end + 1])
        raise


_MAP_SYSTEM = (
    "You are a customer-support analyst for Hammer, a software company for car dealerships. "
    "You are given a numbered list of messages that DEALERSHIP CUSTOMERS sent to Hammer "
    "(from support-form submissions, inbound emails, inbound chat messages, or inbound phone "
    "calls). Each item may be prefixed with a [category] the customer selected.\n"
    "For EACH item, decide if it is a GENUINE customer-initiated support question or problem that "
    "the dealership needs Hammer to help solve — e.g. how-to, troubleshooting, an error or bug, "
    "leads not coming through, integration setup, tuning the AI's responses, billing/payment, "
    "account access, a cancellation request, or a configuration/feature request.\n"
    "Set s=false (NOT a customer support question) for anything that is internal, Hammer-initiated, "
    "or not actually a question, INCLUDING: Hammer-initiated outreach or processes (account reviews, "
    "onboarding, welcome/check-in calls, quality-control, proactive 'how are things going' or "
    "performance-review messages), sales / demos / upsell, scheduling a call or callback with no "
    "stated problem, simple acknowledgements ('thanks', 'approved', 'ok', 'sounds good'), call-log / "
    "voicemail dispositions, signatures and automated/system messages, a name or dealership name "
    "alone, blank or \"N/A\", spam, and test entries.\n"
    "Only mark s=true when the customer is actually asking for help or reporting a problem. When in "
    "doubt about whether a topic is something a dealership CUSTOMER would submit (vs. something "
    "Hammer's own team runs), set s=false.\n"
    "For support items (s=true), write the canonical question from the CUSTOMER'S point of view "
    "(5-9 words, plain language, no names or numbers), e.g. 'How do I change my AI response "
    "settings?' or 'Why aren't my leads coming through?'. Pick a category from: "
    "login, billing, integrations, dashboard, facebook-aia, marketposter, connect, leads, "
    "account, cancellation, training, other.\n"
    "Use 'cancellation' (NOT 'account' or 'billing') for ANY request to cancel, close, pause, "
    "suspend, downgrade, or stop the subscription/service, or to give notice / not be renewed.\n"
    'Return ONLY JSON: {"mappings":[{"i":<index>,"s":true|false,"q":"<question or empty>",'
    '"c":"<category>"}]} with one entry per input index.'
)

# Deterministic category override: any canonical question expressing intent to
# cancel/close/pause/suspend/downgrade/stop the service is force-tagged
# "cancellation" so it never gets scattered into account/billing by the LLM.
_CANCELLATION_RE = re.compile(
    r"\b("
    r"cancel\w*|uncancel\w*|unsubscrib\w*|terminat\w*|discontinu\w*|"
    r"close (my|our|the)\s+account|"
    r"pause (my|our|the)?\s*(service|account|ai|subscription|ads?)|"
    r"suspend\w*|downgrad\w*|"
    r"stop (my|our|the|the )?\s*(service|subscription|ai|ads?|billing|charges?)|"
    r"give (\w+\s+)?notice|(not|don'?t|do not|no longer)\s+(want to\s+)?renew|"
    r"end (my|our|the)\s+(subscription|service|account|trial)"
    r")\b",
    re.I,
)


def _is_cancellation_question(text: str) -> bool:
    return bool(_CANCELLATION_RE.search(text or ""))


_REDUCE_SYSTEM = (
    "You are consolidating a list of canonical customer-support questions for Hammer. "
    "Merge near-duplicates and rephrasings into a single canonical question. "
    "Group the provided labels: every input label must belong to exactly one group. "
    "Use the clearest customer-intent phrasing for each group's canonical question "
    "(5-9 words) and assign a category from: login, billing, integrations, dashboard, "
    "facebook-aia, marketposter, connect, leads, account, cancellation, training, other. "
    "Use 'cancellation' for any cancel/close/pause/suspend/downgrade/stop-service or "
    "notice-to-cancel request. Keep all cancellation questions grouped under cancellation. "
    'Return ONLY JSON: {"groups":[{"canonical":"<question>","category":"<category>",'
    '"members":["<label>", ...]}]}'
)


def _merge_example_tickets(
    bucket: list[dict[str, str]], new_tickets: list[dict[str, str]], cap: int
) -> None:
    """Append unique tickets (by ticket_id, else subject) into bucket up to cap."""
    seen = {(t.get("ticket_id") or t.get("subject") or "") for t in bucket}
    for t in new_tickets:
        if len(bucket) >= cap:
            break
        key = t.get("ticket_id") or t.get("subject") or ""
        if key and key in seen:
            continue
        seen.add(key)
        bucket.append(t)


def _map_one_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    numbered = "\n".join(f"{i}. {str(it['subject'])[:900]}" for i, it in enumerate(batch))
    try:
        data = _openai_json(
            [
                {"role": "system", "content": _MAP_SYSTEM},
                {"role": "user", "content": f"Ticket issue snippets:\n{numbered}"},
            ],
            max_tokens=8000,
        )
        return data.get("mappings") or []
    except Exception as exc:
        _log.warning("cs_questions map batch failed: %s", exc)
        return []


def _map_cache_key(item: dict[str, Any]) -> str:
    text = str(item.get("normalized_subject") or item.get("subject") or "")
    return _sha256(f"{_BUILD_VERSION}|{_model()}|{text}")


def _input_signature(items: list[dict[str, Any]]) -> str:
    parts = []
    for it in items:
        issue_hash = str(it.get("issue_hash") or _map_cache_key(it))
        parts.append(f"{issue_hash}:{int(it.get('count') or 0)}")
    return _sha256(f"{_BUILD_VERSION}|{_model()}|" + "\n".join(parts))


def _apply_mapping_to_labels(
    labels: dict[str, dict[str, Any]],
    item: dict[str, Any],
    mapping: dict[str, Any],
) -> None:
    if not bool(mapping.get("s", True)):
        return
    q = str(mapping.get("q") or "").strip()
    if not q:
        return
    cat = str(mapping.get("c") or "other").strip().lower() or "other"
    key = q.lower()
    bucket = labels.setdefault(
        key,
        {"label": key, "count": 0, "category": cat, "examples": []},
    )
    bucket["count"] += int(item.get("count") or 1)
    bucket.setdefault("category", cat)
    _merge_example_tickets(bucket["examples"], item.get("tickets") or [], _EXAMPLES_PER_QUESTION)


def _map_subjects_to_questions(items: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Map each issue snippet to a canonical label/category, using persistent cache."""
    from support_dashboard_store import get_cs_question_map_cache, set_cs_question_map_cache

    model = _model()
    for it in items:
        it["issue_hash"] = _map_cache_key(it)

    cached = get_cs_question_map_cache(model, [str(it.get("issue_hash") or "") for it in items])
    label_info: dict[str, dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []

    for it in items:
        key = str(it.get("issue_hash") or "")
        mapping = cached.get(key)
        if mapping is None:
            missing.append(it)
            continue
        _apply_mapping_to_labels(label_info, it, mapping)

    stats = {
        "map_cache_hits": len(items) - len(missing),
        "map_cache_misses": len(missing),
        "map_cache_writes": 0,
    }

    if not missing:
        return label_info, stats

    label_counts: Counter[str] = Counter()
    label_category: dict[str, str] = {}
    label_examples: dict[str, list[dict[str, str]]] = {}

    batches = [missing[s : s + MAP_BATCH_SIZE] for s in range(0, len(missing), MAP_BATCH_SIZE)]
    # Fan out the (network-bound) OpenAI calls; aggregate single-threaded for determinism.
    with ThreadPoolExecutor(max_workers=min(LLM_CONCURRENCY, len(batches) or 1)) as pool:
        all_mappings = list(pool.map(_map_one_batch, batches))

    cache_rows: list[dict[str, Any]] = []
    for batch, mappings in zip(batches, all_mappings):
        by_index: dict[int, dict[str, Any]] = {}
        for m in mappings:
            try:
                idx = int(m.get("i"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(batch):
                continue
            it = batch[idx]
            normalized = {
                "s": bool(m.get("s", True)),
                "q": str(m.get("q") or "").strip(),
                "c": str(m.get("c") or "other").strip().lower() or "other",
            }
            by_index[idx] = normalized
            cache_rows.append(
                {
                    "issue_hash": it.get("issue_hash"),
                    "issue_text": it.get("subject"),
                    **normalized,
                }
            )
            if not normalized["s"] or not normalized["q"]:
                continue
            key = normalized["q"].lower()
            label_counts[key] += int(it.get("count") or 1)
            label_category.setdefault(key, normalized["c"])
            ex = label_examples.setdefault(key, [])
            _merge_example_tickets(ex, it.get("tickets") or [], _EXAMPLES_PER_QUESTION)

        # Cache explicit false records for returned indices. Missing indices are not cached.
        for idx, it in enumerate(batch):
            if idx not in by_index:
                continue

    for key, cnt in label_counts.items():
        label_info[key] = {
            "label": key,
            "count": label_info.get(key, {}).get("count", 0) + cnt,
            "category": label_category.get(key, "other"),
            "examples": label_info.get(key, {}).get("examples", []),
        }
        _merge_example_tickets(label_info[key]["examples"], label_examples.get(key, []), _EXAMPLES_PER_QUESTION)

    set_cs_question_map_cache(model, cache_rows)
    stats["map_cache_writes"] = len(cache_rows)
    return label_info, stats


def _reduce_questions(labels: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Consolidate canonical labels into final grouped questions with summed counts."""
    label_list = list(labels.keys())
    if not label_list:
        return []

    groups_raw: list[dict[str, Any]] = []
    # Reduce in chunks if there are many labels, then a final merge pass.
    CHUNK = 120
    chunks = [label_list[s : s + CHUNK] for s in range(0, len(label_list), CHUNK)]

    def _reduce_one_chunk(chunk: list[str]) -> list[dict[str, Any]]:
        listing = "\n".join(f"- {lbl}" for lbl in chunk)
        try:
            data = _openai_json(
                [
                    {"role": "system", "content": _REDUCE_SYSTEM},
                    {"role": "user", "content": f"Labels:\n{listing}"},
                ],
                max_tokens=12000,
            )
            return data.get("groups") or []
        except Exception as exc:
            _log.warning("cs_questions reduce chunk failed: %s", exc)
            # Fallback: each label is its own group
            return [
                {"canonical": lbl, "category": labels[lbl]["category"], "members": [lbl]}
                for lbl in chunk
            ]

    with ThreadPoolExecutor(max_workers=min(LLM_CONCURRENCY, len(chunks) or 1)) as pool:
        for groups in pool.map(_reduce_one_chunk, chunks):
            groups_raw.extend(groups)

    # Build final questions by summing member counts + merging examples
    final: dict[str, dict[str, Any]] = {}
    assigned: set[str] = set()
    for g in groups_raw:
        canonical = str(g.get("canonical") or "").strip()
        if not canonical:
            continue
        ckey = canonical.lower()
        members = g.get("members") or []
        bucket = final.setdefault(
            ckey,
            {"question": canonical, "category": str(g.get("category") or "other").strip().lower(), "count": 0, "examples": []},
        )
        for mlbl in members:
            mkey = str(mlbl or "").strip().lower()
            if mkey in labels and mkey not in assigned:
                assigned.add(mkey)
                bucket["count"] += int(labels[mkey]["count"])
                _merge_example_tickets(
                    bucket["examples"], labels[mkey]["examples"], _EXAMPLES_PER_QUESTION
                )

    # Any labels the model failed to place — keep them standalone
    for mkey, info in labels.items():
        if mkey in assigned:
            continue
        bucket = final.setdefault(
            mkey,
            {"question": info["label"], "category": info["category"], "count": 0, "examples": []},
        )
        bucket["count"] += int(info["count"])
        _merge_example_tickets(bucket["examples"], info["examples"], _EXAMPLES_PER_QUESTION)

    # Deterministic safety net: force any cancel/pause/stop-service intent into the
    # dedicated "cancellation" category regardless of the LLM's choice.
    for bucket in final.values():
        if _is_cancellation_question(bucket.get("question", "")):
            bucket["category"] = "cancellation"

    ranked = sorted(final.values(), key=lambda x: x["count"], reverse=True)
    return ranked[:TOP_N]


def _compute_cs_questions() -> dict[str, Any]:
    rows = read_ticket_rows()
    total_tickets = len(rows)
    if not rows:
        return {
            "ok": False,
            "error": "No synced HubSpot tickets found. Run a HubSpot ticket sync first.",
        }

    aggregated, aggregate_stats = aggregate_subjects(rows)

    # Drop only entries that remain obvious non-support noise after trying to recover
    # useful issue context from the enriched ticket markdown.
    candidates = [it for it in aggregated if not looks_like_noise(it["subject"])]
    # Optionally skip the long tail of rare one-off subjects to speed up the build.
    if MIN_SUBJECT_COUNT > 1:
        candidates = [it for it in candidates if int(it.get("count") or 0) >= MIN_SUBJECT_COUNT]
    obvious_noise_filtered = sum(int(it["count"]) for it in aggregated) - sum(
        int(it["count"]) for it in candidates
    )

    inputs = candidates if MAX_INPUT_SUBJECTS <= 0 else candidates[:MAX_INPUT_SUBJECTS]
    for it in inputs:
        it["issue_hash"] = _map_cache_key(it)
    input_signature = _input_signature(inputs)
    candidate_volume = sum(int(it["count"]) for it in inputs)

    try:
        from support_dashboard_store import get_cs_questions_cache

        cached = get_cs_questions_cache() or {}
        if (
            cached.get("input_signature") == input_signature
            and cached.get("model") == _model()
            and cached.get("questions")
        ):
            out = dict(cached)
            out.update(
                {
                    "ok": True,
                    "cache_full_hit": True,
                    "map_cache_hits": len(inputs),
                    "map_cache_misses": 0,
                    "map_cache_writes": 0,
                    "total_tickets": total_tickets,
                    "candidate_tickets": candidate_volume,
                    "unique_subjects": len(aggregated),
                    "customer_initiated": aggregate_stats["customer_initiated"],
                    "excluded_internal": aggregate_stats["excluded_internal"],
                    "ticket_text_enriched": aggregate_stats["ticket_text_enriched"],
                }
            )
            return out
    except Exception:
        pass

    labels, map_stats = _map_subjects_to_questions(inputs)
    if not labels:
        return {"ok": False, "error": "Clustering failed — no labels produced (check OPENAI_API_KEY)."}

    # Volume of tickets the LLM classified as genuine support questions.
    support_volume = sum(int(info["count"]) for info in labels.values())
    llm_non_support_filtered = max(candidate_volume - support_volume, 0)

    questions = _reduce_questions(labels)
    for rank, q in enumerate(questions, start=1):
        q["rank"] = rank
        q["share"] = round((q["count"] / support_volume) * 100, 1) if support_volume else 0.0

    return {
        "ok": True,
        "questions": questions,
        "input_signature": input_signature,
        "total_tickets": total_tickets,
        "tickets_analyzed": support_volume,
        "candidate_tickets": candidate_volume,
        "non_support_filtered": obvious_noise_filtered + llm_non_support_filtered,
        "obvious_noise_filtered": obvious_noise_filtered,
        "llm_non_support_filtered": llm_non_support_filtered,
        "customer_initiated": aggregate_stats["customer_initiated"],
        "excluded_internal": aggregate_stats["excluded_internal"],
        "ticket_text_enriched": aggregate_stats["ticket_text_enriched"],
        **map_stats,
        "cache_full_hit": False,
        "unique_subjects": len(aggregated),
        "model": _model(),
    }


# ── Public API: cache + background rebuild ──────────────────────────────────

def get_cs_questions() -> dict[str, Any]:
    from support_dashboard_store import get_cs_questions_cache

    cached = get_cs_questions_cache()
    if not cached:
        return {
            "ok": True,
            "built": False,
            "running": _rebuild_running,
            "questions": [],
        }
    cached["running"] = _rebuild_running
    cached["built"] = bool(cached.get("questions"))
    return cached


def cs_questions_status() -> dict[str, Any]:
    from support_dashboard_store import get_cs_questions_cache

    cached = get_cs_questions_cache() or {}
    return {
        "ok": True,
        "running": _rebuild_running,
        "configured": cs_questions_configured(),
        "generated_at": cached.get("generated_at", ""),
        "tickets_analyzed": cached.get("tickets_analyzed", 0),
        "total_tickets": cached.get("total_tickets", 0),
        "count": len(cached.get("questions") or []),
        "last_error": cached.get("last_error", ""),
        "model": cached.get("model", ""),
    }


def _run_rebuild() -> None:
    global _rebuild_running
    from support_dashboard_store import set_cs_questions_cache

    try:
        result = _compute_cs_questions()
        if result.get("ok"):
            set_cs_questions_cache(
                {
                    "questions": result["questions"],
                    "input_signature": result.get("input_signature", ""),
                    "total_tickets": result["total_tickets"],
                    "tickets_analyzed": result["tickets_analyzed"],
                    "candidate_tickets": result.get("candidate_tickets", 0),
                    "non_support_filtered": result.get("non_support_filtered", 0),
                    "obvious_noise_filtered": result.get("obvious_noise_filtered", 0),
                    "llm_non_support_filtered": result.get("llm_non_support_filtered", 0),
                    "customer_initiated": result.get("customer_initiated", 0),
                    "excluded_internal": result.get("excluded_internal", 0),
                    "ticket_text_enriched": result.get("ticket_text_enriched", 0),
                    "map_cache_hits": result.get("map_cache_hits", 0),
                    "map_cache_misses": result.get("map_cache_misses", 0),
                    "map_cache_writes": result.get("map_cache_writes", 0),
                    "cache_full_hit": result.get("cache_full_hit", False),
                    "unique_subjects": result["unique_subjects"],
                    "model": result["model"],
                    "generated_at": _utc_now(),
                    "last_error": "",
                }
            )
        else:
            set_cs_questions_cache({"last_error": result.get("error", "Unknown error"), "generated_at": _utc_now()}, merge=True)
    except Exception as exc:
        _log.exception("cs_questions rebuild failed")
        try:
            set_cs_questions_cache({"last_error": str(exc), "generated_at": _utc_now()}, merge=True)
        except Exception:
            pass
    finally:
        _rebuild_running = False


def start_cs_questions_rebuild() -> dict[str, Any]:
    global _rebuild_running
    if not cs_questions_configured():
        return {"ok": False, "error": "OPENAI_API_KEY not configured"}
    with _rebuild_lock:
        if _rebuild_running:
            return {"ok": True, "started": False, "running": True, "message": "Rebuild already running"}
        _rebuild_running = True
    threading.Thread(target=_run_rebuild, daemon=True).start()
    return {"ok": True, "started": True, "running": True, "message": "CS questions rebuild started"}
