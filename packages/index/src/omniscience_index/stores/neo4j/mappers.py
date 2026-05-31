"""Row mappers, metadata serialisation, datetime coercers, and fingerprinting.

All functions here are pure (no I/O, no driver calls).  They convert
between Neo4j driver row dicts / duck-typed domain objects and the
view types from ``omniscience_core.storage.graph``.

Also contains the traversal Cypher builder (``_build_traverse_cypher``)
and the depth / edge-type validators, which belong here because they
operate on the Cypher template strings from ``_cypher``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphEdgeView,
    GraphResultView,
)

from omniscience_index.stores.neo4j._cypher import (
    _EDGE_TYPE_REGEX,
    _TRAVERSE_AS_OF_CYPHER_TEMPLATE,
    _TRAVERSE_CYPHER_TEMPLATE,
    _WRITE_WORKSPACE_PARAM,
)

# ---------------------------------------------------------------------------
# Depth + edge-type validators
# ---------------------------------------------------------------------------

_FINGERPRINT_DIGEST_BYTES: int = 32  # SHA-256 output


def _clamp_depth(requested: int, ceiling: int) -> int:
    """Clamp ``requested`` into ``[1, ceiling]`` — protects the planner."""
    if requested < 1:
        return 1
    if requested > ceiling:
        return ceiling
    return requested


def _validate_edge_types(edge_types: list[str] | None) -> list[str] | None:
    """Validate user-supplied edge types against the allowlist regex."""
    if edge_types is None:
        return None
    for value in edge_types:
        if not _EDGE_TYPE_REGEX.match(value):
            raise ValueError(f"invalid_edge_type:{value}")
    return edge_types


def _edge_index_name(prefix: str, rel_type: str) -> str:
    """Build a stable, unique index name for a per-type relationship index."""
    return f"{prefix}__{rel_type}"


def _build_traverse_cypher(
    max_depth: int,
    edge_types: list[str] | None,
    *,
    as_of: bool = False,
) -> str:
    """Render the traversal Cypher with a static (validated) depth bound.

    ``max_depth`` must already be clamped by :func:`_clamp_depth`.
    ``edge_types`` must already be validated by :func:`_validate_edge_types`.
    ``as_of=True`` selects the bitemporal template (ADR-0008 §5).
    """
    if edge_types:
        quoted = ",".join(f"'{et}'" for et in edge_types)
        filter_clause = f" AND r.edge_type IN [{quoted}]"
    else:
        filter_clause = ""
    template = _TRAVERSE_AS_OF_CYPHER_TEMPLATE if as_of else _TRAVERSE_CYPHER_TEMPLATE
    rendered = template.replace("__MAX_DEPTH__", str(max_depth))
    return rendered.replace("__EDGE_TYPE_FILTER__", filter_clause)


# ---------------------------------------------------------------------------
# Datetime coercers
# ---------------------------------------------------------------------------


def _coerce_to_datetime(value: Any) -> datetime:
    """Coerce a Neo4j temporal-like value to a Python aware ``datetime``."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if hasattr(value, "to_native"):
        native = value.to_native()
        if isinstance(native, datetime):
            return native if native.tzinfo else native.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise TypeError(f"cannot coerce to datetime: {type(value).__name__}")


def _coerce_to_date(value: Any) -> date:
    """Coerce a Neo4j-like temporal value to ``datetime.date``."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "to_native"):
        native = value.to_native()
        if isinstance(native, datetime):
            return native.date()
        if isinstance(native, date):
            return native
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"cannot coerce to date: {type(value).__name__}")


def _optional_datetime(value: Any) -> datetime | None:
    """Coerce a Neo4j temporal-or-null payload to ``datetime | None``."""
    if value is None:
        return None
    return _coerce_to_datetime(value)


def _as_of_to_param(as_of: datetime) -> str:
    """Serialise a Python ``datetime`` to the Cypher ``$as_of`` parameter."""
    aware = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    return aware.isoformat()


# ---------------------------------------------------------------------------
# Metadata JSON encoding (issue #226)
# ---------------------------------------------------------------------------


def _coerce_metadata(value: Any) -> dict[str, Any]:
    """Coerce a duck-typed ``metadata`` value into a dict[str, Any]."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    raise TypeError(f"metadata must be dict, got {type(value).__name__}")


