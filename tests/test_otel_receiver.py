"""Tests for the OTLP/HTTP trace receiver (#152).

Coverage
--------
* :func:`omniscience_connectors.otel.parse_otlp_payload` decodes both
  OTLP/HTTP encodings: ``application/x-protobuf`` and
  ``application/json``.
* The route emits ``trace://``, ``service://``, and ``pod://`` canonical
  entity names with cross_ref edges between them.
* Span -> entity mapping is correct (services + pods).
* Idempotency: re-posting the same payload does not duplicate entities or
  edges (validated through the in-memory stub ingester).

ACL invariant
-------------
``test_otel_acl.py`` exercises the auth path; this module focuses on the
parsing + entity-mapping contract.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_connectors.otel import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_PROTOBUF,
    OtelDecodeError,
    canonical_pod_name,
    canonical_service_name,
    canonical_trace_name,
    parse_otlp_payload,
)
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_server.rest.otlp import router as otlp_router
from omniscience_server.rest.otlp_ingester import (
    CROSS_REF_EDGE_TYPE,
    OTEL_POD_ENTITY_TYPE,
    OTEL_SERVICE_ENTITY_TYPE,
    OTEL_TRACE_ENTITY_TYPE,
)

# ---------------------------------------------------------------------------
# OTLP payload builders
# ---------------------------------------------------------------------------

_TRACE_ID_HEX = "0102030405060708090a0b0c0d0e0f10"
_SPAN_A_HEX = "1112131415161718"
_SPAN_B_HEX = "2122232425262728"


def _build_json_payload(
    *,
    trace_id_hex: str = _TRACE_ID_HEX,
    span_id_hex: str = _SPAN_A_HEX,
    service_name: str = "checkout",
    pod_name: str = "checkout-7d8f-abc",
    extra_resource_attrs: dict[str, str] | None = None,
) -> bytes:
    """Build a minimal OTLP/JSON ExportTraceServiceRequest body."""
    resource_attrs: list[dict[str, Any]] = [
        {"key": "service.name", "value": {"stringValue": service_name}},
        {"key": "k8s.pod.name", "value": {"stringValue": pod_name}},
    ]
    if extra_resource_attrs:
        for key, value in extra_resource_attrs.items():
            resource_attrs.append({"key": key, "value": {"stringValue": value}})
    document = {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id_hex,
                                "spanId": span_id_hex,
                                "name": "GET /cart",
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000000050000000",
                                "attributes": [],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    return json.dumps(document).encode("utf-8")


def _build_protobuf_payload(
    *,
    trace_id_hex: str = _TRACE_ID_HEX,
    span_id_hex: str = _SPAN_A_HEX,
    service_name: str = "checkout",
    pod_name: str = "checkout-7d8f-abc",
    extra_resource_attrs: dict[str, str] | None = None,
) -> bytes:
    """Build an OTLP/protobuf ExportTraceServiceRequest body."""
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    from opentelemetry.proto.common.v1 import common_pb2
    from opentelemetry.proto.resource.v1 import resource_pb2
    from opentelemetry.proto.trace.v1 import trace_pb2

    def _kv(key: str, value: str) -> common_pb2.KeyValue:
        return common_pb2.KeyValue(
            key=key,
            value=common_pb2.AnyValue(string_value=value),
        )

    attrs = [
        _kv("service.name", service_name),
        _kv("k8s.pod.name", pod_name),
    ]
    if extra_resource_attrs:
        for k, v in extra_resource_attrs.items():
            attrs.append(_kv(k, v))

    span = trace_pb2.Span(
        trace_id=bytes.fromhex(trace_id_hex),
        span_id=bytes.fromhex(span_id_hex),
        name="GET /cart",
        start_time_unix_nano=1_700_000_000_000_000_000,
        end_time_unix_nano=1_700_000_000_050_000_000,
    )
    scope_span = trace_pb2.ScopeSpans(spans=[span])
    resource_span = trace_pb2.ResourceSpans(
        resource=resource_pb2.Resource(attributes=attrs),
        scope_spans=[scope_span],
    )
    request = trace_service_pb2.ExportTraceServiceRequest(resource_spans=[resource_span])
    return request.SerializeToString()


# ---------------------------------------------------------------------------
# In-memory stub ingester so route tests do not need Postgres
# ---------------------------------------------------------------------------


class _StubEntity:
    def __init__(self, *, entity_type: str, name: str, display_name: str) -> None:
        self.id = uuid.uuid4()
        self.entity_type = entity_type
        self.name = name
        self.display_name = display_name


class _StubEdge:
    def __init__(self, *, source_id: uuid.UUID, target_id: uuid.UUID, metadata: dict[str, Any]):
        self.source_entity_id = source_id
        self.target_entity_id = target_id
        self.edge_type = CROSS_REF_EDGE_TYPE
        self.edge_metadata = metadata


class StubIngester:
    """In-memory replacement for :class:`OtlpIngester` — exercises the
    contract the route depends on without needing a database.
    """

    def __init__(self) -> None:
        self.entities: list[_StubEntity] = []
        self.edges: list[_StubEdge] = []
        self.calls: list[tuple[uuid.UUID | None, int]] = []

    async def ingest(self, *, workspace_id: uuid.UUID | None, parsed: Any) -> int:
        self.calls.append((workspace_id, parsed.total_spans))
        for trace in parsed.traces.values():
            trace_entity = self._upsert(
                OTEL_TRACE_ENTITY_TYPE,
                canonical_trace_name(trace.trace_id_hex),
                trace.trace_id_hex,
            )
            for service_name in sorted(trace.service_names):
                service_entity = self._upsert(
                    OTEL_SERVICE_ENTITY_TYPE,
                    canonical_service_name(service_name),
                    service_name,
                )
                self._upsert_edge(trace_entity.id, service_entity.id, trace.trace_id_hex)
            for pod_name in sorted(trace.pod_names):
                pod_entity = self._upsert(
                    OTEL_POD_ENTITY_TYPE, canonical_pod_name(pod_name), pod_name
                )
                self._upsert_edge(trace_entity.id, pod_entity.id, trace.trace_id_hex)
        return parsed.total_spans

    def _upsert(self, entity_type: str, name: str, display: str) -> _StubEntity:
        for ent in self.entities:
            if ent.entity_type == entity_type and ent.name == name:
                return ent
        entity = _StubEntity(entity_type=entity_type, name=name, display_name=display)
        self.entities.append(entity)
        return entity

    def _upsert_edge(self, source_id: uuid.UUID, target_id: uuid.UUID, trace_id_hex: str) -> None:
        for existing in self.edges:
            if existing.source_entity_id == source_id and existing.target_entity_id == target_id:
                return
        self.edges.append(
            _StubEdge(
                source_id=source_id,
                target_id=target_id,
                metadata={
                    "strategy": "otel_trace",
                    "canonical_name": canonical_trace_name(trace_id_hex),
                },
            )
        )


# ---------------------------------------------------------------------------
# Token + app fixtures
# ---------------------------------------------------------------------------


def _build_mock_token(
    *,
    plaintext: str,
    prefix: str,
    workspace_id: uuid.UUID | None,
    scopes: list[str],
) -> ApiToken:
    hashed = hash_token(plaintext)
    token: ApiToken = MagicMock(spec=ApiToken)
    token.id = uuid.uuid4()
    token.name = "test-token"
    token.token_prefix = prefix
    token.hashed_token = hashed
    token.scopes = scopes
    token.workspace_id = workspace_id
    token.expires_at = datetime.now(tz=UTC) + timedelta(hours=1)
    token.last_used_at = None
    token.is_active = True
    return token


def _fake_session_factory(token: ApiToken) -> Any:
    fake_session = AsyncMock()

    async def _execute(_stmt: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [token]
        return result

    fake_session.execute = _execute
    fake_session.flush = AsyncMock()
    fake_session.commit = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=fake_session)


def _build_app(token: ApiToken, ingester: StubIngester) -> FastAPI:
    app = FastAPI()
    app.state.db_session_factory = _fake_session_factory(token)
    app.state.otlp_ingester = ingester
    app.include_router(otlp_router, prefix="/api/v1")
    return app


@pytest_asyncio.fixture()
async def authed_client_factory() -> AsyncIterator[Any]:  # pragma: no mutate — fixture, not a test
    """Factory that builds an (app, client, token-plaintext, ingester) tuple.

    Tests call this with the desired workspace_id / scopes and receive a
    ready-to-use ASGI client.
    """
    contexts: list[Any] = []

    async def _factory(
        *,
        workspace_id: uuid.UUID | None = None,
        scopes: list[str] | None = None,
    ) -> tuple[FastAPI, AsyncClient, str, StubIngester]:
        plaintext, prefix = generate_token("development")
        token = _build_mock_token(
            plaintext=plaintext,
            prefix=prefix,
            workspace_id=workspace_id or uuid.uuid4(),
            scopes=scopes if scopes is not None else [Scope.otel_write.value],
        )
        ingester = StubIngester()
        app = _build_app(token, ingester)
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        contexts.append(client)
        await client.__aenter__()
        return app, client, plaintext, ingester

    yield _factory

    for client in contexts:
        await client.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# parse_otlp_payload — JSON
# ---------------------------------------------------------------------------


def test_parse_json_payload_extracts_trace_service_pod() -> None:
    """OTLP/JSON: the parser extracts trace_id, service.name, k8s.pod.name."""
    body = _build_json_payload()
    parsed = parse_otlp_payload(body, CONTENT_TYPE_JSON)
    assert parsed.total_spans == 1
    trace = parsed.traces[_TRACE_ID_HEX]
    assert trace.service_names == {"checkout"}
    assert trace.pod_names == {"checkout-7d8f-abc"}
    span = trace.spans[0]
    assert span.span_id_hex == _SPAN_A_HEX
    assert span.name == "GET /cart"


def test_parse_json_payload_accepts_base64_ids() -> None:
    """OTLP/JSON: trace_id can be base64-encoded per the spec."""
    trace_bytes = bytes.fromhex(_TRACE_ID_HEX)
    span_bytes = bytes.fromhex(_SPAN_A_HEX)
    document = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "checkout"}}]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": base64.b64encode(trace_bytes).decode("ascii"),
                                "spanId": base64.b64encode(span_bytes).decode("ascii"),
                                "name": "POST /pay",
                                "startTimeUnixNano": "1",
                                "endTimeUnixNano": "2",
                                "attributes": [],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    parsed = parse_otlp_payload(json.dumps(document).encode(), CONTENT_TYPE_JSON)
    assert parsed.total_spans == 1
    assert _TRACE_ID_HEX in parsed.traces


def test_parse_json_payload_rejects_invalid_json() -> None:
    """Malformed JSON raises :class:`OtelDecodeError` (route -> 400)."""
    with pytest.raises(OtelDecodeError):
        parse_otlp_payload(b"{not-json", CONTENT_TYPE_JSON)


def test_parse_payload_rejects_empty_body() -> None:
    """Empty body raises :class:`OtelDecodeError`."""
    with pytest.raises(OtelDecodeError):
        parse_otlp_payload(b"", CONTENT_TYPE_JSON)


def test_parse_payload_rejects_unknown_content_type() -> None:
    """Unknown Content-Type raises :class:`OtelDecodeError`."""
    with pytest.raises(OtelDecodeError):
        parse_otlp_payload(b"{}", "text/plain")


# ---------------------------------------------------------------------------
# parse_otlp_payload — protobuf
# ---------------------------------------------------------------------------


def test_parse_protobuf_payload_extracts_trace_service_pod() -> None:
    """OTLP/protobuf: the parser extracts trace_id, service.name, k8s.pod.name."""
    body = _build_protobuf_payload()
    parsed = parse_otlp_payload(body, CONTENT_TYPE_PROTOBUF)
    assert parsed.total_spans == 1
    trace = parsed.traces[_TRACE_ID_HEX]
    assert trace.service_names == {"checkout"}
    assert trace.pod_names == {"checkout-7d8f-abc"}


def test_parse_protobuf_payload_rejects_invalid_protobuf() -> None:
    """Malformed protobuf raises :class:`OtelDecodeError`."""
    with pytest.raises(OtelDecodeError):
        parse_otlp_payload(b"\xff\xff\xff\xff", CONTENT_TYPE_PROTOBUF)


# ---------------------------------------------------------------------------
# Canonical-name shapes (the entity-linking contract)
# ---------------------------------------------------------------------------


def test_canonical_trace_name_lowercases_hex() -> None:
    assert canonical_trace_name("ABCDEF1234567890" + "0" * 16) == (
        "trace://abcdef1234567890" + "0" * 16
    )


def test_canonical_trace_name_validates_hex() -> None:
    """Non-hex input raises ValueError — guards downstream key handling."""
    with pytest.raises(ValueError):
        canonical_trace_name("not-hex!!")


def test_canonical_service_name_format() -> None:
    assert canonical_service_name("checkout") == "service://checkout"


def test_canonical_pod_name_format() -> None:
    assert canonical_pod_name("checkout-abc") == "pod://checkout-abc"


# ---------------------------------------------------------------------------
# Route — JSON encoding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_accepts_json_payload(authed_client_factory: Any) -> None:
    """JSON-encoded OTLP payload is accepted (200) and persisted."""
    workspace_id = uuid.uuid4()
    _app, client, plaintext, ingester = await authed_client_factory(workspace_id=workspace_id)

    response = await client.post(
        "/api/v1/otlp/v1/traces",
        content=_build_json_payload(),
        headers={
            "Authorization": f"Bearer {plaintext}",
            "Content-Type": CONTENT_TYPE_JSON,
        },
    )
    assert response.status_code == 200, response.text
    assert ingester.calls == [(workspace_id, 1)]


@pytest.mark.asyncio
async def test_route_accepts_protobuf_payload(authed_client_factory: Any) -> None:
    """Protobuf-encoded OTLP payload is accepted (200) and persisted."""
    workspace_id = uuid.uuid4()
    _app, client, plaintext, ingester = await authed_client_factory(workspace_id=workspace_id)

    response = await client.post(
        "/api/v1/otlp/v1/traces",
        content=_build_protobuf_payload(),
        headers={
            "Authorization": f"Bearer {plaintext}",
            "Content-Type": CONTENT_TYPE_PROTOBUF,
        },
    )
    assert response.status_code == 200, response.text
    assert ingester.calls == [(workspace_id, 1)]


@pytest.mark.asyncio
async def test_route_rejects_unknown_content_type(authed_client_factory: Any) -> None:
    """Non-OTLP Content-Type returns 415."""
    _app, client, plaintext, _ingester = await authed_client_factory()
    response = await client.post(
        "/api/v1/otlp/v1/traces",
        content=b"{}",
        headers={
            "Authorization": f"Bearer {plaintext}",
            "Content-Type": "application/xml",
        },
    )
    assert response.status_code == 415


@pytest.mark.asyncio
async def test_route_rejects_invalid_otlp_body(authed_client_factory: Any) -> None:
    """Malformed body returns 400 without leaking decode internals."""
    _app, client, plaintext, _ingester = await authed_client_factory()
    response = await client.post(
        "/api/v1/otlp/v1/traces",
        content=b"{not-json",
        headers={
            "Authorization": f"Bearer {plaintext}",
            "Content-Type": CONTENT_TYPE_JSON,
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["detail"]["code"] == "invalid_otlp_payload"
    # Server does not echo the upstream parser message.
    assert "JSON" not in body["detail"]["message"]


# ---------------------------------------------------------------------------
# Route — entity / edge mapping contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_emits_canonical_trace_service_pod_entities(
    authed_client_factory: Any,
) -> None:
    """Span -> entity mapping: trace, service, pod entities are emitted with
    canonical ``trace://``, ``service://``, ``pod://`` names.
    """
    _app, client, plaintext, ingester = await authed_client_factory()
    response = await client.post(
        "/api/v1/otlp/v1/traces",
        content=_build_json_payload(),
        headers={
            "Authorization": f"Bearer {plaintext}",
            "Content-Type": CONTENT_TYPE_JSON,
        },
    )
    assert response.status_code == 200

    names_by_type = {(e.entity_type, e.name) for e in ingester.entities}
    assert (OTEL_TRACE_ENTITY_TYPE, canonical_trace_name(_TRACE_ID_HEX)) in names_by_type
    assert (OTEL_SERVICE_ENTITY_TYPE, canonical_service_name("checkout")) in names_by_type
    assert (OTEL_POD_ENTITY_TYPE, canonical_pod_name("checkout-7d8f-abc")) in names_by_type


@pytest.mark.asyncio
async def test_route_emits_cross_ref_edges_to_service_and_pod(
    authed_client_factory: Any,
) -> None:
    """``cross_ref`` edges link the trace entity to its service and pod by
    canonical name (the ``otel_trace`` linker contract).
    """
    _app, client, plaintext, ingester = await authed_client_factory()
    await client.post(
        "/api/v1/otlp/v1/traces",
        content=_build_json_payload(),
        headers={
            "Authorization": f"Bearer {plaintext}",
            "Content-Type": CONTENT_TYPE_JSON,
        },
    )
    # Find the trace entity id
    trace_id = next(e.id for e in ingester.entities if e.entity_type == OTEL_TRACE_ENTITY_TYPE)
    service_id = next(e.id for e in ingester.entities if e.entity_type == OTEL_SERVICE_ENTITY_TYPE)
    pod_id = next(e.id for e in ingester.entities if e.entity_type == OTEL_POD_ENTITY_TYPE)

    edge_pairs = {(e.source_entity_id, e.target_entity_id) for e in ingester.edges}
    assert (trace_id, service_id) in edge_pairs
    assert (trace_id, pod_id) in edge_pairs
    for edge in ingester.edges:
        assert edge.edge_type == CROSS_REF_EDGE_TYPE
        assert edge.edge_metadata["canonical_name"] == canonical_trace_name(_TRACE_ID_HEX)
        assert edge.edge_metadata["strategy"] == "otel_trace"


@pytest.mark.asyncio
async def test_route_idempotent_replay(authed_client_factory: Any) -> None:
    """Re-posting the same payload does not duplicate entities or edges."""
    _app, client, plaintext, ingester = await authed_client_factory()
    body = _build_json_payload()
    headers = {
        "Authorization": f"Bearer {plaintext}",
        "Content-Type": CONTENT_TYPE_JSON,
    }
    await client.post("/api/v1/otlp/v1/traces", content=body, headers=headers)
    entity_count = len(ingester.entities)
    edge_count = len(ingester.edges)

    await client.post("/api/v1/otlp/v1/traces", content=body, headers=headers)
    assert len(ingester.entities) == entity_count
    assert len(ingester.edges) == edge_count
