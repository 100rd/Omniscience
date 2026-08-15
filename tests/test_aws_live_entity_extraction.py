"""Live-AWS entity extraction, graph routing, and cross-source linking.

Context: 946 AWS documents were ingested and ``entities`` / ``edges`` were
both empty.  Two independent causes:

1. ``IngestionWorker`` built the pipeline without an ``entity_linker``, so
   ``IngestionPipeline._stage_link`` returned immediately.
2. The only graph extractor wired in was ``extract_symbol_graph``, which
   returns ``([], [])`` for anything that is not Python.

These tests pin the fix from both ends: the extractor produces entities
carrying exactly the fields the linker's matchers read, and those entities —
after the same serialise/deserialise round-trip the Neo4j store performs —
actually make ``EntityLinker`` emit a ``cross_ref`` edge.

Fixtures mirror the shape of real documents observed in the running stack
(``documents.metadata`` jsonb plus the fetched describe body), with every
account id, ARN, resource id and org id replaced by synthetic values.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_connectors.base import DocumentRef, FetchedDocument
from omniscience_core.storage.graph import EntityNodeView, GraphWriteResult
from omniscience_index.linker import CROSS_REF_EDGE_TYPE, EntityLinker
from omniscience_index.stores.neo4j.mappers import (
    _entity_record_to_view,
    _entity_to_params,
    _serialise_metadata_param,
)
from omniscience_parsers.code.graph import ExtractedEdge, ExtractedEntity
from omniscience_parsers.graph_dispatch import GraphRoute, route_document
from omniscience_parsers.infra.aws_live import (
    AWS_LIVE_KIND,
    DEPENDS_ON_EDGE,
    InfraDocument,
    entity_id_for,
    extract_aws_live_graph,
)
from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.pipeline import IngestionPipeline
from omniscience_server.ingestion.worker import IngestionWorker

# ---------------------------------------------------------------------------
# Synthetic fixtures — shapes verified against real ingested documents
# ---------------------------------------------------------------------------

ACCOUNT_A = "000000000000"
ACCOUNT_B = "111111111111"
MGMT_ACCOUNT = "222222222222"
ORG_ID = "o-exampleorg1"
OU_ID = "ou-exam-p1exampl"
REGION = "eu-central-1"
VPC_ID = "vpc-00000000000000001"
INSTANCE_ID = "i-00000000000000001"
SG_ID = "sg-00000000000000001"

S3_BUCKET_META: dict[str, Any] = {
    "arn": "arn:aws:s3:::example-bucket",
    "kind": "aws_live",
    "name": "example-bucket",
    "tags": {"Terraform": "true", "Environment": "dev"},
    "region": REGION,
    "service": "s3",
    "account_id": ACCOUNT_A,
    "versioning": "",
    "resource_type": "aws_s3_bucket",
}
S3_BUCKET_BODY: dict[str, Any] = {
    "name": "example-bucket",
    "location": REGION,
    "versioning_status": "",
    "versioning_mfa_delete": "",
    "tags": {"Terraform": "true", "Environment": "dev"},
    "policy": {"Version": "2012-10-17", "Statement": []},
}

IAM_ROLE_META: dict[str, Any] = {
    "arn": f"arn:aws:iam::{ACCOUNT_A}:role/service-role/ExampleServiceRole",
    "kind": "aws_live",
    "name": "ExampleServiceRole",
    "path": "/service-role/",
    "region": "global",
    "role_id": "AROAEXAMPLEEXAMPLE01",
    "service": "iam",
    "account_id": ACCOUNT_A,
    "resource_type": "aws_iam_role",
}
IAM_ROLE_BODY: dict[str, Any] = {
    "role_name": "ExampleServiceRole",
    "role_id": "AROAEXAMPLEEXAMPLE01",
    "arn": f"arn:aws:iam::{ACCOUNT_A}:role/service-role/ExampleServiceRole",
    "path": "/service-role/",
    "create_date": "2026-01-02 03:04:05+00:00",
    "assume_role_policy": {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "backup.amazonaws.com"}}],
    },
}

EC2_INSTANCE_META: dict[str, Any] = {
    "arn": f"arn:aws:ec2:{REGION}:{ACCOUNT_A}:instance/{INSTANCE_ID}",
    "kind": "aws_live",
    "name": "example-node",
    "tags": {"Name": "example-node", "Terraform": "true", "Environment": "prod"},
    "state": "running",
    "region": REGION,
    "vpc_id": VPC_ID,
    "service": "ec2",
    "account_id": ACCOUNT_A,
    "instance_id": INSTANCE_ID,
    "instance_type": "t3a.large",
    "resource_type": "aws_instance",
}
EC2_INSTANCE_BODY: dict[str, Any] = {
    "instance_id": INSTANCE_ID,
    "instance_type": "t3a.large",
    "state": "running",
    "vpc_id": VPC_ID,
    "subnet_id": "subnet-00000000000000001",
    "private_ip": "10.0.0.1",
    "public_ip": "",
    "image_id": "ami-00000000000000001",
    "launch_time": "2026-01-02 03:04:05+00:00",
    "tags": {"Name": "example-node", "Terraform": "true", "Environment": "prod"},
}

VPC_META: dict[str, Any] = {
    "arn": f"arn:aws:ec2:{REGION}:{ACCOUNT_A}:vpc/{VPC_ID}",
    "kind": "aws_live",
    "name": "example-vpc",
    "region": REGION,
    "vpc_id": VPC_ID,
    "service": "ec2",
    "account_id": ACCOUNT_A,
    "cidr_block": "10.0.0.0/16",
    "resource_type": "aws_vpc",
}
VPC_BODY: dict[str, Any] = {
    "vpc_id": VPC_ID,
    "cidr_block": "10.0.0.0/16",
    "is_default": False,
    "state": "available",
    "dhcp_options_id": "dopt-00000000000000001",
    "tags": {},
}

SECURITY_GROUP_META: dict[str, Any] = {
    "arn": f"arn:aws:ec2:{REGION}:{ACCOUNT_A}:security-group/{SG_ID}",
    "kind": "aws_live",
    "name": "example-sg-20260102",
    "tags": {"Name": "example-sg", "Terraform": "true"},
    "region": REGION,
    "vpc_id": VPC_ID,
    "service": "ec2",
    "group_id": SG_ID,
    "account_id": ACCOUNT_A,
    "description": "Allow traffic from VPC",
    "resource_type": "aws_security_group",
}

ORG_ACCOUNT_META: dict[str, Any] = {
    "arn": f"arn:aws:organizations::{MGMT_ACCOUNT}:account/{ORG_ID}/{ACCOUNT_A}",
    "kind": "aws_live",
    "name": "example-workload",
    "email": "platform+example@example.invalid",
    "org_id": ORG_ID,
    "region": "global",
    "status": "ACTIVE",
    "service": "organizations",
    "parent_id": OU_ID,
    "account_id": ACCOUNT_A,
    "resource_type": "aws_organizations_account",
    "joined_timestamp": "2026-01-02 03:04:05.000000+00:00",
}

ORGANIZATION_META: dict[str, Any] = {
    "arn": f"arn:aws:organizations::{MGMT_ACCOUNT}:organization/{ORG_ID}",
    "kind": "aws_live",
    "name": ORG_ID,
    "org_id": ORG_ID,
    "region": "global",
    "service": "organizations",
    "feature_set": "ALL",
    "resource_type": "aws_organization",
    "master_account_id": MGMT_ACCOUNT,
}

ORG_OU_META: dict[str, Any] = {
    "arn": f"arn:aws:organizations::{MGMT_ACCOUNT}:ou/{ORG_ID}/{OU_ID}",
    "kind": "aws_live",
    "name": "Infrastructure",
    "path": "/root/Infrastructure",
    "ou_id": OU_ID,
    "org_id": ORG_ID,
    "region": "global",
    "service": "organizations",
    "parent_id": "r-exam",
    "resource_type": "aws_organizations_ou",
}

_WORKSPACE = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(
    metadata: dict[str, Any],
    body: dict[str, Any] | None = None,
    source_id: uuid.UUID | None = None,
) -> InfraDocument:
    """Build an InfraDocument the way the pipeline builds it from a ref."""
    return InfraDocument(
        source_id=source_id or uuid.uuid4(),
        external_id=str(metadata["arn"]),
        uri=f"aws://{metadata.get('account_id', 'org')}/{metadata['resource_type']}",
        metadata=metadata,
        content_bytes=json.dumps(body or {}, default=str).encode(),
    )


def _entity_from(
    metadata: dict[str, Any],
    body: dict[str, Any] | None = None,
    source_id: uuid.UUID | None = None,
) -> ExtractedEntity:
    entities, _ = extract_aws_live_graph(_doc(metadata, body, source_id))
    assert len(entities) == 1
    return entities[0]


def _edges_by_relation(edges: list[ExtractedEdge]) -> dict[str, ExtractedEdge]:
    return {str(e.metadata["relation"]): e for e in edges}


def _store_roundtrip(
    entity: ExtractedEntity,
    source_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> EntityNodeView:
    """Mirror the Neo4j store's write-then-read path for one entity.

    Uses the store's own mappers rather than hand-building an
    ``EntityNodeView``: the write serialises ``metadata`` to a JSON string
    (``_serialise_metadata_param``) and ``get_all_entities`` returns the
    stored properties raw, so this is the exact value the linker sees.
    Hand-rolling the view here would test an assumption instead of the
    real contract.
    """
    params = _entity_to_params(entity, source_id, workspace_id, "2026-01-02T03:04:05+00:00")
    _serialise_metadata_param(params)
    record = {
        "id": params["id"],
        "name": params["name"],
        "kind": params["entity_type"],
        "source_id": params["source_id"],
        "chunk_text": None,
        "metadata": params["metadata"],
    }
    return _entity_record_to_view(record)


def _view_from(
    metadata: dict[str, Any],
    body: dict[str, Any] | None,
    source_id: uuid.UUID,
) -> EntityNodeView:
    return _store_roundtrip(_entity_from(metadata, body, source_id), source_id, _WORKSPACE)


def _make_graph_store(entities: list[EntityNodeView]) -> AsyncMock:
    store = AsyncMock()
    store.get_all_entities = AsyncMock(return_value=entities)
    store.upsert_edge = AsyncMock(return_value=None)
    store.resolve_pending_stubs = AsyncMock(return_value=0)
    return store


def _emitted_edges(store: AsyncMock) -> list[Any]:
    return [call.kwargs["edge"] for call in store.upsert_edge.await_args_list]


# ===========================================================================
# Extraction — entity shape
# ===========================================================================


class TestEntityShape:
    def test_s3_bucket_produces_one_entity(self) -> None:
        entities, _ = extract_aws_live_graph(_doc(S3_BUCKET_META, S3_BUCKET_BODY))
        assert len(entities) == 1

    def test_entity_kind_is_the_linker_aws_live_kind(self) -> None:
        """kind must be exactly what the linker declares as _AWS_LIVE_KIND."""
        from omniscience_index import linker as linker_module

        entity = _entity_from(S3_BUCKET_META, S3_BUCKET_BODY)
        assert entity.entity_type == AWS_LIVE_KIND
        assert entity.entity_type == linker_module._AWS_LIVE_KIND

    def test_entity_name_is_the_arn(self) -> None:
        entity = _entity_from(IAM_ROLE_META, IAM_ROLE_BODY)
        assert entity.name == IAM_ROLE_META["arn"]

    def test_display_name_is_the_human_name(self) -> None:
        entity = _entity_from(IAM_ROLE_META, IAM_ROLE_BODY)
        assert entity.display_name == "ExampleServiceRole"

    def test_metadata_carries_arn_for_arn_match(self) -> None:
        entity = _entity_from(EC2_INSTANCE_META, EC2_INSTANCE_BODY)
        assert entity.metadata["arn"] == EC2_INSTANCE_META["arn"]

    def test_metadata_carries_tags_for_tags_match(self) -> None:
        entity = _entity_from(EC2_INSTANCE_META, EC2_INSTANCE_BODY)
        assert entity.metadata["tags"] == EC2_INSTANCE_META["tags"]

    def test_absent_tags_are_not_invented(self) -> None:
        entity = _entity_from(IAM_ROLE_META, IAM_ROLE_BODY)
        assert "tags" not in entity.metadata

    def test_metadata_carries_provenance(self) -> None:
        doc = _doc(S3_BUCKET_META, S3_BUCKET_BODY)
        entities, _ = extract_aws_live_graph(doc)
        meta = entities[0].metadata
        assert meta["source_uri"] == doc.uri
        assert meta["source_external_id"] == doc.external_id
        assert meta["source_id"] == str(doc.source_id)
        assert str(meta["extractor"]).startswith("aws-live-inventory/")

    def test_body_scalars_are_promoted(self) -> None:
        entity = _entity_from(EC2_INSTANCE_META, EC2_INSTANCE_BODY)
        assert entity.metadata["subnet_id"] == "subnet-00000000000000001"
        assert entity.metadata["image_id"] == "ami-00000000000000001"

    def test_nested_policy_documents_are_not_copied_onto_the_node(self) -> None:
        """Trust/bucket policies stay in chunk text, not on the graph node."""
        bucket = _entity_from(S3_BUCKET_META, S3_BUCKET_BODY)
        role = _entity_from(IAM_ROLE_META, IAM_ROLE_BODY)
        assert "policy" not in bucket.metadata
        assert "assume_role_policy" not in role.metadata

    def test_connector_metadata_wins_over_body(self) -> None:
        body = dict(EC2_INSTANCE_BODY, instance_type="m5.24xlarge")
        entity = _entity_from(EC2_INSTANCE_META, body)
        assert entity.metadata["instance_type"] == "t3a.large"

    def test_symbol_is_the_document_uri(self) -> None:
        doc = _doc(S3_BUCKET_META, S3_BUCKET_BODY)
        entities, _ = extract_aws_live_graph(doc)
        assert entities[0].symbol == doc.uri


class TestEntityIdentity:
    def test_id_is_stable_across_re_extraction(self) -> None:
        """A re-sync must update the same node, not create a duplicate."""
        source_id = uuid.uuid4()
        first = _entity_from(S3_BUCKET_META, S3_BUCKET_BODY, source_id)
        second = _entity_from(S3_BUCKET_META, S3_BUCKET_BODY, source_id)
        assert first.id == second.id

    def test_id_is_stable_when_the_body_changes(self) -> None:
        source_id = uuid.uuid4()
        first = _entity_from(S3_BUCKET_META, S3_BUCKET_BODY, source_id)
        changed = dict(S3_BUCKET_BODY, versioning_status="Enabled")
        second = _entity_from(S3_BUCKET_META, changed, source_id)
        assert first.id == second.id

    def test_id_differs_per_source(self) -> None:
        """Two sources seeing one ARN stay two nodes for the linker to relate."""
        a = _entity_from(S3_BUCKET_META, S3_BUCKET_BODY, uuid.uuid4())
        b = _entity_from(S3_BUCKET_META, S3_BUCKET_BODY, uuid.uuid4())
        assert a.id != b.id

    def test_entity_id_for_matches_the_extractor(self) -> None:
        source_id = uuid.uuid4()
        entity = _entity_from(S3_BUCKET_META, S3_BUCKET_BODY, source_id)
        assert entity.id == entity_id_for(source_id, str(S3_BUCKET_META["arn"]))


class TestExtractionGuards:
    def test_non_aws_document_yields_nothing(self) -> None:
        doc = InfraDocument(
            source_id=uuid.uuid4(),
            external_id="src/module.py",
            uri="file://src/module.py",
            metadata={"kind": "git_blob"},
            content_bytes=b"def f(): ...",
        )
        assert extract_aws_live_graph(doc) == ([], [])

    def test_document_without_arn_yields_nothing(self) -> None:
        assert extract_aws_live_graph(_doc({**S3_BUCKET_META, "arn": ""})) == ([], [])

    def test_malformed_body_still_yields_the_entity(self) -> None:
        doc = InfraDocument(
            source_id=uuid.uuid4(),
            external_id=str(S3_BUCKET_META["arn"]),
            uri="aws://example",
            metadata=S3_BUCKET_META,
            content_bytes=b"<not json at all>",
        )
        entities, _ = extract_aws_live_graph(doc)
        assert len(entities) == 1
        assert entities[0].name == S3_BUCKET_META["arn"]

    def test_empty_body_still_yields_the_entity(self) -> None:
        doc = InfraDocument(
            source_id=uuid.uuid4(),
            external_id=str(S3_BUCKET_META["arn"]),
            uri="aws://example",
            metadata=S3_BUCKET_META,
            content_bytes=b"",
        )
        entities, _ = extract_aws_live_graph(doc)
        assert len(entities) == 1


# ===========================================================================
# Extraction — owner-asserted edges
# ===========================================================================


class TestOwnerAssertedEdges:
    def test_every_resource_links_to_its_account(self) -> None:
        _, edges = extract_aws_live_graph(_doc(S3_BUCKET_META, S3_BUCKET_BODY))
        rel = _edges_by_relation(edges)
        assert rel["in_account"].target_name == f"arn:aws:iam::{ACCOUNT_A}:root"
        assert rel["in_account"].edge_type == DEPENDS_ON_EDGE

    def test_instance_links_to_its_vpc(self) -> None:
        _, edges = extract_aws_live_graph(_doc(EC2_INSTANCE_META, EC2_INSTANCE_BODY))
        rel = _edges_by_relation(edges)
        assert rel["in_vpc"].target_name == f"arn:aws:ec2:{REGION}:{ACCOUNT_A}:vpc/{VPC_ID}"

    def test_instance_vpc_edge_target_equals_the_vpc_entity_name(self) -> None:
        """The stub target must be the VPC document's own entity name."""
        _, edges = extract_aws_live_graph(_doc(EC2_INSTANCE_META, EC2_INSTANCE_BODY))
        vpc_entity = _entity_from(VPC_META, VPC_BODY)
        assert _edges_by_relation(edges)["in_vpc"].target_name == vpc_entity.name

    def test_security_group_links_to_its_vpc(self) -> None:
        _, edges = extract_aws_live_graph(_doc(SECURITY_GROUP_META))
        rel = _edges_by_relation(edges)
        assert rel["in_vpc"].target_name == f"arn:aws:ec2:{REGION}:{ACCOUNT_A}:vpc/{VPC_ID}"

    def test_vpc_does_not_link_to_itself(self) -> None:
        """A VPC's own body carries vpc_id; that must not become a self-edge."""
        entities, edges = extract_aws_live_graph(_doc(VPC_META, VPC_BODY))
        assert "in_vpc" not in _edges_by_relation(edges)
        assert all(e.target_name != entities[0].name for e in edges)

    def test_instance_does_not_link_to_its_subnet(self) -> None:
        """Subnets are outside discovery scope — no permanent orphan stub."""
        _, edges = extract_aws_live_graph(_doc(EC2_INSTANCE_META, EC2_INSTANCE_BODY))
        assert all("subnet" not in e.target_name for e in edges)

    def test_org_account_links_to_its_parent_ou(self) -> None:
        _, edges = extract_aws_live_graph(_doc(ORG_ACCOUNT_META))
        expected = f"arn:aws:organizations::{MGMT_ACCOUNT}:ou/{ORG_ID}/{OU_ID}"
        assert _edges_by_relation(edges)["in_ou"].target_name == expected

    def test_org_account_parent_ou_target_equals_the_ou_entity_name(self) -> None:
        _, edges = extract_aws_live_graph(_doc(ORG_ACCOUNT_META))
        ou_entity = _entity_from(ORG_OU_META)
        assert _edges_by_relation(edges)["in_ou"].target_name == ou_entity.name

    def test_org_account_links_to_its_organization(self) -> None:
        _, edges = extract_aws_live_graph(_doc(ORG_ACCOUNT_META))
        organization = _entity_from(ORGANIZATION_META)
        assert _edges_by_relation(edges)["in_organization"].target_name == organization.name

    def test_ou_with_root_parent_emits_no_in_ou_edge(self) -> None:
        """A root (``r-…``) is not a document; no edge to a phantom node."""
        _, edges = extract_aws_live_graph(_doc(ORG_OU_META))
        rel = _edges_by_relation(edges)
        assert "in_ou" not in rel
        assert "in_organization" in rel

    def test_organization_links_to_its_management_account(self) -> None:
        _, edges = extract_aws_live_graph(_doc(ORGANIZATION_META))
        rel = _edges_by_relation(edges)
        assert rel["management_account"].target_name == f"arn:aws:iam::{MGMT_ACCOUNT}:root"

    def test_org_account_bridges_to_the_member_account_node(self) -> None:
        """The org record and that account's own resources share one node.

        This is what connects per-account inventories to the organization
        structure across separate sources.
        """
        _, org_edges = extract_aws_live_graph(_doc(ORG_ACCOUNT_META))
        _, resource_edges = extract_aws_live_graph(_doc(S3_BUCKET_META, S3_BUCKET_BODY))
        org_target = _edges_by_relation(org_edges)["in_account"].target_name
        resource_target = _edges_by_relation(resource_edges)["in_account"].target_name
        assert org_target == resource_target == f"arn:aws:iam::{ACCOUNT_A}:root"

    def test_all_edges_use_depends_on(self) -> None:
        for meta, body in (
            (S3_BUCKET_META, S3_BUCKET_BODY),
            (EC2_INSTANCE_META, EC2_INSTANCE_BODY),
            (ORG_ACCOUNT_META, None),
            (ORGANIZATION_META, None),
        ):
            _, edges = extract_aws_live_graph(_doc(meta, body))
            assert edges
            assert {e.edge_type for e in edges} == {DEPENDS_ON_EDGE}

    def test_edges_record_the_asserting_resource(self) -> None:
        entities, edges = extract_aws_live_graph(_doc(EC2_INSTANCE_META, EC2_INSTANCE_BODY))
        assert all(e.metadata["asserted_by"] == entities[0].name for e in edges)
        assert all(e.source_entity_id == entities[0].id for e in edges)

    def test_no_edge_when_the_payload_states_nothing(self) -> None:
        """Strip the identifiers and no relationship may be invented."""
        bare = {
            "kind": "aws_live",
            "arn": "arn:aws:s3:::orphan-bucket",
            "name": "orphan-bucket",
            "resource_type": "aws_s3_bucket",
        }
        entities, edges = extract_aws_live_graph(_doc(bare))
        assert len(entities) == 1
        assert edges == []


