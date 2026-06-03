# Spec — AWS ingestion Phase 1: Config-aggregator baseline graph

Implements **Phase 1** of [ADR-0014](../decisions/0014-aws-dual-mechanism-ingestion.md):
a pull-only baseline that turns the org AWS Config aggregator into bitemporal
entities + relationship edges, so `get_related_entities` / `blast_radius` work
over real AWS state. **No new AWS infra**, no push, no CloudTrail (those are
later phases). Grounded in `qbiq-ai/infra` (Config already org-wide; aggregator
delegated to Log Archive; recording `all_supported`, `all_regions`).

## Scope of this phase

IN: `acquisition="config"` mode on the existing AWS connector; read the org
aggregator; map Config configuration items (CIs) → entities + edges; bitemporal
`valid_from = configurationItemCaptureTime`; Tier-1 resource types; tombstone on
`ResourceDeleted`; cold-pointer field (S3 path) on each version; reconciliation
semantics (full + diff). Unit/integration tests with mocked boto3.

OUT (later phases): Cloud Control generic poll (Phase 3), EventBridge→SQS push
(Phase 4), CloudTrail activity (Phase 5), org auto-onboarding (Phase 6), Athena
re-hydration query path (warm/cold read — Phase 1 only *writes* the pointer).

## Connector contract

Extend `packages/connectors/src/omniscience_connectors/aws/connector.py` (and
`AwsConfig`) — do NOT fork a new connector. Add:

```
class AwsConfig(BaseModel):
    acquisition: Literal["describe", "cloudcontrol", "config"] = "describe"
    # config-mode fields:
    aggregator_name: str | None = None          # org aggregator in Log Archive
    config_resource_types: list[str] = [<Tier-1 default>]   # AWS::* type names
    config_regions: list[str] = []              # empty = all-regions (aggregator already all_regions)
    # existing fields (regions/services/include_organizations/...) unchanged
```

`acquisition="describe"` keeps the current behaviour byte-for-byte (regression
guard). `acquisition="config"` activates the new path below.

### discover() — config mode

Use the boto3 **`config`** client against the aggregator:

- Enumerate CIs per Tier-1 type via `SelectAggregateResourceConfig`
  (SQL-like: `SELECT ... WHERE resourceType = 'AWS::EC2::Instance'`) OR
  `ListAggregateDiscoveredResources` + `BatchGetAggregateResourceConfig`.
  Page fully. Wrap blocking boto3 in `asyncio.to_thread` (match existing style).
- Yield one `DocumentRef` per resource with metadata:
  `{aws_resource_type, resource_id, arn, account_id, region, capture_time,
    cluster/None}`. Stable `external_id = aws:{account}:{region}:{type}:{resource_id}`,
  `uri = aws://{account}/{region}/{type}/{resource_id}`.
- Gracefully skip a type the aggregator/role can't read (log + continue) — never
  abort the whole discover (mirror the K8s connector's per-kind skip).

### fetch() — config mode

For a per-resource ref, return a `FetchedDocument` whose body is a **concise
human-readable summary** (type, id, account, region, key config fields, tags) so
embeddings are meaningful — not the raw CI JSON dump. Attach structured metadata
for the pipeline (see mapping). Reuse `SelectAggregateResourceConfig` detail or
`BatchGetAggregateResourceConfig` for the single CI.

## Bitemporal + graph mapping

Per CI:

- **Entity / Document.** `entity_type = aws_resource_type`, `name = resource_id`
  (or tag `Name`), `metadata` carries account/region/arn/tags + the compact
  config. **`valid_from = configurationItemCaptureTime`** (bitemporal, ADR-0008).
- **Edges from Config `relationships`.** Each CI's `relationships[]`
  (e.g. `Is associated with Vpc`, `Contains SecurityGroup`) → a graph edge
  `source_entity → target_entity` with `edge_type` derived from the relationship
  name (normalised). Target keyed by the related resourceId → same `external_id`
  scheme so edges resolve to ingested entities. This is what powers
  `get_related_entities` / `blast_radius`.
- **Tombstone.** A CI with `configurationItemStatus in {ResourceDeleted,
  ResourceDeletedNotRecorded}` → tombstone the entity (existing IndexWriter
  tombstone path), preserving bitemporal history.
- **Cold pointer (retention tiering).** Persist on each version a
  `cold_ref = { s3_bucket, s3_key, capture_time }` pointing at the Config
  delivery S3 object, so warm/cold tiers can re-hydrate via Athena later without
  us storing the full historical blob hot. Phase 1 only *writes* this pointer.

## Reconciliation semantics (pull)

A sync = a full enumeration of Tier-1 types from the aggregator. Reconciliation:

- New/changed CIs (by `configurationItemCaptureTime` or config hash) → upsert new
  bitemporal version.
- Resources present last run but absent now → tombstone (drift / deletion catch).
- Idempotent: re-running with no AWS change writes nothing new.

Cadence is operational (full nightly + 6h incremental) — driven by the source's
`freshness_sla_seconds` and the scheduler; the connector itself is stateless per
run.

## IAM (deliverable, applied separately by humans)

Produce the **read-only IAM policy JSON** + a Terraform stub (for `qbiq-ai/infra`,
Log Archive account) granting Omniscience's role:

```
config:SelectAggregateResourceConfig
config:BatchGetAggregateResourceConfig
config:ListAggregateDiscoveredResources
config:GetAggregateResourceConfig
config:GetAggregateDiscoveredResourceCounts
config:GetAggregateResourceConfigHistory   # (Phase 2, include now)
config:DescribeConfigurationAggregators
s3:GetObject  on the Config delivery bucket/prefix  # cold re-hydration (read)
```

The connector consumes the assumed-role creds via the existing env-based
`SecretsResolver` (role ARN + external id, or STS-vended keys) — **no static
long-lived keys**. Applying the role to AWS is a human-approved infra step
(out of scope for the code PR); the policy/stub is a documented deliverable.

## Security

- Reading the org aggregator = full org inventory → strictly **read-only**, role
  scoped to the aggregator + Config-S3 read, audited. No write/mutate perms.
- Never log secrets or full resource configs at info level.
- Workspace/tenant: AWS source is tenant-scoped like any other source.

## Tests (≥80% on touched connector code; full suite stays green)

- `describe` mode unchanged (regression).
- config discover: mocked `config` client returns multi-type, multi-account CIs →
  one ref per resource; correct `external_id`/`uri`/metadata; per-type read error
  skips that type, others still yielded.
- mapping: `relationships[]` → edges resolving to sibling entities;
  `valid_from = captureTime`; `ResourceDeleted` → tombstone; `cold_ref` written.
- fetch: returns text/plain summary with type+id+account.
- reconciliation: absent-now resource tombstoned; no-change run is a no-op.
- Use mocks or `moto` for the `config` client; no live AWS in CI.

## Live wiring (after merge — separate, human-gated)

1. Apply the IAM role/policy to Log Archive (infra repo PR — `/infra-team`, human-approved).
2. Create an AWS Source: `type=aws`, `config={ acquisition:"config",
   aggregator_name:"<org-aggregator>", config_resource_types:[<Tier-1>] }`,
   `secrets_ref` → assumed-role creds.
3. Trigger sync; verify entities + edges in Neo4j and that `blast_radius` /
   `get_related_entities` answer over AWS resources; confirm tombstone on a
   deleted resource; confirm `cold_ref` populated.
