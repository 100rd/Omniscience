"""Build a Neo4j graph from the fetched K8s/ArgoCD state (/tmp/k8s_docs.json):
entities (Namespace, Application, Deployment, Service, GitRepository) and edges
(in_namespace, deploys_to, sources_from), enabling get_related_entities /
blast_radius / get_entity over real cluster state.

Run inside the app container:  /app/.venv/bin/python /tmp/seed_k8s_graph.py
"""

import asyncio
import json
import uuid

from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_core.db.models import Source
from omniscience_core.storage.graph import EdgeUpsert, EntityUpsert
from omniscience_index.stores.neo4j_store import Neo4jGraphStore, Neo4jStoreConfig
from sqlalchemy import select

WS = uuid.UUID("00000000-0000-0000-0000-000000000001")
CLUSTER = "qbiq-shared"
_NS = uuid.NAMESPACE_URL


def eid(key: str) -> uuid.UUID:
    return uuid.uuid5(_NS, f"k8s/{CLUSTER}/{key}")


async def main() -> None:
    docs = json.load(open("/tmp/k8s_docs.json"))
    settings = Settings()
    engine = create_async_engine(settings)
    sf = create_session_factory(engine)
    gs = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    await gs.connect()

    async with sf() as s:
        src = (await s.execute(select(Source).where(Source.name == "k8s-qbiq-shared"))).scalar_one()
    src_id = src.id

    ents: dict[uuid.UUID, EntityUpsert] = {}
    edges: list[EdgeUpsert] = []

    def ent(key: str, etype: str, name: str, meta: dict) -> uuid.UUID:
        i = eid(key)
        ents[i] = EntityUpsert(id=i, source_id=src_id, entity_type=etype, name=name,
                               display_name=name.split("/")[-1], chunk_id=None,
                               metadata={**meta, "workspace_id": str(WS), "cluster": CLUSTER})
        return i

    for d in docs:
        m = d["metadata"]
        kind, ns, name = m["kind"], m.get("namespace", "default"), m["name"]
        ns_id = ent(f"ns/{ns}", "Namespace", ns, {"kind": "Namespace"})
        res_id = ent(f"{kind}/{ns}/{name}", kind, f"{ns}/{name}", m)
        edges.append(EdgeUpsert(source_entity_id=res_id, target_entity_id=ns_id,
                                edge_type="in_namespace", metadata={"workspace_id": str(WS)}))
        if kind == "Application":
            repo = m.get("repo")
            if repo and repo != "?":
                repo_id = ent(f"repo/{repo}", "GitRepository", repo, {"kind": "GitRepository"})
                edges.append(EdgeUpsert(source_entity_id=res_id, target_entity_id=repo_id,
                                        edge_type="sources_from", metadata={"workspace_id": str(WS)}))
            dns = m.get("dest_namespace")
            if dns and dns != "?":
                dns_id = ent(f"ns/{dns}", "Namespace", dns, {"kind": "Namespace"})
                edges.append(EdgeUpsert(source_entity_id=res_id, target_entity_id=dns_id,
                                        edge_type="deploys_to", metadata={"workspace_id": str(WS)}))

    for e in ents.values():
        await gs.upsert_entity(entity=e, workspace_id=WS)
    for ed in edges:
        await gs.upsert_edge(edge=ed, workspace_id=WS)

    kinds: dict[str, int] = {}
    for e in ents.values():
        kinds[e.entity_type] = kinds.get(e.entity_type, 0) + 1
    print(f"Graph: {len(ents)} entities {kinds}, {len(edges)} edges")
    await gs.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
