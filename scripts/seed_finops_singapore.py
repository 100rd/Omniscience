"""Seed mock FinOps documents so an Omniscience-connected agent can answer:
'how much will it cost to migrate from gp2 to gp3 for servers in Singapore'.

Real embeddings are computed with the configured provider (Ollama / nomic-embed-text)
so semantic search actually matches the question. Run inside the app container:
    /app/.venv/bin/python /tmp/seed_finops_singapore.py
"""

import asyncio
import hashlib
import uuid

from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_core.db.models import Source, SourceStatus, SourceType
from omniscience_embeddings.factory import create_embedding_provider
from omniscience_index.stores.neo4j_store import Neo4jGraphStore, Neo4jStoreConfig
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from omniscience_index.writer import ChunkData, IndexWriter
from sqlalchemy import delete

WS_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# ---------------------------------------------------------------------------
# Mock corpus — the gp2→gp3 Singapore cost answer plus corroborating docs.
# Each doc is split into a few chunks so retrieval is granular.
# ---------------------------------------------------------------------------
DOCS: list[dict] = [
    {
        "external_id": "finops/ebs-gp2-gp3-singapore-cost-analysis.md",
        "uri": "confluence://finops/ebs-gp2-gp3-singapore",
        "title": "EBS gp2 → gp3 Migration Cost Analysis — Singapore (ap-southeast-1)",
        "metadata": {
            "region": "ap-southeast-1",
            "region_name": "Singapore",
            "service": "EBS",
            "doc_type": "cost_analysis",
            "team": "FinOps",
        },
        "chunks": [
            # Headline answer
            "EBS gp2 to gp3 migration cost analysis for the Singapore (ap-southeast-1) "
            "region. Scope: the Singapore server fleet of approximately 200 EC2 instances "
            "with about 100,000 GB (100 TB) of provisioned gp2 EBS volumes. Question: how "
            "much will it cost to migrate these servers from gp2 to gp3? "
            "Answer: the migration itself costs $0 in AWS charges and SAVES about "
            "$2,400 per month (around $28,800 per year), a ~20% reduction in EBS storage cost.",
            # Migration cost = $0
            "Migration cost: $0. Switching an EBS volume from gp2 to gp3 is an online "
            "volume-type modification (the AWS ModifyVolume API / console 'Modify volume'). "
            "It happens in place with no downtime, no detach/reattach, no snapshot, and no "
            "data-transfer or migration fee. The only real cost is a one-time engineering "
            "effort of roughly one engineer-day to script and roll out ModifyVolume across "
            "the ~200 Singapore instances during the optimizing state.",
            # Pricing + math (ap-southeast-1)
            "Pricing in ap-southeast-1 (Singapore): gp2 is $0.12 per GB-month; gp3 is "
            "$0.096 per GB-month for storage and includes a free baseline of 3,000 IOPS and "
            "125 MB/s throughput per volume. Math for the 100,000 GB Singapore fleet: "
            "gp2 = 100,000 GB x $0.12 = $12,000 per month. "
            "gp3 = 100,000 GB x $0.096 = $9,600 per month. "
            "Net monthly saving = $12,000 - $9,600 = $2,400 per month; annual saving = $28,800.",
            # Caveats
            "Caveats for the gp2 to gp3 Singapore migration: gp3 only adds cost when a "
            "volume needs more than the free baseline — provisioned IOPS above 3,000 cost "
            "$0.006 per IOPS-month and throughput above 125 MB/s costs $0.048 per MB/s-month "
            "in ap-southeast-1. In this fleet about 15 high-throughput volumes exceed the "
            "baseline, trimming the net saving from $2,400 to roughly $2,150 per month. "
            "Most gp2 volumes under 1 TB already had <= 3,000 baseline IOPS, so gp3 matches "
            "or beats them at lower cost. Recommendation: migrate the whole Singapore fleet "
            "gp2 -> gp3.",
        ],
    },
    {
        "external_id": "finops/aws-ebs-pricing-ap-southeast-1.md",
        "uri": "confluence://finops/ebs-pricing-singapore",
        "title": "AWS EBS Pricing Reference — ap-southeast-1 (Singapore)",
        "metadata": {
            "region": "ap-southeast-1",
            "region_name": "Singapore",
            "service": "EBS",
            "doc_type": "pricing_reference",
        },
        "chunks": [
            "AWS EBS volume pricing in the Singapore region (ap-southeast-1), monthly: "
            "gp2 (General Purpose SSD) = $0.12 per GB-month. "
            "gp3 (General Purpose SSD) = $0.096 per GB-month storage, plus 3,000 IOPS and "
            "125 MB/s free; extra IOPS $0.006 each, extra throughput $0.048 per MB/s. "
            "io2 = $0.138 per GB-month plus provisioned IOPS. "
            "st1 (Throughput HDD) = $0.054 per GB-month. gp3 storage is 20% cheaper than gp2.",
        ],
    },
    {
        "external_id": "infra/singapore-ebs-inventory.md",
        "uri": "git://infra/inventory/singapore-ebs.md",
        "title": "Singapore Region Infrastructure Inventory — EBS Volumes",
        "metadata": {
            "region": "ap-southeast-1",
            "region_name": "Singapore",
            "service": "EC2",
            "doc_type": "inventory",
        },
        "chunks": [
            "Singapore (ap-southeast-1) infrastructure inventory: approximately 200 EC2 "
            "instances across the prod, staging, and data tiers. Attached EBS storage totals "
            "about 100,000 GB (100 TB), currently almost entirely gp2 volumes. Average volume "
            "size ~500 GB. This is the fleet targeted for the gp2 to gp3 migration. No other "
            "AWS region in this account runs gp2 at this scale.",
        ],
    },
    {
        "external_id": "finops/runbook-ebs-gp2-to-gp3.md",
        "uri": "confluence://finops/runbook-gp2-gp3",
        "title": "Runbook: Migrating EBS Volumes from gp2 to gp3",
        "metadata": {"service": "EBS", "doc_type": "runbook"},
        "chunks": [
            "Runbook: migrate an EBS volume from gp2 to gp3. Use aws ec2 modify-volume "
            "--volume-id vol-xxxx --volume-type gp3. The change is online: the volume enters "
            "the 'optimizing' state and stays fully available with no downtime and no data "
            "loss. gp3 keeps the gp2 size; you may also set --iops and --throughput. No "
            "snapshot or data copy is required, so there is no migration charge. Roll it out "
            "fleet-wide with a loop over describe-volumes filtered to volume-type=gp2.",
        ],
    },
]


