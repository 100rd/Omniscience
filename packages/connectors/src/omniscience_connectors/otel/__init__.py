"""OpenTelemetry receiver connector.

Push-based connector that accepts OTLP/HTTP trace exports.  Tenant identity
is resolved exclusively from the bearer token presented by the OTel client;
span attributes are tenant-writable upstream content and are NEVER used to
infer or override workspace identity.

Public surface
--------------
::

    from omniscience_connectors.otel import (
        OtelConfig,
        OtelConnector,
        OtelDecodeError,
        ParsedSpan,
        ParsedTrace,
        ParsedTraces,
        canonical_pod_name,
        canonical_service_name,
        canonical_trace_name,
        parse_otlp_payload,
    )
"""

from omniscience_connectors.otel.connector import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_PROTOBUF,
    OtelConfig,
    OtelConnector,
    OtelDecodeError,
    ParsedSpan,
    ParsedTrace,
    ParsedTraces,
    canonical_pod_name,
    canonical_service_name,
    canonical_trace_name,
    parse_otlp_payload,
)

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
