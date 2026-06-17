"""Document retrieval endpoint.

GET /api/v1/documents/{id} — retrieve document with all chunks.

Requires ``search`` scope.

ACL invariant (cross-tenant isolation)
---------------------------------------
The handler extracts workspace_id from the bearer token and verifies that
the document's parent Source belongs to the same workspace.  A mismatch
returns 404 — not 403 — to avoid leaking document/source existence to
other tenants.  Legacy tokens without workspace_id are rejected fail-closed
with 403: ``get_workspace_id`` raises ``PermissionError`` which the global
handler in ``rest/errors.py`` converts to HTTP 403.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.db.models import ApiToken, Chunk, Document, Source
from omniscience_core.db.schemas import ChunkRead, DocumentRead
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

router = APIRouter(tags=["documents"])

# Module-level Depends singleton — avoids ruff B008
_search_scope_dep: Any = Depends(require_scope(Scope.search))


class DocumentWithChunks(BaseModel):
    """Document representation including all associated chunks."""

    document: DocumentRead
    chunks: list[ChunkRead]


@router.get(
    "/documents/{document_id}",
    response_model=DocumentWithChunks,
    summary="Get document with chunks",
    dependencies=[_search_scope_dep],
)
async def get_document(
    document_id: uuid.UUID,
    request: Request,
    token: ApiToken = _search_scope_dep,
) -> DocumentWithChunks:
    """Retrieve a document and all its associated chunks.

    Returns 404 when the document does not exist OR when its parent source
    belongs to a different workspace than the caller's token.
    Legacy tokens (no workspace_id) are rejected fail-closed with 403:
    get_workspace_id raises PermissionError, caught by the global handler.

    Requires scope: ``search``
    """
    # PermissionError from get_workspace_id (legacy token) is caught by the
    # global handler in rest/errors.py and rendered as HTTP 403.
    workspace_id = get_workspace_id(token)

    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Database not available"},
        )

    db: AsyncSession
    async with factory() as db:
        doc = await db.get(Document, document_id)
        if doc is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "document_not_found",
                    "message": f"Document {document_id} not found",
                },
            )

        # Tenant isolation: verify the parent source belongs to our workspace.
        source = await db.get(Source, doc.source_id)
        if source is None or source.tenant_id != workspace_id:
            # 404 — do not reveal that the document exists in another tenant.
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "document_not_found",
                    "message": f"Document {document_id} not found",
                },
            )

        chunks_result = await db.execute(
            select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.ord)
        )
        chunks = chunks_result.scalars().all()

        log.info("document_fetched", document_id=str(document_id), chunk_count=len(chunks))

        return DocumentWithChunks(
            document=DocumentRead.model_validate(doc),
            chunks=[ChunkRead.model_validate(c) for c in chunks],
        )


__all__ = ["router"]
