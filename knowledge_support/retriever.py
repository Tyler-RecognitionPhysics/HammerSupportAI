"""Support knowledge retriever — wiki-support + raw/support-data + playbook."""

from __future__ import annotations

import math
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from knowledge.chunking import chunk_markdown, strip_frontmatter

ALLOWED_WIKI_FILES: Tuple[str, ...] = (
    "company-support-canonical.md",
    "entity-hammer-support.md",
    "source-slack-support.md",
    "demo-public-site-copy.md",
)

ENTITY_DOC_ID = "entity-hammer-support.md"
RAW_DOC_PREFIX = "raw/support-data/"

_STOPWORDS = frozenset(
    "a an the and or but if to of in on for with as by at from is was are were be been being "
    "it this that these those you we they he she i me my our your their its not no yes so than "
    "then can could should would will may might just also into about over after before out up "
    "down all any some more most other such what which who whom whose how when where why "
    "tell explain describe give show know want need like please thanks thank hello hi hey um uh "
    "does do did doing done have has had having".split()
)

_TERM_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "login": ("password", "reset", "otp", "sms", "426637", "access", "account"),
    "password": ("reset", "welcome", "email", "activation", "login"),
    "billing": ("invoice", "card", "payment", "renewal", "subscription", "charge"),
    "integration": ("crm", "dealertrack", "tekion", "vin", "cdk", "eleads", "dms"),
    "integrations": ("crm", "dealertrack", "tekion", "vin", "cdk"),
    "crm": ("dealertrack", "tekion", "vin", "cdk", "eleads", "dealercenter", "integration"),
    "facebook": ("meta", "aia", "marketplace", "ads", "instagram"),
    "aia": ("facebook", "meta", "inventory", "ads", "marketplace"),
    "marketplace": ("marketposter", "facebook", "listing", "connect"),
    "marketposter": ("marketplace", "chrome", "extension", "listing", "post"),
    "connect": ("marketplace", "messaging", "sms", "text", "thread"),
    "messaging": ("connect", "sms", "text", "marketplace"),
    "dashboard": ("settings", "inbox", "leads", "reporting", "metrics"),
    "lead": ("leads", "inbox", "follow", "followup", "follow-up"),
    "leads": ("lead", "inbox", "follow", "followup"),
    "sms": ("text", "426637", "otp", "stop", "opt-out"),
    "error": ("bug", "issue", "broken", "not working", "failed"),
    "sync": ("inventory", "feed", "listing", "integration"),
    "inventory": ("feed", "listing", "vehicles", "stock", "vin"),
    "hammer": ("hammertime", "support", "account"),
    "support": ("help", "escalate", "ticket", "contact"),
    "escalate": ("support", "human", "manager", "phone"),
    "user": ("users", "permission", "role", "admin"),
    "users": ("user", "permission", "role", "admin"),
}


def _tokenize(text: str) -> List[str]:
    return [
        t
        for t in re.findall(r"[a-z0-9]+", text.lower())
        if t not in _STOPWORDS and len(t) > 1
    ]


def _escape_fts_token(token: str) -> str:
    return re.sub(r"[^\w]", "", token, flags=re.UNICODE)


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    chunk_id: int
    text: str


@dataclass
class _MemChunk:
    doc_id: str
    chunk_id: int
    text: str
    tokens: List[str]