# ===========================================================================
# Routing
# ===========================================================================


class TestGraphRouting:
    def test_aws_live_metadata_routes_to_the_infra_extractor(self) -> None:
        assert route_document(source_type="aws", metadata=S3_BUCKET_META) is GraphRoute.AWS_LIVE

    def test_kind_stamp_wins_regardless_of_source_type(self) -> None:
        route = route_document(source_type="unknown", metadata=EC2_INSTANCE_META)
        assert route is GraphRoute.AWS_LIVE

    def test_aws_source_without_kind_stamp_falls_back_on_resource_type(self) -> None:
        meta = {k: v for k, v in S3_BUCKET_META.items() if k != "kind"}
        assert route_document(source_type="aws", metadata=meta) is GraphRoute.AWS_LIVE

    def test_git_document_routes_to_code(self) -> None:
        assert route_document(source_type="git", metadata={}) is GraphRoute.CODE

    def test_missing_metadata_routes_to_code(self) -> None:
        assert route_document(source_type="git", metadata=None) is GraphRoute.CODE

    def test_non_aws_kind_routes_to_code(self) -> None:
        route = route_document(source_type="k8s", metadata={"kind": "k8s_resource"})
        assert route is GraphRoute.CODE


# ===========================================================================
# Cross-source linking — the load-bearing behaviour
# ===========================================================================


