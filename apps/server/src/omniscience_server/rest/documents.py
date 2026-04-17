"""Document retrieval endpoint.

GET /api/v1/documents/{id} — retrieve document with all chunks.

Requires ``search`` scope.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.middleware import require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.db.models import Chunk, Document
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
) -> DocumentWithChunks:
    """Retrieve a document and all its associated chunks.

    Requires scope: ``search``
    """
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
