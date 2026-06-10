"""Questions & Answers board.

Surfaces the top 3-5 customer questions per category (from the CS Questions
clustering) and lets Customer Support staff write the canonical answer for each.
These curated answers can later feed the AI's knowledge base.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable

_log = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _top_per_category() -> int:
    """How many top questions to surface per category (3-5)."""
    try:
        n = int(os.environ.get("QA_TOP_PER_CATEGORY", "5") or "5")
    except ValueError:
        n = 5
    return max(3, min(5, n))


def question_key(question: str) -> str:
    """Stable key for a canonical question so answers survive CS Questions rebuilds."""
    norm = " ".join((question or "").strip().lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


# ── Duplicate-scenario de-duplication ───────────────────────────────────────
# CS Questions clustering still leaves near-duplicate phrasings of the same
# customer scenario (e.g. several "cancel my <product>" variants). For the Q&A
# board we collapse questions that a support agent would answer the same way,
# combining their ticket volume. A deterministic pass merges trivially-identical
# phrasings; an LLM pass then merges same-scenario questions within a category.
# Results are cached by a signature of the question set so this never runs on the
# hot render/poll path — only when the underlying CS Questions change.

_DEDUP_VERSION = "qa-dedup-v1"
_dedup_lock = threading.Lock()

_QA_STOPWORDS = frozenset(
    {
        "a", "an", "the", "to", "of", "for", "in", "on", "at", "is", "it", "do",
        "does", "did", "i", "we", "you", "my", "our", "your", "us", "me", "can",
        "could", "would", "should", "please", "help", "with", "how", "what",
        "when", "why", "there", "any", "some", "get", "im", "am", "are", "be",
        "been", "being", "this", "that", "these", "those", "and", "or", "but",
        "if", "so", "as", "about", "need", "want", "have", "has", "had", "will",
    }
)


def _dedup_signature(text: str) -> str:
    """Normalized content fingerprint: lowercased, de-stopworded, lightly stemmed."""
    s = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    toks: list[str] = []
    for w in s.split():
        if w in _QA_STOPWORDS:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 5 and w.endswith("ing"):
            w = w[:-3]
        elif len(w) > 4 and w.endswith("es"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        toks.append(w)
    return " ".join(sorted(set(toks)))


def _deterministic_dedup_map(candidates: list[dict[str, Any]]) -> dict[str, str]:
    """Map each question text to a canonical text by (category, content signature)."""
    best: dict[tuple[str, str], int] = {}
    for i, c in enumerate(candidates):
        key = (c["category"], _dedup_signature(c["question"]))
        cur = best.get(key)
        cnt = int(c.get("count") or 0)
        if (
            cur is None
            or cnt > int(candidates[cur].get("count") or 0)
            or (
                cnt == int(candidates[cur].get("count") or 0)
                and len(c["question"]) < len(candidates[cur]["question"])
            )
        ):
            best[key] = i
    mapping: dict[str, str] = {}
    for c in candidates:
        key = (c["category"], _dedup_signature(c["question"]))
        mapping[c["question"]] = candidates[best[key]]["question"]
    return mapping


_DEDUP_SYSTEM = (
    "You de-duplicate customer-support questions for a knowledge base. You are given a "
    "numbered list of questions, each tagged with a category. Group together questions that "
    "represent the SAME underlying customer scenario — one a support agent would answer with "
    "essentially the same response. ONLY group questions that share the exact same category. "
    "Do NOT merge questions whose correct answer would meaningfully differ (for example, "
    "cancelling advertising via a special email vs. closing the entire account are different "
    "scenarios). When in doubt, keep them separate. For each group choose the clearest, most "
    "general question as the canonical one. Every id must appear in exactly one group. "
    'Return ONLY JSON: {"groups":[{"canonical":<id>,"members":[<id>,...]}]}.'
)


def _llm_dedup_groups(reps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from cs_questions import _openai_json

    numbered = "\n".join(f'{i}. [{r["category"]}] {r["question"]}' for i, r in enumerate(reps))
    data = _openai_json(
        [
            {"role": "system", "content": _DEDUP_SYSTEM},
            {"role": "user", "content": f"Questions:\n{numbered}"},
        ],
        max_tokens=4000,
    )
    return data.get("groups") or []


def _apply_llm_groups(reps: list[dict[str, Any]], groups: list[dict[str, Any]]) -> dict[str, str]:
    """Map each representative text to its canonical text, enforcing same-category merges."""
    mapping: dict[str, str] = {}
    assigned: set[int] = set()
    for g in groups:
        member_idx: list[int] = []
        for m in [g.get("canonical"), *(g.get("members") or [])]:
            try:
                idx = int(m)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(reps) and idx not in member_idx:
                member_idx.append(idx)
        if not member_idx:
            continue
        try:
            can = int(g.get("canonical"))
        except (TypeError, ValueError):
            can = member_idx[0]
        if not (0 <= can < len(reps)):
            can = member_idx[0]
        can_cat = reps[can]["category"]
        for idx in member_idx:
            if idx in assigned:
                continue
            # Never merge across categories even if the model suggests it.
            if reps[idx]["category"] != can_cat:
                continue
            assigned.add(idx)
            mapping[reps[idx]["question"]] = reps[can]["question"]
    for i, r in enumerate(reps):
        mapping.setdefault(r["question"], r["question"])
    return mapping


def _compute_qa_dedup_map(candidates: list[dict[str, Any]]) -> dict[str, str]:
    """Full text->canonical map: deterministic collapse, then LLM scenario merge."""
    det = _deterministic_dedup_map(candidates)

    # Build one representative per deterministic cluster (with summed volume).
    cat_by_text = {c["question"]: c["category"] for c in candidates}
    rep_count: dict[str, int] = {}
    for c in candidates:
        rep = det[c["question"]]
        rep_count[rep] = rep_count.get(rep, 0) + int(c.get("count") or 0)
    reps = [
        {"question": rep, "category": cat_by_text.get(rep, "other"), "count": cnt}
        for rep, cnt in rep_count.items()
    ]
    reps.sort(key=lambda r: r["count"], reverse=True)

    rep_map = {r["question"]: r["question"] for r in reps}
    if os.environ.get("OPENAI_API_KEY", "").strip() and len(reps) > 1:
        try:
            rep_map.update(_apply_llm_groups(reps, _llm_dedup_groups(reps)))
        except Exception as exc:  # noqa: BLE001 - dedup is best-effort
            _log.warning("qa dedup LLM pass failed, using deterministic only: %s", exc)

    return {c["question"]: rep_map.get(det[c["question"]], det[c["question"]]) for c in candidates}


def _qa_dedup_signature(candidates: list[dict[str, Any]]) -> str:
    parts = sorted(
        f'{c["category"]}|{c["question"]}|{int(c.get("count") or 0)}' for c in candidates
    )
    raw = f"{_DEDUP_VERSION}||" + "\n".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def get_qa_dedup_map(candidates: list[dict[str, Any]]) -> dict[str, str]:
    """Return a cached text->canonical map, recomputing only when the question set changes."""
    from support_dashboard_store import get_qa_dedup_cache, set_qa_dedup_cache

    if not candidates:
        return {}
    sig = _qa_dedup_signature(candidates)
    cached = get_qa_dedup_cache()
    if cached and cached.get("signature") == sig:
        return cached.get("map") or {}
    with _dedup_lock:
        cached = get_qa_dedup_cache()
        if cached and cached.get("signature") == sig:
            return cached.get("map") or {}
        mapping = _compute_qa_dedup_map(candidates)
        try:
            set_qa_dedup_cache(sig, mapping)
        except Exception:  # noqa: BLE001
            pass
        return mapping


def _best_saved_answer(answers: dict[str, Any], member_texts: list[str]) -> dict[str, Any]:
    """Pick the most authoritative saved answer among a merged group's members."""
    found = [answers.get(question_key(t)) for t in member_texts]
    found = [s for s in found if s]
    if not found:
        return {}
    approved = [s for s in found if str(s.get("status")) == "approved" and str(s.get("answer") or "").strip()]
    if approved:
        return approved[0]
    drafts = [s for s in found if str(s.get("status")) == "draft" and str(s.get("answer") or "").strip()]
    if drafts:
        return drafts[0]
    return found[0]


