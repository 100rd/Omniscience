"""Unit tests for the new deterministic matchers (ARN, OTel, tags)."""

from __future__ import annotations

import uuid

from omniscience_core.storage.graph import EntityNodeView
from omniscience_index.matchers import arn_match, otel_match, tags_match


def test_arn_match() -> None:
    ent_a = EntityNodeView(
        id=uuid.uuid4(),
        name="node-a",
        kind="aws_resource",
        source="aws",
        chunk_text=None,
        metadata={"arn": "arn:aws:s3:::mybucket"},
    )
    ent_b = EntityNodeView(
        id=uuid.uuid4(),
        name="node-b",
        kind="tfstate_instance",
        source="terraform",
        chunk_text=None,
        metadata={"arn": "arn:aws:s3:::mybucket"},
    )
    ent_c = EntityNodeView(
        id=uuid.uuid4(),
        name="node-c",
        kind="tfstate_instance",
        source="terraform",
        chunk_text=None,
        metadata={"arn": "arn:aws:s3:::otherbucket"},
    )
    ent_d = EntityNodeView(
        id=uuid.uuid4(),
        name="node-d",
        kind="tfstate_instance",
        source="terraform",
        chunk_text=None,
        metadata={},
    )

    assert arn_match(ent_a, ent_b) == 1.0
    assert arn_match(ent_a, ent_c) == 0.0
    assert arn_match(ent_a, ent_d) == 0.0


def test_otel_match() -> None:
    # Service name match
    ent_a = EntityNodeView(
        id=uuid.uuid4(),
        name="my-service",
        kind="otel_service",
        source="otel",
        chunk_text=None,
        metadata={"service_name": "my-service"},
    )
    ent_b = EntityNodeView(
        id=uuid.uuid4(),
        name="My-Service",
        kind="service",
        source="k8s",
        chunk_text=None,
        metadata={"service_name": "my-service"},
    )

    # Pod name match
    ent_c = EntityNodeView(
        id=uuid.uuid4(),
        name="pod-123",
        kind="otel_pod",
        source="otel",
        chunk_text=None,
        metadata={"pod_name": "pod-123"},
    )
    ent_d = EntityNodeView(
        id=uuid.uuid4(),
        name="pod-123",
        kind="pod",
        source="k8s",
        chunk_text=None,
        metadata={"pod_name": "pod-123"},
    )

    assert otel_match(ent_a, ent_b) == 1.0
    assert otel_match(ent_c, ent_d) == 1.0
    assert otel_match(ent_a, ent_c) == 0.0


def test_tags_match() -> None:
    ent_a = EntityNodeView(
        id=uuid.uuid4(),
        name="node-a",
        kind="resource",
        source="aws",
        chunk_text=None,
        metadata={"tags": {"env": "prod", "team": "platform"}},
    )
    ent_b = EntityNodeView(
        id=uuid.uuid4(),
        name="node-b",
        kind="resource",
        source="k8s",
        chunk_text=None,
        metadata={"tags": {"env": "prod", "team": "platform", "app": "gateway"}},
    )
    ent_c = EntityNodeView(
        id=uuid.uuid4(),
        name="node-c",
        kind="resource",
        source="k8s",
        chunk_text=None,
        metadata={"tags": {"env": "staging"}},
    )

    assert tags_match(ent_a, ent_b) == 1.0
    assert tags_match(ent_a, ent_c) == 0.0
