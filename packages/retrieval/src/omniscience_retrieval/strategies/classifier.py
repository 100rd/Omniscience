"""Lightweight heuristic query classifier for adaptive retrieval.

No LLM required — pure rule-based classification on the query string.
Designed to be fast (microseconds) and deterministic.
"""

from __future__ import annotations

import re
from typing import Literal

StrategyName = Literal["hybrid", "keyword", "structural"]

# Patterns that indicate the caller wants graph-traversal retrieval.
_STRUCTURAL_PHRASES: tuple[str, ...] = (
    "depends on",
    "depend on",
    "dependencies of",
    "dependency of",
    "imports",
    "imported by",
    "calls",
    "called by",
    "references",
    "referenced by",
    "uses",
    "used by",
    "inherits from",
    "extends",
    "what calls",
    "what imports",
    "what depends",
    "who calls",
    "who imports",
    "who uses",
)

# Patterns that indicate the caller wants exact-match BM25-only retrieval:
# - quoted strings (single or double)
# - error codes / identifiers with underscores / camelCase
_QUOTED_RE = re.compile(r'["\'](.+?)["\']')
_ERROR_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")  # e.g. HTTP_404, ERR_CONN_REFUSED


def classify_query(query: str) -> StrategyName:
    """Return the best retrieval strategy for *query* using heuristic rules.

    Evaluation order (first match wins):
      1. Structural phrases  → ``"structural"``
      2. Quoted strings      → ``"keyword"``
      3. Error codes         → ``"keyword"``
      4. Default             → ``"hybrid"``

    Args:
        query: The raw search query string.

    Returns:
        One of ``"structural"``, ``"keyword"``, or ``"hybrid"``.
    """
    lower = query.lower()

    # Rule 1 — structural traversal keywords
    if any(phrase in lower for phrase in _STRUCTURAL_PHRASES):
        return "structural"

    # Rule 2 — quoted string (exact match intent)
    if _QUOTED_RE.search(query):
        return "keyword"

    # Rule 3 — SCREAMING_CASE error/config codes
    if _ERROR_CODE_RE.search(query):
        return "keyword"

    # Default
    return "hybrid"


__all__ = ["StrategyName", "classify_query"]