def get_qa_board() -> dict[str, Any]:
    """Group the top questions per category and merge in any saved answers."""
    from cs_questions import get_cs_questions
    from support_dashboard_store import get_qa_answers

    cs = get_cs_questions()
    questions = list(cs.get("questions") or [])
    if not questions:
        return {
            "ok": True,
            "built": False,
            "running": bool(cs.get("running")),
            "categories": [],
            "answered": 0,
            "total": 0,
            "generated_at": cs.get("generated_at", ""),
        }

    answers = get_qa_answers()
    top_n = _top_per_category()

    # Collapse duplicate scenarios (same question asked many ways) before ranking.
    candidates = [
        {
            "question": str(q.get("question") or "").strip(),
            "category": str(q.get("category") or "other").strip().lower() or "other",
            "count": int(q.get("count") or 0),
        }
        for q in questions
        if str(q.get("question") or "").strip()
    ]
    dedup_map = get_qa_dedup_map(candidates)

    # Group by category, merging duplicates into their canonical question.
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for q in questions:
        text = str(q.get("question") or "").strip()
        if not text:
            continue
        cat = str(q.get("category") or "other").strip().lower() or "other"
        canonical = dedup_map.get(text, text)
        bucket = grouped.setdefault(cat, {})
        item = bucket.get(canonical)
        cnt = int(q.get("count") or 0)
        share = float(q.get("share") or 0)
        if item is None:
            bucket[canonical] = {
                "question": canonical,
                "count": cnt,
                "share": share,
                "members": [text],
            }
        else:
            item["count"] += cnt
            item["share"] += share
            item["members"].append(text)

    categories: list[dict[str, Any]] = []
    answered = 0
    drafts = 0
    total = 0
    for cat, bucket in grouped.items():
        items = sorted(bucket.values(), key=lambda x: x["count"], reverse=True)
        top_items = items[:top_n]
        cat_volume = sum(int(x["count"]) for x in items)
        out_questions: list[dict[str, Any]] = []
        for it in top_items:
            text = it["question"]
            key = question_key(text)
            saved = _best_saved_answer(answers, it.get("members") or [text])
            answer = str(saved.get("answer") or "")
            status = str(saved.get("status") or ("approved" if answer.strip() else ""))
            is_approved = status == "approved" and bool(answer.strip())
            is_draft = status == "draft" and bool(answer.strip())
            total += 1
            if is_approved:
                answered += 1
            if is_draft:
                drafts += 1
            out_questions.append(
                {
                    "key": key,
                    "question": text,
                    "category": cat,
                    "count": int(it["count"]),
                    "share": round(float(it.get("share") or 0), 1),
                    "answer": answer,
                    "answered": is_approved,
                    "status": status if status else "empty",
                    "has_draft": is_draft,
                    "ai_generated": str(saved.get("source") or "") == "ai",
                    "sources": saved.get("sources") or [],
                    "merged_count": len(it.get("members") or []),
                    "updated_at": saved.get("updated_at", ""),
                    "updated_by": saved.get("updated_by", ""),
                }
            )
        if out_questions:
            categories.append(
                {
                    "category": cat,
                    "volume": cat_volume,
                    "answered": sum(1 for x in out_questions if x["answered"]),
                    "drafts": sum(1 for x in out_questions if x["has_draft"]),
                    "total": len(out_questions),
                    "questions": out_questions,
                }
            )

    # Most-asked categories first.
    categories.sort(key=lambda c: c["volume"], reverse=True)

    return {
        "ok": True,
        "built": True,
        "running": bool(cs.get("running")),
        "categories": categories,
        "answered": answered,
        "drafts": drafts,
        "total": total,
        "top_per_category": top_n,
        "generated_at": cs.get("generated_at", ""),
        "generation": qa_generation_status(),
    }


