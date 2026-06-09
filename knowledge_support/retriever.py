"""Support knowledge retriever — wiki-support + raw/support-data + playbook."""

from __future__ import annotations

import json
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
TICKET_DOC_MARKER = "hubspot-tickets/"
DEFAULT_EXCLUDED_TICKET_STAGE_IDS: Tuple[str, ...] = ("1269291037",)  # Hammer CS: Support Pipeline -> Spam

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
    "connect": ("add", "setup", "configure", "link", "integrate"),
    "add": ("connect", "setup", "configure", "enable"),
    "setup": ("connect", "configure", "settings", "onboarding"),
    "configure": ("setup", "connect", "settings"),
    "messaging": ("connect", "sms", "text", "marketplace"),
    "dashboard": ("settings", "inbox", "leads", "reporting", "metrics", "office"),
    "lead": ("leads", "inbox", "follow", "followup", "follow-up"),
    "leads": ("lead", "inbox", "follow", "followup"),
    "source": ("sources", "provider", "providers", "feed", "ulp"),
    "sources": ("source", "provider", "providers", "feed"),
    "provider": ("providers", "source", "sources", "feed"),
    "providers": ("provider", "source", "sources"),
    "feed": ("provider", "source", "inventory", "listing"),
    "sms": ("text", "426637", "otp", "stop", "opt-out"),
    "error": ("bug", "issue", "broken", "not working", "failed"),
    "sync": ("inventory", "feed", "listing", "integration"),
    "inventory": ("feed", "listing", "vehicles", "stock", "vin"),
    "hammer": ("hammertime", "support", "account", "office"),
    "support": ("help", "escalate", "ticket", "contact"),
    "escalate": ("support", "human", "manager", "phone"),
    "user": ("users", "permission", "role", "admin"),
    "users": ("user", "permission", "role", "admin"),
}

# Phrase-level synonyms (customers say "lead source"; KB often says "lead provider").
_PHRASE_REWRITES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\blead\s+sources?\b", re.I), ("lead provider", "lead providers")),
    (re.compile(r"\blead\s+providers?\b", re.I), ("lead source", "lead sources")),
)

# --- Ticket "solution quality" signals --------------------------------------
# A ticket is only useful as Help Desk evidence if it shows how the issue was
# worked or resolved — a resolution/close note, an agent reply, a note, or a
# call summary. Bare stub tickets (e.g. a single inbound "Cancel my account"
# line with no response) carry no solution and should not be surfaced.
_RESOLUTION_HEADER_RE = re.compile(r"^##\s*Resolution\b", re.I | re.M)
_TIMELINE_HEADER_RE = re.compile(r"^##\s*Help Desk Timeline\b", re.I | re.M)
_TIMELINE_ENTRY_RE = re.compile(r"^###\s+", re.M)
_OUTBOUND_DIR_RE = re.compile(r"direction:\s*(outgoing|outbound)", re.I)
_AGENT_NOTE_RE = re.compile(r"—\s*Note\b", re.I)
_CALL_ENTRY_RE = re.compile(r"—\s*Call\b", re.I)
# Tickets worked via email render at least one "### … — Email: …" timeline entry.
# Operators have found these carry the richest step-by-step fixes, so the AI
# prioritizes them when pulling supporting cases.
_EMAIL_ENTRY_RE = re.compile(r"—\s*Email\b", re.I)
# Step-by-step / directional language inside a Resolution section is a strong
# sign the ticket spells out exactly how the issue was fixed.
_RESOLUTION_STEP_RE = re.compile(r"(?m)^\s*\d+[.)]\s+\S")
_RESOLUTION_DIRECTION_RE = re.compile(
    r"\b(then|next|click|select|go to|navigate|open|enable|disable|toggle|set|update|reset)\b",
    re.I,
)
# Tickets scoring below this are treated as thin/no-solution stubs.
_MIN_SOLUTION_SCORE = 1.0
# A ticket whose written Resolution scores at least this is treated as a
# "clear resolution" and is ranked above every thin/unresolved ticket.
_CLEAR_RESOLUTION_MIN_SCORE = 3.0
# Relevance-proportional boost applied to email-worked tickets so they are
# preferred when pulling supporting cases for an answer.
_EMAIL_WORKED_BOOST = 0.8