class TestCrossSourceLinking:
    @pytest.mark.asyncio
    async def test_same_resource_in_two_sources_produces_a_cross_ref_edge(self) -> None:
        """Two sources describing one ARN must be linked, not left isolated."""
        src_a, src_b = uuid.uuid4(), uuid.uuid4()
        view_a = _view_from(S3_BUCKET_META, S3_BUCKET_BODY, src_a)
        view_b = _view_from(S3_BUCKET_META, S3_BUCKET_BODY, src_b)

        store = _make_graph_store([view_a, view_b])
        created = await EntityLinker(store).link_entities(source_id=src_a, workspace_id=_WORKSPACE)

        assert created == 1
        edges = _emitted_edges(store)
        assert len(edges) == 1
        assert edges[0].edge_type == CROSS_REF_EDGE_TYPE
        assert {edges[0].source_entity_id, edges[0].target_entity_id} == {view_a.id, view_b.id}

    @pytest.mark.asyncio
    async def test_arn_match_links_an_aws_entity_to_a_tfstate_entity(self) -> None:
        """arn_match reads metadata['arn'] — prove the extractor feeds it.

        The tfstate side carries a different ``name`` (a Terraform address),
        so only the ARN in metadata can produce this link.
        """
        src_aws, src_tf = uuid.uuid4(), uuid.uuid4()
        aws_view = _view_from(S3_BUCKET_META, S3_BUCKET_BODY, src_aws)
        tfstate_view = EntityNodeView(
            id=uuid.uuid4(),
            name="resource.aws_s3_bucket.example",
            kind="tfstate_instance",
            source=str(src_tf),
            chunk_text=None,
            metadata={"arn": S3_BUCKET_META["arn"]},
        )

        store = _make_graph_store([aws_view, tfstate_view])
        created = await EntityLinker(store).link_entities(
            source_id=src_aws, workspace_id=_WORKSPACE
        )

        assert created == 1
        edge = _emitted_edges(store)[0]
        assert edge.edge_type == CROSS_REF_EDGE_TYPE
        assert edge.metadata["strategy"] == "arn_match"

    @pytest.mark.asyncio
    async def test_tags_match_links_entities_sharing_tag_values(self) -> None:
        """tags_match reads metadata['tags'] — prove the extractor feeds it.

        Also documents the matcher's breadth: one shared key/value pair is
        enough for a 1.0 score, so tag-based links are broad by design.
        """
        src_a, src_b = uuid.uuid4(), uuid.uuid4()
        other_meta = dict(
            EC2_INSTANCE_META,
            arn=f"arn:aws:ec2:{REGION}:{ACCOUNT_B}:instance/i-00000000000000002",
            account_id=ACCOUNT_B,
            tags={"Terraform": "true"},
        )
        view_a = _view_from(EC2_INSTANCE_META, EC2_INSTANCE_BODY, src_a)
        view_b = _view_from(other_meta, None, src_b)

        store = _make_graph_store([view_a, view_b])
        created = await EntityLinker(store).link_entities(source_id=src_a, workspace_id=_WORKSPACE)

        assert created == 1
        assert _emitted_edges(store)[0].metadata["strategy"] == "tags_match"

    @pytest.mark.asyncio
    async def test_unrelated_resources_are_not_linked(self) -> None:
        """No shared ARN, name, or tag value — no fabricated edge."""
        src_a, src_b = uuid.uuid4(), uuid.uuid4()
        unrelated = dict(
            IAM_ROLE_META,
            arn=f"arn:aws:iam::{ACCOUNT_B}:role/OtherRole",
            account_id=ACCOUNT_B,
            name="OtherRole",
        )
        view_a = _view_from(S3_BUCKET_META, None, src_a)
        view_b = _view_from(unrelated, None, src_b)

        store = _make_graph_store([view_a, view_b])
        created = await EntityLinker(store).link_entities(source_id=src_a, workspace_id=_WORKSPACE)

        assert created == 0
        assert store.upsert_edge.await_count == 0

    @pytest.mark.asyncio
    async def test_entities_from_the_same_source_are_not_linked(self) -> None:
        src = uuid.uuid4()
        view_a = _view_from(S3_BUCKET_META, None, src)
        view_b = _view_from(VPC_META, VPC_BODY, src)

        store = _make_graph_store([view_a, view_b])
        created = await EntityLinker(store).link_entities(source_id=src, workspace_id=_WORKSPACE)

        assert created == 0

    @pytest.mark.asyncio
    async def test_same_role_name_in_two_accounts_is_not_linked(self) -> None:
        """Human names repeat across accounts; ARNs do not.

        Guards the decision to name entities by ARN: naming them by the
        human name would link two unrelated roles.
        """
        src_a, src_b = uuid.uuid4(), uuid.uuid4()
        twin = dict(
            IAM_ROLE_META,
            arn=f"arn:aws:iam::{ACCOUNT_B}:role/service-role/ExampleServiceRole",
            account_id=ACCOUNT_B,
        )
        view_a = _view_from(IAM_ROLE_META, IAM_ROLE_BODY, src_a)
        view_b = _view_from(twin, None, src_b)

        store = _make_graph_store([view_a, view_b])
        created = await EntityLinker(store).link_entities(source_id=src_a, workspace_id=_WORKSPACE)

        assert created == 0

    @pytest.mark.asyncio
    async def test_linker_reads_metadata_as_a_mapping_after_the_store_roundtrip(self) -> None:
        """Regression: the store serialises metadata to a JSON string.

        Before the mapper decoded it back, every metadata matcher raised
        ``AttributeError`` on a ``str`` and ``link_entities`` produced
        nothing while looking healthy.
        """
        src = uuid.uuid4()
        view = _view_from(S3_BUCKET_META, S3_BUCKET_BODY, src)
        assert isinstance(view.metadata, dict)
        assert view.metadata["arn"] == S3_BUCKET_META["arn"]
        assert view.metadata["tags"] == S3_BUCKET_META["tags"]


