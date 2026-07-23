"""SP-81 management-context server boundary (SPEC-MCTX, ADR-0021).

Wraps ``omniscience_core.management.ManagementContextProducer`` as the server-side
entrypoint. No MCP/REST call site is wired to it in this task -- ADR-0021's
development authority is schema/fixtures/read-only producer code only, no live
consumer pin.
"""

from __future__ import annotations

from omniscience_server.management.boundary import ManagementContextBoundary, request_from_payload

__all__ = ["ManagementContextBoundary", "request_from_payload"]
