# Connector SDK

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

## Writing a new connector

1. Create package: `packages/connectors/omniscience_connectors/<name>/`
2. Implement `Connector` protocol
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
