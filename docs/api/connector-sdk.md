# Connector framework

> **Note on naming.** This document was previously titled "Connector SDK". For v0.1 and v0.2 the connector mechanism is an **internal framework** — part of the Omniscience monorepo, not a published PyPI package. Adding a new connector = PR against Omniscience. See [ADR 0002](../decisions/0002-connector-framework-vs-sdk.md) for the reasoning and revisit triggers.

How to add a new source type to Omniscience.

## Interface

Every connector implements `Connector` from `packages/connectors/omniscience_connectors/base.py`:

```python
class Connector(Protocol):
    type: ClassVar[str]                      # e.g. "git", "confluence"
    config_schema: ClassVar[type[BaseModel]] # Pydantic model for source.config

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Dry-run connectivity + permission check. Raises on failure."""

    async def discover(
        self, config: BaseModel, secrets: dict[str, str]
    ) -> AsyncIterator[DocumentRef]:
        """Yield every document currently present in the source."""

    async def fetch(
        self, config: BaseModel, secrets: dict[str, str], ref: DocumentRef
    ) -> FetchedDocument:
        """Return content + metadata for one document."""

    def webhook_handler(self) -> WebhookHandler | None:
        """If this connector supports push-style updates, return a handler."""
```

Where:

- `DocumentRef` — `{ external_id, uri, updated_at?, metadata? }`
- `FetchedDocument` — `DocumentRef` + `content_bytes`, `content_type`
- `WebhookHandler` — verifies signature, extracts affected `DocumentRef` list from payload

## Lifecycle

```
Create source via API
       │
       ▼
┌──────────────┐
│ .validate()  │  — fail-fast if config/secrets wrong
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Initial sync │  — .discover() yields all refs, pipeline fetches each
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Steady state │
│ push: webhook│  — webhook_handler() triggers partial sync
│ pull: cron   │  — periodic .discover() compares hashes
└──────────────┘
```

## Push vs pull

- **Push-capable** connectors implement `webhook_handler()`. Ingestion latency: seconds.
- **Pull-only** connectors are polled on a schedule (default 15 min, configurable per-source via `freshness_sla_seconds`).
- Some are **hybrid**: webhook for change notifications, periodic full-sync as safety net.

## Parsing & chunking

Connectors return raw `content_bytes` + `content_type`. **Parsing and chunking happen downstream**, in the ingestion pipeline, not in the connector. This keeps connectors small and keeps parser logic reusable across sources (a markdown file is a markdown file whether it came from `fs` or Confluence).

If a source requires source-specific parsing (e.g., Confluence storage format XML), the connector converts to a neutral format (markdown or plain text) before returning.

## Secrets handling

Connectors never see raw secrets in config. They receive:

- `config: BaseModel` — public, validated, persisted
- `secrets: dict[str, str]` — private, resolved at runtime from `secrets_ref` (env/vault)

Logs MUST NOT include secret values. Connector failure messages must redact credentials.

## Built-in connectors

- `git` — local path or remote (GitHub/GitLab) over HTTPS token or SSH. Supports webhook.
- `fs` — local filesystem, watches via `fsnotify` when process stays alive.

Planned:

- `confluence` — Cloud and Server/DC. OAuth + PAT.
- `notion` — Internal integration token.
- `slack` — Bot token; channels configurable.
- `jira` / `linear` — Issue streams.
- `grafana` — Dashboards as documents (metadata-heavy).
- `k8s` — Cluster resources as structured documents.
- `terraform` — State files + tf modules.

## AgenticConnector — LLM-driven discovery

Some sources cannot be fully described declaratively. Examples:

- **Kubernetes** — which resource kinds matter? Deployments and ConfigMaps yes; transient events no; Secrets never. The right subset depends on the cluster.
- **Databases** — index schemas, comments, saved queries. Skip transactional row data. Which tables are reference vs transactional?
- **Selective log ingestion** — ingest error/warn from specific services only; summarize rather than embed raw lines.

For these, we define **`AgenticConnector`** — a connector whose `discover()` phase is **LLM-driven**:

```python
class AgenticConnector(Connector, Protocol):
    """A connector whose discovery phase is LLM-driven.

    Overrides `discover()` to run an agent that inspects the source, decides
    what to include, and yields DocumentRefs. Everything else (fetch, webhook)
    is unchanged.
    """

    agent_config: ClassVar[AgentConfig]
    """Default agent config: instructions, model, max_iterations."""

    async def discover(
        self, config: BaseModel, secrets: dict[str, str]
    ) -> AsyncIterator[DocumentRef]:
        """Runs an agent with MCP tools that expose the source's API.
        Agent yields DocumentRefs as it decides what to index."""
```

The agent under the hood uses **LangGraph** ([ADR 0003](../decisions/0003-agent-framework-langgraph-primary.md)). CrewAI and PydanticAI adapters land in v0.2.

Agentic connectors are **not** the default — regular (declarative) `Connector` is. Use `AgenticConnector` only when declarative discovery cannot express the right scope.

## Writing a new connector

1. Create package: `packages/connectors/omniscience_connectors/<name>/`
2. Implement `Connector` protocol (or `AgenticConnector` if LLM-driven discovery is needed)
3. Register in connector registry (`packages/connectors/omniscience_connectors/__init__.py`)
4. Add contract tests (see `tests/connectors/contract_tests.py`)
5. Add docs: `docs/connectors/<name>.md` — required config, minimum scopes, caveats
6. Submit PR with example source config in `examples/`

## Contract tests

Every connector must pass the shared contract test suite:

- `validate()` succeeds with valid config
- `validate()` raises with invalid config
- `discover()` yields at least one ref against a fixture source
- `fetch()` returns bytes matching a known hash
- `webhook_handler()` (if present) rejects invalid signatures

Run: `pytest tests/connectors/contract_tests.py::TestMyConnector`.
