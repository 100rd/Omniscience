# Omniscience — Project Description

> Stop tracing, start resolving.

## 1. What Omniscience is

Omniscience is a self-hosted **Living Semantic Core** for platform and SRE teams. It turns fragmented operational data — cloud infrastructure, IaC code, Kubernetes runtime, CI/CD, alerts, incident chat, docs — into a single **causal, temporal, semantic graph** exposed through **Model Context Protocol (MCP)**.

Any MCP-compatible client (Claude Code, Cursor, internal agents) reads the organization's infrastructure as connected context rather than a set of disconnected tables, dashboards, and search boxes.

## 2. Problem

When a GPU cluster degrades at 03:00, an on-call SRE opens seven tabs: CloudWatch, Terraform state, GitHub, Slack, Datadog, PagerDuty, internal wiki. ~80% of resolution time is spent correlating across them, not fixing anything.

- Git knows what *should* be.
- Terraform knows what was *ordered*.
- Kubernetes knows what is *running*.
- Slack knows *why* something broke last time.
- No single system says: *these are the same entity*.

AI agents layered on top of fragmented data generate plausible but ungrounded hypotheses. Context assembly, not root-cause, is the real bottleneck.

## 3. Target user

- **Platform / SRE teams** operating infrastructure at 500+ nodes, frequent change, high cost of downtime (typically GPU/inference, fintech trading, data platforms).
- **Engineering leaders** measuring MTTR and alert fatigue.
- **Security / compliance functions** that need cross-source lineage (code → deployed resource → who changed it when).

Explicit non-users (v1): individual developers looking for a personal codebase search; teams whose primary need is documentation search.

## 4. Product modes

Omniscience ships as a **read-only knowledge layer**. Write operations against infrastructure are explicitly out of scope for this product.

| Mode | Scope | Status |
|---|---|---|
| **Insight Mode** | Indexing, linking, query, recommendation generation with confidence scores. No side effects on customer infrastructure. | v1 — this product |
| **Action Mode** | Agentic execution of recommended changes (rollback, scaling, config apply) gated by policy and approval. | v2 — separate product built on top of Omniscience; **not** bundled with v1 |

This separation is deliberate. The security, policy, and auditability surface of a write-capable system is large enough to warrant a distinct product boundary.

## 5. Architecture

### 5.1 Hybrid knowledge graph

Two specialized stores behind one query API:

| Store | Role | Technology |
|---|---|---|
| **Graph** | Topology, ownership, dependencies, causal edges, temporal state | **Neo4j** |
| **Vector** | Semantic content — docs, Slack threads, post-mortems, log narratives, commit messages | **Qdrant** |

The **GraphRAG composition** is the product's core query pattern:
1. Resolve the subject entity in the graph (*"pod gpu-worker-7a3f"*).
2. Traverse causal/ownership edges to candidate causes (*"deployed by MR #502"*).
3. Use the candidate set to scope a vector search (*"MR #502 discussion in Slack"*).
4. Return a ranked bundle of linked artifacts with provenance and confidence scores.

This is substantively different from naive RAG (vector-only) and from CMDB-style structured-only query. It is the reason a single-store pgvector architecture does not extend to the product we are building.

### 5.2 Cross-domain identity

The graph is only useful if the same real-world entity carries the same node identity across sources. Identity resolution combines:

1. **Declarative metadata** — labels, annotations, tags, IaC addresses, ARN.
2. **OpenTelemetry trace context** — request/resource correlation for runtime entities.
3. **ML clustering** (v1.1) — fallback for unlabeled or legacy resources.

Every cross-source link carries a **confidence score** with explicit strategy attribution (`exact_address`, `arn_match`, `otel_trace`, `label_match`, `ml_cluster`). Scores are visible to downstream consumers so agents can weigh evidence.

### 5.3 Temporal graph

State that existed yesterday is different from state that exists now. Incident reasoning frequently requires both.

- **Bitemporal model** per entity and edge: `valid_from`, `valid_to` (real-world time) plus `recorded_at` (ingestion time). See [ADR-0008](decisions/0008-bitemporal-schema-for-neo4j.md) for the canonical schema and the open-closed `[valid_from, valid_to)` predicate convention.
- **Retention**: 90 days hot (queryable at full fidelity), 1 year warm (queryable at snapshot granularity), archive beyond. See [ADR-0009](decisions/0009-retention-tiering-policy.md) for the tier shapes, eviction triggers, and worker design.
- **Delta layers** (v2, for Action Mode): a proposed change materializes as a graph overlay that policy engines validate before any real action.

### 5.4 Collection layer — passive observers

Sources feed the graph through connectors that prefer **passive observation** over polling where the source supports it:

| Source class | Mechanism | Status |
|---|---|---|
| Kubernetes runtime | Native **K8s Operator** (controller-runtime) watching API server | Planned — separate track |
| Cloud resources | Per-provider connectors (AWS first) | In pilot |
| IaC code | Git connectors + per-repo webhooks | Implemented |
| IaC state | Object-storage connectors (S3) with event notifications + ETag fallback | In pilot |
| Incident chat | Slack connector | Implemented, integration in pilot |
| CI / PRs | GitHub API connector (PR/MR as first-class entities) | Planned |
| Alerts / observability | PagerDuty, Datadog, OpenTelemetry receivers | Planned |

Reconciliation loops detect drift — resources that exist in live infrastructure but not in declared state, or vice versa.

### 5.5 Protocol layer — MCP-first

