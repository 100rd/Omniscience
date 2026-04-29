"""OpenTelemetry trace receiver connector.

This connector is push-based: an OTel client (e.g. the OTel Collector or a
language SDK) speaks OTLP/HTTP to the Omniscience server, and the server
hands the parsed payload to the persistence layer that lives outside this
module.  ``discover()`` and ``fetch()`` are no-ops — data does not arrive
through the pull-style ingestion pipeline.

ACL invariant — TENANT IDENTITY IS RESOLVED FROM THE BEARER TOKEN ONLY
----------------------------------------------------------------------
The OTLP route reads the workspace UUID from the authenticated
``ApiToken`` (via ``request.state.api_token``).  Span attributes
(``resource.attributes['tenant.id']``, ``service.namespace``,
``deployment.environment``, custom keys, etc.) are tenant-writable
upstream content.  Any client process can set them to arbitrary values.
This module therefore treats span attributes strictly as opaque data:
they may be carried as entity metadata but they MUST NOT be promoted to
workspace identity.

Module surface
--------------
``parse_otlp_payload(body, content_type) -> ParsedTraces``
    Pure function; decodes OTLP/HTTP protobuf or JSON into a
    workspace-agnostic intermediate form.  Raises :class:`OtelDecodeError`
    on malformed input.  Does no persistence and consults no auth state.

``OtelConnector``
    Subclass of :class:`~omniscience_connectors.base.Connector` registered
    under ``connector_type = "otel"`` for catalogue/discovery uniformity.
    Provides ``parse_payload`` as a thin instance-level wrapper around
    :func:`parse_otlp_payload`.

Canonical naming helpers
~~~~~~~~~~~~~~~~~~~~~~~~
Cross-source entity linking matches on the canonical names produced by:

* :func:`canonical_trace_name`   — ``trace://{trace_id_hex}``
* :func:`canonical_service_name` — ``service://{service.name}``
* :func:`canonical_pod_name`     — ``pod://{k8s.pod.name}``

These shapes are the contract every connector that emits trace context
agrees on; the ``otel_trace`` cross-reference linker is implemented as a
thin wrapper over :func:`omniscience_index.matchers.exact_name_match`
applied to the ``trace://`` canonical form (see PR body for the
linker-strategy rationale).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

from pydantic import BaseModel

from omniscience_connectors.base import Connector, DocumentRef, FetchedDocument

__all__ = [
    "CONTENT_TYPE_JSON",
    "CONTENT_TYPE_PROTOBUF",
    "OtelConfig",
    "OtelConnector",
    "OtelDecodeError",
    "ParsedSpan",
    "ParsedTrace",
    "ParsedTraces",
    "canonical_pod_name",
    "canonical_service_name",
    "canonical_trace_name",
    "parse_otlp_payload",
]

logger = logging.getLogger(__name__)


CONTENT_TYPE_PROTOBUF: Final[str] = "application/x-protobuf"
CONTENT_TYPE_JSON: Final[str] = "application/json"

# Hard cap so a malicious client cannot exhaust memory.  Standard OTel
# Collector flush is ~1MB; 16MB is a generous ceiling that still fits in
# a single FastAPI request buffer.
_MAX_BODY_BYTES: Final[int] = 16 * 1024 * 1024


class OtelDecodeError(ValueError):
    """Raised when an OTLP payload cannot be decoded.

    The route translates this into HTTP 400 without leaking decode-internal
    detail to the client (the message is logged server-side only).
    """


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class OtelConfig(BaseModel):
    """Public configuration for the OTel receiver (no secrets)."""

    endpoint_path: str = "/v1/traces"
    """OTLP/HTTP path the receiver listens on (relative to the OTLP prefix)."""

    accept_json: bool = True
    """Whether to accept OTLP/JSON payloads."""

    accept_protobuf: bool = True
    """Whether to accept OTLP/protobuf payloads (the standard wire format)."""


# ---------------------------------------------------------------------------
# Parsed-trace data model (workspace-agnostic — no tenant identity here)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedSpan:
    """A single span extracted from an OTLP payload.

    All fields are derived from span content alone — no tenant signal is
    carried because tenant identity comes from the bearer token, never
    from the span.
    """

    trace_id_hex: str
    span_id_hex: str
    name: str
    service_name: str
    service_namespace: str
    pod_name: str
    start_unix_nano: int
    end_unix_nano: int


@dataclass
class ParsedTrace:
    """A trace assembled from one or more spans sharing a ``trace_id``."""

    trace_id_hex: str
    spans: list[ParsedSpan] = field(default_factory=list)
    service_names: set[str] = field(default_factory=set)
    pod_names: set[str] = field(default_factory=set)

    @property
    def start_unix_nano(self) -> int:
        """Earliest ``start_unix_nano`` across the trace's spans."""
        return min((s.start_unix_nano for s in self.spans), default=0)

    @property
    def end_unix_nano(self) -> int:
        """Latest ``end_unix_nano`` across the trace's spans."""
        return max((s.end_unix_nano for s in self.spans), default=0)


