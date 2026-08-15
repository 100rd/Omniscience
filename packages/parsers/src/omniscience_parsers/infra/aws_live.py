"""Graph extractor for live AWS inventory documents (``kind == "aws_live"``).

The AWS connector (:mod:`omniscience_connectors.aws.connector`) emits one
document per live resource with a rich ``DocumentRef.metadata`` payload
(ARN, account id, region, service, resource type, tags, and the resource's
own identifiers) plus a JSON describe body.  Neither is source code, so
:func:`~omniscience_parsers.code.graph.extract_symbol_graph` returns
``([], [])`` for them — this module is the infrastructure counterpart.

Why the output shape matches the *code* graph DTOs
--------------------------------------------------
The ingestion pipeline writes whatever the extractor returns through
``IndexWriter.upsert_graph`` -> ``GraphStore.upsert_graph``.  The Neo4j
mapper (``omniscience_index.stores.neo4j.mappers``) duck-types entities on
``id`` / ``entity_type`` / ``name`` / ``display_name`` / ``metadata`` and
edges on ``source_entity_id`` / ``target_name`` / ``edge_type`` — i.e. the
:class:`~omniscience_parsers.code.graph.ExtractedEntity` and
:class:`~omniscience_parsers.code.graph.ExtractedEdge` shape.  The older
:class:`~omniscience_parsers.infra.graph.EntityData` / ``EdgeData`` pair
(symbol/kind/from_symbol/to_symbol) is *not* writable through that path, so
this extractor deliberately emits the pipeline DTOs.

Contract with the cross-source linker
-------------------------------------
``omniscience_index.linker.EntityLinker`` matches on fields its matchers
actually read (``omniscience_index.matchers``):

===================== ============================================
matcher               field consumed
===================== ============================================
``exact_name_match``  ``EntityNodeView.name``
``arn_match``         ``metadata["arn"]``
``tags_match``        ``metadata["tags"]`` / ``metadata["labels"]``
``otel_match``        ``metadata["service_name"|"pod_name"|"trace_id"]``
===================== ============================================

Every entity emitted here therefore carries the ARN both as ``name`` and as
``metadata["arn"]``, and carries the resource's tags verbatim under
``metadata["tags"]`` when the owner reported any.  ``entity_type`` is the
connector's own ``kind`` (``"aws_live"``), which is the value the linker
declares as ``_AWS_LIVE_KIND``.

Why ``name`` is the ARN and not the human name
----------------------------------------------
``exact_name_match`` scores 1.0 on normalised name equality and the linker
turns that into a ``cross_ref`` edge.  Human names are *not* unique across
AWS accounts (``AWSReservedSSO_AdministratorAccess`` exists in every
account), so using them would link unrelated resources in different
accounts — a fabricated relationship.  The ARN is globally unique and is
the owner's own identifier, so name-equality means "the same resource".
The human name is preserved as ``display_name``.

Edges
-----
Only relationships the owner's own payload asserts are emitted, all with the
existing ``depends_on`` edge type and a ``metadata["relation"]``
discriminator:

``in_account``          every resource states its ``account_id``
``management_account``  an organization states its ``master_account_id``
``in_vpc``              an EC2 instance / security group states its ``vpc_id``
``in_ou``               an OU / account states an OU ``parent_id``
``in_organization``     an OU / account states its ``org_id``

Edge targets are named by the *target's* ARN, reconstructed from identifiers
the owner itself reports.  The graph store resolves an unknown target name by
``MERGE``-ing a stub node keyed on ``(workspace_id, name)``, which is later
merged into the real entity by ``EntityLinker.resolve_stubs`` — so an edge to
a not-yet-ingested resource is not lost, and an edge to an already-ingested
one attaches directly.

Deliberately NOT emitted: an ``in_subnet`` edge.  ``subnet_id`` is present in
an EC2 instance's describe body, but subnets are outside the connector's
discovery scope, so such an edge could only ever point at a permanent orphan
stub.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from omniscience_parsers.code.graph import ExtractedEdge, ExtractedEntity

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: ``entity_type`` stamped on every entity produced here.  Mirrors the
#: connector's ``DocumentRef.metadata["kind"]`` and the linker's
#: ``_AWS_LIVE_KIND``.  The Neo4j mapper writes it to ``Entity.kind``.
AWS_LIVE_KIND: str = "aws_live"

#: Edge type for every owner-asserted relationship.  Already part of the
#: infrastructure graph vocabulary (``omniscience_parsers.infra.graph``) and
#: accepted by the Neo4j edge-type allowlist regex.
DEPENDS_ON_EDGE: str = "depends_on"

#: Version stamp written to ``metadata["extractor"]`` for provenance.
EXTRACTOR_VERSION: str = "aws-live-inventory/v1"

#: Fixed namespace for deterministic (uuid5) entity ids.  Ids are derived
#: from ``(source_id, arn)`` so a re-sync of the same resource updates the
#: same node instead of creating a duplicate, while the same resource seen
#: through two different sources stays two nodes for the linker to relate.
_AWS_ENTITY_NAMESPACE: uuid.UUID = uuid.UUID("6f1f0d2e-4a4b-5c6d-8e9f-0a1b2c3d4e5f")

#: Scalar describe-body fields worth promoting into entity metadata.  An
#: allowlist rather than a full body merge: IAM trust policies, bucket
#: policies and security-group rule lists are large nested documents that
#: belong in the chunk text, not on a graph node.
_BODY_SCALAR_FIELDS: frozenset[str] = frozenset(
    {
        "attachment_count",
        "cidr_block",
        "description",
        "dhcp_options_id",
        "feature_set",
        "group_name",
        "image_id",
        "instance_type",
        "is_default",
        "location",
        "master_account_id",
        "path",
        "policy_id",
        "policy_name",
        "private_ip",
        "role_id",
        "state",
        "status",
        "subnet_id",
        "user_id",
        "versioning_status",
        "vpc_id",
    }
)

#: Resource types whose ``vpc_id`` denotes containment in *another* resource.
#: A VPC's own describe body also carries ``vpc_id`` (itself), so restricting
#: the rule avoids a self-edge.
_VPC_MEMBER_TYPES: frozenset[str] = frozenset({"aws_instance", "aws_security_group"})

#: Resource types that participate in the AWS Organizations hierarchy.
_ORG_MEMBER_TYPES: frozenset[str] = frozenset(
    {"aws_organizations_account", "aws_organizations_ou"}
)

_ARN_ACCOUNT_FIELD: int = 4
_ARN_MIN_FIELDS: int = 6


# ---------------------------------------------------------------------------
# Input DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InfraDocument:
    """One fetched infrastructure document, as the pipeline sees it.

    Carries both halves of the owner's payload: the connector's discovery
    ``metadata`` (stable identity — ARN, account, region, resource type,
    tags) and the fetched describe ``content_bytes`` (richer per-resource
    detail).  ``source_id`` scopes the deterministic entity id.
    """

    source_id: uuid.UUID
    external_id: str
    uri: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    content_bytes: bytes = b""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_body(content_bytes: bytes) -> dict[str, Any]:
    """Decode the describe body, tolerating anything that is not a JSON object.

    The body is supplementary — identity comes from the connector metadata —
    so a malformed or non-JSON payload degrades to an empty mapping instead
    of failing extraction for the whole document.
    """
    if not content_bytes:
        return {}
    try:
        decoded = json.loads(content_bytes.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _arn_account(arn: str) -> str:
    """Return the account-id field of *arn*, or ``""`` when it is absent.

    ``arn:aws:organizations::168798727026:ou/o-x/ou-y`` -> ``168798727026``.
    Organizations ARNs are owned by the *management* account, which is the
    only place a member-account document states it.
    """
    parts = arn.split(":")
    if len(parts) < _ARN_MIN_FIELDS:
        return ""
    return parts[_ARN_ACCOUNT_FIELD]


def _account_root_arn(account_id: str) -> str:
    """Canonical ARN of an AWS account principal (``arn:aws:iam::<id>:root``).

    Derived purely from the ``account_id`` the owner reports.  Every AWS
    document — in every account — can compute the same value, which is what
    lets per-account inventories and the organization's account records
    converge on one shared graph node.
    """
    return f"arn:aws:iam::{account_id}:root"


def _vpc_arn(region: str, account_id: str, vpc_id: str) -> str:
    return f"arn:aws:ec2:{region}:{account_id}:vpc/{vpc_id}"


def _ou_arn(owner_account: str, org_id: str, ou_id: str) -> str:
    return f"arn:aws:organizations::{owner_account}:ou/{org_id}/{ou_id}"


def _organization_arn(owner_account: str, org_id: str) -> str:
    return f"arn:aws:organizations::{owner_account}:organization/{org_id}"


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def _entity_metadata(
    doc: InfraDocument,
    ref_meta: Mapping[str, Any],
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble entity metadata from the owner's payload plus provenance.

    Precedence: connector discovery metadata wins over the describe body —
    discovery metadata is what the document row was indexed with, so keeping
    it authoritative means the graph node and the Postgres document agree.
    """
    meta: dict[str, Any] = {}

    for key, value in body.items():
        if key in _BODY_SCALAR_FIELDS and _is_scalar(value) and value != "":
            meta[key] = value

    body_tags = body.get("tags")
    if isinstance(body_tags, dict) and body_tags:
        meta["tags"] = {str(k): v for k, v in body_tags.items()}

    for key, value in ref_meta.items():
        if key == "tags":
            if isinstance(value, dict) and value:
                meta["tags"] = {str(k): v for k, v in value.items()}
            continue
        if value == "" or value is None:
            continue
        meta[key] = value

    # Provenance — the source document this node was derived from.
    meta["source_uri"] = doc.uri
    meta["source_external_id"] = doc.external_id
    meta["source_id"] = str(doc.source_id)
    meta["extractor"] = EXTRACTOR_VERSION
    return meta