# ===========================================================================
# Pipeline wiring
# ===========================================================================


def _aws_connector(
    metadata: dict[str, Any],
    body: dict[str, Any],
) -> tuple[MagicMock, DocumentRef]:
    from pydantic import BaseModel

    class _EmptyConfig(BaseModel):
        pass

    ref = DocumentRef(
        external_id=str(metadata["arn"]),
        uri=f"aws://{metadata.get('account_id', 'org')}/{metadata['resource_type']}",
        metadata=metadata,
    )
    connector = MagicMock()
    connector.config_schema = _EmptyConfig
    connector.fetch = AsyncMock(
        return_value=FetchedDocument(
            ref=ref,
            content_bytes=json.dumps(body, default=str).encode(),
            content_type="application/json",
        )
    )
    return connector, ref


def _embedding_provider() -> MagicMock:
    provider = MagicMock()
    provider.dim = 4
    provider.model_name = "test-model"
    provider.provider_name = "test-provider"
    provider.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    return provider


def _index_writer() -> MagicMock:
    result = MagicMock()
    result.action = "created"
    result.chunks_written = 1
    result.document_id = uuid.uuid4()
    result.doc_version = 1

    writer = MagicMock()
    writer.upsert_document = AsyncMock(return_value=result)
    # A real writer returns a GraphWriteResult; returning None here is what let
    # ``getattr(result, "applied", True)`` in the pipeline pass a double that
    # reports nothing as a successful write.
    writer.upsert_graph = AsyncMock(
        return_value=GraphWriteResult(
            applied=True,
            entities_written=1,
            edges_written=1,
            entities_submitted=1,
            edges_submitted=1,
        )
    )
    writer.tombstone = AsyncMock(return_value=True)
    return writer


