"""Workspace management endpoints."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from omniscience_core.auth.middleware import get_current_token
from omniscience_core.db.models import ApiToken, Workspace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/workspace", tags=["workspace"])

class WorkspaceSchema(BaseModel):
    id: Any
    name: str
    metadata: dict[str, Any]

class WorkspaceUpdate(BaseModel):
    metadata: dict[str, Any]

async def _get_db(request: Request) -> AsyncSession:
    """Helper to get DB session from app state."""
    factory = request.app.state.db_session_factory
    async with factory() as session:
        yield session

@router.get("", response_model=WorkspaceSchema)
async def get_current_workspace(
    token: ApiToken = Depends(get_current_token),
    db: AsyncSession = Depends(_get_db),
) -> Any:
    """Return the current workspace details."""
    workspace_id = token.workspace_id
    if not workspace_id:
        raise HTTPException(status_code=403, detail="Token not bound to a workspace")
    
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()
    
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found in database")
        
    return {
        "id": str(workspace.id),
        "name": workspace.name,
        "metadata": workspace.settings or {}
    }

@router.patch("", response_model=WorkspaceSchema)
async def update_workspace(
    update: WorkspaceUpdate,
    token: ApiToken = Depends(get_current_token),
    db: AsyncSession = Depends(_get_db),
) -> Any:
    """Update workspace metadata (Discovery settings)."""
    workspace_id = token.workspace_id
    if not workspace_id:
        raise HTTPException(status_code=403, detail="Token not bound to a workspace")

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()
    
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found in database")

    workspace.settings = update.metadata
    await db.commit()
    
    return {
        "id": str(workspace.id),
        "name": workspace.name,
        "metadata": workspace.settings
    }
