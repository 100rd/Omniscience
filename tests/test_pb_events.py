"""Tests for pure-Python Protobuf serialization and deserialization."""

from __future__ import annotations

import datetime
import uuid

from omniscience_core.queue.messages import DLQMessage, EdgeUpsertEvent, EntityUpsertEvent
from omniscience_core.queue.pb_events import (
    deserialize_dlq_message,
    deserialize_document_change_event,
    deserialize_edge_upsert_event,
    deserialize_entity_upsert_event,
    serialize_dlq_message,
    serialize_document_change_event,
    serialize_edge_upsert_event,
    serialize_entity_upsert_event,
)
from omniscience_server.ingestion.events import DocumentChangeEvent


def test_protobuf_document_change_event() -> None:
    event = DocumentChangeEvent(
        source_id=uuid.uuid4(),
        source_type="git",
        external_id="file.py",
        uri="file:///path/to/file.py",
        action="created",
    )
    serialized = serialize_document_change_event(event)
    deserialized = deserialize_document_change_event(serialized)

    assert deserialized["source_id"] == event.source_id
    assert deserialized["source_type"] == event.source_type
    assert deserialized["external_id"] == event.external_id
    assert deserialized["uri"] == event.uri
    assert deserialized["action"] == event.action


def test_protobuf_entity_upsert_event() -> None:
    event = EntityUpsertEvent(
        workspace_id=str(uuid.uuid4()),
        id=str(uuid.uuid4()),
        source_id=str(uuid.uuid4()),
        entity_type="otel_service",
        name="service://checkout",
        display_name="checkout",
        metadata={"foo": "bar", "num": "123"},
    )
    serialized = serialize_entity_upsert_event(event)
    deserialized = deserialize_entity_upsert_event(serialized)

    assert deserialized["workspace_id"] == event.workspace_id
    assert deserialized["id"] == event.id
    assert deserialized["source_id"] == event.source_id
    assert deserialized["entity_type"] == event.entity_type
    assert deserialized["name"] == event.name
    assert deserialized["display_name"] == event.display_name
    assert deserialized["metadata"] == event.metadata


def test_protobuf_edge_upsert_event() -> None:
    event = EdgeUpsertEvent(
        workspace_id=str(uuid.uuid4()),
        id=str(uuid.uuid4()),
        source_entity_id=str(uuid.uuid4()),
        target_entity_id=str(uuid.uuid4()),
        edge_type="cross_ref",
        metadata={"strategy": "otel_trace"},
    )
    serialized = serialize_edge_upsert_event(event)
    deserialized = deserialize_edge_upsert_event(serialized)

    assert deserialized["workspace_id"] == event.workspace_id
    assert deserialized["id"] == event.id
    assert deserialized["source_entity_id"] == event.source_entity_id
    assert deserialized["target_entity_id"] == event.target_entity_id
    assert deserialized["edge_type"] == event.edge_type
    assert deserialized["metadata"] == event.metadata


def test_protobuf_dlq_message() -> None:
    now = datetime.datetime.now(datetime.UTC)
    # round-trip string representation for ISO comparison
    failed_at = datetime.datetime.fromisoformat(now.isoformat())
    event = DLQMessage(
        original_subject="ingest.changes.git",
        original_payload='{"source_id": "abc"}',
        error="Connection timed out",
        attempt_count=3,
        failed_at=failed_at,
    )
    serialized = serialize_dlq_message(event)
    deserialized = deserialize_dlq_message(serialized)

    assert deserialized["original_subject"] == event.original_subject
    assert deserialized["original_payload"] == event.original_payload
    assert deserialized["error"] == event.error
    assert deserialized["attempt_count"] == event.attempt_count
    assert deserialized["failed_at"] == event.failed_at