def save_qa_answer(
    *,
    key: str,
    question: str,
    category: str,
    answer: str,
    updated_by: str = "",
    status: str = "approved",
    source: str = "human",
) -> dict[str, Any]:
    from support_dashboard_store import set_qa_answer

    key = (key or "").strip() or question_key(question)
    if not question.strip():
        return {"ok": False, "error": "Question text required."}
    row = set_qa_answer(
        question_key=key,
        question=question,
        category=category,
        answer=answer,
        updated_by=updated_by,
        status=status,
        source=source,
    )
    return {"ok": True, "answer": row}


# ── AI auto-drafting ────────────────────────────────────────────────────────
# Hannah drafts answers for the top questions using the same wiki-grounded
# pipeline she uses live. Drafts are saved with status="draft" so a human must
# review/approve them before they become the source of truth (status="approved").

_gen_lock = threading.Lock()
_gen_state: dict[str, Any] = {
    "running": False,
    "total": 0,
    "done": 0,
    "generated": 0,
    "failed": 0,
    "started_at": "",
    "finished_at": "",
    "last_error": "",
}


def qa_generation_status() -> dict[str, Any]:
    return dict(_gen_state)


def _compact_sources(raw_sources: Any, *, limit: int = 6) -> list[dict[str, Any]]:
    """Keep a small, display-friendly slice of the AI's cited sources."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in (raw_sources or []):
        doc_id = str(s.get("doc_id") or "").strip()
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        title = str(s.get("title") or s.get("doc_title") or "").strip()
        out.append(
            {
                "doc_id": doc_id,
                "title": title or doc_id,
                "source_group": str(s.get("source_group") or ""),
            }
        )
        if len(out) >= limit:
            break
    return out


async def generate_and_store_draft(
    retriever: Any,
    executor: Any,
    *,
    key: str,
    question: str,
    category: str = "other",
) -> dict[str, Any]:
    """Draft an answer for one question with Hannah's pipeline and save it as a draft."""
    from support_chat import preview_support_response
    from support_dashboard_store import set_qa_answer

    q = (question or "").strip()
    if not q:
        return {"ok": False, "error": "Question required."}

    result = await preview_support_response(executor, retriever, q)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or "Generation failed."}

    answer = str(result.get("response") or "").strip()
    if not answer:
        return {"ok": False, "error": "The AI could not draft an answer for this question."}

    key = (key or "").strip() or question_key(q)
    sources = _compact_sources(result.get("sources"))
    row = set_qa_answer(
        question_key=key,
        question=q,
        category=category or "other",
        answer=answer,
        updated_by="ai",
        status="draft",
        source="ai",
        sources=sources,
    )
    return {
        "ok": True,
        "key": key,
        "answer": answer,
        "sources": sources,
        "model": result.get("model", ""),
        "row": row,
    }