def _serialise_metadata(value: Any) -> str:
    """Encode a metadata dict (or ``None``) to a JSON string for Neo4j."""
    coerced = _coerce_metadata(value)
    return json.dumps(coerced, separators=(",", ":"), sort_keys=True, default=str)


def _deserialise_metadata(raw: Any) -> dict[str, Any]:
    """Decode a JSON string from Neo4j back into a ``dict[str, Any]``."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        if not raw:
            return {}
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise TypeError(f"metadata JSON must decode to dict, got {type(decoded).__name__}")
        return {str(k): v for k, v in decoded.items()}
    if isinstance(raw, dict):
        return _coerce_metadata(raw)
    raise TypeError(f"cannot deserialise metadata of type {type(raw).__name__}")


def _serialise_metadata_param(params: dict[str, Any]) -> None:
    """Replace ``params['metadata']`` (dict) with its JSON-encoded form."""
    current = params.get("metadata")
    if isinstance(current, str):
        return
    params["metadata"] = _serialise_metadata(current)


# ---------------------------------------------------------------------------
# State-change fingerprinting (ADR-0008 §2 — issue #131)
# ---------------------------------------------------------------------------


def _stable_repr(value: Any) -> str:
    """Stable string repr for a primitive / dict / list — sorted keys."""
    if value is None:
        return "None"
    if isinstance(value, dict):
        items = sorted(((str(k), _stable_repr(v)) for k, v in value.items()))
        return "{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_stable_repr(v) for v in value) + "]"
    return repr(value)


def _entity_state_fingerprint(params: dict[str, Any]) -> str:
    """Compute the entity-state fingerprint for ``params``.

    Covers ``kind``, ``name``, ``display_name``, ``source_id``,
    ``chunk_id``, ``metadata`` per ADR-0008 §2.
    """
    payload = "|".join(
        (
            _stable_repr(params.get("entity_type")),
            _stable_repr(params.get("name")),
            _stable_repr(params.get("display_name")),
            _stable_repr(params.get("source_id")),
            _stable_repr(params.get("chunk_id")),
            _stable_repr(params.get("metadata")),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _edge_state_fingerprint(params: dict[str, Any]) -> str:
    """Compute the edge-state fingerprint covering ``edge_type``, ``source_id``, ``metadata``."""
    payload = "|".join(
        (
            _stable_repr(params.get("edge_type")),
            _stable_repr(params.get("source_id")),
            _stable_repr(params.get("metadata")),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Row → view converters
# ---------------------------------------------------------------------------


def _entity_record_to_view(record: dict[str, Any]) -> EntityNodeView:
    """Build an :class:`EntityNodeView` from a single-row Cypher result."""
    return EntityNodeView(
        id=uuid.UUID(str(record["id"])),
        name=str(record["name"]),
        kind=str(record["kind"]),
        source=str(record["source_id"]),
        chunk_text=record.get("chunk_text"),
        depth=0,
        edge_type=None,
        valid_from=_optional_datetime(record.get("valid_from")),
        valid_to=_optional_datetime(record.get("valid_to")),
        recorded_at=_optional_datetime(record.get("recorded_at")),
    )


def _neighbour_to_view(payload: dict[str, Any]) -> EntityNodeView:
    """Build a neighbour :class:`EntityNodeView` from collect() output."""
    return EntityNodeView(
        id=uuid.UUID(str(payload["id"])),
        name=str(payload["name"]),
        kind=str(payload["kind"]),
        source=str(payload["source_id"]),
        chunk_text=payload.get("chunk_text"),
        depth=int(payload.get("depth", 1)),
        edge_type=(str(payload["edge_type"]) if payload.get("edge_type") else None),
        valid_from=_optional_datetime(payload.get("valid_from")),
        valid_to=_optional_datetime(payload.get("valid_to")),
        recorded_at=_optional_datetime(payload.get("recorded_at")),
    )


def _edge_tuple_to_view(triplet: list[Any]) -> GraphEdgeView:
    """Build a :class:`GraphEdgeView` from a traversal collect() row."""
    valid_from = triplet[3] if len(triplet) > 3 else None
    valid_to = triplet[4] if len(triplet) > 4 else None
    recorded_at = triplet[5] if len(triplet) > 5 else None
    return GraphEdgeView(
        from_entity=str(triplet[0]),
        to_entity=str(triplet[1]),
        edge_type=str(triplet[2]),
        valid_from=_optional_datetime(valid_from),
        valid_to=_optional_datetime(valid_to),
        recorded_at=_optional_datetime(recorded_at),
    )


def _rows_to_graph_result(row: dict[str, Any]) -> GraphResultView:
    """Assemble a :class:`GraphResultView` from a single traversal row."""
    seed = EntityNodeView(
        id=uuid.UUID(str(row["id"])),
        name=str(row["seed_name"]),
        kind=str(row["seed_kind"]),
        source=str(row["seed_source_id"]),
        chunk_text=row.get("seed_chunk_text"),
        depth=0,
        edge_type=None,
        valid_from=_optional_datetime(row.get("seed_valid_from")),
        valid_to=_optional_datetime(row.get("seed_valid_to")),
        recorded_at=_optional_datetime(row.get("seed_recorded_at")),
    )
    related_raw = row.get("neighbours") or []
    related = [_neighbour_to_view(n) for n in related_raw if n and n.get("name")]
    edges_raw = row.get("edges") or []
    edges = [_edge_tuple_to_view(e) for e in edges_raw if e and len(e) >= 3 and e[0] and e[1]]
    return GraphResultView(seed=seed, related=related, edges=edges)


# ---------------------------------------------------------------------------
# Parameter builders
# ---------------------------------------------------------------------------


def _entity_to_params(
    ext_ent: Any,
    source_id: uuid.UUID,
    workspace_id: uuid.UUID,
    now: str,
) -> dict[str, Any]:
    """Build the parameter map for the entity upsert Cypher.

    Duck-types over :class:`EntityUpsert` and the parser's
    ``ExtractedEntity`` shape.
    """
    entity_id = getattr(ext_ent, "id", None)
    if entity_id is None:
        raise ValueError("entity_missing_id")
    chunk_id = getattr(ext_ent, "chunk_id", None)
    return {
        _WRITE_WORKSPACE_PARAM: str(workspace_id),
        "id": str(entity_id),
        "source_id": str(source_id),
        "entity_type": str(getattr(ext_ent, "entity_type", "")),
        "name": str(getattr(ext_ent, "name", "")),
        "display_name": str(getattr(ext_ent, "display_name", "")),
        "chunk_id": str(chunk_id) if chunk_id else None,
        "metadata": _coerce_metadata(getattr(ext_ent, "metadata", None)),
        "now": now,
    }


def _edge_to_params(
    ext_edge: Any,
    source_id: uuid.UUID,
    workspace_id: uuid.UUID,
    name_to_id: dict[str, uuid.UUID],
    now: str,
) -> dict[str, Any] | None:
    """Build the parameter map for the edge upsert Cypher.

    Returns ``None`` when the edge's target cannot be resolved in the
    current batch — mirrors :meth:`IndexWriter.upsert_graph`, which
    skips unresolvable cross-file edges for later passes.
    """
    source_entity_id = getattr(ext_edge, "source_entity_id", None)
    target_name = getattr(ext_edge, "target_name", None)
    target_entity_id = getattr(ext_edge, "target_entity_id", None)

    if target_entity_id is None and target_name is not None:
        target_entity_id = name_to_id.get(str(target_name))

    if source_entity_id is None or target_entity_id is None:
        return None
    if source_entity_id == target_entity_id:
        return None  # skip self-loops (parity with pgvector writer)

    edge_type = str(getattr(ext_edge, "edge_type", ""))
    if not _EDGE_TYPE_REGEX.match(edge_type):
        raise ValueError(f"invalid_edge_type:{edge_type}")

    return {
        _WRITE_WORKSPACE_PARAM: str(workspace_id),
        "source_id_ent": str(source_entity_id),
        "target_id_ent": str(target_entity_id),
        "source_id": str(source_id),
        "edge_type": edge_type,
        "metadata": _coerce_metadata(getattr(ext_edge, "metadata", None)),
        "now": now,
    }
