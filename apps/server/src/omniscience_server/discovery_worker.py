"""Discovery worker — automatic source provisioning (v0.4 Preview).

Periodically runs discovery connectors (GitHub, GitLab) to find new 
repositories and register them as Sources in the database.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from omniscience_core.config import Settings
from omniscience_core.db.models import Source, Workspace
from omniscience_connectors.github_discovery import GitHubDiscoveryConnector
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

class DiscoveryWorker:
    """Background task that discovers and provisions new sources."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._interval_seconds = getattr(settings, "discovery_interval_seconds", 3600)
        self._running = False
        self._github_connector = GitHubDiscoveryConnector()

    async def start(self) -> None:
        """Run the worker loop."""
        self._running = True
        log.info("discovery_worker_started", interval=self._interval_seconds)
        
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                log.error("discovery_worker_error", error=str(exc))
            
            await asyncio.sleep(self._interval_seconds)

    def stop(self) -> None:
        self._running = False

    async def run_once(self) -> None:
        """Perform one discovery cycle across all enabled platforms."""
        # For v0.4 Preview, we assume discovery is mapped to workspaces via settings
        # or we just discover into a default workspace.
        
        async with self._session_factory() as session:
            # 1. Load active workspaces
            result = await session.execute(select(Workspace))
            workspaces = result.scalars().all()
            
            for ws in workspaces:
                await self._discover_for_workspace(session, ws)
                await session.commit()

    async def _discover_for_workspace(self, session: AsyncSession, workspace: Workspace) -> None:
        """Run discovery for a specific workspace based on its settings."""
        ws_metadata = workspace.settings or {}
        discovery_config = ws_metadata.get("discovery", {})
        
        if not discovery_config:
            return

        # GitHub Discovery
        github_cfg = discovery_config.get("github")
        if github_cfg:
            discovered = await self._github_connector.discover(
                config=github_cfg,
                secrets={"github_token": github_cfg.get("token")} # In production, pull from secrets manager
            )
            
            for ds in discovered:
                await self._ensure_source(session, workspace.id, ds)

    async def _ensure_source(self, session: AsyncSession, workspace_id: uuid.UUID, ds: Any) -> None:
        """Create source if it doesn't exist (keyed by URI)."""
        stmt = select(Source).where(Source.uri == ds.uri, Source.tenant_id == workspace_id)
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
        
        if existing:
            return

        log.info("discovery_provisioning_new_source", name=ds.name, uri=ds.uri, workspace_id=str(workspace_id))
        
        new_source = Source(
            id=uuid.uuid4(),
            name=ds.name,
            type=ds.type,
            uri=ds.uri,
            tenant_id=workspace_id,
            status="active",
            freshness_sla_seconds=86400, # Default 1 day
            last_sync_at=None,
            source_metadata={
                **ds.metadata,
                "provisioned_by": "discovery_worker",
                "discovered_at": datetime.now(UTC).isoformat()
            }
        )
        session.add(new_source)
