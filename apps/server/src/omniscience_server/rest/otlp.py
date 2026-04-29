"""OTLP/HTTP trace receiver route (#152).

POST /api/v1/otlp/v1/traces

The OpenTelemetry client speaks OTLP/HTTP directly to this endpoint.

ACL invariant — TENANT IDENTITY IS RESOLVED FROM THE BEARER TOKEN ONLY
----------------------------------------------------------------------
The tenant under which spans are ingested is taken from
``request.state.api_token.workspace_id`` (set by the standard auth
middleware).  Span attributes are tenant-writable upstream content and
are NEVER consulted for tenant identity — a token for workspace A NEVER
lands traces in workspace B's graph, regardless of any
``resource.attributes['tenant.id']`` / ``service.namespace`` /
``deployment.environment`` value the client may send.

Auth
----
* No / invalid bearer token              → 401 ``unauthorized``
* Token without ``otel:write`` scope     → 403 ``forbidden``
* Authenticated, scoped, valid OTLP body → 200 with empty JSON object
  (per OTLP/HTTP spec — ``Content-Type: application/json``)

Encodings
---------
``application/x-protobuf`` (the OTLP standard) and ``application/json``
(dev-friendly) are both accepted.  gRPC is out of scope.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_connectors.otel import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_PROTOBUF,
    OtelDecodeError,
    parse_otlp_payload,
)
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.db.models import ApiToken
from starlette.responses import JSONResponse

from omniscience_server.rest.otlp_ingester import OtlpIngester
from omniscience_server.rest.otlp_metrics import (
    OTEL_REQUEST_DURATION_SECONDS,
    OTEL_SPANS_RECEIVED_TOTAL,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/otlp", tags=["otlp"])

# Module-level singleton — avoids ruff B008 in the route signature.
_otel_write_dep: Any = Depends(require_scope(Scope.otel_write))


@router.post(
    "/v1/traces",
    summary="OTLP/HTTP trace receiver",
    dependencies=[_otel_write_dep],
)
async def post_traces(
    request: Request,
    token: ApiToken = _otel_write_dep,
) -> JSONResponse:
    """Accept an OTLP/HTTP trace export.

    Tenant scope is the bearer token's workspace_id; span attributes are
    NEVER read for tenant identity (#152).
    """
    started = time.perf_counter()
    content_type = request.headers.get("content-type", "")
    content_type_kind = _classify_content_type(content_type)

    if content_type_kind == "other":
        _record_duration(started, content_type_kind, outcome="rejected")
        raise HTTPException(
            status_code=415,
            detail={
                "code": "unsupported_media_type",
                "message": (
                    "OTLP/HTTP trace receiver accepts application/x-protobuf "
                    "or application/json only"
                ),
            },
        )

    body = await request.body()
    try:
        parsed = parse_otlp_payload(body, content_type)
    except OtelDecodeError as exc:
        log.info("otlp_decode_failed", reason=str(exc))
        _record_duration(started, content_type_kind, outcome="rejected")
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_otlp_payload", "message": "Failed to decode OTLP body"},
        ) from None

    workspace_id = token.workspace_id
    workspace_kind = "present" if workspace_id is not None else "missing"

    ingester: OtlpIngester | None = getattr(request.app.state, "otlp_ingester", None)
    if ingester is not None:
        await ingester.ingest(workspace_id=workspace_id, parsed=parsed)
    else:
        log.warning("otlp_ingester_unwired", spans=parsed.total_spans)

    OTEL_SPANS_RECEIVED_TOTAL.labels(workspace_id_kind=workspace_kind).inc(parsed.total_spans)
    log.info(
        "otlp_traces_received",
        workspace_id=str(workspace_id) if workspace_id else None,
        token_prefix=token.token_prefix,
        traces=len(parsed.traces),
        spans=parsed.total_spans,
        content_type_kind=content_type_kind,
    )

    _record_duration(started, content_type_kind, outcome="accepted")
    # OTLP/HTTP spec: success is 2xx with an empty
    # ExportTraceServiceResponse.  Empty JSON object suffices for both
    # content types since clients only assert on the status code.
    return JSONResponse(content={}, status_code=200)


def _classify_content_type(content_type: str) -> str:
    """Map a Content-Type header value to a bounded label.

    Returns one of ``"protobuf"``, ``"json"``, ``"other"`` — the
    cardinality of the metric label is therefore bounded to three.
    """
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == CONTENT_TYPE_PROTOBUF:
        return "protobuf"
    if media_type == CONTENT_TYPE_JSON:
        return "json"
    return "other"


def _record_duration(started: float, content_type_kind: str, *, outcome: str) -> None:
    OTEL_REQUEST_DURATION_SECONDS.labels(
        content_type_kind=content_type_kind,
        outcome=outcome,
    ).observe(time.perf_counter() - started)


__all__ = ["router"]