def _reference_edge(
    entity_id: uuid.UUID,
    own_name: str,
    target_name: str,
    relation: str,
) -> ExtractedEdge | None:
    """Build a ``depends_on`` edge, dropping empty and self-referential targets."""
    if not target_name or target_name == own_name:
        return None
    return ExtractedEdge(
        source_entity_id=entity_id,
        target_name=target_name,
        edge_type=DEPENDS_ON_EDGE,
        metadata={"relation": relation, "asserted_by": own_name},
    )


def _owner_asserted_edges(
    entity_id: uuid.UUID,
    arn: str,
    resource_type: str,
    meta: Mapping[str, Any],
) -> list[ExtractedEdge]:
    """Derive every edge the owner's own payload asserts.

    Nothing here is inferred: each branch fires only when the resource itself
    reported the identifier that names the target.
    """
    candidates: list[ExtractedEdge | None] = []
    account_id = str(meta.get("account_id", "") or "")
    region = str(meta.get("region", "") or "")
    org_id = str(meta.get("org_id", "") or "")
    owner_account = _arn_account(arn)

    if account_id:
        candidates.append(
            _reference_edge(entity_id, arn, _account_root_arn(account_id), "in_account")
        )

    # An organization has no ``account_id`` of its own; it names the account
    # that manages it.
    if resource_type == "aws_organization":
        master = str(meta.get("master_account_id", "") or "")
        if master:
            candidates.append(
                _reference_edge(entity_id, arn, _account_root_arn(master), "management_account")
            )

    if resource_type in _VPC_MEMBER_TYPES:
        vpc_id = str(meta.get("vpc_id", "") or "")
        if vpc_id and region and account_id:
            candidates.append(
                _reference_edge(entity_id, arn, _vpc_arn(region, account_id, vpc_id), "in_vpc")
            )

    if resource_type in _ORG_MEMBER_TYPES and org_id and owner_account:
        parent_id = str(meta.get("parent_id", "") or "")
        # A root parent (``r-…``) is not discovered as a document; the
        # organization edge below already expresses that membership.
        if parent_id.startswith("ou-"):
            candidates.append(
                _reference_edge(entity_id, arn, _ou_arn(owner_account, org_id, parent_id), "in_ou")
            )
        candidates.append(
            _reference_edge(
                entity_id, arn, _organization_arn(owner_account, org_id), "in_organization"
            )
        )

    return [edge for edge in candidates if edge is not None]


