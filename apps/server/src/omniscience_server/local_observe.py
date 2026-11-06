"""Seed the isolated management-local-observe-v1 stores with synthetic data."""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from urllib.parse import urlsplit

from omniscience_core.auth.tokens import hash_token, verify_token
from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_core.db.models import (
    ApiToken,
    Document,
    Source,
    SourceStatus,
    SourceType,
    SyncMode,
    Workspace,
)
from omniscience_core.storage.graph import EdgeUpsert, EntityUpsert, GraphStore
from omniscience_embeddings import create_embedding_provider
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_index import IndexWriter
from omniscience_index.hashing import compute_content_hash
from omniscience_index.stores import Neo4jGraphStore, Neo4jStoreConfig
from omniscience_index.writer import ChunkData
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_server.app import _build_qdrant_store
from omniscience_server.local_observe_policy import (
    BROKER_ID,
    BROKER_SCOPE,
    PROFILE_ID,
    WORKSPACES,
    fixture_id,
    workspace_name,
)

BROKER_ENV = "OMNISCIENCE_LOCAL_OBSERVE_BROKER_TOKEN"
_BROKER_NAME = "management-local-observe-v1-portal-broker"
_LOCK_KEY = 2_026_110_603


def _require_local_target(settings: Settings) -> None:
    database = make_url(settings.database_url)
    if (
        not settings.local_observe_enabled
        or settings.environment != "development"
        or database.host != "omniscience-postgres"
        or database.database != "omniscience"
        or urlsplit(settings.neo4j_uri).hostname != "omniscience-neo4j"
        or settings.qdrant_host != "omniscience-qdrant"
        or settings.storage_graph_backend != "neo4j"
        or settings.graph_bitemporal != "enabled"
        or settings.storage_vector_backend != "qdrant"
        or settings.embedding_provider != "local"
        or os.environ.get("HF_HUB_OFFLINE") != "1"
    ):
        raise ValueError(f"Bootstrap requires the isolated {PROFILE_ID} source profile")


def _broker_matches(token: ApiToken, secret: str) -> bool:
    return (
        token.name == _BROKER_NAME
        and token.workspace_id is None
        and token.scopes == [BROKER_SCOPE]
        and token.is_active
        and token.expires_at is None
        and verify_token(secret, token.hashed_token)
    )


