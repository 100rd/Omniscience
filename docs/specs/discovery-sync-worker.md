# Spec: Discovery-based sync in the ingestion worker (proper live connectors)

## Problem

Discovery-style connectors (K8s agentic, GitHub) cannot run as **live, NATS-driven
sources** today. `POST /api/v1/sources/{id}/sync` publishes a single
`DocumentChangeEvent(external_id="*", uri="sync://{id}")`, and the worker treats it
as one document. Two structural gaps:

1. **`Source.config` never reaches the connector.** `IngestionWorker.process_document`
   (`apps/server/src/omniscience_server/ingestion/worker.py:257`) calls
   `pipeline.run(..., config=None, ...)`. The connector's public config
   (api_server, namespace, CA refs, `use_llm_kind_selection`, include/exclude kinds)
   is dropped.
2. **`connector.discover()` is never called in the live path.** Enumeration of
   cluster resources lives in `K8sAgenticConnector.discover()`
   (`packages/connectors/.../agentic/k8s.py:546`), which yields `DocumentRef`s that
   carry `metadata["kind"]`. The worker only does single-document fetch, and even
   then `IngestionPipeline._stage_fetch` (`.../ingestion/pipeline.py:367`) rebuilds
   `DocumentRef(external_id, uri)` **without metadata**, so `fetch` (which reads
   `ref.metadata["kind"]`) gets nothing. `discovery_worker.py` is unrelated — it
   auto-provisions Source *rows* from GitHub, not document fan-out.

Net: creating a K8s Source and calling `/sync` produces a `"*"` event that fails at
fetch. The current working mechanism is an out-of-band scheduled seed
(`scripts/refresh_k8s_state.sh` + launchd, every 30 min). This spec replaces it with
a first-class live path.

## Goal

A sync event for a discovery-capable source must:
1. Load the `Source` row, validate `Source.config` via
   `connector.config_schema.model_validate(source.config)`.
2. Resolve secrets from `source.secrets_ref` via `SecretsResolver`.
3. Call `connector.discover(config, secrets)` → async-iterate `DocumentRef`s.
4. For **each** ref, run fetch→hash→parse→chunk→embed→index with the **validated
   config** and the **ref's own metadata** (so `fetch` sees `kind`).
5. Track per-ref results in the existing `IngestionRun` (counts, errors → DLQ/NAK
   semantics preserved).

Non-sync events (a concrete `external_id`) must also get the validated `Source.config`
(fix gap #1) — but keep current single-document behaviour otherwise. Connectors that
don't override `discover` (single-doc connectors) keep working: detect the sync marker
(`external_id == "*"` / `uri.startswith("sync://")`) to branch.

## Design constraints

- **ACL invariant preserved**: `workspace_id` is still resolved server-side from
  `Source.tenant_id` (`resolve_source_workspace`), never from event payload.
- **Config validation is fail-closed**: invalid `Source.config` → `action="error"`,
  structured log, NAK/DLQ — never silently fall back to `config=None`.
- **Secrets never logged.** CA/token only flow through `SecretsResolver` → connector.
- **Dedup gate** still runs per produced document.
- **Pipeline reuse**: prefer threading discovered `DocumentRef`s through the existing
  pipeline stages. `_stage_fetch` must use the discovered ref **with metadata**, not a
  reconstructed bare ref. Add a ref-driven entrypoint (e.g. `pipeline.run_ref(ref, config,
  secrets, workspace_id, run_id)`) rather than forcing everything through
  `DocumentChangeEvent`.
- Bounded concurrency for the per-ref fan-out (don't blast the cluster API / embedder).

## Tests (QA, ≥80% on touched modules)

- Worker: sync event → discover yields N refs → N documents indexed; config validated;
  invalid config → error result, no index writes.
- Worker: non-sync event now receives validated config (regression for gap #1).
- Pipeline: ref-driven path passes ref metadata to `connector.fetch`.
- Secrets: `secrets_ref` resolution for the prefix form (`env:PREFIX_` →
  `{token, ca_cert_b64}`) and the `VAR=key` form.
- Single-doc connectors unaffected (no `discover` override) — existing suite green.

## Live wiring (after the worker change lands, on this machine)

Target cluster `qbiq-shared` (read-only SA already exists):
- API server: `https://BA10C107B974586722E4BA7658376E47.sk1.eu-west-1.eks.amazonaws.com`
- SA: `omniscience-reader` / secret `omniscience-reader-token` in ns `omniscience-reader`.

Source row:
- `type = k8s`, `tenant_id = 00000000-0000-0000-0000-000000000001`, `status = active`.
- `config` = `K8sAgenticConfig` fields:
  `{ "api_server": "<above>", "namespace": "", "use_llm_kind_selection": false,
     "default_include_kinds": ["Namespace","Deployment","Service","Ingress",
       "argoproj.io/Application"] }`
- `secrets_ref = "env:OMNI_K8S_QBIQ_"` (prefix form → keys = suffix, case-preserved).

App env (compose, not committed — provide via shell/.env):
- `OMNI_K8S_QBIQ_token`     = `kubectl get secret omniscience-reader-token -n omniscience-reader -o jsonpath='{.data.token}' | base64 -d`
- `OMNI_K8S_QBIQ_ca_cert_b64` = `kubectl get secret omniscience-reader-token -n omniscience-reader -o jsonpath='{.data.ca\.crt}'`  (already base64)

Connector resolves CA via `ca_cert_b64` → `ssl.create_default_context(cadata=pem)`;
`use_llm_kind_selection=false` → deterministic refs from `default_include_kinds`.

Verify: `POST /api/v1/sources/{id}/sync` → NATS `ingest.changes.k8s` → worker discovers
+ indexes; `GET /api/v1/admin/components` shows growth; MCP/search returns live K8s docs.

**Security trade-off (note, not blocker):** prod-cluster SA token now lives in the app
container env (vs `/tmp`-only in the scheduled approach). Acceptable for a read-only SA;
flag for the Security Engineer to sign off and document rotation.