@dataclass
class ParsedTraces:
    """Result of decoding one OTLP trace export request."""

    traces: dict[str, ParsedTrace] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_spans(self) -> int:
        return sum(len(t.spans) for t in self.traces.values())


# ---------------------------------------------------------------------------
# Canonical-name helpers (the entity-linking contract)
# ---------------------------------------------------------------------------


def canonical_trace_name(trace_id_hex: str) -> str:
    """Return ``trace://{trace_id_hex}`` (lower-cased, hex-validated)."""
    return f"trace://{_normalise_hex(trace_id_hex)}"


def canonical_service_name(service_name: str) -> str:
    """Return ``service://{service_name}`` for cross-source linking."""
    return f"service://{service_name}"


def canonical_pod_name(pod_name: str) -> str:
    """Return ``pod://{pod_name}`` for cross-source linking."""
    return f"pod://{pod_name}"


def _normalise_hex(value: str) -> str:
    cleaned = value.lower()
    if not cleaned:
        return ""
    # Validate that the string is actually hex; reject upstream-supplied
    # control characters / unicode that could break downstream key handling.
    int(cleaned, 16)
    return cleaned


# ---------------------------------------------------------------------------
# Public parsing API
# ---------------------------------------------------------------------------


def parse_otlp_payload(body: bytes, content_type: str) -> ParsedTraces:
    """Decode an OTLP/HTTP trace export request body.

    Args:
        body: Raw request body bytes.
        content_type: ``Content-Type`` header value.  Only the media-type
            part is consulted (``application/json`` and
            ``application/x-protobuf`` are accepted; charsets are ignored).

    Returns:
        :class:`ParsedTraces` aggregated by ``trace_id``.

    Raises:
        OtelDecodeError: if the body is empty, oversize, has an unknown
            content type, or is malformed for the declared encoding.
    """
    if not body:
        raise OtelDecodeError("empty body")
    if len(body) > _MAX_BODY_BYTES:
        raise OtelDecodeError("body too large")

    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == CONTENT_TYPE_JSON:
        return _parse_json_payload(body)
    if media_type == CONTENT_TYPE_PROTOBUF:
        return _parse_protobuf_payload(body)
    raise OtelDecodeError(f"unsupported content type: {media_type!r}")


# ---------------------------------------------------------------------------
# JSON decoding (OTLP/HTTP+JSON)
# ---------------------------------------------------------------------------


