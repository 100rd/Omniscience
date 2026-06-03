# ADR-0014 — Dual-mechanism AWS ingestion: generic poll + AWS Config

- **Status**: Accepted
- **Date**: 2026-06-03
- **Deciders**: Architecture, Backend Engineer
- **Builds on**: [ADR-0002](0002-connector-framework-vs-sdk.md) (connector
  framework), [ADR-0008](0008-bitemporal-schema-for-neo4j.md) (bitemporal
  schema — AWS state is versioned per `valid_from`/`valid_to`),
  [ADR-0009](0009-retention-tiering-policy.md) (retention/tombstone — applies
  to AWS history), [ADR-0010](0010-server-side-emitter-dedup.md) (emitter dedup
  — a resource visible via two mechanisms must not double-write).
- **Supersedes scope of**: the per-service AWS connector shipped in
  `packages/connectors/src/omniscience_connectors/aws/connector.py` (PR #285
  added Organizations enumeration). That connector is retained but reframed as
  one *acquisition mode* among three (see Decision).

## Context

The current AWS connector polls services **directly** via boto3: `list_buckets`
(S3), `list_users`/`list_roles` (IAM), `describe_instances`/`describe_vpcs`
(EC2), plus optional Organizations enumeration. Each service is **hand-coded** —
its own operations, response shapes, and pagination. Three structural problems
surface over a long operating period:

1. **No dynamic coverage.** Every new service or resource type requires new
   connector code. AWS has 200+ services; per-service `describe_*` does not
   scale to "show me everything".
2. **Full-scan pull cost.** Each sync re-enumerates every service × region ×
   account. Cost and API-throttling grow with the *resource count*, and
   freshness is bounded by the sync interval (minutes-to-hours stale).
3. **No native history or change signal.** Snapshots only; the bitemporal value
   of Omniscience (ADR-0008) — "what did this security group look like on date
   X" — can only be approximated by diffing our own repeated scans.

We evaluated whether the direct-poll pattern can be made *dynamic* (auto-pick-up
new resource types without code) and how state should be acquired **over a long
period**, given Omniscience is bitemporal and already has a freshness scheduler,
tombstone, and janitor purge.

Key finding: "loop over all boto3 services and call `list_*`" does **not** work —
`session.get_available_services()` enumerates clients, but operation names,
required parameters, pagination, and result shapes differ per service, so the
output cannot be parsed uniformly. Dynamic coverage requires a **uniform API**,
not a loop over heterogeneous ones.

Two uniform sources of AWS resource state exist:

- **AWS Cloud Control API** (`cloudcontrolapi`) — a single `ListResources` /
  `GetResource` interface over the **CloudFormation resource type registry**
  (`AWS::EC2::Instance`, `AWS::S3::Bucket`, … ~1000+ types). Types are
  enumerable via `cloudformation:ListTypes`. New types appear in the registry
  and are picked up **without new code**. Pull-based; no native history/events;
  some types cannot `LIST` without a parent identifier; needs broad read IAM
  (≈ `ReadOnlyAccess`) but a single code path.
- **AWS Config** (+ org **aggregator**) — records the configuration **and its
  history** for supported resource types automatically, exposes a query API
  (`SelectAggregateResourceConfig`), native resource **`relationships`**, and a
  **change stream** (configuration-item changes → EventBridge). Push-capable and
  history-native. Costs money (per configuration item recorded + aggregator) and
  must be enabled org-wide.

These are two **acquisition mechanisms for the same thing** — a resource is a
resource; both feed the identical bitemporal ingest (entity + edges + tombstone).
They differ only in *how state is obtained* and *whether history/events come
from AWS or from our own diffing*.

Operator context driving this ADR (captured 2026-06-03): the target environment
is an **AWS Organization with a delegated-admin/hub account** (cross-account
`AssumeRole` available), and we want to capture **resource state + history**
*and* **activity** (CloudTrail).

## Decision

Adopt a **dual-mechanism** (effectively three acquisition modes) AWS ingestion
model behind a single connector and a single bitemporal pipeline. Do **not**
create divergent connectors per mechanism.

### 1. One connector, an `acquisition` mode switch

Extend `AwsConfig` with `acquisition: "describe" | "cloudcontrol" | "config"`
(default chosen per environment — see Phases):

- **`describe`** (legacy, retained) — the existing per-service path. Highest
  fidelity for the few hand-modelled services; works with minimal IAM; no new
  dependency. Used for small setups or to fill a gap a uniform API misses.
- **`cloudcontrol`** (Pattern 1, *dynamic*) — generic `ListResources`/
  `GetResource` over the CloudFormation registry. This is the answer to
  "dynamically pull new services/resources without code". Optionally seeded by
  **Resource Groups Tagging API** / **Resource Explorer** for fast breadth
  enumeration (ARN + type + tags) before per-type `GetResource` for detail.
  Pull-only; works in any account with a read role; no Config prerequisite.
- **`config`** (Pattern 2) — AWS Config aggregator as source-of-truth: dynamic
  coverage **plus** native history (`GetResourceConfigHistory`), native graph
  **`relationships`**, and a **push** change stream. Used where Config is
  enabled (org-scale).