def _aws_event(source_id: uuid.UUID, ref: DocumentRef) -> DocumentChangeEvent:
    return DocumentChangeEvent(
        source_id=source_id,
        source_type="aws",
        external_id=ref.external_id,
        uri=ref.uri,
        action="created",
    )


class TestPipelineWiring:
    @pytest.mark.asyncio
    async def test_aws_document_reaches_upsert_graph_with_aws_live_entities(self) -> None:
        from omniscience_parsers.code.graph import extract_symbol_graph

        source_id = uuid.uuid4()
        connector, ref = _aws_connector(EC2_INSTANCE_META, EC2_INSTANCE_BODY)
        writer = _index_writer()
        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=_embedding_provider(),
            index_writer=writer,
            graph_extractor=extract_symbol_graph,
        )

        result = await pipeline.run_ref(
            event=_aws_event(source_id, ref),
            ref=ref,
            config=None,
            secrets={},
            workspace_id=_WORKSPACE,
        )

        assert result.action == "created"
        writer.upsert_graph.assert_awaited_once()
        entities = writer.upsert_graph.await_args.kwargs["entities"]
        edges = writer.upsert_graph.await_args.kwargs["edges"]
        assert [e.entity_type for e in entities] == [AWS_LIVE_KIND]
        assert entities[0].name == EC2_INSTANCE_META["arn"]
        assert {e.edge_type for e in edges} == {DEPENDS_ON_EDGE}

    @pytest.mark.asyncio
    async def test_stage_link_runs_for_an_aws_document(self) -> None:
        from omniscience_parsers.code.graph import extract_symbol_graph

        source_id = uuid.uuid4()
        connector, ref = _aws_connector(S3_BUCKET_META, S3_BUCKET_BODY)
        linker = MagicMock()
        linker.link_entities = AsyncMock(return_value=3)

        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=_embedding_provider(),
            index_writer=_index_writer(),
            graph_extractor=extract_symbol_graph,
            entity_linker=linker,
        )
        await pipeline.run_ref(
            event=_aws_event(source_id, ref),
            ref=ref,
            config=None,
            secrets={},
            workspace_id=_WORKSPACE,
        )

        linker.link_entities.assert_awaited_once_with(source_id=source_id, workspace_id=_WORKSPACE)

    @pytest.mark.asyncio
    async def test_link_failure_does_not_fail_the_document(self) -> None:
        from omniscience_parsers.code.graph import extract_symbol_graph

        connector, ref = _aws_connector(S3_BUCKET_META, S3_BUCKET_BODY)
        linker = MagicMock()
        linker.link_entities = AsyncMock(side_effect=RuntimeError("neo4j unavailable"))

        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=_embedding_provider(),
            index_writer=_index_writer(),
            graph_extractor=extract_symbol_graph,
            entity_linker=linker,
        )
        result = await pipeline.run_ref(
            event=_aws_event(uuid.uuid4(), ref),
            ref=ref,
            config=None,
            secrets={},
            workspace_id=_WORKSPACE,
        )

        assert result.action == "created"
        assert result.error is None