def _tokenize(text: str) -> List[str]:
    return [
        t
        for t in re.findall(r"[a-z0-9]+", text.lower())
        if t not in _STOPWORDS and len(t) > 1
    ]


def _escape_fts_token(token: str) -> str:
    return re.sub(r"[^\w]", "", token, flags=re.UNICODE)


def _is_ticket_doc_id(doc_id: str) -> bool:
    return TICKET_DOC_MARKER in (doc_id or "").lower()


def _frontmatter_value(raw: str, key: str) -> str:
    if not raw.startswith("---"):
        return ""
    end = raw.find("\n---", 3)
    if end == -1:
        return ""
    target = key.lower().strip()
    for line in raw[3:end].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        if k.strip().lower() == target:
            return v.strip()
    return ""


def _excluded_ticket_stage_ids() -> set[str]:
    raw = (
        os.environ.get("SUPPORT_EXCLUDE_TICKET_STAGE_IDS", "").strip()
        or os.environ.get("CS_QUESTIONS_EXCLUDE_STAGE_IDS", "").strip()
    )
    values = {s.strip() for s in raw.split(",") if s.strip()} if raw else set()
    values.update(DEFAULT_EXCLUDED_TICKET_STAGE_IDS)
    return values


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
        ticket_pins_path: Path | None = None,
    ) -> None:
        self.wiki_dir = Path(wiki_dir).resolve()
        self.support_raw_dir = Path(support_raw_dir).resolve() if support_raw_dir else None
        repo = self.wiki_dir.parent
        self._playbook_md_path = Path(playbook_md_path).resolve() if playbook_md_path else None
        self._ticket_pins_path = Path(ticket_pins_path).resolve() if ticket_pins_path else None
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
        self._ticket_stage_by_doc_id: dict[str, str] = {}
        self._ticket_text_cache: dict[str, str] = {}
        self._ticket_solution_cache: dict[str, float] = {}
        self._ticket_resolution_cache: dict[str, float] = {}
        self._ticket_email_cache: dict[str, bool] = {}
        self._ticket_pins: list[dict[str, object]] = []
        self._sqlite_ok = self.db_path.is_file()
        if self._sqlite_ok:
            self._load_product_terms()
            self._load_ticket_stage_map_from_sqlite()
        self._build_memory_index()
        self._load_ticket_pins()

    def _ticket_pins_default_path(self) -> Path:
        return (self.wiki_dir.parent / "knowledge_support" / "playbook" / "ticket_pins.json").resolve()

    def _load_ticket_pins(self) -> None:
        """Load operator-pinned question→ticket associations (best-effort)."""
        path = self._ticket_pins_path or self._ticket_pins_default_path()
        self._ticket_pins = []
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        raw = data.get("pins") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return
        for pin in raw:
            if not isinstance(pin, dict):
                continue
            doc_id = str(pin.get("ticket_doc_id") or "").strip()
            if not doc_id:
                continue
            tokens: set[str] = set()
            for tok in pin.get("keywords") or []:
                tokens.update(_tokenize(str(tok)))
            tokens.update(_tokenize(str(pin.get("topic") or "")))
            if not tokens:
                continue
            self._ticket_pins.append({"doc_id": doc_id, "tokens": tokens})

    def _matched_pin_doc_ids(self, query: str, *, limit: int = 3) -> list[str]:
        """Ticket doc_ids pinned to questions close enough to this query.

        A pin matches when the query shares most of the pinned topic's meaningful
        keywords, so the same question and clearly related phrasings reuse the
        chosen ticket without dragging in loosely-related ones.
        """
        if not self._ticket_pins:
            return []
        q_tokens = set(_tokenize(query))
        if not q_tokens:
            return []
        scored: list[tuple[float, int, str]] = []
        for pin in self._ticket_pins:
            tokens = pin["tokens"]  # type: ignore[index]
            if not tokens:
                continue
            overlap = len(q_tokens & tokens)  # type: ignore[operator]
            if not overlap:
                continue
            ratio = overlap / len(tokens)  # type: ignore[arg-type]
            # Short topics must match in full; longer ones need a strong majority.
            min_overlap = len(tokens) if len(tokens) <= 2 else 2  # type: ignore[arg-type]
            if overlap < min_overlap or ratio < 0.5:
                continue
            scored.append((ratio, overlap, str(pin["doc_id"])))  # type: ignore[index]
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out: list[str] = []
        seen: set[str] = set()
        for _ratio, _overlap, doc_id in scored:
            if doc_id in seen:
                continue
            seen.add(doc_id)
            out.append(doc_id)
            if len(out) >= limit:
                break
        return out

    def _load_doc_chunks(self, doc_id: str, limit: int = 2) -> list[Tuple[Chunk, float]]:
        """Load the first chunks of a specific doc by id (for force-included pins)."""
        rows: list[tuple[int, str]] = []
        if self._sqlite_ok:
            try:
                conn = sqlite3.connect(str(self.db_path))
                try:
                    rows = [
                        (int(idx), str(text or ""))
                        for idx, text in conn.execute(
                            """
                            SELECT c.chunk_index, c.text
                            FROM kb_chunk c
                            JOIN kb_document d ON d.id = c.document_id
                            WHERE d.path = ?
                            ORDER BY c.chunk_index
                            LIMIT ?
                            """,
                            (doc_id, limit),
                        ).fetchall()
                    ]
                finally:
                    conn.close()
            except sqlite3.Error:
                rows = []
        if not rows:
            mem = [(c.chunk_id, c.text) for c in self._mem_chunks if c.doc_id == doc_id]
            mem.sort(key=lambda x: x[0])
            rows = [(idx, text) for idx, text in mem[:limit]]
        out: list[Tuple[Chunk, float]] = []
        for offset, (idx, text) in enumerate(rows):
            if not text.strip():
                continue
            out.append(
                (
                    Chunk(doc_id=doc_id, chunk_id=idx, text=text.strip(), tokens=_tokenize(text)),
                    1.0 - offset * 0.01,
                )
            )
        return out

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

    def _load_ticket_stage_map_from_sqlite(self) -> None:
        try:
            conn = sqlite3.connect(str(self.db_path))
            try:
                rows = conn.execute(
                    """
                    SELECT d.path, c.text
                    FROM kb_document d
                    JOIN kb_chunk c ON c.document_id = d.id AND c.chunk_index = 0
                    WHERE lower(d.path) LIKE ?
                    """,
                    (f"%{TICKET_DOC_MARKER}%",),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return
        for doc_id, first_chunk in rows:
            stage = _frontmatter_value(str(first_chunk or ""), "stage")
            if stage:
                self._ticket_stage_by_doc_id[str(doc_id)] = stage

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
                # HubSpot ticket files are numerous and already indexed in SQLite FTS.
                # Search them as ticket-level Help Desk cases instead of loading every
                # ticket chunk into the general in-memory BM25 corpus.
                if self._sqlite_ok and _is_ticket_doc_id(doc_id):
                    continue
                try:
                    md = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if _is_ticket_doc_id(doc_id):
                    self._ticket_stage_by_doc_id[doc_id] = _frontmatter_value(md, "stage")
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

    def add_playbook_entry_to_index(self, title: str, content: str) -> bool:
        """Index one newly-approved playbook entry in place, without rebuilding
        the whole corpus.

        A full rebuild re-reads tens of thousands of source files (~seconds),
        which makes saving a playbook answer feel slow. A playbook entry is tiny,
        so we just ingest its chunks and recompute the BM25 statistics over the
        augmented corpus. Returns True when something was indexed.
        """
        content = (content or "").strip()
        if not content:
            return False
        title = (title or "").strip()
        if title.startswith("###"):
            heading = title
        elif title:
            heading = f"### {title}"
        else:
            heading = "### Approved answer"
        text = f"{heading}\n\n{content}"
        doc_id = "playbook/approved.md"

        new_mem = list(self._mem_chunks)
        new_corpus = list(self._corpus)
        next_chunk_id = sum(1 for c in self._mem_chunks if c.doc_id == doc_id)
        added = 0
        for piece in chunk_markdown(text):
            toks = _tokenize(piece)
            if not toks:
                continue
            new_mem.append(
                _MemChunk(doc_id=doc_id, chunk_id=next_chunk_id + added, text=piece, tokens=toks)
            )
            new_corpus.append(toks)
            added += 1
        if not added:
            return False

        n = len(new_corpus)
        doc_lens = [len(doc) for doc in new_corpus]
        df: dict[str, int] = {}
        for doc in new_corpus:
            for w in set(doc):
                df[w] = df.get(w, 0) + 1
        avg = sum(doc_lens) / n if n else 1.0
        avgdl = avg if avg > 0 else 1.0
        idf = {w: math.log((n - d + 0.5) / (d + 0.5) + 1.0) for w, d in df.items()}

        # Assign so a concurrent reader never sees _corpus longer than the
        # length-dependent structures it indexes alongside it.
        self._df = df
        self._idf = idf
        self._avgdl = avgdl
        self._N = n
        self._mem_chunks = new_mem
        self._doc_lens = doc_lens
        self._corpus = new_corpus
        return True

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
                # Substring matching on short tokens is dangerous: "ai" matches
                # "aia"/"email"/"training", contaminating AI questions with the
                # unrelated Facebook AIA product. Require a meaningful overlap.
                if min(len(q), len(t)) < 4:
                    continue
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

    def _search_query_variants(self, query: str) -> List[str]:
        """Phrase synonym variants only — avoid extra searches that drift off-topic."""
        variants = list(self._query_variants(query))
        lower = query.lower().strip()
        if lower:
            for pattern, replacements in _PHRASE_REWRITES:
                if not pattern.search(lower):
                    continue
                for repl in replacements:
                    variants.append(pattern.sub(repl, lower, count=1))

        deduped: list[str] = []
        seen: set[str] = set()
        for v in variants:
            key = v.lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(v)
        return deduped[:8]

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

    def _doc_matches_scope(
        self,
        doc_id: str,
        *,
        ticket_only: bool = False,
        exclude_tickets: bool = False,
    ) -> bool:
        is_ticket = _is_ticket_doc_id(doc_id)
        if ticket_only and not is_ticket:
            return False
        if exclude_tickets and is_ticket:
            return False
        if is_ticket and self._ticket_stage_by_doc_id.get(doc_id, "") in _excluded_ticket_stage_ids():
            return False
        return True

    def _sqlite_search(
        self,
        query: str,
        limit: int,
        *,
        ticket_only: bool = False,
        exclude_tickets: bool = False,
    ) -> List[Tuple[Chunk, float]]:
        terms = self._expand_terms(_tokenize(query))
        if not terms:
            return []
        match = self._fts_match(terms)
        if not match:
            return []
        where = ["kb_chunk_fts MATCH ?"]
        params: list[object] = [match]
        if ticket_only:
            where.append("lower(d.path) LIKE ?")
            params.append(f"%{TICKET_DOC_MARKER}%")
        elif exclude_tickets:
            where.append("lower(d.path) NOT LIKE ?")
            params.append(f"%{TICKET_DOC_MARKER}%")
        params.append(limit)
        sql = f"""
            SELECT d.path, c.chunk_index, c.text, bm25(kb_chunk_fts) AS rank
            FROM kb_chunk_fts
            JOIN kb_chunk c ON c.id = kb_chunk_fts.rowid
            JOIN kb_document d ON d.id = c.document_id
            WHERE {" AND ".join(where)}
            ORDER BY rank
            LIMIT ?
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            try:
                rows = conn.execute(sql, tuple(params)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        out: List[Tuple[Chunk, float]] = []
        for path, chunk_index, text, rank in rows:
            if not self._doc_matches_scope(str(path), ticket_only=ticket_only, exclude_tickets=exclude_tickets):
                continue
            score = float(-rank) if rank is not None else 0.0
            out.append((Chunk(doc_id=str(path), chunk_id=int(chunk_index), text=str(text)), score))
        return out

    def _memory_bm25(
        self,
        query: str,
        limit: int,
        *,
        ticket_only: bool = False,
        exclude_tickets: bool = False,
    ) -> List[Tuple[Chunk, float]]:
        q_tokens = self._expand_terms(_tokenize(query))
        if not q_tokens:
            return []
        ranked: List[Tuple[int, float]] = []
        for i, doc in enumerate(self._corpus):
            if not self._doc_matches_scope(
                self._mem_chunks[i].doc_id,
                ticket_only=ticket_only,
                exclude_tickets=exclude_tickets,
            ):
                continue
            s = self._score_one(q_tokens, doc, self._doc_lens[i])
            ranked.append((i, s))
        ranked.sort(key=lambda x: x[1], reverse=True)
        out: List[Tuple[Chunk, float]] = []
        for idx, sc in ranked[:limit]:
            ch = self._mem_chunks[idx]
            out.append((Chunk(doc_id=ch.doc_id, chunk_id=ch.chunk_id, text=ch.text), sc))
        return out

    def _substring_fallback(
        self,
        query: str,
        limit: int,
        *,
        ticket_only: bool = False,
        exclude_tickets: bool = False,
    ) -> List[Tuple[Chunk, float]]:
        terms = self._expand_terms(_tokenize(query))
        if not terms:
            return []
        scored: List[Tuple[int, float]] = []
        lower_terms = [t.lower() for t in terms]
        for i, ch in enumerate(self._mem_chunks):
            if not self._doc_matches_scope(
                ch.doc_id,
                ticket_only=ticket_only,
                exclude_tickets=exclude_tickets,
            ):
                continue
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

    def _search_flat(
        self,
        query: str,
        k: int,
        *,
        ticket_only: bool = False,
        exclude_tickets: bool = False,
        max_per_doc: int = 2,
    ) -> List[Tuple[Chunk, float]]:
        q = query.strip()
        if not q:
            return []
        limit = max(k * 8, 40)
        variants = self._search_query_variants(q)
        lists: list[List[Tuple[Chunk, float]]] = []
        if self._sqlite_ok:
            for variant in variants:
                hits = self._sqlite_search(
                    variant,
                    limit,
                    ticket_only=ticket_only,
                    exclude_tickets=exclude_tickets,
                )
                if hits:
                    lists.append(hits)
        for variant in variants:
            bm = self._memory_bm25(
                variant,
                limit,
                ticket_only=ticket_only,
                exclude_tickets=exclude_tickets,
            )
            if bm:
                lists.append(bm)
        merged = self._rrf_merge(lists) if lists else []
        if len(merged) < k:
            for variant in variants[:8]:
                sub = self._substring_fallback(
                    variant,
                    limit,
                    ticket_only=ticket_only,
                    exclude_tickets=exclude_tickets,
                )
                if sub:
                    merged = self._rrf_merge([merged, sub]) if merged else sub
                if len(merged) >= k:
                    break
        result = self._dedupe_by_doc(merged, k, max_per_doc=max_per_doc)
        if not result:
            result = self._entity_fallback(k)
        return result

    def _playbook_search(self, query: str, *, limit: int = 3) -> List[Tuple[Chunk, float]]:
        """Surface relevant admin-approved playbook answers as the highest authority.

        Playbook entries live only in the in-memory index (not the SQLite FTS that
        backs the prebuilt KB), so the blended RRF ranking used for official docs
        buries them — newly saved answers in particular never surface. We score
        playbook chunks directly by query-token overlap so an approved answer
        reliably wins for the question it was written for and close rephrasings,
        while staying out of unrelated queries.
        """
        q_tokens = set(_tokenize(query))
        if not q_tokens:
            return []
        scored: list[tuple[int, float, _MemChunk]] = []
        for ch in self._mem_chunks:
            if not ch.doc_id.startswith("playbook/"):
                continue
            shared = q_tokens & set(ch.tokens)
            if not shared:
                continue
            ratio = len(shared) / len(q_tokens)
            # Require a real topical match: either several shared keywords or a
            # strong share of the question's meaningful tokens. A single common
            # token (e.g. "support") is not enough to promote an approved answer.
            if len(shared) < 2 and ratio < 0.6:
                continue
            scored.append((len(shared), ratio, ch))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out: list[Tuple[Chunk, float]] = []
        for i, (_shared, _ratio, ch) in enumerate(scored[:limit]):
            out.append((Chunk(doc_id=ch.doc_id, chunk_id=ch.chunk_id, text=ch.text), 1.0 - i * 0.01))
        return out

    def _ticket_full_text(self, doc_id: str) -> str:
        """Full markdown text of a ticket doc (cached), used for quality scoring."""
        cached = self._ticket_text_cache.get(doc_id)
        if cached is not None:
            return cached
        text = ""
        if self._sqlite_ok:
            try:
                conn = sqlite3.connect(str(self.db_path))
                try:
                    rows = conn.execute(
                        """
                        SELECT c.text
                        FROM kb_chunk c
                        JOIN kb_document d ON d.id = c.document_id
                        WHERE d.path = ?
                        ORDER BY c.chunk_index
                        """,
                        (doc_id,),
                    ).fetchall()
                finally:
                    conn.close()
                text = "\n".join(str(r[0] or "") for r in rows)
            except sqlite3.Error:
                text = ""
        else:
            text = "\n".join(c.text for c in self._mem_chunks if c.doc_id == doc_id)
        self._ticket_text_cache[doc_id] = text
        return text

    @staticmethod
    def _compute_resolution_score(text: str) -> float:
        """Grade the strength/clarity of a ticket's written Resolution section.

        Returns 0.0 when there is no Resolution section with real content. Longer,
        step-by-step resolutions score higher — these are the tickets we most want
        Hannah to learn from and surface first.
        """
        if not text:
            return 0.0
        body = strip_frontmatter(text) if text.lstrip().startswith("---") else text
        res_match = _RESOLUTION_HEADER_RE.search(body)
        if not res_match:
            return 0.0
        after = body[res_match.end():]
        nxt = re.search(r"^##\s", after, re.M)
        res_text = after[: nxt.start()] if nxt else after
        res_clean = re.sub(r"\s+", " ", res_text).strip()
        if not res_clean:
            return 0.0
        n = len(res_clean)
        if n >= 240:
            score = 5.0
        elif n >= 120:
            score = 4.0
        elif n >= 40:
            score = 3.0
        else:
            score = 1.0
        # Reward resolutions that read like an explicit fix (numbered steps or
        # multiple directional instructions like "go to ... then click ...").
        if _RESOLUTION_STEP_RE.search(res_text) or len(_RESOLUTION_DIRECTION_RE.findall(res_text)) >= 2:
            score += 1.5
        return round(score, 4)

    @staticmethod
    def _compute_solution_score(text: str) -> float:
        """Grade how much a ticket shows the issue being worked toward a solution."""
        if not text:
            return 0.0
        body = strip_frontmatter(text) if text.lstrip().startswith("---") else text
        score = 0.0

        # 1) Resolution / close-notes section with real content is the strongest signal.
        score += SupportKnowledgeRetriever._compute_resolution_score(body)

        # 2) Help Desk timeline: agent replies, notes, and call summaries show work done.
        tl_match = _TIMELINE_HEADER_RE.search(body)
        if tl_match:
            timeline = body[tl_match.end():]
            entries = len(_TIMELINE_ENTRY_RE.findall(timeline))
            score += min(entries, 4) * 0.5
            agent_actions = len(_OUTBOUND_DIR_RE.findall(timeline)) + len(
                _AGENT_NOTE_RE.findall(timeline)
            )
            score += min(agent_actions, 4) * 0.9
            score += min(len(_CALL_ENTRY_RE.findall(timeline)), 3) * 0.4

        # 3) Modest credit for overall substance beyond a one-line description.
        substantive = re.sub(r"\s+", " ", body).strip()
        score += min(len(substantive) / 600.0, 2.0)

        return round(score, 4)

    def _ticket_solution_score(self, doc_id: str) -> float:
        cached = self._ticket_solution_cache.get(doc_id)
        if cached is not None:
            return cached
        score = self._compute_solution_score(self._ticket_full_text(doc_id))
        self._ticket_solution_cache[doc_id] = score
        return score

    def _ticket_resolution_score(self, doc_id: str) -> float:
        cached = self._ticket_resolution_cache.get(doc_id)
        if cached is not None:
            return cached
        score = self._compute_resolution_score(self._ticket_full_text(doc_id))
        self._ticket_resolution_cache[doc_id] = score
        return score

    def _ticket_is_email_worked(self, doc_id: str) -> bool:
        """True when the ticket was worked via email (has an email timeline entry)."""
        cached = self._ticket_email_cache.get(doc_id)
        if cached is not None:
            return cached
        flag = bool(_EMAIL_ENTRY_RE.search(self._ticket_full_text(doc_id)))
        self._ticket_email_cache[doc_id] = flag
        return flag

    def _ticket_case_search(
        self,
        query: str,
        *,
        case_limit: int = 4,
        chunks_per_case: int = 2,
    ) -> list[dict[str, object]]:
        q = query.strip()
        if not q:
            return []

        variants = self._search_query_variants(q)
        search_limit = max(case_limit * chunks_per_case * 12, 80)
        case_scores: dict[str, float] = {}
        chunk_scores: dict[tuple[str, int], float] = {}
        chunks: dict[tuple[str, int], Chunk] = {}

        for variant in variants:
            hits: list[tuple[Chunk, float]] = []
            if self._sqlite_ok:
                hits.extend(self._sqlite_search(variant, search_limit, ticket_only=True))
            if not hits:
                hits.extend(self._memory_bm25(variant, search_limit, ticket_only=True))
            for rank, (ch, score) in enumerate(hits):
                if not self._doc_matches_scope(ch.doc_id, ticket_only=True):
                    continue
                doc_id = ch.doc_id
                case_scores[doc_id] = case_scores.get(doc_id, 0.0) + 1.0 / (60 + rank + 1)
                key = (doc_id, ch.chunk_id)
                chunk_scores[key] = chunk_scores.get(key, 0.0) + (1.0 / (30 + rank + 1)) + (float(score) * 0.0001)
                chunks[key] = ch

        ranked_docs = sorted(case_scores.items(), key=lambda x: x[1], reverse=True)

        # Re-rank a wider candidate pool by blending keyword relevance with a
        # "solution quality" score, then prefer tickets that actually show how the
        # issue was resolved. Thin stub tickets (a one-line inbound message with no
        # reply or resolution) only survive when nothing more substantive matched.
        pool_size = max(case_limit * 4, case_limit + 8)
        scored: list[tuple[str, float, float, float, float, bool]] = []
        for doc_id, case_score in ranked_docs[:pool_size]:
            sol = self._ticket_solution_score(doc_id)
            res = self._ticket_resolution_score(doc_id)
            email = self._ticket_is_email_worked(doc_id)
            # Weight solution quality heavily so detailed, resolved tickets win
            # against thin matches that merely share keywords. Email-worked tickets
            # get an extra relevance-proportional lift — operators find these carry
            # the clearest, most actionable fixes.
            combined = case_score * (1.0 + 1.0 * min(sol, 6.0) + (_EMAIL_WORKED_BOOST if email else 0.0))
            scored.append((doc_id, combined, case_score, sol, res, email))

        with_solution = [s for s in scored if s[3] >= _MIN_SOLUTION_SCORE]
        chosen = with_solution if len(with_solution) >= min(case_limit, 2) else scored
        # Tier the ranking so the best teaching material surfaces first:
        #   1) email-worked tickets (richest step-by-step fixes),
        #   2) then tickets with a clear written Resolution,
        #   3) finally by blended relevance within each tier.
        chosen.sort(
            key=lambda x: (
                1 if x[5] else 0,
                1 if x[4] >= _CLEAR_RESOLUTION_MIN_SCORE else 0,
                x[1],
            ),
            reverse=True,
        )

        cases: list[dict[str, object]] = []
        for doc_id, combined, case_score, sol, res, email in chosen[:case_limit]:
            doc_chunks = [
                (chunks[key], chunk_scores[key])
                for key in chunks
                if key[0] == doc_id
            ]
            doc_chunks.sort(key=lambda x: x[1], reverse=True)
            cases.append(
                {
                    "doc_id": doc_id,
                    "score": combined,
                    "relevance": case_score,
                    "solution_score": sol,
                    "resolution_score": res,
                    "email_worked": email,
                    "pinned": False,
                    "chunks": doc_chunks[:chunks_per_case],
                }
            )

        # Force operator-pinned tickets to the front for matching questions. These
        # are deliberate associations, so they override keyword ranking and the
        # solution-score gate, and are fetched directly even if BM25 missed them.
        pinned_ids = self._matched_pin_doc_ids(q, limit=case_limit)
        if pinned_ids:
            pinned_cases: list[dict[str, object]] = []
            for doc_id in pinned_ids:
                pin_chunks = [
                    (chunks[key], chunk_scores[key]) for key in chunks if key[0] == doc_id
                ]
                pin_chunks.sort(key=lambda x: x[1], reverse=True)
                if not pin_chunks:
                    pin_chunks = self._load_doc_chunks(doc_id, limit=chunks_per_case)
                if not pin_chunks:
                    continue
                pinned_cases.append(
                    {
                        "doc_id": doc_id,
                        "score": float(case_scores.get(doc_id, 0.0)) + 1000.0,
                        "relevance": float(case_scores.get(doc_id, 0.0)),
                        "solution_score": self._ticket_solution_score(doc_id),
                        "resolution_score": self._ticket_resolution_score(doc_id),
                        "email_worked": self._ticket_is_email_worked(doc_id),
                        "pinned": True,
                        "chunks": pin_chunks[:chunks_per_case],
                    }
                )
            if pinned_cases:
                pinned_set = {str(c["doc_id"]) for c in pinned_cases}
                rest = [c for c in cases if str(c["doc_id"]) not in pinned_set]
                cases = (pinned_cases + rest)[: max(case_limit, len(pinned_cases))]
        return cases

    def search_support_knowledge(
        self,
        query: str,
        *,
        official_k: int = 4,
        ticket_case_limit: int = 4,
        ticket_chunks_per_case: int = 2,
    ) -> dict[str, object]:
        """Search like Help Desk: official docs first, then related resolved ticket cases."""
        official_raw = self._search_flat(query, official_k, exclude_tickets=True, max_per_doc=2)
        # Approved playbook answers are handled by a dedicated, higher-authority
        # pass below; keep them out of the general official list so they neither
        # double-appear nor get buried (or surface off-topic) via RRF ranking.
        official = [(ch, sc) for ch, sc in official_raw if not str(ch.doc_id).startswith("playbook/")]
        playbook = self._playbook_search(query, limit=3)
        ticket_cases = self._ticket_case_search(
            query,
            case_limit=ticket_case_limit,
            chunks_per_case=ticket_chunks_per_case,
        )
        ticket_pairs: list[tuple[Chunk, float]] = []
        for case in ticket_cases:
            case_score = float(case.get("score") or 0.0)
            for ch, sc in case.get("chunks") or []:  # type: ignore[union-attr]
                ticket_pairs.append((ch, case_score + float(sc)))
        return {
            "playbook": playbook,
            "official": official,
            "ticket_cases": ticket_cases,
            "all": playbook + official + ticket_pairs,
        }

    def search(self, query: str, k: int = 8) -> List[Tuple[Chunk, float]]:
        q = query.strip()
        if not q:
            return []
        result = self.search_support_knowledge(
            q,
            official_k=max(3, min(4, k // 2 or 1)),
            ticket_case_limit=max(2, min(4, k // 2 or 1)),
            ticket_chunks_per_case=2,
        )
        pairs = list(result.get("all") or [])
        if not pairs:
            pairs = self._search_flat(q, k)
        return pairs[:k]

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
