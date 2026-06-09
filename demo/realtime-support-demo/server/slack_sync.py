"""Slack channel sync — export threads to raw/support-data and synthesize wiki topics."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.I)


def _repo_root() -> Path:
    env = os.environ.get("SUPPORT_REPO_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def _slack_token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN", "").strip()


def _slack_channel_id() -> str:
    return os.environ.get("SLACK_SUPPORT_CHANNEL_ID", "").strip()


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def _state_db_path() -> Path:
    override = os.environ.get("SUPPORT_SLACK_STATE_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _is_serverless():
        return Path("/tmp/realtime-support-demo/slack_sync.sqlite")
    return _repo_root() / "knowledge_support" / "data" / "slack_sync.sqlite"


def _raw_slack_dir() -> Path:
    return _repo_root() / "raw" / "support-data" / "slack"


def _wiki_topics_dir() -> Path:
    return _repo_root() / "wiki-support" / "topics"


def _redact_pii(text: str) -> str:
    text = _EMAIL_RE.sub("[email-redacted]", text)
    text = _PHONE_RE.sub("[phone-redacted]", text)
    text = _VIN_RE.sub("[vin-redacted]", text)
    return text


def _init_state_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS slack_sync_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_sync_at TEXT NOT NULL DEFAULT '',
            last_message_ts TEXT NOT NULL DEFAULT '',
            thread_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT ''
        );
        INSERT OR IGNORE INTO slack_sync_state (id) VALUES (1);
        CREATE TABLE IF NOT EXISTS slack_thread_fingerprints (
            thread_ts TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL DEFAULT '',
            topic_file TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        """
    )


def _get_state() -> dict[str, Any]:
    path = _state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        _init_state_db(conn)
        conn.commit()
        row = conn.execute("SELECT * FROM slack_sync_state WHERE id = 1").fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def slack_sync_status() -> dict[str, Any]:
    state = _get_state()
    raw_dir = _raw_slack_dir()
    thread_files = list(raw_dir.glob("*.md")) if raw_dir.is_dir() else []
    return {
        "configured": bool(_slack_token() and _slack_channel_id()),
        "channel_id": _slack_channel_id() or None,
        "last_sync_at": state.get("last_sync_at") or None,
        "last_message_ts": state.get("last_message_ts") or None,
        "thread_count": len(thread_files),
        "last_error": state.get("last_error") or None,
        "raw_dir": str(raw_dir),
    }


@dataclass
class SlackMessage:
    ts: str
    user: str
    text: str
    thread_ts: str | None = None


def _slack_client():
    token = _slack_token()
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is not configured")
    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        raise RuntimeError("slack-sdk is required — pip install slack-sdk") from exc
    return WebClient(token=token)