class SupportKnowledgeRetriever:
    def __init__(
        self,
        wiki_dir: Path,
        support_raw_dir: Path | None = None,
        db_path: Path | None = None,
        playbook_md_path: Path | None = None,
    ) -> None:
        self.wiki_dir = Path(wiki_dir).resolve()
        self.support_raw_dir = Path(support_raw_dir).resolve() if support_raw_dir else None
        repo = self.wiki_dir.parent
        self._playbook_md_path = Path(playbook_md_path).resolve() if playbook_md_path else None
        self.db_path = (
            Path(db_path).resolve()
            if db_path
            else (repo / "knowledge_support" / "data" / "support_kb.sqlite")
        )
        self._mem_chunks: List[_MemChunk] = []
        self._corpus: List[List[str]] = []
        self._doc_lens: List[int] = []
        self._avgdl: float = 1.0
        self._df: dict[str, int] = {}
        self._idf: dict[str, float] = {}
        self._N: int = 0
        self._k1 = 1.5
        self._b = 0.75
        self._product_terms: List[str] = []
        self._sqlite_ok = self.db_path.is_file()
        if self._sqlite_ok:
            self._load_product_terms()
        self._build_memory_index()

    def _load_product_terms(self) -> None:
        try:
            conn = sqlite3.connect(str(self.db_path))
            try:
                rows = conn.execute("SELECT slug, name, summary FROM kb_product ORDER BY sort_order").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return
        terms: list[str] = []
        for slug, name, summary in rows:
            terms.extend(_tokenize(str(slug or "")))
            terms.extend(_tokenize(str(name or "")))
            terms.extend(_tokenize(str(summary or "")))
        self._product_terms = list(dict.fromkeys(terms))

    def _ingest_text(self, doc_id: str, raw: str) -> None:
        for i, piece in enumerate(chunk_markdown(raw)):
            toks = _tokenize(piece)
            if not toks:
                continue
            self._mem_chunks.append(_MemChunk(doc_id=doc_id, chunk_id=i, text=piece, tokens=toks))
            self._corpus.append(toks)

    def _build_memory_index(self) -> None:
        for name in ALLOWED_WIKI_FILES:
            path = self.wiki_dir / name
            if not path.is_file():
                raise FileNotFoundError(f"Missing allowlisted wiki file: {path}")
            self._ingest_text(name, path.read_text(encoding="utf-8"))

        topics_dir = self.wiki_dir / "topics"
        if topics_dir.is_dir():
            for path in sorted(topics_dir.rglob("*.md")):
                rel = "topics/" + path.relative_to(topics_dir).as_posix()
                try:
                    self._ingest_text(rel, path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue

        if self.support_raw_dir and self.support_raw_dir.is_dir():
            root = self.support_raw_dir.resolve()
            for path in sorted(root.rglob("*.md")):
                rel_parts = path.relative_to(root).parts
                if any(p.startswith(".") for p in rel_parts):
                    continue
                doc_id = RAW_DOC_PREFIX + path.relative_to(root).as_posix()
                try:
                    md = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                self._ingest_text(doc_id, md)

        pb = self._playbook_md_path or (self.wiki_dir.parent / "knowledge_support" / "playbook" / "approved.md")
        pb = Path(pb).resolve()
        if pb.is_file():
            try:
                self._ingest_text("playbook/approved.md", pb.read_text(encoding="utf-8"))
            except OSError:
                pass

        self._N = len(self._corpus)
        if self._N == 0:
            raise ValueError("No indexable chunks in support wiki corpus")

        for doc in self._corpus:
            self._doc_lens.append(len(doc))
            for w in set(doc):
                self._df[w] = self._df.get(w, 0) + 1

        avg = sum(self._doc_lens) / self._N
        self._avgdl = avg if avg > 0 else 1.0
        for w, df in self._df.items():
            self._idf[w] = math.log((self._N - df + 0.5) / (df + 0.5) + 1.0)

    def _expand_terms(self, terms: Sequence[str]) -> List[str]:
        out: list[str] = []
        seen: set[str] = set()

        def add(t: str) -> None:
            t = t.lower().strip()
            if len(t) < 2 or t in _STOPWORDS or t in seen:
                return
            seen.add(t)
            out.append(t)

        for t in terms:
            add(t)
            for extra in _TERM_EXPANSIONS.get(t, ()):
                add(extra)
        for t in self._product_terms:
            if t in seen:
                continue
            for q in terms:
                if q in t or t in q:
                    add(t)
        return out

    def _query_variants(self, query: str) -> List[str]:
        base = _tokenize(query)
        if not base:
            return [query.strip()]
        expanded = self._expand_terms(base)
        variants = [query.strip(), " ".join(base), " ".join(expanded), " ".join(expanded[:8])]
        deduped: list[str] = []
        seen: set[str] = set()
        for v in variants:
            key = v.lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(v)
        return deduped

    def _fts_match(self, terms: Sequence[str]) -> str:
        parts: list[str] = []
        for t in terms:
            safe = _escape_fts_token(t)
            if len(safe) < 2:
                continue
            if len(safe) >= 3:
                parts.append(f"{safe}*")
            else:
                parts.append(f'"{safe}"')
        return " OR ".join(parts[:18])

    def _sqlite_search(self, query: str, limit: int) -> List[Tuple[Chunk, float]]:
        terms = self._expand_terms(_tokenize(query))
        if not terms:
            return []
        match = self._fts_match(terms)
        if not match:
            return []
        sql = """
            SELECT d.path, c.chunk_index, c.text, bm25(kb_chunk_fts) AS rank
            FROM kb_chunk_fts
            JOIN kb_chunk c ON c.id = kb_chunk_fts.rowid
            JOIN kb_document d ON d.id = c.document_id
            WHERE kb_chunk_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            try:
                rows = conn.execute(sql, (match, limit)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        out: List[Tuple[Chunk, float]] = []
        for path, chunk_index, text, rank in rows:
            score = float(-rank) if rank is not None else 0.0
            out.append((Chunk(doc_id=str(path), chunk_id=int(chunk_index), text=str(text)), score))
        return out

    def _memory_bm25(self, query: str, limit: int) -> List[Tuple[Chunk, float]]:
        q_tokens = self._expand_terms(_tokenize(query))
        if not q_tokens:
            return []
        ranked: List[Tuple[int, float]] = []
        for i, doc in enumerate(self._corpus):
            s = self._score_one(q_tokens, doc, self._doc_lens[i])
            ranked.append((i, s))
        ranked.sort(key=lambda x: x[1], reverse=True)
        out: List[Tuple[Chunk, float]] = []
        for idx, sc in ranked[:limit]:
            ch = self._mem_chunks[idx]
            out.append((Chunk(doc_id=ch.doc_id, chunk_id=ch.chunk_id, text=ch.text), sc))
        return out

    def _substring_fallback(self, query: str, limit: int) -> List[Tuple[Chunk, float]]:
        terms = self._expand_terms(_tokenize(query))
        if not terms:
            return []
        scored: List[Tuple[int, float]] = []
        lower_terms = [t.lower() for t in terms]
        for i, ch in enumerate(self._mem_chunks):
            hay = ch.text.lower()
            score = sum(1.0 for t in lower_terms if t in hay)
            if score > 0:
                scored.append((i, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            (
                Chunk(
                    doc_id=self._mem_chunks[i].doc_id,
                    chunk_id=self._mem_chunks[i].chunk_id,
                    text=self._mem_chunks[i].text,
                ),
                sc,
            )
            for i, sc in scored[:limit]
        ]

    def _score_one(self, q_tokens: Sequence[str], doc: Sequence[str], doc_len: int) -> float:
        qfreq = Counter(q_tokens)
        dcnt = Counter(doc)
        score = 0.0
        for w in qfreq:
            if w not in self._idf:
                continue
            f = float(dcnt.get(w, 0))
            idf = self._idf[w]
            denom = f + self._k1 * (1 - self._b + self._b * doc_len / self._avgdl)
            if denom <= 0:
                continue
            score += idf * (f * (self._k1 + 1)) / denom
        return score

    @staticmethod
    def _rrf_merge(lists: Iterable[Sequence[Tuple[Chunk, float]]], k: int = 60) -> List[Tuple[Chunk, float]]:
        scores: dict[tuple[str, int], float] = {}
        chunks: dict[tuple[str, int], Chunk] = {}
        for results in lists:
            for rank, (ch, _) in enumerate(results):
                key = (ch.doc_id, ch.chunk_id)
                chunks[key] = ch
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [(chunks[key], sc) for key, sc in ranked]

    @staticmethod
    def _dedupe_by_doc(pairs: List[Tuple[Chunk, float]], k: int, *, max_per_doc: int = 2) -> List[Tuple[Chunk, float]]:
        out: List[Tuple[Chunk, float]] = []
        per_doc: dict[str, int] = {}
        for ch, sc in pairs:
            n = per_doc.get(ch.doc_id, 0)
            if n >= max_per_doc:
                continue
            per_doc[ch.doc_id] = n + 1
            out.append((ch, sc))
            if len(out) >= k:
                break
        return out

    def _entity_fallback(self, k: int) -> List[Tuple[Chunk, float]]:
        hits = [
            (Chunk(doc_id=ch.doc_id, chunk_id=ch.chunk_id, text=ch.text), 0.01)
            for ch in self._mem_chunks
            if ch.doc_id == ENTITY_DOC_ID
        ]
        return hits[:k]

    def search(self, query: str, k: int = 8) -> List[Tuple[Chunk, float]]:
        q = query.strip()
        if not q:
            return []
        limit = max(k * 3, 12)
        lists: list[List[Tuple[Chunk, float]]] = []
        if self._sqlite_ok:
            for variant in self._query_variants(q):
                hits = self._sqlite_search(variant, limit)
                if hits:
                    lists.append(hits)
        for variant in self._query_variants(q):
            bm = self._memory_bm25(variant, limit)
            if bm:
                lists.append(bm)
        merged = self._rrf_merge(lists) if lists else []
        if len(merged) < k:
            sub = self._substring_fallback(q, limit)
            if sub:
                merged = self._rrf_merge([merged, sub]) if merged else sub
        result = self._dedupe_by_doc(merged, k, max_per_doc=2)
        if not result:
            result = self._entity_fallback(k)
        return result

    def top_k(self, query: str, k: int = 8) -> List[Tuple[Chunk, float]]:
        return self.search(query, k)

    def best_score(self, query: str) -> float:
        top = self.search(query, k=1)
        return top[0][1] if top else 0.0


class SupportWikiRetriever(SupportKnowledgeRetriever):
    """Alias for demo imports."""


__all__ = [
    "ALLOWED_WIKI_FILES",
    "Chunk",
    "ENTITY_DOC_ID",
    "RAW_DOC_PREFIX",
    "SupportKnowledgeRetriever",
    "SupportWikiRetriever",
]