def _collect_targets(scope: str, keys: list[str] | None) -> list[dict[str, str]]:
    """Resolve which questions to draft answers for."""
    board = get_qa_board()
    all_questions: list[dict[str, Any]] = []
    for cat in board.get("categories") or []:
        all_questions.extend(cat.get("questions") or [])

    if keys:
        wanted = {k for k in keys if k}
        chosen = [q for q in all_questions if q.get("key") in wanted]
    elif scope == "all":
        # (Re)draft anything not already human-approved.
        chosen = [q for q in all_questions if not q.get("answered")]
    else:  # "unanswered" (default): skip approved answers and existing drafts.
        chosen = [
            q for q in all_questions
            if not q.get("answered") and not q.get("has_draft")
        ]

    return [
        {
            "key": str(q.get("key") or ""),
            "question": str(q.get("question") or ""),
            "category": str(q.get("category") or "other"),
        }
        for q in chosen
        if q.get("question")
    ]


def _run_qa_generation(
    get_retriever: Callable[[], Any],
    get_tool_executor: Callable[[], Any],
    targets: list[dict[str, str]],
) -> None:
    try:
        retriever = get_retriever()
        executor = get_tool_executor()
        for t in targets:
            if not _gen_state.get("running"):
                break  # cancelled
            try:
                res = asyncio.run(
                    generate_and_store_draft(
                        retriever,
                        executor,
                        key=t["key"],
                        question=t["question"],
                        category=t["category"],
                    )
                )
                if res.get("ok"):
                    _gen_state["generated"] += 1
                else:
                    _gen_state["failed"] += 1
                    _gen_state["last_error"] = res.get("error", "")
            except Exception as exc:  # noqa: BLE001 - keep the batch going
                _log.warning("qa draft generation failed for %s: %s", t.get("key"), exc)
                _gen_state["failed"] += 1
                _gen_state["last_error"] = str(exc)
            finally:
                _gen_state["done"] += 1
    except Exception as exc:  # noqa: BLE001
        _log.exception("qa generation run failed")
        _gen_state["last_error"] = str(exc)
    finally:
        _gen_state["running"] = False
        _gen_state["finished_at"] = _utc_now()