async def _ensure_workspaces(db: AsyncSession) -> None:
    for slug, workspace_id in WORKSPACES.items():
        name = workspace_name(slug)
        marker = {"synthetic_profile": PROFILE_ID}
        await db.execute(
            insert(Workspace)
            .values(
                id=workspace_id, name=name, display_name=f"Local Observe {slug}", settings=marker
            )
            .on_conflict_do_nothing()
        )
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None or workspace.name != name or workspace.settings != marker:
            raise ValueError(f"Refusing to reuse conflicting workspace {name}")
        source_id = fixture_id(f"source/{slug}")
        await db.execute(
            insert(Source)
            .values(
                id=source_id,
                tenant_id=workspace_id,
                type=SourceType.fs,
                name=name,
                config=marker,
                status=SourceStatus.active,
                sync_mode=SyncMode.push,
                last_sync_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing()
        )
        source = await db.get(Source, source_id)
        if (
            source is None
            or source.tenant_id != workspace_id
            or source.name != name
            or source.type != SourceType.fs
            or source.sync_mode != SyncMode.push
            or source.config != marker
        ):
            raise ValueError(f"Refusing to reuse conflicting source {name}")


def _documents(slug: str) -> tuple[tuple[str, str, str], ...]:
    label = f"Observe {slug.title()}"
    documents = (
        (
            "topology.md",
            f"{label} topology evidence",
            f"{label} synthetic topology. The observe-{slug}-api sends work to the "
            f"observe-{slug}-worker. The worker reads the observe-{slug}-database. "
            "The local health probe checks queue depth and the database connection.",
        ),
        (
            "runbook.md",
            f"{label} database runbook",
            f"{label} synthetic database runbook. Check database readiness and compare "
            f"the observe-{slug}-worker queue depth with the observe-{slug}-api error count. "
            "Record the observed status and timestamps for the local exercise.",
        ),
    )
    if slug == "alpha":
        documents += (
            (
                "platform.md",
                "Local platform observation runbook",
                "Local platform observation runbook. This prepared, synthetic topology describes "
                "the management-local-observe-v1 deployment on one host. Portal reads knowledge "
                "from Omniscience and observations from Barbarossa. Barbarossa's observer probes "
                "Omniscience readiness, its own API readiness and Portal backend readiness every "
                "30 seconds. Two failures open a case; two successes resolve it. Missing evidence "
                "over 90 seconds is unknown or stale. One host shares a failure domain: these "
                "probes cannot establish external availability or HA. Check dependency readiness "
                "and recorded timestamps before proposing a cause. Codex assessments are "
                "non-authoritative text and must never execute remediation.",
            ),
        )
    return documents


async def _seed_corpus(
    factory: async_sessionmaker[AsyncSession],
    writer: IndexWriter,
    graph: GraphStore,
    embedding: EmbeddingProvider,
) -> None:
    for slug, workspace_id in WORKSPACES.items():
        source_id = fixture_id(f"source/{slug}")
        for external_id, title, content in _documents(slug):
            content_hash = compute_content_hash(content)
            chunk_id = fixture_id(f"chunk/{slug}/{external_id}")
            async with factory() as db:
                existing = await db.scalar(
                    select(Document).where(
                        Document.source_id == source_id, Document.external_id == external_id
                    )
                )
                if existing and (
                    existing.content_hash != content_hash
                    or existing.tombstoned_at is not None
                    or existing.doc_metadata.get("synthetic_profile") != PROFILE_ID
                ):
                    raise ValueError(f"Refusing to overwrite {slug}/{external_id}")
            (vector,) = await embedding.embed([content])
            result = await writer.upsert_document(
                source_id=source_id,
                external_id=external_id,
                uri=f"fixture://{PROFILE_ID}/{slug}/{external_id}",
                title=title,
                content_hash=content_hash,
                metadata={"synthetic_profile": PROFILE_ID},
                chunks=[
                    ChunkData(
                        ord=0,
                        text=content,
                        id=chunk_id,
                        embedding=vector,
                        embedding_model=embedding.model_name,
                        embedding_provider=embedding.provider_name,
                        parser_version=PROFILE_ID,
                        chunker_strategy="whole-document",
                    )
                ],
                workspace_id=workspace_id,
                # Each immutable fixture starts at version 1. Let Qdrant compare
                # its document hash even when another fixture set the source checkpoint.
                forced_replay=True,
            )
            if external_id == "runbook.md":
                continue
            entities = []
            platform = external_id == "platform.md"
            components = (
                ("portal", "barbarossa", "omniscience")
                if platform
                else ("api", "worker", "database")
            )
            for component in components:
                name = (
                    f"service://local-platform-{component}"
                    if platform
                    else f"service://observe-{slug}-{component}"
                )
                entity_id = fixture_id(
                    f"entity/platform/{component}" if platform else f"entity/{slug}/{component}"
                )
                node = await graph.get_entity(entity_name=name, workspace_id=workspace_id)
                if node is not None and (
                    node.id != entity_id or node.metadata.get("synthetic_profile") != PROFILE_ID
                ):
                    raise ValueError(f"Refusing to overwrite existing entity {name}")
                entities.append(
                    EntityUpsert(
                        id=entity_id,
                        source_id=source_id,
                        entity_type="service",
                        name=name,
                        display_name=f"Local platform {component}"
                        if platform
                        else f"Observe {slug} {component}",
                        chunk_id=chunk_id,
                        metadata={"synthetic_profile": PROFILE_ID},
                    )
                )
            write = await writer.upsert_graph(
                source_id=source_id,
                document_id=result.document_id,
                entities=entities,
                edges=[
                    EdgeUpsert(entities[0].id, entities[1].id, "DEPENDS_ON"),
                    EdgeUpsert(entities[1].id, entities[2].id, "DEPENDS_ON"),
                ]
                + ([EdgeUpsert(entities[0].id, entities[2].id, "DEPENDS_ON")] if platform else []),
                workspace_id=workspace_id,
                version=result.doc_version,
            )
            if not write.applied and write.skip_reason != "stale_document_version":
                raise RuntimeError(
                    f"Could not persist local topology for {slug}: {write.skip_reason}"
                )


async def bootstrap(settings: Settings, broker_secret: str) -> None:
    """Insert reserved fixtures and broker identity. Reruns use store checkpoints."""
    _require_local_target(settings)
    if re.fullmatch(r"[A-Za-z0-9_\-]{32,256}", broker_secret) is None:
        raise ValueError(f"Set {BROKER_ENV} to a random URL-safe value of at least 32 characters")
    async with AsyncExitStack() as stack:
        engine = create_async_engine(settings)
        engine.echo = False
        stack.push_async_callback(engine.dispose)
        factory = create_session_factory(engine)
        # Keep the advisory lock in a separate transaction so all store writes
        # can commit and resume independently after an interruption.
        lock = await stack.enter_async_context(engine.begin())
        await lock.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
        async with factory.begin() as db:
            broker = await db.get(ApiToken, BROKER_ID)
            if broker is not None and not _broker_matches(broker, broker_secret):
                raise ValueError("Existing local broker differs from supplied credential")
            await _ensure_workspaces(db)

        embedding = create_embedding_provider(settings)
        stack.push_async_callback(embedding.close)
        graph = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
        stack.push_async_callback(graph.close)
        await graph.connect()
        vector = await _build_qdrant_store(settings, embedding)
        stack.push_async_callback(vector.close)
        writer = IndexWriter(session_factory=factory, graph_store=graph, vector_store=vector)
        await _seed_corpus(factory, writer, graph, embedding)

        if broker is None:
            async with factory.begin() as db:
                db.add(
                    ApiToken(
                        id=BROKER_ID,
                        name=_BROKER_NAME,
                        hashed_token=hash_token(broker_secret),
                        token_prefix=broker_secret[:8],
                        scopes=[BROKER_SCOPE],
                    )
                )


def main() -> None:
    try:
        asyncio.run(bootstrap(Settings(), os.environ.get(BROKER_ENV, "")))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(  # noqa: T201 - local CLI output excludes all credentials
        json.dumps(
            {"profile": PROFILE_ID, "workspaces": {k: str(v) for k, v in WORKSPACES.items()}}
        )
    )


if __name__ == "__main__":
    main()
