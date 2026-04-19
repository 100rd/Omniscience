"""Query rewriting and expansion for improved retrieval recall.

v0.4 MVP uses heuristic expansion (synonym and acronym substitution) so it
works in fully air-gapped environments without any external model.  The
:class:`QueryRewriter` API is deliberately designed to accept a *model_path*
so that a real local LLM (e.g. via llama.cpp or Ollama) can be plugged in
transparently in a future version without changing call sites.
"""

from __future__ import annotations

import re

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Heuristic expansion tables (v0.4 MVP)
# ---------------------------------------------------------------------------

# Common abbreviation / acronym expansions for technical and general domains.
# Keys are lower-cased tokens; values are the expanded forms to add as
# additional query variants.
_ACRONYM_MAP: dict[str, str] = {
    "api": "application programming interface",
    "db": "database",
    "sql": "structured query language",
    "llm": "large language model",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "rag": "retrieval augmented generation",
    "nlp": "natural language processing",
    "k8s": "kubernetes",
    "ci": "continuous integration",
    "cd": "continuous deployment",
    "jwt": "json web token",
    "auth": "authentication",
    "iam": "identity and access management",
    "vpc": "virtual private cloud",
    "s3": "simple storage service",
    "ec2": "elastic compute cloud",
    "rds": "relational database service",
    "cdk": "cloud development kit",
    "sdk": "software development kit",
    "ide": "integrated development environment",
    "ui": "user interface",
    "ux": "user experience",
    "http": "hypertext transfer protocol",
    "https": "hypertext transfer protocol secure",
    "url": "uniform resource locator",
    "gpu": "graphics processing unit",
    "cpu": "central processing unit",
    "slo": "service level objective",
    "sla": "service level agreement",
    "p99": "99th percentile latency",
    "p95": "95th percentile latency",
}

# Common synonym pairs for query expansion.  Each tuple is a symmetric pair;
# the first element triggers addition of the second and vice-versa.
_SYNONYM_PAIRS: list[tuple[str, str]] = [
    ("error", "exception"),
    ("exception", "error"),
    ("create", "add"),
    ("add", "create"),
    ("delete", "remove"),
    ("remove", "delete"),
    ("update", "modify"),
    ("modify", "update"),
    ("search", "query"),
    ("query", "search"),
    ("document", "doc"),
    ("doc", "document"),
    ("configure", "setup"),
    ("setup", "configure"),
    ("install", "setup"),
    ("deploy", "release"),
    ("release", "deploy"),
    ("log", "logging"),
    ("logging", "log"),
    ("monitor", "observability"),
    ("observability", "monitor"),
    ("fast", "performance"),
    ("slow", "latency"),
    ("secure", "security"),
    ("security", "secure"),
    ("chunk", "segment"),
    ("segment", "chunk"),
    ("retrieve", "fetch"),
    ("fetch", "retrieve"),
    ("embed", "embedding"),
    ("embedding", "embed"),
    ("index", "indexing"),
    ("indexing", "index"),
]

# Build synonym lookup for O(1) access
_SYNONYM_MAP: dict[str, str] = dict(_SYNONYM_PAIRS)


def _tokenize(text: str) -> list[str]:
    """Split *text* into lower-cased alpha-numeric tokens."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _apply_acronym_expansion(query: str) -> str | None:
    """Return an acronym-expanded variant of *query*, or None if no expansions apply."""
    tokens = _tokenize(query)
    expansions: list[tuple[str, str]] = [
        (tok, _ACRONYM_MAP[tok]) for tok in tokens if tok in _ACRONYM_MAP
    ]
    if not expansions:
        return None
    result = query
    for original, expanded in expansions:
        # Replace first occurrence of the token (case-insensitive) with its expansion
        result = re.sub(rf"\b{re.escape(original)}\b", expanded, result, count=1, flags=re.I)
    return result if result != query else None


def _apply_synonym_expansion(query: str) -> str | None:
    """Return a synonym-expanded variant of *query*, or None if no synonyms apply."""
    tokens = _tokenize(query)
    synonyms: list[tuple[str, str]] = [
        (tok, _SYNONYM_MAP[tok]) for tok in tokens if tok in _SYNONYM_MAP
    ]
    if not synonyms:
        return None
    result = query
    for original, synonym in synonyms:
        result = re.sub(rf"\b{re.escape(original)}\b", synonym, result, count=1, flags=re.I)
    return result if result != query else None


def _deduplicate(items: list[str]) -> list[str]:
    """Return *items* with duplicates removed, preserving insertion order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class QueryRewriter:
    """Rewrites and expands search queries for better retrieval recall.

    v0.4 MVP
    --------
    Implements heuristic expansion only (synonym substitution + acronym
    expansion) so the rewriter is fully self-contained and works in air-gapped
    environments with no model downloads.

    Future versions
    ---------------
    Pass *model_path* to wire in a local LLM (e.g. a GGUF model loaded via
    llama.cpp).  The public API (:meth:`rewrite`, :meth:`expand`) remains
    identical so no call-site changes are needed.

    Args:
        model_path: Reserved for future use.  When provided in a future
            release the rewriter will use a local LLM for semantic rewriting
            instead of heuristics.  Pass ``None`` (the default) to use
            the v0.4 heuristic engine.
    """

    def __init__(self, model_path: str | None = None) -> None:
        self._model_path = model_path
        if model_path is not None:
            log.warning(
                "query_rewriter_model_path_ignored",
                model_path=model_path,
                reason="Local LLM support is not yet implemented; using heuristic engine.",
            )
        log.debug("query_rewriter_init", engine="heuristic")

    async def rewrite(self, query: str) -> str:
        """Rewrite *query* for better retrieval.

        Falls back to the original query on any failure so retrieval is never
        blocked by a rewriting error.

        In the v0.4 heuristic implementation, this returns the first non-trivial
        expansion found (acronym expansion takes priority over synonym swap),
        or the original query unchanged if no rules apply.

        Args:
            query: Raw user search query.

        Returns:
            Rewritten query string.
        """
        try:
            expanded = _apply_acronym_expansion(query)
            if expanded is not None:
                log.debug("query_rewriter_rewrite", original=query, rewritten=expanded)
                return expanded
            synonymed = _apply_synonym_expansion(query)
            if synonymed is not None:
                log.debug("query_rewriter_rewrite", original=query, rewritten=synonymed)
                return synonymed
        except Exception:
            log.warning("query_rewriter_error", query=query, exc_info=True)
        return query

    async def expand(self, query: str) -> list[str]:
        """Generate multiple query variations for multi-query retrieval.

        Produces up to three variants:
        1. The original query (always present)
        2. Acronym-expanded form (if any acronyms match the expansion table)
        3. Synonym-swapped form (if any tokens match the synonym table)

        Duplicates are removed so the list always contains at least one entry.

        Args:
            query: Raw user search query.

        Returns:
            Ordered list of query strings starting with the original.
        """
        try:
            variants: list[str] = [query]

            acronym_variant = _apply_acronym_expansion(query)
            if acronym_variant is not None:
                variants.append(acronym_variant)

            synonym_variant = _apply_synonym_expansion(query)
            if synonym_variant is not None:
                variants.append(synonym_variant)

            result = _deduplicate(variants)
            log.debug("query_rewriter_expand", query=query, variants=result)
            return result

        except Exception:
            log.warning("query_rewriter_expand_error", query=query, exc_info=True)
            return [query]