def start_qa_generation(
    get_retriever: Callable[[], Any],
    get_tool_executor: Callable[[], Any],
    *,
    scope: str = "unanswered",
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """Kick off background AI drafting for the requested questions."""
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return {"ok": False, "error": "OPENAI_API_KEY not configured"}

    with _gen_lock:
        if _gen_state.get("running"):
            return {"ok": True, "started": False, "running": True, "message": "Generation already running"}
        targets = _collect_targets(scope, keys)
        if not targets:
            return {
                "ok": True,
                "started": False,
                "running": False,
                "total": 0,
                "message": "No questions need an AI draft right now.",
            }
        _gen_state.update(
            running=True,
            total=len(targets),
            done=0,
            generated=0,
            failed=0,
            started_at=_utc_now(),
            finished_at="",
            last_error="",
        )

    threading.Thread(
        target=_run_qa_generation,
        args=(get_retriever, get_tool_executor, targets),
        daemon=True,
    ).start()
    return {"ok": True, "started": True, "running": True, "total": len(targets)}


def _autodraft_enabled() -> bool:
    return os.environ.get("SUPPORT_QA_AUTODRAFT", "1").strip().lower() not in ("0", "false", "no", "off")


def autostart_qa_drafts(
    get_retriever: Callable[[], Any],
    get_tool_executor: Callable[[], Any],
) -> dict[str, Any]:
    """Auto-draft answers for any top questions that have no answer/draft yet.

    Called when the Q&A board is opened so the answer boxes come pre-filled with
    AI drafts — the human only needs to edit or approve. Safe to call on every
    board load: it no-ops when auto-draft is disabled, OpenAI isn't configured,
    a run is already in progress, or every question already has a draft/approved
    answer (``start_qa_generation`` returns ``started: False`` when there are no
    targets)."""
    if not _autodraft_enabled():
        return {"ok": True, "started": False, "disabled": True}
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return {"ok": True, "started": False, "reason": "no_api_key"}
    if _gen_state.get("running"):
        return {"ok": True, "started": False, "running": True}
    return start_qa_generation(get_retriever, get_tool_executor, scope="unanswered")


def cancel_qa_generation() -> dict[str, Any]:
    """Signal the background drafting loop to stop after the current question."""
    was_running = bool(_gen_state.get("running"))
    _gen_state["running"] = False
    return {"ok": True, "cancelled": was_running}


async def regenerate_qa_answer(
    retriever: Any,
    executor: Any,
    *,
    key: str,
    question: str,
    category: str = "other",
) -> dict[str, Any]:
    """Re-draft a single answer on demand (saved as a draft pending approval)."""
    return await generate_and_store_draft(
        retriever, executor, key=key, question=question, category=category
    )


def approve_qa_draft(*, key: str, question: str, category: str, answer: str, updated_by: str = "") -> dict[str, Any]:
    """Approve a (possibly edited) draft so it becomes the source of truth."""
    return save_qa_answer(
        key=key,
        question=question,
        category=category,
        answer=answer,
        updated_by=updated_by or "approved",
        status="approved",
        source="human",
    )


def approve_all_qa_drafts(*, updated_by: str = "") -> dict[str, Any]:
    """Approve every pending AI draft as-is."""
    from support_dashboard_store import get_qa_answers, set_qa_answer

    approved = 0
    for key, row in get_qa_answers().items():
        if str(row.get("status")) != "draft":
            continue
        text = str(row.get("answer") or "").strip()
        if not text:
            continue
        set_qa_answer(
            question_key=key,
            question=str(row.get("question") or ""),
            category=str(row.get("category") or "other"),
            answer=text,
            updated_by=updated_by or "approved",
            status="approved",
            source="human",
        )
        approved += 1
    return {"ok": True, "approved": approved}


def discard_qa_draft(*, key: str) -> dict[str, Any]:
    """Delete a pending AI draft (only if it has not been approved)."""
    from support_dashboard_store import delete_qa_answer, get_qa_answers

    row = get_qa_answers().get((key or "").strip())
    if not row or str(row.get("status")) != "draft":
        return {"ok": False, "error": "No draft to discard."}
    delete_qa_answer((key or "").strip())
    return {"ok": True}
