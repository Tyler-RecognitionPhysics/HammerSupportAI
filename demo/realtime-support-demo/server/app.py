"""Backend for Hammer Support AI — voice + chat with wiki-grounded retrieval."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent


def _load_local_dotenv() -> None:
    if os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_SERVER_DIR / ".env", override=True, encoding="utf-8-sig")


_load_local_dotenv()

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from functools import lru_cache

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from support_admin_auth import AdminAuthRequest, require_admin_auth, verify_admin_token

STATIC_DIR = _SERVER_DIR / "static"
_HAMMER_WORDMARK = STATIC_DIR / "hammer-wordmark.png"


def _find_repo_root() -> Path:
    env = os.environ.get("SUPPORT_REPO_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return _SERVER_DIR.parents[2]


REPO_ROOT = _find_repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SHARED_DIR = REPO_ROOT / "demo" / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from site_copy import load_site_copy  # noqa: E402

DEFAULT_WIKI_DIR = REPO_ROOT / "wiki-support"
DEFAULT_RAW_DIR = REPO_ROOT / "raw" / "support-data"


def _default_kb_db() -> Path:
    override = os.environ.get("SUPPORT_KB_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _is_production():
        return Path("/tmp/realtime-support-demo/support_kb.sqlite")
    return REPO_ROOT / "knowledge_support" / "data" / "support_kb.sqlite"


def _support_raw_dir() -> Path | None:
    override = os.environ.get("SUPPORT_RAW_DIR", "").strip()
    if override:
        p = Path(override).expanduser().resolve()
        return p if p.is_dir() else None
    return DEFAULT_RAW_DIR if DEFAULT_RAW_DIR.is_dir() else None


@lru_cache(maxsize=1)
def get_retriever():
    from kb_source_control import wrap_retriever
    from wiki_retrieval import SupportWikiRetriever

    wiki_dir = Path(os.environ.get("SUPPORT_WIKI_DIR", str(DEFAULT_WIKI_DIR))).resolve()
    db = _default_kb_db()
    pb_env = os.environ.get("SUPPORT_PLAYBOOK_MD", "").strip()
    playbook = Path(pb_env).expanduser().resolve() if pb_env else None
    from support_ticket_pins import pins_path

    inner = SupportWikiRetriever(
        wiki_dir,
        support_raw_dir=_support_raw_dir(),
        db_path=db,
        playbook_md_path=playbook,
        ticket_pins_path=pins_path(REPO_ROOT),
    )
    return wrap_retriever(inner)


def invalidate_support_retriever_cache() -> None:
    get_retriever.cache_clear()
    try:
        from support_agent import invalidate_executor_wiki

        invalidate_executor_wiki()
    except Exception:
        pass


def warm_support_retriever_in_background() -> None:
    """Rebuild the retriever index off the request thread.

    Playbook edits only need to write a small file; rebuilding the BM25 corpus
    is the slow part, so we do it asynchronously and let callers return
    immediately. The next /knowledge/ask will use the warmed index (or lazily
    rebuild if this hasn't finished yet)."""
    import threading

    def _warm() -> None:
        try:
            get_retriever()
        except Exception:
            logging.getLogger("support").exception("background retriever warm failed")

    threading.Thread(target=_warm, name="retriever-warm", daemon=True).start()


@lru_cache(maxsize=1)
def get_tool_executor():
    from support_tools import SupportToolExecutor

    return SupportToolExecutor(get_retriever)


def _cors_origins() -> list[str]:
    raw = os.environ.get(
        "SUPPORT_CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    )
    return [o.strip() for o in raw.split(",") if o.strip()]


def _is_production() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


async def _startup_warmup() -> None:
    from knowledge_support.kb_bootstrap import ensure_support_kb_database

    try:
        ensure_support_kb_database(REPO_ROOT, db_path=_default_kb_db())
        from support_dashboard_store import init_db

        init_db()
        await asyncio.to_thread(get_retriever)
    except Exception:
        logging.getLogger(__name__).exception("support warmup failed")


def _auto_sync_interval_hours() -> float:
    """Hours between automatic HubSpot ticket syncs. 0/unset disables the scheduler."""
    raw = os.environ.get("SUPPORT_AUTO_SYNC_INTERVAL_HOURS", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


async def _auto_sync_loop() -> None:
    """Background loop that keeps the HubSpot ticket index current on the persistent host."""
    interval_hours = _auto_sync_interval_hours()
    if interval_hours <= 0:
        return

    log = logging.getLogger(__name__)
    interval_seconds = interval_hours * 3600.0
    # Let warmup finish and the machine settle before the first run.
    await asyncio.sleep(float(os.environ.get("SUPPORT_AUTO_SYNC_START_DELAY_SECONDS", "120")))

    while True:
        try:
            from hubspot_tickets_sync import start_hubspot_tickets_sync_background

            result = await start_hubspot_tickets_sync_background(full_backfill=False)
            if result.get("started"):
                log.info("Auto-sync: HubSpot ticket sync started (interval %.1fh)", interval_hours)
            else:
                log.info("Auto-sync: skipped (%s)", result.get("message", "already running"))
        except Exception:
            log.exception("Auto-sync: HubSpot ticket sync failed")

        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On Vercel, do not block requests while the KB index builds (ElevenLabs times out).
    if _is_production():
        asyncio.create_task(_startup_warmup())
    else:
        await _startup_warmup()
    # Persistent sync host only: keep the ticket index fresh on a schedule.
    if not _is_production() and _auto_sync_interval_hours() > 0:
        asyncio.create_task(_auto_sync_loop())
    yield


app = FastAPI(title="Hammer Support AI", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)
    session_id: str | None = None


class SupportTicketRequest(BaseModel):
    dealership: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., min_length=3, max_length=200)
    phone: str = Field(..., min_length=7, max_length=32)
    message: str = Field(..., min_length=1, max_length=4000)
    first_name: str = Field(default="", max_length=80)
    last_name: str = Field(default="", max_length=80)
    contact_name: str = Field(default="", max_length=120)
    session_id: str = Field(default="", max_length=120)
    channel: str = Field(default="api", max_length=32)
    resolved: bool = False
    issue_category: str = Field(default="", max_length=64)


class PlaybookEntryRequest(BaseModel):
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)


class IngestTextRequest(BaseModel):
    filename: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)