class TestCodeRouteRegression:
    @pytest.mark.asyncio
    async def test_python_document_still_uses_the_symbol_extractor(self) -> None:
        """The code route must be untouched by the infra dispatch."""
        from pydantic import BaseModel

        class _EmptyConfig(BaseModel):
            pass

        source = b"import os\n\n\ndef handler():\n    os.getcwd()\n"
        ref = DocumentRef(external_id="pkg/mod.py", uri="file://pkg/mod.py")
        connector = MagicMock()
        connector.config_schema = _EmptyConfig
        connector.fetch = AsyncMock(
            return_value=FetchedDocument(ref=ref, content_bytes=source, content_type="text/plain")
        )

        seen: dict[str, Any] = {}

        def _spy(parsed: Any, content: bytes) -> tuple[list[Any], list[Any]]:
            from omniscience_parsers.code.graph import extract_symbol_graph

            seen["language"] = parsed.language
            return extract_symbol_graph(parsed, content)

        writer = _index_writer()
        pipeline = IngestionPipeline(
            connector=connector,
            embedding_provider=_embedding_provider(),
            index_writer=writer,
            graph_extractor=_spy,
        )
        event = DocumentChangeEvent(
            source_id=uuid.uuid4(),
            source_type="git",
            external_id="pkg/mod.py",
            uri="file://pkg/mod.py",
            action="created",
        )
        await pipeline.run_ref(
            event=event, ref=ref, config=None, secrets={}, workspace_id=_WORKSPACE
        )

        assert seen["language"] == "python"
        writer.upsert_graph.assert_awaited_once()
        kinds = {e.entity_type for e in writer.upsert_graph.await_args.kwargs["entities"]}
        assert "module" in kinds
        assert AWS_LIVE_KIND not in kinds


