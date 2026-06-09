"""Sync HubSpot Knowledge Base articles into raw/support-data/hubspot-kb/."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from hubspot_budget import (
    HubSpotBudgetExceeded,
    consume as _consume_hubspot_budget,
    remaining_today as _hubspot_budget_remaining,
)

_log = logging.getLogger(__name__)

_HUBSPOT_API = "https://api.hubapi.com"
_RAW_SUBDIR = "hubspot-kb"
# Broad terms — HubSpot site search requires a non-empty query; combine results and dedupe.
_SEARCH_TERMS = (
    "a_b_c_d_e_f_g_h_i_j_k_l_m_n_o_p_q",
    "hammer",
    "support",
    "account",
    "login",
    "billing",
    "integration",
    "facebook",
    "dashboard",
    "password",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_ARTICLE_BODY_RE = (
    re.compile(r'<div[^>]+class="[^"]*kb-article__body[^"]*"[^>]*>(.*?)</div>', re.I | re.S),
    re.compile(r'<div[^>]+class="[^"]*kb-article-body[^"]*"[^>]*>(.*?)</div>', re.I | re.S),
    re.compile(r"<article[^>]*>(.*?)</article>", re.I | re.S),
)
_PARA_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)


def _repo_root() -> Path:
    env = os.environ.get("SUPPORT_REPO_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def _hubspot_token() -> str:
    return (
        os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "").strip()
        or os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()
    )


def _portal_id() -> str:
    return os.environ.get("HUBSPOT_PORTAL_ID", "3355079").strip()


def _knowledge_base_id() -> str:
    return os.environ.get("HUBSPOT_KNOWLEDGE_BASE_ID", "206977575318").strip()


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def _state_db_path() -> Path:
    override = os.environ.get("SUPPORT_HUBSPOT_STATE_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _is_serverless():
        return Path("/tmp/realtime-support-demo/hubspot_kb_sync.sqlite")
    return _repo_root() / "knowledge_support" / "data" / "hubspot_kb_sync.sqlite"


def _raw_hubspot_dir() -> Path:
    return _repo_root() / "raw" / "support-data" / _RAW_SUBDIR


def _html_to_text(raw: str) -> str:
    if not raw.strip():
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r" +", " ", text)
    return _WS_RE.sub("\n\n", text.strip())


def _strip_inline_html(value: str) -> str:
    return _html_to_text(value) if "<" in value else value.strip()


def _extract_article_html(page_html: str) -> str:
    cleaned = _SCRIPT_STYLE_RE.sub(" ", page_html)
    for pattern in _ARTICLE_BODY_RE:
        match = pattern.search(cleaned)
        if match and len(match.group(1)) > 200:
            return _html_to_text(match.group(1))
    paras = [_html_to_text(p) for p in _PARA_RE.findall(cleaned)]
    paras = [p for p in paras if len(p) > 40]
    if paras:
        return "\n\n".join(paras)
    return _html_to_text(cleaned[:120_000])


def _slugify(title: str, article_id: str) -> str:
    slug = re.sub(r"[^\w\s-]+", "", title.lower(), flags=re.UNICODE)
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:80]
    return slug or f"article-{article_id}"


def _init_state_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hubspot_kb_sync_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_sync_at TEXT NOT NULL DEFAULT '',
            article_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT ''
        );
        INSERT OR IGNORE INTO hubspot_kb_sync_state (id) VALUES (1);
        CREATE TABLE IF NOT EXISTS hubspot_kb_articles (
            article_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            file_name TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        """
    )


def _extract_indexed_payload(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("fields"), dict):
        return data["fields"]
    if isinstance(data.get("content"), dict):
        inner = data["content"]
        if isinstance(inner.get("fields"), dict):
            return inner["fields"]
        return inner
    return data