def _fetch_channel_messages(client, channel_id: str, oldest: str | None = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": 200}
        if oldest:
            kwargs["oldest"] = oldest
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.conversations_history(**kwargs)
        messages.extend(resp.get("messages") or [])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return messages


def _fetch_thread_replies(client, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
    resp = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
    return list(resp.get("messages") or [])


def _message_to_slack_message(raw: dict[str, Any]) -> SlackMessage | None:
    text = str(raw.get("text") or "").strip()
    if not text or raw.get("subtype") in ("channel_join", "bot_message"):
        if not text:
            return None
    ts = str(raw.get("ts") or "")
    if not ts:
        return None
    return SlackMessage(
        ts=ts,
        user=str(raw.get("user") or raw.get("username") or "unknown"),
        text=_redact_pii(text),
        thread_ts=str(raw.get("thread_ts") or "") or None,
    )


def _thread_ts_for_message(msg: dict[str, Any]) -> str:
    if msg.get("thread_ts"):
        return str(msg["thread_ts"])
    reply_count = int(msg.get("reply_count") or 0)
    if reply_count > 0:
        return str(msg.get("ts") or "")
    return str(msg.get("ts") or "")


def _export_thread_markdown(thread_ts: str, messages: list[SlackMessage]) -> str:
    dt = datetime.fromtimestamp(float(thread_ts.split(".")[0]), tz=timezone.utc)
    participants = sorted({m.user for m in messages if m.user})
    lines = [
        "---",
        f"slack_ts: {thread_ts}",
        f"date: {dt.date().isoformat()}",
        f"participants: {json.dumps(participants)}",
        "source: slack",
        "---",
        "",
        f"# Slack thread {thread_ts}",
        "",
    ]
    for m in sorted(messages, key=lambda x: float(x.ts)):
        t = datetime.fromtimestamp(float(m.ts.split(".")[0]), tz=timezone.utc)
        lines.append(f"**{m.user}** ({t.strftime('%Y-%m-%d %H:%M UTC')}):")
        lines.append(m.text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _guess_topic_file(messages: list[SlackMessage]) -> str:
    blob = " ".join(m.text.lower() for m in messages)
    rules = [
        ("login", ("password", "login", "otp", "426637", "access", "reset")),
        ("billing", ("billing", "invoice", "payment", "card", "renewal", "charge")),
        ("integrations", ("integration", "crm", "dealertrack", "tekion", "vin", "cdk")),
        ("facebook-aia", ("facebook", "aia", "meta", "ads", "marketplace ad")),
        ("marketposter", ("marketposter", "chrome extension", "posting", "marketplace post")),
        ("hammer-connect", ("connect", "messaging", "sms thread", "marketplace message")),
        ("dashboard", ("dashboard", "inbox", "settings", "users", "reporting")),
    ]
    for slug, keywords in rules:
        if any(k in blob for k in keywords):
            return f"{slug}.md"
    return "general.md"


def _append_wiki_qa(topic_path: Path, question: str, answer: str, thread_ts: str) -> None:
    topic_path.parent.mkdir(parents=True, exist_ok=True)
    block = f"\n\n### {question.strip()}\n\n{answer.strip()}\n\n<!-- slack:{thread_ts} -->\n"
    prev = topic_path.read_text(encoding="utf-8") if topic_path.is_file() else (
        f"---\ntitle: {topic_path.stem.replace('-', ' ').title()} support Q&A\n"
        f"tags: [hammer, support, topics]\n---\n\n# {topic_path.stem.replace('-', ' ').title()}\n"
    )
    if f"<!-- slack:{thread_ts} -->" in prev:
        return
    topic_path.write_text(prev.rstrip() + block, encoding="utf-8")


def _synthesize_qa_with_gpt(messages: list[SlackMessage]) -> tuple[str, str] | None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.environ.get("SUPPORT_SLACK_SYNTH_MODEL", "gpt-4o-mini").strip()
    transcript = "\n".join(f"{m.user}: {m.text}" for m in messages)
    prompt = (
        "Extract one customer support question and a concise answer from this Slack thread. "
        "Return JSON with keys question and answer. If no clear Q&A, return question empty.\n\n"
        f"Thread:\n{transcript}"
    )
    try:
        import httpx

        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        q = str(data.get("question") or "").strip()
        a = str(data.get("answer") or "").strip()
        if q and a:
            return q, a
    except Exception:
        _log.exception("GPT synthesis failed")
    return None


def _update_wiki_log(count: int) -> None:
    log_path = _repo_root() / "wiki-support" / "log.md"
    if not log_path.is_file():
        return
    today = datetime.now(timezone.utc).date().isoformat()
    line = f"| {today} | Slack sync: {count} thread(s) processed |"
    text = log_path.read_text(encoding="utf-8")
    if line in text:
        return
    log_path.write_text(text.rstrip() + "\n" + line + "\n", encoding="utf-8")


def run_slack_sync(*, full_backfill: bool = False) -> dict[str, Any]:
    channel_id = _slack_channel_id()
    if not channel_id:
        raise RuntimeError("SLACK_SUPPORT_CHANNEL_ID is not configured")

    client = _slack_client()
    state = _get_state()
    oldest = None if full_backfill else (state.get("last_message_ts") or None)

    raw_dir = _raw_slack_dir()
    raw_dir.mkdir(parents=True, exist_ok=True)
    topics_dir = _wiki_topics_dir()
    topics_dir.mkdir(parents=True, exist_ok=True)

    messages = _fetch_channel_messages(client, channel_id, oldest=oldest)
    if not messages and not full_backfill:
        return {"ok": True, "threads_written": 0, "message": "No new messages"}

    threads_written = 0
    latest_ts = state.get("last_message_ts") or "0"
    processed_threads: set[str] = set()

    for raw in messages:
        ts = str(raw.get("ts") or "")
        if ts and float(ts) > float(latest_ts or "0"):
            latest_ts = ts
        thread_ts = _thread_ts_for_message(raw)
        if thread_ts in processed_threads:
            continue
        processed_threads.add(thread_ts)

        if int(raw.get("reply_count") or 0) > 0 or raw.get("thread_ts"):
            reply_raw = _fetch_thread_replies(client, channel_id, thread_ts)
        else:
            reply_raw = [raw]

        slack_msgs = [m for r in reply_raw if (m := _message_to_slack_message(r))]
        if not slack_msgs:
            continue

        md = _export_thread_markdown(thread_ts, slack_msgs)
        safe_name = thread_ts.replace(".", "_")
        out_path = raw_dir / f"{safe_name}.md"
        out_path.write_text(md, encoding="utf-8")
        threads_written += 1

        topic_file = _guess_topic_file(slack_msgs)
        qa = _synthesize_qa_with_gpt(slack_msgs)
        if qa:
            _append_wiki_qa(topics_dir / topic_file, qa[0], qa[1], thread_ts)

    state_path = _state_db_path()
    conn = sqlite3.connect(str(state_path))
    try:
        _init_state_db(conn)
        conn.execute(
            """
            UPDATE slack_sync_state SET
              last_sync_at = ?,
              last_message_ts = ?,
              thread_count = ?,
              last_error = ''
            WHERE id = 1
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                latest_ts,
                len(list(raw_dir.glob("*.md"))),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if threads_written:
        _update_wiki_log(threads_written)
        from knowledge_support.scripts.sync_sqlite import sync as rebuild_index

        rebuild_index(
            _repo_root() / "wiki-support",
            _repo_root() / "knowledge_support" / "data" / "support_kb.sqlite",
            support_raw_dir=_repo_root() / "raw" / "support-data",
            chunk_size=1200,
            chunk_overlap=150,
            full_wiki=False,
        )

    return {
        "ok": True,
        "threads_written": threads_written,
        "last_message_ts": latest_ts,
        "thread_count": len(list(raw_dir.glob("*.md"))),
    }