class SlackSyncRequest(BaseModel):
    full_backfill: bool = False


@app.get("/api/health")
def health() -> dict:
    from hubspot_ticket_create import hubspot_ticket_create_configured
    from support_ticket_slack import slack_ticket_notify_configured

    return {
        "ok": True,
        "service": "hammer-support-ai",
        "openai_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "elevenlabs_configured": bool(
            os.environ.get("ELEVENLABS_API_KEY", "").strip()
            and os.environ.get("ELEVENLABS_AGENT_ID", "").strip()
        ),
        "hubspot_ticket_create_configured": hubspot_ticket_create_configured(),
        "slack_ticket_notify_configured": slack_ticket_notify_configured(),
    }


@app.get("/api/warmup")
async def warmup() -> dict:
    """Keep-warm target (hit by Vercel cron every 5 min). Warms everything the
    first voice turn needs so a real caller never pays the cold-start tax:
    KB database in /tmp, BM25 retriever, voice tool executor, and the pooled
    TLS connection to OpenAI."""
    import time as _time

    from knowledge_support.kb_bootstrap import ensure_support_kb_database

    timings: dict[str, int] = {}
    t0 = _time.perf_counter()
    try:
        ensure_support_kb_database(REPO_ROOT, db_path=_default_kb_db())
        timings["kb_ms"] = int((_time.perf_counter() - t0) * 1000)

        t1 = _time.perf_counter()
        await asyncio.to_thread(get_retriever)
        timings["retriever_ms"] = int((_time.perf_counter() - t1) * 1000)

        t2 = _time.perf_counter()
        from support_agent import _prewarm_openai_connection, prewarm_elevenlabs_session

        await prewarm_elevenlabs_session(get_retriever)
        await _prewarm_openai_connection()
        timings["agent_ms"] = int((_time.perf_counter() - t2) * 1000)
    except Exception as exc:
        logging.getLogger(__name__).exception("warmup failed")
        return {"ok": False, "error": str(exc), **timings}

    timings["total_ms"] = int((_time.perf_counter() - t0) * 1000)
    return {"ok": True, **timings}


@app.get("/api/branding/hammer-wordmark.png")
def hammer_wordmark() -> FileResponse:
    if not _HAMMER_WORDMARK.is_file():
        raise HTTPException(404, "hammer-wordmark.png not found")
    return FileResponse(_HAMMER_WORDMARK, media_type="image/png")