def _article_matches_kb(url: str, fields: dict[str, Any]) -> bool:
    kb_id = _knowledge_base_id()
    if not kb_id:
        return True
    url_l = (url or "").lower()
    portal = _portal_id()
    if kb_id in url_l or f"/knowledge/{portal}/{kb_id}/" in url_l:
        return True
    # Public HubSpot-hosted KB pages (e.g. 3355079.hs-sites.com/migration/knowledge/...)
    if portal and f"{portal}.hs-sites.com" in url_l and "/knowledge/" in url_l:
        return True
    blob = json.dumps(fields, default=str).lower()
    return kb_id in blob


def _pick_text(fields: dict[str, Any]) -> str:
    for key in ("htmlBody", "body", "htmlContent", "content", "description", "summary", "snippet"):
        val = fields.get(key)
        if isinstance(val, str) and val.strip():
            return _html_to_text(val) if "<" in val else val.strip()
    return ""


def _pick_title(fields: dict[str, Any], fallback: str = "") -> str:
    for key in ("title", "htmlTitle", "name"):
        val = fields.get(key)
        if isinstance(val, str) and val.strip():
            return _html_to_text(val) if "<" in val else val.strip()
    return fallback or "Untitled article"


def _pick_url(fields: dict[str, Any], article_id: str) -> str:
    for key in ("url", "absoluteUrl", "absolute_url", "link"):
        val = fields.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    portal = _portal_id()
    kb = _knowledge_base_id()
    if portal and kb:
        return f"https://app.hubspot.com/knowledge/{portal}/{kb}/article/{article_id}"
    return ""


