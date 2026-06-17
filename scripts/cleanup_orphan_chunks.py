"""One-time cleanup of orphaned Qdrant points left by per-run source churn.

Earlier versions of seed_from_docs.py deleted and recreated the Source row
(with a fresh uuid4) on every run.  Postgres documents were cascade-deleted,
but the Qdrant points kept the old source_id in their payload and were never
removed — so search returned N copies of every k8s object, one per run.

This script deletes every point whose payload ``source_id`` is no longer
present in the Postgres ``sources`` table, across all omniscience-managed
collections.  Pass --dry-run to only report what would be deleted.

Run inside the app container:
    /app/.venv/bin/python /tmp/cleanup_orphan_chunks.py [--dry-run]
"""

import asyncio
import sys

from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_core.db.models import Source
from omniscience_index.stores.qdrant_constants import (
    COLLECTION_NAME_PREFIX,
    PAYLOAD_SOURCE_ID,
)
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm
from sqlalchemy import select

DRY_RUN = "--dry-run" in sys.argv
SCROLL_BATCH = 256


async def main() -> None:
    settings = Settings()
    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        rows = (await session.execute(select(Source.id))).scalars().all()
    valid_ids = {str(sid) for sid in rows}
    print(f"Valid sources in Postgres: {len(valid_ids)}")

    client = AsyncQdrantClient(
        host=settings.qdrant_host,
        grpc_port=settings.qdrant_grpc_port,
        port=settings.qdrant_http_port,
        api_key=settings.qdrant_api_key,
        https=False,
        prefer_grpc=True,
    )

    collections = [
        c.name
        for c in (await client.get_collections()).collections
        if c.name.startswith(COLLECTION_NAME_PREFIX)
    ]
    print(f"Collections to scan: {collections}")

    total_deleted = 0
    for collection in collections:
        orphan_points: list[str] = []
        orphan_sources: set[str] = set()
        offset = None
        while True:
            points, offset = await client.scroll(
                collection_name=collection,
                limit=SCROLL_BATCH,
                offset=offset,
                with_payload=[PAYLOAD_SOURCE_ID],
                with_vectors=False,
            )
            for p in points:
                sid = (p.payload or {}).get(PAYLOAD_SOURCE_ID)
                if sid is not None and str(sid) not in valid_ids:
                    orphan_points.append(str(p.id))
                    orphan_sources.add(str(sid))
            if offset is None:
                break

        print(
            f"[{collection}] orphan points: {len(orphan_points)} "
            f"from {len(orphan_sources)} dead sources"
        )
        if orphan_points and not DRY_RUN:
            for i in range(0, len(orphan_points), SCROLL_BATCH):
                batch = orphan_points[i : i + SCROLL_BATCH]
                await client.delete(
                    collection_name=collection,
                    points_selector=qm.PointIdsList(points=list(batch)),
                    wait=True,
                )
            total_deleted += len(orphan_points)

    verb = "Would delete" if DRY_RUN else "Deleted"
    print(f"{verb} {total_deleted if not DRY_RUN else 'see per-collection counts'} orphan points")
    await client.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
