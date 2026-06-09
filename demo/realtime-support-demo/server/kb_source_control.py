"""Knowledge source enable/disable for AI retrieval."""

from __future__ import annotations

from typing import Any, Callable, Iterable

from knowledge_support.retriever import RAW_DOC_PREFIX

KB_SOURCE_TYPES: tuple[str, ...] = (
    "Wiki",
    "HubSpot KB",
    "Playbook",
    "Slack",
    "HubSpot Resolved Tickets",
    "Upload",
)


def _default_enabled_map() -> dict[str, bool]:
    return {label: True for label in KB_SOURCE_TYPES}


def kb_source_label_for_doc_id(doc_id: str) -> str:
    """Map a chunk doc_id / document path to a dashboard source group label."""
    from support_knowledge_api import _classify_raw_source, _source_label_from_path

    path = (doc_id or "").strip()
    if not path:
        return "Wiki"
    if path.startswith("playbook/"):
        return "Playbook"
    if path.startswith("raw/") or path.startswith(RAW_DOC_PREFIX):
        return _classify_raw_source(path)
    kind = "wiki"
    if path.startswith("raw/"):
        kind = "raw"
    return _source_label_from_path(path, kind)


def get_enabled_kb_sources() -> dict[str, bool]:
    from support_dashboard_store import get_all_settings

    raw = get_all_settings().get("kb_enabled_sources")
    defaults = _default_enabled_map()
    if not isinstance(raw, dict):
        return defaults
    out = dict(defaults)
    for label in KB_SOURCE_TYPES:
        if label in raw:
            out[label] = bool(raw[label])
    return out


def get_enabled_kb_source_set() -> set[str]:
    enabled = get_enabled_kb_sources()
    return {label for label, on in enabled.items() if on}


def set_kb_enabled_sources(values: dict[str, bool]) -> dict[str, bool]:
    from support_dashboard_store import set_settings

    current = get_enabled_kb_sources()
    for label in KB_SOURCE_TYPES:
        if label in values:
            current[label] = bool(values[label])
    set_settings({"kb_enabled_sources": current})
    return current


def filter_pairs_by_enabled(
    pairs: Iterable[tuple[Any, float]],
    *,
    enabled: set[str] | None = None,
) -> list[tuple[Any, float]]:
    labels = enabled if enabled is not None else get_enabled_kb_source_set()
    if not labels:
        return []
    return [(ch, sc) for ch, sc in pairs if kb_source_label_for_doc_id(ch.doc_id) in labels]


class SourceFilteredRetriever:
    """Wraps SupportKnowledgeRetriever and excludes disabled source groups."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def top_k(self, query: str, k: int = 8) -> list[tuple[Any, float]]:
        enabled = get_enabled_kb_source_set()
        if not enabled:
            return []
        oversample = max(k * 5, 30)
        pairs = self._inner.top_k(query, k=oversample)
        filtered = filter_pairs_by_enabled(pairs, enabled=enabled)
        return filtered[:k]

    def search(self, query: str, k: int = 8) -> list[tuple[Any, float]]:
        return self.top_k(query, k=k)

    def search_support_knowledge(self, query: str, **kwargs: Any) -> dict[str, Any]:
        enabled = get_enabled_kb_source_set()
        if not enabled:
            return {"playbook": [], "official": [], "ticket_cases": [], "all": []}
        if not hasattr(self._inner, "search_support_knowledge"):
            pairs = self.top_k(query, k=int(kwargs.get("official_k") or 8))
            return {"playbook": [], "official": pairs, "ticket_cases": [], "all": pairs}

        result = self._inner.search_support_knowledge(query, **kwargs)
        playbook = filter_pairs_by_enabled(result.get("playbook") or [], enabled=enabled)
        official = filter_pairs_by_enabled(result.get("official") or [], enabled=enabled)
        ticket_cases: list[dict[str, Any]] = []
        for case in result.get("ticket_cases") or []:
            chunks = filter_pairs_by_enabled(case.get("chunks") or [], enabled=enabled)
            if chunks:
                next_case = dict(case)
                next_case["chunks"] = chunks
                ticket_cases.append(next_case)
        all_pairs = list(playbook) + list(official)
        for case in ticket_cases:
            all_pairs.extend(case.get("chunks") or [])
        return {"playbook": playbook, "official": official, "ticket_cases": ticket_cases, "all": all_pairs}

    def best_score(self, query: str) -> float:
        top = self.top_k(query, k=1)
        return top[0][1] if top else 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap_retriever(inner: Any) -> Any:
    return SourceFilteredRetriever(inner)


def knowledge_sources_state(*, group_totals: dict[str, int] | None = None) -> dict[str, Any]:
    enabled = get_enabled_kb_sources()
    totals = group_totals or {}
    return {
        "ok": True,
        "sources": KB_SOURCE_TYPES,
        "enabled": enabled,
        "group_totals": {k: int(totals.get(k) or 0) for k in KB_SOURCE_TYPES},
        "enabled_count": sum(1 for v in enabled.values() if v),
    }