async def _search_article_ids_v2(
    client: httpx.AsyncClient,
    headers: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Legacy content search — works with typical private-app content scopes."""
    found: dict[str, dict[str, Any]] = {}
    portal = _portal_id()
    for term in _SEARCH_TERMS:
        offset = 0
        while True:
            _consume_hubspot_budget(1)
            resp = await client.get(
                f"{_HUBSPOT_API}/contentsearch/v2/search",
                headers=headers,
                params={
                    "portalId": portal,
                    "type": "KNOWLEDGE_ARTICLE",
                    "term": term,
                    "limit": 100,
                    "offset": offset,
                },
            )
            if resp.status_code == 401:
                raise RuntimeError("HubSpot auth failed — check HUBSPOT_PRIVATE_APP_TOKEN.")
            resp.raise_for_status()
            payload = resp.json()
            results = payload.get("results") or []
            if not results:
                break
            for row in results:
                aid = str(row.get("id") or "").strip()
                if aid:
                    found[aid] = row
            total = int(payload.get("total") or 0)
            offset += len(results)
            if offset >= total or len(results) < 100:
                break
    return found


async def _search_article_ids_v3(
    client: httpx.AsyncClient,
    headers: dict[str, str],
) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for term in _SEARCH_TERMS:
        offset = 0
        while True:
            _consume_hubspot_budget(1)
            resp = await client.get(
                f"{_HUBSPOT_API}/cms/v3/site-search/search",
                headers=headers,
                params={
                    "q": term,
                    "type": "KNOWLEDGE_ARTICLE",
                    "limit": 100,
                    "offset": offset,
                },
            )
            if resp.status_code in (401, 403):
                return {}
            resp.raise_for_status()
            payload = resp.json()
            results = payload.get("results") or []
            if not results:
                break
            for row in results:
                aid = str(row.get("id") or "").strip()
                if aid:
                    found[aid] = row
            total = int(payload.get("total") or 0)
            offset += len(results)
            if offset >= total or len(results) < 100:
                break
    return found


async def _search_article_ids(
    client: httpx.AsyncClient,
    headers: dict[str, str],
) -> tuple[str, dict[str, dict[str, Any]]]:
    found = await _search_article_ids_v3(client, headers)
    if found:
        return "v3", found
    return "v2", await _search_article_ids_v2(client, headers)


def _fields_from_v2_hit(hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _strip_inline_html(str(hit.get("title") or "")),
        "url": str(hit.get("url") or "").strip(),
        "description": _strip_inline_html(str(hit.get("description") or "")),
        "category": str(hit.get("category") or ""),
        "subcategory": str(hit.get("subcategory") or ""),
        "tags": hit.get("tags") or [],
    }


async def _fetch_indexed_article(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    article_id: str,
) -> dict[str, Any] | None:
    _consume_hubspot_budget(1)
    resp = await client.get(
        f"{_HUBSPOT_API}/cms/v3/site-search/indexed-data/{article_id}",
        headers=headers,
        params={"type": "KNOWLEDGE_ARTICLE"},
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def _fetch_public_body(client: httpx.AsyncClient, url: str) -> str:
    if not url.startswith("http"):
        return ""
    try:
        resp = await client.get(url, follow_redirects=True, timeout=30.0)
        if resp.status_code >= 400:
            return ""
        return _extract_article_html(resp.text)
    except Exception as exc:
        _log.debug("public fetch failed url=%s err=%s", url[:80], exc)
        return ""


def _write_article_markdown(
    dest: Path,
    *,
    article_id: str,
    title: str,
    url: str,
    category: str,
    subcategory: str,
    body: str,
) -> str:
    lines = [
        f"# {title}",
        "",
        f"source: hubspot-knowledge-base",
        f"hubspot_article_id: {article_id}",
        f"hubspot_portal_id: {_portal_id()}",
        f"hubspot_knowledge_base_id: {_knowledge_base_id()}",
    ]
    if url:
        lines.append(f"url: {url}")
    if category:
        lines.append(f"category: {category}")
    if subcategory:
        lines.append(f"subcategory: {subcategory}")
    lines.extend(["", body.strip(), ""])
    dest.write_text("\n".join(lines), encoding="utf-8")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _rebuild_kb_index() -> None:
    import sys

    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from knowledge_support.scripts.sync_sqlite import sync as rebuild_index
    rebuild_index(
        root / "wiki-support",
        root / "knowledge_support" / "data" / "support_kb.sqlite",
        support_raw_dir=root / "raw" / "support-data",
        chunk_size=1200,
        chunk_overlap=150,
        full_wiki=False,
    )


def hubspot_kb_sync_status() -> dict[str, Any]:
    token = _hubspot_token()
    state_path = _state_db_path()
    out: dict[str, Any] = {
        "configured": bool(token),
        "portal_id": _portal_id(),
        "knowledge_base_id": _knowledge_base_id(),
        "raw_dir": str(_raw_hubspot_dir()),
    }
    if not state_path.is_file():
        out.update({"last_sync_at": "", "article_count": 0, "last_error": ""})
        return out
    conn = sqlite3.connect(str(state_path))
    try:
        _init_state_db(conn)
        row = conn.execute(
            "SELECT last_sync_at, article_count, last_error FROM hubspot_kb_sync_state WHERE id = 1"
        ).fetchone()
        if row:
            out["last_sync_at"] = row[0]
            out["article_count"] = row[1]
            out["last_error"] = row[2]
    finally:
        conn.close()
    out["files_on_disk"] = len(list(_raw_hubspot_dir().glob("*.md"))) if _raw_hubspot_dir().is_dir() else 0
    return out


async def run_hubspot_kb_sync_async(*, fetch_public_html: bool = True) -> dict[str, Any]:
    token = _hubspot_token()
    if not token:
        return {
            "ok": False,
            "error": "Set HUBSPOT_PRIVATE_APP_TOKEN (or HUBSPOT_ACCESS_TOKEN) in server/.env",
        }

    if _hubspot_budget_remaining() <= 0:
        return {
            "ok": False,
            "budget_paused": True,
            "error": "Daily HubSpot API budget reached — KB sync paused until tomorrow (UTC).",
        }

    headers = {"Authorization": f"Bearer {token}"}
    raw_dir = _raw_hubspot_dir()
    raw_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            search_api, search_hits = await _search_article_ids(client, headers)
        except HubSpotBudgetExceeded:
            return {
                "ok": False,
                "budget_paused": True,
                "error": "Daily HubSpot API budget reached during KB search — paused until tomorrow (UTC).",
            }
        except Exception as exc:
            _log.exception("hubspot search failed")
            return {"ok": False, "error": str(exc)}

        if not search_hits:
            return {
                "ok": False,
                "error": "No knowledge articles found — verify token has 'content' scope and KB articles are published.",
            }

        for article_id, hit in search_hits.items():
            if _hubspot_budget_remaining() <= 0:
                errors.append("Paused: daily HubSpot budget reached during KB enrichment.")
                break
            try:
                fields: dict[str, Any]
                if hit.get("description") is not None or hit.get("domain"):
                    fields = _fields_from_v2_hit(hit)
                else:
                    indexed = await _fetch_indexed_article(client, headers, article_id)
                    if not indexed:
                        skipped += 1
                        continue
                    fields = _extract_indexed_payload(indexed)

                url = _pick_url(fields, article_id)
                if not _article_matches_kb(url, fields):
                    skipped += 1
                    continue
                title = _pick_title(fields, str(hit.get("title") or ""))
                summary = _strip_inline_html(str(fields.get("description") or ""))
                body = _pick_text(fields)
                if fetch_public_html and url:
                    public_body = await _fetch_public_body(client, url)
                    if len(public_body) > len(body):
                        body = public_body
                if summary and summary not in body:
                    body = f"{summary}\n\n{body}".strip() if body else summary
                if not body.strip():
                    skipped += 1
                    continue
                category = str(fields.get("category") or "")
                subcategory = str(fields.get("subcategory") or "")
                slug = _slugify(title, article_id)
                dest = raw_dir / f"{article_id}-{slug}.md"
                digest = _write_article_markdown(
                    dest,
                    article_id=article_id,
                    title=title,
                    url=url,
                    category=category,
                    subcategory=subcategory,
                    body=body,
                )
                written += 1
                state_path = _state_db_path()
                conn = sqlite3.connect(str(state_path))
                try:
                    _init_state_db(conn)
                    conn.execute(
                        """
                        INSERT INTO hubspot_kb_articles (article_id, title, url, content_hash, file_name, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(article_id) DO UPDATE SET
                          title=excluded.title,
                          url=excluded.url,
                          content_hash=excluded.content_hash,
                          file_name=excluded.file_name,
                          updated_at=excluded.updated_at
                        """,
                        (
                            article_id,
                            title,
                            url,
                            digest,
                            dest.name,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception as exc:
                _log.warning("hubspot article %s failed: %s", article_id, exc)
                errors.append(f"{article_id}: {exc}")

    if written:
        try:
            _rebuild_kb_index()
        except Exception as exc:
            _log.exception("kb rebuild failed after hubspot sync")
            errors.append(f"index rebuild: {exc}")

    now = datetime.now(timezone.utc).isoformat()
    state_path = _state_db_path()
    conn = sqlite3.connect(str(state_path))
    try:
        _init_state_db(conn)
        conn.execute(
            """
            UPDATE hubspot_kb_sync_state SET
              last_sync_at = ?,
              article_count = ?,
              last_error = ?
            WHERE id = 1
            """,
            (now, len(list(raw_dir.glob("*.md"))), "; ".join(errors[:3])),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "search_api": search_api,
        "articles_discovered": len(search_hits),
        "articles_written": written,
        "articles_skipped": skipped,
        "files_on_disk": len(list(raw_dir.glob("*.md"))),
        "errors": errors[:10],
        "last_sync_at": now,
    }


def run_hubspot_kb_sync(*, fetch_public_html: bool = True) -> dict[str, Any]:
    import asyncio

    return asyncio.run(run_hubspot_kb_sync_async(fetch_public_html=fetch_public_html))
