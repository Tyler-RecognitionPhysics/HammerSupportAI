"""Retrieval for Hammer Support demo."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    env = os.environ.get("SUPPORT_REPO_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


_REPO = _repo_root()
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from knowledge_support.retriever import (  # noqa: E402
    ALLOWED_WIKI_FILES,
    Chunk,
    SupportKnowledgeRetriever,
    SupportWikiRetriever,
)

__all__ = ["ALLOWED_WIKI_FILES", "Chunk", "SupportKnowledgeRetriever", "SupportWikiRetriever"]