def entity_id_for(source_id: uuid.UUID, arn: str) -> uuid.UUID:
    """Return the deterministic entity id for *arn* within *source_id*.

    Exposed so callers (and tests) can assert idempotency without
    re-implementing the derivation.
    """
    return uuid.uuid5(_AWS_ENTITY_NAMESPACE, f"{source_id}|{arn}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def supports(metadata: Mapping[str, Any] | None) -> bool:
    """Return True when *metadata* identifies a live AWS inventory document."""
    if not metadata:
        return False
    return str(metadata.get("kind", "")) == AWS_LIVE_KIND


def extract_aws_live_graph(
    doc: InfraDocument,
) -> tuple[list[ExtractedEntity], list[ExtractedEdge]]:
    """Extract one entity — plus the edges its payload asserts — from *doc*.

    Returns two empty lists when the document is not a live AWS resource or
    when it carries no ARN, since the ARN is the entity's identity and the
    field every matcher keys on.  Callers treat that as "nothing to write"
    rather than as an error.
    """
    ref_meta = doc.metadata or {}
    if not supports(ref_meta):
        return [], []

    body = _decode_body(doc.content_bytes)
    arn = str(ref_meta.get("arn", "") or body.get("arn", "") or "")
    if not arn:
        return [], []

    resource_type = str(ref_meta.get("resource_type", "") or "")
    display_name = str(ref_meta.get("name", "") or "") or arn
    entity_id = entity_id_for(doc.source_id, arn)
    meta = _entity_metadata(doc, ref_meta, body)
    # Identity fields are authoritative regardless of what the body said.
    meta["arn"] = arn

    entity = ExtractedEntity(
        id=entity_id,
        entity_type=AWS_LIVE_KIND,
        name=arn,
        display_name=display_name,
        symbol=doc.uri,
        metadata=meta,
    )
    edges = _owner_asserted_edges(entity_id, arn, resource_type, meta)
    return [entity], edges


__all__ = [
    "AWS_LIVE_KIND",
    "DEPENDS_ON_EDGE",
    "EXTRACTOR_VERSION",
    "InfraDocument",
    "entity_id_for",
    "extract_aws_live_graph",
    "supports",
]
