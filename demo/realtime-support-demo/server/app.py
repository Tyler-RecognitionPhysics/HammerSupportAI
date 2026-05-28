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
DEFAULT_KB_DB = REPO_ROOT / "knowledge_support" / "data" / "support_kb.sqlite"
DEFAULT_RAW_DIR = REPO_ROOT / "raw" / "support-data"


def _support_raw_dir() -> Path | None:
    override = os.environ.get("SUPPORT_RAW_DIR", "").strip()
    if override:
        p = Path(override).expanduser().resolve()
        return p if p.is_dir() else None
    return DEFAULT_RAW_DIR if DEFAULT_RAW_DIR.is_dir() else None


@lru_cache(maxsize=1)
def get_retriever():
    from wiki_retrieval import SupportWikiRetriever

    wiki_dir = Path(os.environ.get("SUPPORT_WIKI_DIR", str(DEFAULT_WIKI_DIR))).resolve()
    db = Path(os.environ.get("SUPPORT_KB_DB", str(DEFAULT_KB_DB))).resolve()
    pb_env = os.environ.get("SUPPORT_PLAYBOOK_MD", "").strip()
    playbook = Path(pb_env).expanduser().resolve() if pb_env else None
    return SupportWikiRetriever(
        wiki_dir,
        support_raw_dir=_support_raw_dir(),
        db_path=db,
        playbook_md_path=playbook,
    )


def invalidate_support_retriever_cache() -> None:
    get_retriever.cache_clear()
    try:
        from support_agent import invalidate_executor_wiki

        invalidate_executor_wiki()
    except Exception:
        pass


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    from knowledge_support.kb_bootstrap import ensure_support_kb_database

    ensure_support_kb_database(REPO_ROOT)
    try:
        from support_dashboard_store import init_db

        init_db()
    except Exception:
        logging.getLogger(__name__).exception("dashboard init failed")
    await asyncio.to_thread(get_retriever)
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


class SupportTicketRequest(BaseModel):
    dealership: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., min_length=3, max_length=200)
    phone: str = Field(..., min_length=7, max_length=32)
    message: str = Field(..., min_length=1, max_length=4000)
    contact_name: str = Field(default="", max_length=120)


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
    return {
        "ok": True,
        "service": "hammer-support-ai",
        "openai_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "elevenlabs_configured": bool(
            os.environ.get("ELEVENLABS_API_KEY", "").strip()
            and os.environ.get("ELEVENLABS_AGENT_ID", "").strip()
        ),
    }


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


@app.post("/api/chat")
async def chat(body: ChatRequest) -> dict:
    from support_chat import complete_support_chat

    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    try:
        reply = await complete_support_chat(get_tool_executor(), get_retriever(), messages)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"reply": reply}


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
    from support_dashboard_store import create_support_ticket

    return create_support_ticket(
        dealership=body.dealership,
        email=body.email,
        phone=body.phone,
        message=body.message,
        contact_name=body.contact_name,
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


@app.get("/api/admin/support/calls/{call_id}")
def admin_call_detail(request: Request, call_id: str) -> dict:
    require_admin_auth(request)
    from support_dashboard_api import dashboard_call_detail

    return dashboard_call_detail(call_id)


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


@app.get("/api/admin/support/knowledge/docs")
def admin_knowledge_docs(request: Request) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import list_knowledge_docs

    return list_knowledge_docs(get_retriever())


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


@app.get("/api/admin/support/knowledge/playbook")
def admin_playbook_get(request: Request) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import get_playbook

    return get_playbook(REPO_ROOT)


@app.post("/api/admin/support/knowledge/playbook/entry")
async def admin_playbook_append(request: Request) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import append_playbook_entry

    body = PlaybookEntryRequest(**(await request.json()))
    result = append_playbook_entry(REPO_ROOT, body.title, body.content)
    if result.get("ok"):
        invalidate_support_retriever_cache()
        get_retriever()
    return result


@app.delete("/api/admin/support/knowledge/playbook/entry/{entry_id}")
def admin_playbook_delete(request: Request, entry_id: str) -> dict:
    require_admin_auth(request)
    from support_knowledge_api import delete_playbook_entry

    result = delete_playbook_entry(REPO_ROOT, entry_id)
    if result.get("ok"):
        invalidate_support_retriever_cache()
        get_retriever()
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