@app.get("/api/site_copy")
def site_copy() -> dict:
    wiki_dir = Path(os.environ.get("SUPPORT_WIKI_DIR", str(DEFAULT_WIKI_DIR))).resolve()
    return load_site_copy(wiki_dir)


@app.get("/api/elevenlabs/token")
async def elevenlabs_token() -> dict:
    from support_agent import handle_elevenlabs_token

    return await handle_elevenlabs_token(get_retriever)


@app.post("/api/elevenlabs/llm")
@app.post("/api/elevenlabs/llm/chat/completions")
@app.post("/api/elevenlabs/chat/completions")
async def elevenlabs_llm(request: Request) -> object:
    from support_agent import handle_elevenlabs_llm

    body = await request.json()
    return await handle_elevenlabs_llm(body, get_retriever)


@app.post("/api/elevenlabs/call-end")
async def elevenlabs_call_end(request: Request) -> dict:
    from support_elevenlabs_call_end import handle_support_elevenlabs_call_end

    raw = await request.body()
    sig = request.headers.get("ElevenLabs-Signature") or request.headers.get("elevenlabs-signature")
    try:
        event = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid JSON body") from exc
    return await handle_support_elevenlabs_call_end(raw, sig, event)


@app.post("/api/chat")
async def chat(body: ChatRequest) -> dict:
    import uuid
    from support_chat import complete_support_chat
    from support_dashboard_store import register_session_start, persist_session
    from support_tools import SupportSession

    session_id = body.session_id or f"chat-{uuid.uuid4().hex[:12]}"

    try:
        register_session_start(session_id, channel="chat")
    except Exception:
        pass

    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    session = SupportSession(call_id=session_id, channel="chat")

    from support_dashboard_store import hydrate_support_session

    try:
        hydrate_support_session(session, session_id)
    except Exception:
        pass

    try:
        reply = await complete_support_chat(get_tool_executor(), get_retriever(), messages, session=session)
        persist_session(session, messages, agent_reply=reply)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    return {
        "reply": reply,
        "session_id": session_id,
        "ticket_created": bool(getattr(session, "ticket_created", False)),
        "resolved": bool(getattr(session, "resolved", False)),
        "escalated": bool(getattr(session, "escalated", False)),
        "hubspot_ticket_id": str(getattr(session, "hubspot_ticket_id", "") or ""),
    }


@app.post("/api/voice/browser-call-start")
async def browser_call_start(request: Request) -> dict:
    data = await request.json()
    call_id = str(data.get("call_id") or data.get("conversation_id") or "").strip()
    if call_id:
        from support_dashboard_store import register_session_start

        register_session_start(call_id, channel="browser_voice")
    return {"ok": True}


@app.post("/api/support/ticket")
async def support_ticket(body: SupportTicketRequest) -> dict:
    from support_ticket_service import create_and_notify_ticket

    return await create_and_notify_ticket(
        {
            "dealership_name": body.dealership,
            "first_name": body.first_name,
            "last_name": body.last_name,
            "email": body.email,
            "phone": body.phone,
            "issue_summary": body.message,
            "session_id": body.session_id,
            "channel": body.channel,
            "resolved": body.resolved,
            "issue_category": body.issue_category,
        }
    )


def _dashboard_html() -> Path:
    return STATIC_DIR / "support-dashboard.html"


def _dashboard_css() -> Path:
    return STATIC_DIR / "support-dashboard.css"


@app.get("/admin/support/dashboard.css")
def admin_support_css() -> FileResponse:
    path = _dashboard_css()
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/css")


@app.get("/admin/support")
def admin_support_page() -> FileResponse:
    from support_admin_auth import admin_auth_configured

    if not admin_auth_configured():
        raise HTTPException(404)
    path = _dashboard_html()
    if not path.is_file():
        raise HTTPException(404, "Support dashboard UI missing")
    return FileResponse(path, media_type="text/html")


@app.post("/api/admin/support/auth")
def admin_auth(body: AdminAuthRequest) -> dict:
    if verify_admin_token(body.secret):
        return {"ok": True}
    raise HTTPException(401, "Invalid secret")