The product's contract to the outside world is a set of MCP tools. REST, CLI and Admin UI exist but are conveniences.

Minimum tool surface for the Insight Mode:

- `search(query, filters)` — hybrid retrieval across graph + vector.
- `get_entity(id)` / `get_document(id)` — direct fetch with citations.
- `get_related_entities(id, edge_types?, depth?)` — graph traversal.
- `resolve_incident(alert_id)` — high-level composition that produces a recommendation bundle.
- `list_sources` / `source_stats` — operational introspection.

## 6. Deployment and security

- **Self-hosted** on Kubernetes (Helm) or **private SaaS** on dedicated customer infrastructure. Data does not leave the customer perimeter.
- **Local embedding models** are the default; no outbound calls required for the ingest path.
- **Integration with existing policy** — OPA/Rego, not a replacement. Omniscience emits structured evidence that policy engines consume.
- **Auditability** — every query and every recommendation is traceable back to the underlying graph nodes, documents, and timestamps.
- **Compliance roadmap** — SOC 2 Type II targeted for Q3 2026.

## 7. What Omniscience is not

- **Not a chatbot.** No embedded LLM. No opinionated synthesis. Consumers receive structured evidence; the calling agent crafts the answer.
- **Not a CMDB.** CMDBs are manually curated and static. Omniscience is observed, versioned, and causal.
- **Not a dashboard product.** The product is the graph and the MCP contract. UI is operational, not the offering.
- **Not an action/orchestration system in v1.** See §4.
- **Not a replacement for general-purpose retrieval tools** like Glean or Sourcegraph Cody for broad corporate search. Focus is operational infrastructure.

## 8. Competitive positioning

| Solution | What it does | Why insufficient for this problem |
|---|---|---|
| ServiceNow CMDB | Configuration item catalog | Manual, static, no runtime, no semantics, no MCP |
| Datadog Service Catalog | Service topology from runtime | Runtime-only, no code lineage, no historical reasoning |
| Backstage | Developer portal | Manually populated, no temporal graph, no causal reasoning |
| Port / Cortex | Internal engineering platform | Strong catalog; do not build a causal graph spanning code → state → runtime |
| Neo4j / Graph DB alone | Graph storage primitive | Storage layer, not a product. No connectors, no identity resolution, no MCP |
| Vector DB + naive RAG | Semantic search over unstructured text | No structural reasoning, no temporal dimension, no cross-domain identity |

Omniscience's claim is the combination: **causal + temporal + semantic + MCP-native**, with the connectors and identity resolution that make the graph real.

## 9. Economics — working hypothesis

All metrics in this section are **hypotheses**, not measured outcomes. They will be validated through design-partner engagements and replaced with measured values once data is available.

| Metric | Hypothesis (without / with Omniscience) |
|---|---|
| MTTR on targeted incident classes | 3–4 hours / 10–20 minutes |
| Engineer-hours per incident | ~6 / ~0.5 |
| Alert fatigue (false correlations) | High / reduced via confidence score filtering |
| Hourly cost of a GPU-cluster incident | $50K–$500K / substantially lower via faster resolution |

Pricing model under consideration: per-node-per-month, flat tier for <500 nodes, enterprise tier above.

## 10. Roadmap

The following is the intended direction, subject to design-partner feedback.

**Q2 2026 — Pilot foundation**
- Cross-source graph over GitHub IaC + Terraform state (S3) + live AWS (pilot epic: [#84](https://github.com/100rd/Omniscience/issues/84)).
- Unblock ingestion plumbing (sync → NATS, secrets resolution).
- MCP tool for graph traversal.

**Q3 2026 — Architecture migration and demo-grade scope**
- Migrate graph + vector stores to **Neo4j + Qdrant** (separate epic).
- **Temporal graph** with bitemporal semantics and retention tiers.
- Incident demo source pack: Slack, GitHub PRs, alerts (Datadog/PagerDuty), OpenTelemetry.
- Public MCP specification, beta access for platform teams.

**Q4 2026 — Native runtime observer and GA**
- Native Kubernetes operator (controller-runtime) replacing agentic K8s discovery.
- GA release with OPA/Rego integration and FinOps connectors.
- SOC 2 Type II completion target.

**Post-v1**
- Action Mode (v2) as a separate product.
- ML-based identity clustering.
- Additional cloud providers beyond AWS.

## 11. Open questions and risks

- **Graph store migration cost.** Moving from pgvector to Neo4j + Qdrant is a multi-month refactor of the writer, retrieval layer, and tests. This is a deliberate choice, but it gates Q3 milestones.
- **Temporal model complexity.** Bitemporal semantics are powerful but expensive to query correctly. Initial implementation may restrict time-travel queries to a subset of entity types.
- **Identity resolution accuracy.** Confidence scores below a trust threshold produce wrong answers just as readily as bad data. Threshold tuning is an empirical, design-partner-driven task.
- **Operator vs connector boundary.** A native K8s operator is a separate software artifact (Go, controller-runtime) with its own release cadence. Coordinating schema compatibility across two repositories is a known ongoing cost.
- **Action Mode product boundary.** v2 is a new product, but customers will expect a migration path from Insight Mode. Contract/data-model stability of v1 becomes a customer commitment early.

## 12. Reference documents

- [Architecture details](architecture.md)
- [Schema](schema.md)
- [Freshness and lineage](freshness-and-lineage.md)
- [Roadmap](roadmap.md)
- [ADRs](decisions/)
- [MCP API](api/mcp.md)
- [REST API](api/rest.md)