# ===========================================================================
# Worker wiring
# ===========================================================================


def _session_factory(source: Any) -> MagicMock:
    inner = AsyncMock()
    inner.begin = MagicMock(return_value=inner)
    inner.add = MagicMock()
    inner.flush = AsyncMock()
    inner.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=source))
    )
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value = cm
    return factory


def _source_row() -> MagicMock:
    src = MagicMock()
    src.id = uuid.uuid4()
    src.name = "aws-management"
    src.tenant_id = _WORKSPACE
    src.config = {}
    src.secrets_ref = None
    return src


def _worker(entity_linker: Any, connector: MagicMock) -> IngestionWorker:
    consumer = MagicMock()
    consumer.stop = MagicMock()
    registry = MagicMock()
    registry.get = MagicMock(return_value=connector)
    return IngestionWorker(
        queue_consumer=consumer,
        connector_registry=registry,
        embedding_provider=_embedding_provider(),
        index_writer=_index_writer(),
        session_factory=_session_factory(_source_row()),
        entity_linker=entity_linker,
    )


class TestWorkerWiring:
    @pytest.mark.asyncio
    async def test_worker_drives_the_linker_for_each_document(self) -> None:
        from omniscience_server.ingestion.dedup import DedupAction

        connector, ref = _aws_connector(S3_BUCKET_META, S3_BUCKET_BODY)
        linker = MagicMock()
        linker.link_entities = AsyncMock(return_value=1)
        worker = _worker(linker, connector)
        decision = MagicMock()
        decision.action = DedupAction.accept
        worker._dedup_gate = MagicMock()
        worker._dedup_gate.evaluate = AsyncMock(return_value=decision)

        event = _aws_event(uuid.uuid4(), ref)
        result = await worker.process_document(event)

        assert result.action == "created"
        linker.link_entities.assert_awaited_once_with(
            source_id=event.source_id, workspace_id=_WORKSPACE
        )

    def test_worker_without_a_linker_keeps_linking_off(self) -> None:
        connector, _ = _aws_connector(S3_BUCKET_META, S3_BUCKET_BODY)
        assert _worker(None, connector)._entity_linker is None

    @pytest.mark.parametrize(
        "value",
        [
            "false",
            "0",
            "no",
            "off",
            # Every one of these left linking ON under the old denylist.
            "disabled",
            "n",
            "none",
            "",
            "   ",
            "FALSE",
            "not-a-boolean",
        ],
    )
    def test_env_flag_disables_an_injected_linker(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """The switch exists to shed load, so an unrecognised value must shed.

        ``EntityLinker.link_entities`` reads every entity in the workspace on
        every document.  An operator setting this flag is trying to stop that.
        The previous denylist understood four spellings and treated
        ``disabled`` — the spelling the sibling ``OMNISCIENCE_GRAPH_*`` flags
        use — as "stay enabled", as it did an empty string, which is what an
        unset-but-declared Helm or compose variable produces.
        """
        monkeypatch.setenv("OMNISCIENCE_ENTITY_LINKING_ENABLED", value)
        connector, _ = _aws_connector(S3_BUCKET_META, S3_BUCKET_BODY)
        assert _worker(MagicMock(), connector)._entity_linker is None

    @pytest.mark.parametrize("value", ["enabled", "true", "1", "yes", "on", "ON", " true "])
    def test_an_explicit_enabled_value_keeps_the_linker(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Failing closed on garbage must not mean failing closed on a real 'on'."""
        monkeypatch.setenv("OMNISCIENCE_ENTITY_LINKING_ENABLED", value)
        connector, _ = _aws_connector(S3_BUCKET_META, S3_BUCKET_BODY)
        linker = MagicMock()
        assert _worker(linker, connector)._entity_linker is linker

    def test_linker_is_on_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMNISCIENCE_ENTITY_LINKING_ENABLED", raising=False)
        connector, _ = _aws_connector(S3_BUCKET_META, S3_BUCKET_BODY)
        linker = MagicMock()
        assert _worker(linker, connector)._entity_linker is linker
