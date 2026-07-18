"""Per-response MCP v1 lineage, freshness, and consistency metadata."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from typing import Any

from fastapi import FastAPI
from omniscience_core.db.models import Source
from sqlalchemy import select

from omniscience_server.mcp.contract import (
    CONTRACT_VERSION,
    is_pinnable_source_commit,
    resolve_source_commit,
    tool_schema_sha256,
)
from omniscience_server.mcp.tools import NO_PROJECTION_READ_TOOLS


@dataclass(frozen=True)
class SourceSnapshot:
    """Freshness fields for one authoritative source used by a response."""

    source_id: uuid.UUID
    last_sync_at: datetime | None
    freshness_sla_seconds: int | None
    status: str = "active"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _wire_time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _collect_lineage(value: Any, collected: set[uuid.UUID]) -> None:
    """Collect only explicit wire lineage keys, never similarly named fields."""
    if isinstance(value, list):
        for item in value:
            _collect_lineage(item, collected)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key == "source_id":
            parsed = _as_uuid(item)
            if parsed is not None:
                collected.add(parsed)
        elif key == "source":
            candidate = item.get("id") if isinstance(item, dict) else item
            parsed = _as_uuid(candidate)
            if parsed is not None:
                collected.add(parsed)
            if isinstance(item, dict):
                _collect_lineage(item, collected)
        elif isinstance(item, (dict, list)):
            _collect_lineage(item, collected)


def extract_used_source_ids(tool_name: str, payload: Mapping[str, Any]) -> set[uuid.UUID]:
    """Extract source IDs from the actual response lineage for a known tool."""
    roots: dict[str, tuple[str, ...]] = {
        "search": ("hits",),
        "get_document": ("document",),
        "get_entity": ("entity",),
        "get_related_entities": ("seed", "related"),
        "list_entities": ("entities",),
        "list_sources": ("sources",),
        "source_stats": ("id",),
        "resolve_incident": (
            "alert",
            "target_resource",
            "responsible_pr",
            "slack_threads",
            "similar_past",
        ),
        "incident_timeline": ("events", "incident"),
        "blast_radius": ("seed", "impacts"),
        "replay_context": ("response", "replay"),
        "suggest_runbook": ("suggestions",),
        "find_similar_incidents": ("incident", "matches"),
        "generate_postmortem": ("report", "timeline", "evidence"),
    }
    collected: set[uuid.UUID] = set()
    if tool_name == "list_sources":
        for source in payload.get("sources", []):
            if isinstance(source, dict):
                parsed = _as_uuid(source.get("id"))
                if parsed is not None:
                    collected.add(parsed)
        return collected
    for root in roots.get(tool_name, ()):
        if root == "id" and tool_name == "source_stats":
            parsed = _as_uuid(payload.get(root)) or _as_uuid(payload.get("source_id"))
            if parsed is not None:
                collected.add(parsed)
            continue
        _collect_lineage(payload.get(root), collected)
    return collected


def _freshness(
    source_ids: set[uuid.UUID],
    snapshots: Mapping[uuid.UUID, SourceSnapshot],
    now: datetime,
    *,
    effective_at: datetime | None = None,
) -> tuple[dict[str, Any], str | None]:
    evaluated = _utc(now)
    evidence_boundary = _utc(effective_at) if effective_at is not None else evaluated
    if not source_ids:
        return {
            "status": "unknown",
            "evaluated_at": _wire_time(evaluated),
            "oldest_source_age_seconds": None,
            "stale_source_ids": [],
            "reason": "missing_lineage",
        }, "missing_lineage"

    missing = source_ids.difference(snapshots)
    never_synced: list[uuid.UUID] = []
    unknown_sla: list[uuid.UUID] = []
    stale: list[uuid.UUID] = []
    degraded: list[uuid.UUID] = []
    post_boundary: list[uuid.UUID] = []
    ages: list[float] = []
    for source_id in sorted(source_ids, key=str):
        snapshot = snapshots.get(source_id)
        if snapshot is None:
            continue
        if str(snapshot.status) == "error":
            degraded.append(source_id)
        if snapshot.last_sync_at is None:
            never_synced.append(source_id)
            continue
        synced_at = _utc(snapshot.last_sync_at)
        if synced_at > evidence_boundary:
            post_boundary.append(source_id)
            continue
        age = max(0.0, (evidence_boundary - synced_at).total_seconds())
        ages.append(age)
        if snapshot.freshness_sla_seconds is None:
            unknown_sla.append(source_id)
        elif age > snapshot.freshness_sla_seconds:
            stale.append(source_id)

    oldest = max(ages) if ages else None
    oldest_wire = ceil(oldest) if oldest is not None else None
    if degraded:
        status = "degraded"
        reason = "source_degraded"
    elif stale:
        status = "stale"
        reason = "stale_source"
    elif never_synced:
        status = "unknown"
        reason = "never_synced"
    elif missing or post_boundary or unknown_sla:
        status = "unknown"
        reason = "missing_lineage" if missing or post_boundary else "freshness_sla_unknown"
    else:
        status = "fresh"
        reason = None
    result: dict[str, Any] = {
        "status": status,
        "evaluated_at": _wire_time(evaluated),
        "oldest_source_age_seconds": oldest_wire,
        "stale_source_ids": [str(item) for item in stale],
    }
    if degraded:
        result["degraded_source_ids"] = [str(item) for item in degraded]
    if reason is not None:
        result["reason"] = reason
    return result, reason


def _consistency(
    tool_name: str,
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str | None]:
    nested_meta = payload.get("meta")
    nested = nested_meta if isinstance(nested_meta, dict) else {}
    degraded_value = payload.get(
        "degraded_subsystems",
        nested.get("degraded_subsystems", []),
    )
    degraded = (
        sorted(set(degraded_value))
        if type(degraded_value) is list
        and all(type(item) is str and item for item in degraded_value)
        else []
    )
    lag_value = payload.get(
        "projection_lag_versions",
        nested.get("projection_lag_versions"),
    )
    has_lag_evidence = type(lag_value) is int and lag_value >= 0
    has_watermark_evidence = payload.get("pinned_watermark") is not None
    lag = lag_value if has_lag_evidence else 0
    if degraded or lag > 0:
        return {
            "status": "degraded",
            "degraded_subsystems": degraded,
            "projection_lag_versions": lag,
        }, "projection_divergence"
    if (
        has_lag_evidence
        or has_watermark_evidence
        or tool_name in NO_PROJECTION_READ_TOOLS
    ):
        return {
            "status": "converged",
            "degraded_subsystems": [],
            "projection_lag_versions": 0,
        }, None
    return {
        "status": "unknown",
        "degraded_subsystems": [],
        "projection_lag_versions": 0,
    }, "consistency_unknown"


def build_response_meta(
    *,
    tool_name: str,
    payload: Mapping[str, Any],
    workspace_id: uuid.UUID,
    source_snapshots: Mapping[uuid.UUID, SourceSnapshot],
    now: datetime | None = None,
    effective_as_of: datetime | None = None,
    source_commit: str | None = None,
    contract_match: bool = True,
) -> dict[str, Any]:
    """Build additive v1 metadata while leaving legacy fields untouched."""
    generated = _utc(now or datetime.now(UTC))
    source_ids = extract_used_source_ids(tool_name, payload)
    freshness, freshness_reason = _freshness(
        source_ids,
        source_snapshots,
        generated,
        effective_at=effective_as_of,
    )
    consistency, consistency_reason = _consistency(tool_name, payload)
    commit = source_commit or resolve_source_commit()
    reason = freshness_reason or consistency_reason
    if not contract_match or not is_pinnable_source_commit(commit):
        reason = "contract_mismatch"
    return {
        "contract": {
            "version": CONTRACT_VERSION,
            "commit": commit,
            "schema_sha256": tool_schema_sha256(tool_name),
        },
        "workspace_id": str(workspace_id),
        "generated_at": _wire_time(generated),
        "effective_as_of": _wire_time(effective_as_of or generated),
        "freshness": freshness,
        "consistency": consistency,
        "fallback": {"required": reason is not None, "reason": reason},
    }


def attach_response_meta(
    *,
    tool_name: str,
    payload: dict[str, Any],
    workspace_id: uuid.UUID,
    source_snapshots: Mapping[uuid.UUID, SourceSnapshot],
    now: datetime | None = None,
    effective_as_of: datetime | None = None,
    source_commit: str | None = None,
) -> dict[str, Any]:
    """Copy a successful payload and replace/extend its top-level ``meta``."""
    result = dict(payload)
    legacy_meta = payload.get("meta")
    meta = build_response_meta(
        tool_name=tool_name,
        payload=payload,
        workspace_id=workspace_id,
        source_snapshots=source_snapshots,
        now=now,
        effective_as_of=effective_as_of,
        source_commit=source_commit,
    )
    if isinstance(legacy_meta, dict):
        meta.update({key: value for key, value in legacy_meta.items() if key not in meta})
    result["meta"] = meta
    return result


async def attach_runtime_meta(
    *,
    app: FastAPI,
    tool_name: str,
    payload: dict[str, Any],
    workspace_id: uuid.UUID,
    effective_as_of: datetime | None = None,
) -> dict[str, Any]:
    """Resolve workspace-filtered source snapshots and attach v1 metadata."""
    source_ids = extract_used_source_ids(tool_name, payload)
    snapshots: dict[uuid.UUID, SourceSnapshot] = {}
    factory = getattr(app.state, "db_session_factory", None)
    if factory is not None and source_ids:
        async with factory() as session:
            result = await session.execute(
                select(Source).where(
                    Source.id.in_(source_ids),
                    Source.tenant_id == workspace_id,
                )
            )
            for source in result.scalars().all():
                snapshots[source.id] = SourceSnapshot(
                    source_id=source.id,
                    last_sync_at=source.last_sync_at,
                    freshness_sla_seconds=source.freshness_sla_seconds,
                    status=str(source.status),
                )
    return attach_response_meta(
        tool_name=tool_name,
        payload=payload,
        workspace_id=workspace_id,
        source_snapshots=snapshots,
        effective_as_of=effective_as_of,
    )


def build_source_free_meta(
    *,
    workspace_id: uuid.UUID,
    source_commit: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build source-free metadata and flag an unpinnable contract identity."""
    generated = _utc(now or datetime.now(UTC))
    contract_pinnable = is_pinnable_source_commit(source_commit)
    return {
        "contract": {
            "version": CONTRACT_VERSION,
            "commit": source_commit,
            "schema_sha256": tool_schema_sha256("contract_info"),
        },
        "workspace_id": str(workspace_id),
        "generated_at": _wire_time(generated),
        "effective_as_of": _wire_time(generated),
        "freshness": {
            "status": "unknown",
            "evaluated_at": _wire_time(generated),
            "oldest_source_age_seconds": None,
            "stale_source_ids": [],
            "reason": reason,
        },
        "consistency": {
            "status": "unknown",
            "degraded_subsystems": [],
            "projection_lag_versions": 0,
        },
        "fallback": {
            "required": not contract_pinnable,
            "reason": None if contract_pinnable else "contract_mismatch",
        },
    }


__all__ = [
    "SourceSnapshot",
    "attach_response_meta",
    "attach_runtime_meta",
    "build_response_meta",
    "build_source_free_meta",
    "extract_used_source_ids",
]