@app.get("/api/admin/support/overview")
def admin_overview(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_overview

    return dashboard_overview()


@app.get("/api/admin/support/calls")
def admin_calls(request: Request, limit: int = Query(100, ge=1, le=500)) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_calls

    return dashboard_calls(limit=limit)


@app.delete("/api/admin/support/sessions")
def admin_sessions_clear(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_clear_sessions

    return dashboard_clear_sessions()


@app.get("/api/admin/support/calls/{call_id}")
def admin_call_detail(request: Request, call_id: str) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_call_detail

    return dashboard_call_detail(call_id)


@app.get("/api/admin/support/tickets")
def admin_tickets(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_tickets

    return dashboard_tickets(limit=limit)


@app.get("/api/admin/support/tickets/billing")
def admin_billing_tickets(request: Request, limit: int = Query(500, ge=1, le=1000)) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_billing_tickets

    return dashboard_billing_tickets(limit=limit)


@app.post("/api/admin/support/tickets/billing/dismiss")
async def admin_billing_dismiss(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_billing_dismiss

    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    return dashboard_billing_dismiss(str(body.get("id") or ""))


@app.post("/api/admin/support/tickets/{ticket_id}/resolve")
async def admin_ticket_resolve(ticket_id: int, request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_ticket_set_resolved

    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    resolved = bool(body.get("resolved", True))
    return dashboard_ticket_set_resolved(ticket_id, resolved)


@app.post("/api/admin/support/sessions/coach/variations")
async def admin_session_coach_variations(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_response_variations

    body = await request.json()
    return dashboard_response_variations(
        str(body.get("user_message") or ""),
        str(body.get("original_response") or ""),
        str(body.get("draft") or ""),
    )


@app.post("/api/admin/support/sessions/{call_id}/resolve")
async def admin_session_resolve(call_id: str, request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_session_set_resolved

    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    resolved = bool(body.get("resolved", True))
    return dashboard_session_set_resolved(call_id, resolved)


@app.get("/api/admin/support/appointments")
def admin_appointments(
    request: Request,
    start: str = Query(""),
    end: str = Query(""),
    status: str = Query(""),
    limit: int = Query(500, ge=1, le=2000),
) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_appointments

    return dashboard_appointments(start=start, end=end, status=status, limit=limit)


@app.post("/api/admin/support/appointments")
async def admin_appointment_create(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import AppointmentCreate, dashboard_appointment_create

    body = AppointmentCreate(**(await request.json()))
    return dashboard_appointment_create(body)


@app.patch("/api/admin/support/appointments/{appointment_id}")
async def admin_appointment_update(request: Request, appointment_id: int) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import AppointmentUpdate, dashboard_appointment_update

    body = AppointmentUpdate(**(await request.json()))
    return dashboard_appointment_update(appointment_id, body)


@app.delete("/api/admin/support/appointments/{appointment_id}")
def admin_appointment_delete(request: Request, appointment_id: int) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_appointment_delete

    return dashboard_appointment_delete(appointment_id)


@app.get("/api/admin/support/cs-questions")
def admin_cs_questions(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_cs_questions

    return dashboard_cs_questions()


@app.get("/api/admin/support/cs-questions/status")
def admin_cs_questions_status(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_cs_questions_status

    return dashboard_cs_questions_status()


@app.post("/api/admin/support/cs-questions/rebuild")
def admin_cs_questions_rebuild(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_cs_questions_rebuild

    return dashboard_cs_questions_rebuild()


@app.get("/api/admin/support/qa")
def admin_qa(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_qa

    board = dashboard_qa()
    # Pre-fill answer boxes with AI drafts so the reviewer only edits/approves.
    # No-ops when nothing needs drafting or a run is already in progress.
    if board.get("built"):
        try:
            from support_qa import autostart_qa_drafts, qa_generation_status

            autostart_qa_drafts(get_retriever, get_tool_executor)
            board["generation"] = qa_generation_status()
        except Exception:
            logging.getLogger("support").exception("qa autodraft failed")
    return board


@app.post("/api/admin/support/qa/answer")
async def admin_qa_save(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import QaAnswerSave, dashboard_qa_save

    body = QaAnswerSave(**(await request.json()))
    return dashboard_qa_save(body)


@app.post("/api/admin/support/qa/generate")
async def admin_qa_generate(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import QaGenerateRequest, dashboard_qa_generate

    body = QaGenerateRequest(**(await request.json() if await request.body() else {}))
    return dashboard_qa_generate(body, get_retriever, get_tool_executor)


@app.get("/api/admin/support/qa/generate/status")
def admin_qa_generate_status(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_qa_generate_status

    return dashboard_qa_generate_status()


@app.post("/api/admin/support/qa/generate/cancel")
def admin_qa_generate_cancel(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_qa_generate_cancel

    return dashboard_qa_generate_cancel()


@app.post("/api/admin/support/qa/regenerate")
async def admin_qa_regenerate(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import QaAnswerSave, dashboard_qa_regenerate

    body = QaAnswerSave(**(await request.json()))
    try:
        await asyncio.to_thread(get_retriever)
        return await dashboard_qa_regenerate(body, get_retriever(), get_tool_executor())
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/admin/support/qa/approve-all")
def admin_qa_approve_all(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_qa_approve_all

    return dashboard_qa_approve_all()


@app.post("/api/admin/support/qa/discard")
async def admin_qa_discard(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import QaDiscardRequest, dashboard_qa_discard

    body = QaDiscardRequest(**(await request.json()))
    return dashboard_qa_discard(body)


@app.get("/api/admin/support/settings")
def admin_settings_get(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_settings_get

    return dashboard_settings_get()


@app.patch("/api/admin/support/settings")
async def admin_settings_patch(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import SupportSettingsPatch, dashboard_settings_patch

    body = SupportSettingsPatch(**(await request.json()))
    return dashboard_settings_patch(body)


@app.post("/api/admin/support/settings/reset")
def admin_settings_reset(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_settings_reset

    return dashboard_settings_reset()


@app.get("/api/admin/support/knowledge/stats")
def admin_knowledge_stats(request: Request) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import knowledge_stats

    return knowledge_stats(get_retriever(), REPO_ROOT)


@app.get("/api/admin/support/knowledge/sources")
def admin_knowledge_sources(request: Request) -> dict:
    require_admin_auth(request)
    from kb_source_control import knowledge_sources_state

    return knowledge_sources_state()


@app.patch("/api/admin/support/knowledge/sources")
async def admin_knowledge_sources_patch(request: Request) -> dict:
    require_admin_auth(request)
    from kb_source_control import knowledge_sources_state, set_kb_enabled_sources

    body = await request.json()
    patch = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(patch, dict):
        patch = body if isinstance(body, dict) else {}
    enabled = set_kb_enabled_sources({str(k): bool(v) for k, v in patch.items()})
    return {"ok": True, "enabled": enabled, **knowledge_sources_state()}


@app.get("/api/admin/support/knowledge/docs")
def admin_knowledge_docs(
    request: Request,
    ticket_offset: int = Query(0, ge=0),
    ticket_limit: int = Query(200, ge=0, le=500),
    tickets_only: bool = Query(False),
) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import list_knowledge_docs

    return list_knowledge_docs(
        get_retriever(),
        ticket_offset=ticket_offset,
        ticket_limit=ticket_limit,
        tickets_only=tickets_only,
    )


@app.get("/api/admin/support/knowledge/email-tickets")
def admin_knowledge_email_tickets(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=300),
    q: str = Query(""),
) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import list_email_worked_tickets

    return list_email_worked_tickets(get_retriever(), offset=offset, limit=limit, q=q)


@app.get("/api/admin/support/knowledge/doc")
def admin_knowledge_doc(request: Request, doc_id: str = Query(...)) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import get_doc_content

    return get_doc_content(get_retriever(), doc_id)


@app.get("/api/admin/support/knowledge/search")
def admin_knowledge_search(request: Request, q: str = Query(""), k: int = Query(8, ge=1, le=20)) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import search_knowledge

    return search_knowledge(get_retriever(), q, k=k)


@app.post("/api/admin/support/knowledge/ask")
async def admin_knowledge_ask(request: Request) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import ask_knowledge

    try:
        body = await request.json()
    except Exception:
        body = {}
    question = str(body.get("q") or body.get("query") or "").strip()
    if not question:
        raise HTTPException(400, "Question required.")
    try:
        await asyncio.to_thread(get_retriever)
        return await ask_knowledge(get_retriever(), get_tool_executor(), question)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/admin/support/knowledge/ask/regenerate")
async def admin_knowledge_ask_regenerate(request: Request) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import regenerate_knowledge

    try:
        body = await request.json()
    except Exception:
        body = {}
    question = str(body.get("q") or body.get("query") or "").strip()
    correct_info = str(body.get("correct_info") or body.get("content") or "").strip()
    if not question:
        raise HTTPException(400, "Question required.")
    if not correct_info:
        raise HTTPException(400, "Correct information required.")
    try:
        await asyncio.to_thread(get_retriever)
        return await regenerate_knowledge(get_retriever(), question, correct_info)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/admin/support/knowledge/playbook")
def admin_playbook_get(request: Request) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import get_playbook

    return get_playbook(REPO_ROOT)


def _append_and_index_playbook_entry(title: str, content: str) -> dict:
    """Append a playbook entry and make it live without a blocking rebuild.

    Keep this fast: never rebuild the whole BM25 corpus on the request thread.
    If the retriever is already warm we index the new entry in place (~50ms).
    If it is cold (cache cleared by another action), we just rebuild lazily
    off-thread — the entry is already on disk, so the background warm / next
    ask will pick it up."""
    from support_knowledge_api import append_playbook_entry

    result = append_playbook_entry(REPO_ROOT, title, content)
    if result.get("ok"):
        indexed = False
        if get_retriever.cache_info().currsize:
            try:
                adder = getattr(get_retriever(), "add_playbook_entry_to_index", None)
                if callable(adder):
                    indexed = bool(adder(title, content))
            except Exception:
                indexed = False
        # Drop the agent's cached wiki context so the new answer is used next ask.
        try:
            from support_agent import invalidate_executor_wiki

            invalidate_executor_wiki()
        except Exception:
            pass
        if not indexed:
            get_retriever.cache_clear()
            warm_support_retriever_in_background()
    return result


@app.post("/api/admin/support/knowledge/playbook/entry")
async def admin_playbook_append(request: Request) -> dict:
    require_admin_auth(request)
    body = PlaybookEntryRequest(**(await request.json()))
    return _append_and_index_playbook_entry(body.title, body.content)


@app.post("/api/admin/support/sessions/coach/save")
async def admin_session_coach_save(request: Request) -> dict:
    """Save a corrected session response as a context-aware playbook entry."""
    require_admin_auth(request)
    from support_dashboard_api import build_coach_playbook_entry

    body = await request.json()
    entry = build_coach_playbook_entry(
        trigger=str(body.get("trigger") or ""),
        trigger_edited=bool(body.get("trigger_edited")),
        original_response=str(body.get("original_response") or ""),
        corrected_response=str(body.get("corrected_response") or ""),
        context_turns=list(body.get("context_turns") or []),
    )
    if not entry.get("ok"):
        return entry
    result = _append_and_index_playbook_entry(entry["title"], entry["content"])
    if not result.get("ok"):
        return result
    return {"ok": True, "id": result.get("id"), "trigger": entry["trigger"], "context": entry["context"]}


@app.delete("/api/admin/support/knowledge/playbook/entry/{entry_id}")
def admin_playbook_delete(request: Request, entry_id: str) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import delete_playbook_entry

    result = delete_playbook_entry(REPO_ROOT, entry_id)
    if result.get("ok"):
        invalidate_support_retriever_cache()
        warm_support_retriever_in_background()
    return result


@app.put("/api/admin/support/knowledge/playbook/entry/{entry_id}")
async def admin_playbook_update(request: Request, entry_id: str) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import update_playbook_entry

    body = PlaybookEntryRequest(**(await request.json()))
    result = update_playbook_entry(REPO_ROOT, entry_id, body.title, body.content)
    if result.get("ok"):
        invalidate_support_retriever_cache()
        warm_support_retriever_in_background()
        try:
            from support_agent import invalidate_executor_wiki

            invalidate_executor_wiki()
        except Exception:
            pass
    return result


@app.get("/api/admin/support/knowledge/ticket-pins")
def admin_ticket_pins_get(request: Request) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import list_ticket_pins

    return list_ticket_pins(REPO_ROOT)


@app.post("/api/admin/support/knowledge/ticket-pins")
async def admin_ticket_pins_add(request: Request) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import add_ticket_pin

    try:
        body = await request.json()
    except Exception:
        body = {}
    topic = str(body.get("topic") or body.get("q") or "").strip()
    doc_id = str(body.get("doc_id") or body.get("ticket_doc_id") or "").strip()
    if not topic:
        raise HTTPException(400, "A question/topic is required.")
    if not doc_id:
        raise HTTPException(400, "A ticket is required.")
    result = add_ticket_pin(get_retriever(), REPO_ROOT, topic, doc_id)
    if result.get("ok"):
        invalidate_support_retriever_cache()
        warm_support_retriever_in_background()
    return result


@app.delete("/api/admin/support/knowledge/ticket-pins/{pin_id}")
def admin_ticket_pins_delete(request: Request, pin_id: str) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import delete_ticket_pin

    result = delete_ticket_pin(REPO_ROOT, pin_id)
    if result.get("ok"):
        invalidate_support_retriever_cache()
        warm_support_retriever_in_background()
    return result


@app.post("/api/admin/support/knowledge/ingest")
async def admin_knowledge_ingest(request: Request) -> dict:
    require_admin_auth(request)
    from support_knowledge_ingest import ingest_support_raw_from_text

    body = IngestTextRequest(**(await request.json()))
    result = ingest_support_raw_from_text(REPO_ROOT, filename=body.filename, markdown_content=body.content)
    if result.get("ok"):
        invalidate_support_retriever_cache()
        get_retriever()
    return result


@app.post("/api/admin/support/knowledge/slack/sync")
async def admin_slack_sync(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_slack_sync

    body = SlackSyncRequest(**(await request.json()))
    try:
        result = await dashboard_slack_sync(full_backfill=body.full_backfill)
        invalidate_support_retriever_cache()
        get_retriever()
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/admin/support/knowledge/slack/status")
def admin_slack_status(request: Request) -> dict:
    require_admin_auth(request)
    from slack_sync import slack_sync_status

    return slack_sync_status()


@app.post("/api/admin/support/knowledge/hubspot/sync")
async def admin_hubspot_kb_sync(request: Request) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_hubspot_kb_sync

    try:
        result = await dashboard_hubspot_kb_sync()
        if result.get("ok"):
            invalidate_support_retriever_cache()
            get_retriever()
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/admin/support/knowledge/hubspot/status")
def admin_hubspot_kb_status(request: Request) -> dict:
    require_admin_auth(request)
    from hubspot_kb_sync import hubspot_kb_sync_status

    return hubspot_kb_sync_status()


@app.post("/api/admin/support/knowledge/hubspot-tickets/sync")
async def admin_hubspot_tickets_sync(request: Request) -> dict:
    require_admin_auth(request)
    from support_kb_artifact_api import run_hubspot_tickets_sync_handler

    full_backfill = False
    background = True
    try:
        body = await request.json()
        full_backfill = bool(body.get("full_backfill"))
        background = bool(body.get("background", True))
    except Exception:
        pass

    try:
        return await run_hubspot_tickets_sync_handler(
            request,
            full_backfill=full_backfill,
            background=background,
            is_production=_is_production,
            invalidate_cache=invalidate_support_retriever_cache,
            warm_retriever=get_retriever,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/knowledge/artifact/manifest.json")
def admin_knowledge_artifact_manifest(request: Request):
    from support_kb_artifact_api import knowledge_artifact_manifest

    return knowledge_artifact_manifest(request)


@app.get("/api/knowledge/artifact/support_kb.sqlite")
def admin_knowledge_artifact_sqlite(request: Request):
    from support_kb_artifact_api import knowledge_artifact_sqlite

    return knowledge_artifact_sqlite(request)


@app.get("/api/knowledge/artifact/hubspot_tickets_sync.sqlite")
def admin_knowledge_artifact_tickets_state(request: Request):
    from support_kb_artifact_api import knowledge_artifact_tickets_state

    return knowledge_artifact_tickets_state(request)


@app.get("/api/admin/support/knowledge/hubspot-tickets/status")
def admin_hubspot_tickets_status(request: Request) -> dict:
    require_admin_auth(request)
    from hubspot_tickets_sync import hubspot_tickets_sync_status

    return hubspot_tickets_sync_status()


@app.post("/api/admin/support/knowledge/reload")
def admin_knowledge_reload(request: Request) -> dict:
    require_admin_auth(request)
    from knowledge_support.kb_bootstrap import ensure_support_kb_database

    try:
        ensure_support_kb_database(REPO_ROOT, db_path=_default_kb_db(), force=_is_production())
        invalidate_support_retriever_cache()
        get_retriever()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


if not _is_production():

    @app.get("/debug/support-dashboard")
    def debug_dashboard() -> FileResponse:
        path = _dashboard_html()
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="text/html")

    @app.get("/debug/support-dashboard.css")
    def debug_dashboard_css() -> FileResponse:
        return admin_support_css()