### 2. Shared bitemporal mapping (mechanism-independent)

Whatever the mechanism, a resource maps to the same shapes:

- **Resource → Entity / Document.** `resourceType`, `resourceId`/ARN,
  `configuration` JSON, `tags`, account, region. `valid_from` =
  `configurationItemCaptureTime` (config) or observation time (cloudcontrol/
  describe).
- **Relationships → graph edges.** Config `relationships` (instance→SG→VPC,
  role→policy) map directly to edges feeding `get_related_entities` /
  **`blast_radius`**. Under `cloudcontrol`/`describe` we derive edges from
  resource fields (a thinner graph).
- **Deletion → tombstone.** Config `ResourceDeleted` (or a reconciliation diff
  under pull modes) tombstones the resource; ADR-0009 janitor purges after
  retention. Bitemporal history is preserved.
- **Activity → linked records.** CloudTrail **management** events become
  activity records linked to the resource entity (for incident / postmortem
  correlation: "who changed this resource before the incident").

Emitter dedup (ADR-0010): when a resource is visible via two mechanisms, **Config
wins** (richest: history + native relationships); `cloudcontrol`/`describe` are
fallback/coverage-fill. This is expressed as an emitter precedence so the same
resource is not double-written.

### 3. Access model — org delegated-admin + `AssumeRole`

- A **delegated-admin/hub** account hosts the Config **aggregator** (org-wide
  read from one place) and the EventBridge bus.
- Omniscience assumes a **read-only role** per member account via STS for detail
  fetches and CloudTrail; **no long-lived keys**. The existing env-based
  `SecretsResolver` carries the role ARN / external id rather than static
  credentials.
- New accounts are auto-onboarded via Organizations events (account-joined) →
  register a source / extend aggregator scope.

### 4. Push ↔ pull bridge (for `config` mode)

EventBridge cannot reach into the service directly, so a single new piece of AWS
infra bridges push to our pull-based ingest:

```
Config change / CloudTrail ─▶ EventBridge rule ─▶ SQS (omniscience-aws-ingress)
                                                        │ long-poll
                                Omniscience aws-ingress poller ─▶ NATS ingest.changes.aws
                                                        │
                                         existing discovery-sync pipeline (PR #287)
```

Reconciliation (pull) runs on a schedule via Config advanced query (or a
`cloudcontrol` re-scan) to correct drift and catch missed events. Steady-state
cost is proportional to **change volume**, not resource count.

### 5. Grounded parameters (verified against `qbiq-ai/infra`)

Inspection of the `infra` repo's `shared` env and org config (`modules/config-org`,
`_envcommon/config.hcl`, `management/eu-west-1/organizations`) fixes the
otherwise-open parameters on fact rather than assumption:

- **Start mode = `config`, no enablement needed.** AWS Config is already running
  org-wide: `recording_group{ all_supported = true, include_global_resource_types
  = true }` + `aws_config_configuration_aggregator "org"` with
  `organization_aggregation_source{ all_regions = true }`. All types, all
  regions, all accounts are already recorded.