async def seed() -> None:
    settings = Settings()
    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)

    provider = create_embedding_provider(settings)
    graph_store = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    vector_store = QdrantVectorStore(
        config=QdrantConfig(
            host=settings.qdrant_host,
            grpc_port=settings.qdrant_grpc_port,
            http_port=settings.qdrant_http_port,
            api_key=settings.qdrant_api_key,
        ),
        embedding_provider=provider,
    )
    await graph_store.connect()
    await vector_store.connect()

    src_id = uuid.uuid4()
    async with session_factory() as session:
        await session.execute(delete(Source).where(Source.name == "finops-singapore"))
        session.add(
            Source(
                id=src_id,
                name="finops-singapore",
                type=SourceType.git,
                config={"repo_url": "confluence://finops"},
                tenant_id=WS_ID,
                status=SourceStatus.active,
            )
        )
        await session.commit()

    writer = IndexWriter(session_factory, graph_store, vector_store)
    model_name = getattr(settings, "ollama_embedding_model", None) or "nomic-embed-text"

    total_chunks = 0
    for doc in DOCS:
        texts: list[str] = doc["chunks"]
        vectors = await provider.embed(texts)  # real embeddings
        chunks = [
            ChunkData(
                ord=i,
                text=t,
                embedding=vectors[i],
                embedding_model=model_name,
                embedding_provider="ollama",
                metadata=doc["metadata"],
            )
            for i, t in enumerate(texts)
        ]
        content_hash = hashlib.sha256("\n".join(texts).encode()).hexdigest()
        res = await writer.upsert_document(
            source_id=src_id,
            external_id=doc["external_id"],
            uri=doc["uri"],
            title=doc["title"],
            content_hash=content_hash,
            metadata=doc["metadata"],
            chunks=chunks,
            workspace_id=WS_ID,
        )
        total_chunks += len(chunks)
        print(f"  {res.action:8} {doc['title'][:55]:55} ({len(chunks)} chunks)")

    print(f"Seeded {len(DOCS)} documents / {total_chunks} chunks into workspace {WS_ID}")
    await vector_store.close()
    await graph_store.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