def _parse_json_payload(body: bytes) -> ParsedTraces:
    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OtelDecodeError(f"invalid JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise OtelDecodeError("OTLP/JSON root must be an object")

    parsed = ParsedTraces()
    resource_spans = document.get("resourceSpans", [])
    if not isinstance(resource_spans, list):
        raise OtelDecodeError("'resourceSpans' must be a list")

    for resource_span in resource_spans:
        if not isinstance(resource_span, dict):
            continue
        attrs = _json_resource_attributes(resource_span)
        for scope_span in resource_span.get("scopeSpans", []) or []:
            if not isinstance(scope_span, dict):
                continue
            for span_dict in scope_span.get("spans", []) or []:
                if not isinstance(span_dict, dict):
                    continue
                parsed_span = _parse_json_span(span_dict, attrs)
                if parsed_span is not None:
                    _accumulate_span(parsed, parsed_span)
    return parsed


def _json_resource_attributes(resource_span: dict[str, Any]) -> dict[str, str]:
    """Flatten resource attributes from a JSON ResourceSpans block."""
    resource = resource_span.get("resource") or {}
    attributes = resource.get("attributes") or []
    return _json_attribute_list_to_map(attributes)


def _json_attribute_list_to_map(attributes: list[Any] | Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(attributes, list):
        return out
    for kv in attributes:
        if not isinstance(kv, dict):
            continue
        key = kv.get("key")
        if not isinstance(key, str):
            continue
        value = kv.get("value")
        if not isinstance(value, dict):
            continue
        # OTLP/JSON encodes string values as {"stringValue": "..."}.  Any
        # non-string scalar is converted with ``str()`` so we never crash on
        # exotic inputs but also never silently drop a known-good key.
        if "stringValue" in value:
            out[key] = str(value["stringValue"])
        elif "intValue" in value:
            out[key] = str(value["intValue"])
        elif "boolValue" in value:
            out[key] = str(value["boolValue"]).lower()
        elif "doubleValue" in value:
            out[key] = str(value["doubleValue"])
    return out


def _parse_json_span(
    span_dict: dict[str, Any], resource_attrs: dict[str, str]
) -> ParsedSpan | None:
    trace_id_hex = _normalise_json_id(span_dict.get("traceId"), expected_bytes=16)
    span_id_hex = _normalise_json_id(span_dict.get("spanId"), expected_bytes=8)
    if not trace_id_hex or not span_id_hex:
        return None

    span_attrs = _json_attribute_list_to_map(span_dict.get("attributes"))
    return _build_parsed_span(
        trace_id_hex=trace_id_hex,
        span_id_hex=span_id_hex,
        name=str(span_dict.get("name") or ""),
        start=_to_int(span_dict.get("startTimeUnixNano")),
        end=_to_int(span_dict.get("endTimeUnixNano")),
        resource_attrs=resource_attrs,
        span_attrs=span_attrs,
    )


def _normalise_json_id(raw: Any, *, expected_bytes: int) -> str:
    """Return lowercase hex form of an OTLP id, or ``""`` when invalid.

    Per the OTLP/JSON spec the id is base64; many SDKs emit hex instead.
    Accept both encodings so any conformant client interoperates.
    """
    if not isinstance(raw, str) or not raw:
        return ""
    expected_hex_chars = expected_bytes * 2
    candidate = raw.lower()
    if len(candidate) == expected_hex_chars:
        try:
            int(candidate, 16)
        except ValueError:
            return ""
        return candidate
    # Try base64
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error):
        return ""
    if len(decoded) != expected_bytes:
        return ""
    return decoded.hex()


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Protobuf decoding (OTLP/HTTP+protobuf)
# ---------------------------------------------------------------------------


def _parse_protobuf_payload(body: bytes) -> ParsedTraces:
    # Imported lazily so the import error is reported as a decode error
    # rather than a server-startup failure if the optional dep is missing.
    try:
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    except ImportError as exc:  # pragma: no cover — exercised only on broken envs
        raise OtelDecodeError("opentelemetry-proto not installed") from exc

    request = trace_service_pb2.ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except Exception as exc:  # google.protobuf.message.DecodeError
        raise OtelDecodeError(f"invalid protobuf: {exc}") from exc

    parsed = ParsedTraces()
    for resource_span in request.resource_spans:
        attrs = _proto_attributes_to_map(resource_span.resource.attributes)
        for scope_span in resource_span.scope_spans:
            for span in scope_span.spans:
                parsed_span = _parse_proto_span(span, attrs)
                if parsed_span is not None:
                    _accumulate_span(parsed, parsed_span)
    return parsed


def _proto_attributes_to_map(attributes: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for kv in attributes:
        key = getattr(kv, "key", None)
        value = getattr(kv, "value", None)
        if not isinstance(key, str) or value is None:
            continue
        # AnyValue oneof — pick whichever scalar variant is present.
        if value.HasField("string_value"):
            out[key] = value.string_value
        elif value.HasField("int_value"):
            out[key] = str(value.int_value)
        elif value.HasField("bool_value"):
            out[key] = str(value.bool_value).lower()
        elif value.HasField("double_value"):
            out[key] = str(value.double_value)
    return out


def _parse_proto_span(span: Any, resource_attrs: dict[str, str]) -> ParsedSpan | None:
    trace_id_bytes: bytes = bytes(span.trace_id)
    span_id_bytes: bytes = bytes(span.span_id)
    if len(trace_id_bytes) != 16 or len(span_id_bytes) != 8:
        return None

    span_attrs = _proto_attributes_to_map(span.attributes)
    return _build_parsed_span(
        trace_id_hex=trace_id_bytes.hex(),
        span_id_hex=span_id_bytes.hex(),
        name=str(span.name or ""),
        start=int(span.start_time_unix_nano),
        end=int(span.end_time_unix_nano),
        resource_attrs=resource_attrs,
        span_attrs=span_attrs,
    )


# ---------------------------------------------------------------------------
# Span normalisation helpers (shared by JSON + protobuf paths)
# ---------------------------------------------------------------------------


def _build_parsed_span(
    *,
    trace_id_hex: str,
    span_id_hex: str,
    name: str,
    start: int,
    end: int,
    resource_attrs: dict[str, str],
    span_attrs: dict[str, str],
) -> ParsedSpan:
    service_name = resource_attrs.get("service.name", "") or span_attrs.get("service.name", "")
    service_namespace = resource_attrs.get("service.namespace", "") or span_attrs.get(
        "service.namespace", ""
    )
    pod_name = (
        resource_attrs.get("k8s.pod.name", "")
        or resource_attrs.get("host.name", "")
        or span_attrs.get("k8s.pod.name", "")
        or span_attrs.get("host.name", "")
    )
    return ParsedSpan(
        trace_id_hex=trace_id_hex,
        span_id_hex=span_id_hex,
        name=name,
        service_name=service_name,
        service_namespace=service_namespace,
        pod_name=pod_name,
        start_unix_nano=start,
        end_unix_nano=end,
    )


def _accumulate_span(parsed: ParsedTraces, span: ParsedSpan) -> None:
    trace = parsed.traces.get(span.trace_id_hex)
    if trace is None:
        trace = ParsedTrace(trace_id_hex=span.trace_id_hex)
        parsed.traces[span.trace_id_hex] = trace
    trace.spans.append(span)
    if span.service_name:
        trace.service_names.add(span.service_name)
    if span.pod_name:
        trace.pod_names.add(span.pod_name)


# ---------------------------------------------------------------------------
# Connector class
# ---------------------------------------------------------------------------


class OtelConnector(Connector):
    """Push-based OTLP/HTTP receiver connector.

    Discovery and pull-style fetch are not applicable: traces arrive only
    via the authenticated OTLP HTTP route.  The class exists so the OTel
    receiver appears in the connector registry like every other source
    type and so consumers can call :meth:`parse_payload` through the
    registry-resolved instance.

    ACL invariant: this class does **not** read or care about workspace
    identity.  Tenant resolution is the route's responsibility; this
    connector deals only in the workspace-agnostic intermediate form.
    """

    connector_type: ClassVar[str] = "otel"
    config_schema: ClassVar[type[BaseModel]] = OtelConfig

    async def validate(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> None:
        """Validate is a no-op — the OTel client authenticates against the
        server, not the other way around.
        """
        # Accept only the documented config shape.
        if not isinstance(config, OtelConfig):
            raise TypeError("OtelConnector.validate expects OtelConfig")

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """No-op: OTel is push-only.  Yields nothing."""
        # Empty async generator: we never yield, but the compiler needs a
        # ``yield`` statement to mark this function as one.  Returning before
        # ``yield`` is the documented Python idiom (see PEP 525 / PEP 380).
        return
        yield DocumentRef(external_id="", uri="")  # pragma: no cover

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Push-only connector — fetch never has work to do.

        Raises:
            NotImplementedError: always.  The route persists parsed traces
                directly; there is no pull-side ``fetch`` semantics.
        """
        raise NotImplementedError("OtelConnector is push-only; use parse_payload")

    def parse_payload(self, body: bytes, content_type: str) -> ParsedTraces:
        """Convenience wrapper around :func:`parse_otlp_payload`."""
        return parse_otlp_payload(body, content_type)