- **Aggregator hub = Log Archive account** (delegated admin, infra #158). Omniscience
  reads the org aggregator from Log Archive via a **read-only role** —
  `config:SelectAggregateResourceConfig` / `BatchGetAggregateResourceConfig` /
  `GetAggregateResourceConfigHistory`. The aggregator already spans every member
  account (qbiq-prod, qbiq-dev, analytics, shared, …), so per-account `AssumeRole`
  is **not** required for the baseline graph.
- **Cold archive already exists.** Config delivers configuration snapshots/history
  to **S3** (delivery channel, prefix `config`, in Log Archive). This is reused as
  the cold tier (below) instead of duplicating old history in our hot stores.
- **CloudTrail and EventBridge are already deployed** → Phases 4–5 (push,
  activity) have their AWS-side primitives in place; the work is wiring, not
  enablement.
- **No RDS** in the estate → excluded from the starting scope.

#### Decided execution parameters

- **Reconciliation cadence:** full nightly + 6h incremental reconcile (pull).
  After push (Phase 4) lands, reconciliation drops to nightly drift-check only.
- **Starting scope (Tier-1):** `AWS::EC2::VPC|Subnet|SecurityGroup|RouteTable|
  NatGateway|InternetGateway|EIP`, `AWS::EKS::Cluster|Nodegroup|Addon`,
  `AWS::ElasticLoadBalancingV2::LoadBalancer`, `AWS::IAM::Role|Policy|User|Group|
  OIDCProvider`, `AWS::S3::Bucket`, `AWS::KMS::Key`, `AWS::ECR::Repository`,
  `AWS::SecretsManager::Secret`, `AWS::Lambda::Function`, `AWS::Route53::HostedZone`.
  Tier-2 (TransitGateway, NetworkFirewall, DynamoDB, Backup, GuardDuty, Config
  rules, CloudWatch, IdentityStore, AccessAnalyzer, Budgets/CE) follows on demand.
- **Retention / tiering** (extends ADR-0009): **hot 90d** full bitemporal versions
  in Neo4j/Qdrant/PG → **warm** summarised (changed fields + S3 snapshot pointer)
  → **cold** = a *pointer* (S3 path + `captureTime`) into the existing Config S3
  archive, full object re-hydrated on demand via **Athena** (slower deep search is
  acceptable per operator). Our raw rows purge ~1y; S3 archive lives by its own
  lifecycle (Glacier). Security-relevant types (IAM/SG/KMS) kept hot longer.
- **Push:** deferred — **pull-only first** (Phases 1–3); SQS/EventBridge bridge is
  Phase 4.

## Consequences

- **Dynamic coverage without per-service code** via `cloudcontrol` — the
  explicit ask is satisfied without depending on Config.
- **History + push freshness + native graph** via `config` where enabled — the
  bitemporal/blast-radius value is fully realised.
- **One pipeline, one entity/edge/tombstone contract** — mechanisms are
  swappable per source/account; no connector fan-out.
- **Cost is now a first-class operational input**, not hidden: AWS Config
  (per-item + aggregator), CloudTrail data events (expensive — default to
  management events only), and growing bitemporal history (extend ADR-0009
  retention to the aws source).
- **New AWS-side infra for `config` mode**: an SQS ingress queue + IAM roles +
  (optionally) the Config aggregator and EventBridge rules. `cloudcontrol`/
  `describe` modes need none of this.
- **Broader IAM** for `cloudcontrol` (≈ `ReadOnlyAccess`) traded for a single
  code path; reviewed and scoped read-only.
- The legacy `describe` connector keeps working unchanged ("works anywhere,
  day 1") and becomes a deliberate fallback rather than the only option.

## Phase plan

1. **Baseline graph (pull, no new infra).** `acquisition=config` against the
   Config **aggregator** advanced query → map configuration items to bitemporal
   entities + native `relationships` edges. Immediately delivers the resource
   graph and `blast_radius` over real AWS state. (Where Config is absent, the
   same phase runs `acquisition=cloudcontrol`.)
2. **History backfill.** `GetResourceConfigHistory` (config) per resource to
   populate the bitemporal past; under `cloudcontrol`, history accrues forward
   from first observation only.
3. **Dynamic generic poll.** `acquisition=cloudcontrol` productionised:
   enumerate CFN registry types, `ListResources`/`GetResource`, derive edges —
   covers accounts/types outside Config and the "new services automatically"
   requirement.
4. **Push freshness.** EventBridge → SQS → `aws-ingress` poller → NATS
   `ingest.changes.aws`; `ResourceDeleted` → tombstone. Near-real-time
   increments; scheduled reconciliation as the drift safety-net.
5. **Activity.** CloudTrail **management** events → activity records linked to
   resource entities for incident/postmortem correlation. (Data events opt-in
   per critical resource only, for cost.)
6. **Org onboarding.** Auto-register new accounts via Organizations events;
   extend aggregator scope automatically.

## Alternatives rejected

- **Loop over all boto3 services generically.** Rejected: heterogeneous
  operations/shapes/pagination; not uniformly parseable. Cloud Control API is
  the correct uniform substitute.
- **Per-service `describe` as the primary, hand-code each new service.**
  Rejected as the *primary* path: does not scale to 200+ services or "everything"
  coverage. Retained only as a fallback/high-fidelity mode.
- **Config-only (drop pull entirely).** Rejected: Config must be enabled and
  costs money; some accounts/types are not covered; pull (`cloudcontrol`) is the
  universal floor that works without prerequisites.
- **Cloud-Control-only (skip Config).** Rejected: loses native history,
  native `relationships`, and push freshness — the exact bitemporal/blast-radius
  value Omniscience exists to provide.
- **EventBridge API destination (webhook) straight into the REST API instead of
  SQS.** Rejected for the first cut: SQS gives durable buffering, retry, and
  back-pressure, and reuses the existing NATS/discovery-sync ingestion path; a
  webhook couples AWS delivery to service availability.

## Revisit triggers

- AWS Config coverage or pricing changes materially (e.g. Config records the
  types we care about for free / or aggregator cost spikes).
- Cloud Control API `LIST` coverage closes its current gaps (parent-identifier
  requirement), making `cloudcontrol` viable as the sole mechanism.
- We need sub-minute freshness on resources Config does not stream (push gaps).
- Bitemporal AWS history volume forces a dedicated retention tier beyond
  ADR-0009.

## Links

- Current AWS connector: `packages/connectors/src/omniscience_connectors/aws/connector.py` (PR #285 — Organizations enumeration).
- Ingestion fan-out the push bridge reuses: PR #287 (discovery-sync worker), `ingest.changes.<type>` on NATS.
- Bitemporal contract: [ADR-0008](0008-bitemporal-schema-for-neo4j.md).
- Retention/tombstone: [ADR-0009](0009-retention-tiering-policy.md).
- Emitter precedence/dedup: [ADR-0010](0010-server-side-emitter-dedup.md).
- AWS Cloud Control API — uniform `ListResources`/`GetResource` over the CloudFormation registry.
- AWS Config aggregator — `SelectAggregateResourceConfig`, `GetResourceConfigHistory`, configuration-item change stream.
