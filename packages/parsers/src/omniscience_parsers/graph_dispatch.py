"""Graph-extraction routing — picks *which* graph extractor owns a document.

Distinct from :mod:`omniscience_parsers.dispatch`, which picks a **parser**
(bytes -> :class:`~omniscience_parsers.base.ParsedDocument`) from a
content-type / file-extension pair.  This module answers the earlier
question: given a document that the ingestion pipeline just fetched, which
graph extractor — if any — can turn it into entities and edges?

The two routes have incompatible inputs, which is exactly why the choice
cannot be a content-type lookup:

``GraphRoute.CODE``
    Needs a tree-sitter :class:`~omniscience_parsers.base.ParsedDocument`;
    consumed by :func:`~omniscience_parsers.code.graph.extract_symbol_graph`.

``GraphRoute.AWS_LIVE``
    Needs the connector's ``DocumentRef.metadata`` (ARN, account, region,
    tags) alongside the raw describe body; consumed by
    :func:`~omniscience_parsers.infra.aws_live.extract_aws_live_graph`.
    Running the code route on these documents is what produced an empty
    knowledge graph: the AWS connector's ``external_id`` is an ARN with no
    file extension, so the tree-sitter gate rejected it, and
    ``extract_symbol_graph`` returns ``([], [])`` for anything that is not
    Python anyway.

Routing evidence, strongest first:

1. ``metadata["kind"] == "aws_live"`` — stamped by the AWS connector on
   every ref it discovers.  This is the connector's own declaration.
2. ``source_type == "aws"`` together with an ``aws_``-prefixed
   ``resource_type`` — a defensive fallback for refs that reached the
   pipeline without the ``kind`` stamp.

Anything else routes to ``CODE``, which preserves the previous behaviour
verbatim for every non-AWS source.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from omniscience_parsers.infra import aws_live

#: ``Source.type`` value used by the AWS connector.
AWS_SOURCE_TYPE: str = "aws"

_AWS_RESOURCE_TYPE_PREFIX: str = "aws_"


class GraphRoute(Enum):
    """Which graph extractor owns a document."""

    CODE = "code"
    """Tree-sitter symbol graph (Python today; other languages degrade to empty)."""

    AWS_LIVE = "aws_live"
    """Live AWS inventory documents produced by the AWS connector."""


def route_document(
    *,
    source_type: str,
    metadata: Mapping[str, Any] | None = None,
) -> GraphRoute:
    """Return the graph route for a document.

    Args:
        source_type: ``Source.type`` / ``DocumentChangeEvent.source_type``.
        metadata: the fetched ``DocumentRef.metadata`` (never the event
            payload's metadata, which is operator-specific).

    Returns:
        :class:`GraphRoute` — never ``None``, so callers always have a
        defined path.  ``CODE`` is the default and keeps existing sources
        on exactly the behaviour they had before this dispatch existed.
    """
    meta = metadata or {}

    if aws_live.supports(meta):
        return GraphRoute.AWS_LIVE

    if source_type == AWS_SOURCE_TYPE:
        resource_type = str(meta.get("resource_type", "") or "")
        if resource_type.startswith(_AWS_RESOURCE_TYPE_PREFIX):
            return GraphRoute.AWS_LIVE

    return GraphRoute.CODE


__all__ = ["AWS_SOURCE_TYPE", "GraphRoute", "route_document"]
