# Connecting Omniscience to multiqlti

[multiqlti](https://github.com/100rd/multiqlti) is an AI pipeline platform. Pipeline stages can consume Omniscience as an **External Connection** (MCP source), giving every agent grounded retrieval during Planning, Architecture, Development, Code Review, and Research stages.

## Prerequisites

- Running Omniscience instance reachable by the multiqlti host
- Omniscience API token with `search` + `sources:read` scopes
- multiqlti workspace with External Connections enabled (requires multiqlti v0.9+ with [External Connections](https://github.com/100rd/multiqlti/issues/277))

## Step 1 — Deploy Omniscience (if not already running)

```bash
cat > .env << 'EOF'
POSTGRES_PASSWORD=change-me-strong-password
OMNISCIENCE_SECRET_KEY=change-me-32-char-secret-key-here
EOF

docker compose up -d
```

Confirm health:

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"0.1.0"}
```

## Step 2 — Create an API token

```bash
docker compose exec app omniscience tokens create \
  --name multiqlti \
  --scopes search,sources:read
```

Copy the token (`omni_dev_...`).

## Step 3 — Add Omniscience as an External Connection in multiqlti

In your multiqlti workspace:

1. Go to **Settings → Connections → Add Connection**
2. Select **Generic MCP** as the connection type
3. Fill in the form:

```
Name:        omniscience
Transport:   streamable-http
URL:         https://your-omniscience-host/mcp
Auth type:   Bearer token
Token:       omni_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

4. Click **Test Connection**. multiqlti will connect to Omniscience and list its tools. You should see:

```
Connected. Available tools: search, get_document, list_sources, source_stats
```

5. Click **Save**.

For a locally running Omniscience (dev/testing), use the machine's LAN IP or hostname if multiqlti runs in Docker:

```
URL: http://host.docker.internal:8000/mcp   # macOS/Windows Docker Desktop
URL: http://172.17.0.1:8000/mcp             # Linux Docker bridge
```

## Step 4 — Grant the connection to pipeline stages

In your pipeline definition, add `omniscience` to `allowed_connections` for each stage that needs retrieval:

```yaml
pipeline:
  name: feature-development
  stages:
    - id: research
      agent: researcher
      allowed_connections: [omniscience, gitlab]
      prompt: |
        Research {task}. Use omniscience.search() to find prior decisions,
        existing code, and related documentation. Cite every claim with
        chunk_id and URI.

    - id: architecture
      agent: architect
      allowed_connections: [omniscience]
      depends_on: [research]
      prompt: |
        Propose an architecture for {task}. Before introducing any new
        pattern, query omniscience to check whether an equivalent already
        exists in the codebase. Reference specific files and line ranges.

    - id: development
      agent: developer
      allowed_connections: [omniscience, gitlab]
      depends_on: [architecture]
      prompt: |
        Implement {task} based on the approved architecture. Use
        omniscience.search() to find existing utilities, helpers, and
        patterns to reuse. Do not reinvent what already exists.

    - id: code_review
      agent: reviewer
      allowed_connections: [omniscience]
      depends_on: [development]
      prompt: |
        Review the implementation for {task}. Use omniscience.search() to
        find the team's existing conventions for authentication, error
        handling, and testing. Flag deviations with specific citations.
```

## Step 5 — Test the pipeline

Trigger a pipeline run via the multiqlti UI or CLI:

```bash
multiqlti run feature-development --input '{"task": "add rate limiting to the payments API"}'
```

Watch the Research stage log. You should see tool calls like:

```
[research] Calling omniscience.search(query="rate limiting payments API", top_k=10)
[research] Got 8 results from sources: main-gitlab, company-wiki
[research] Calling omniscience.get_document(document_id="...")
```

The agent's output will include citation blocks with `chunk_id` and `uri` fields sourced from Omniscience.

## Example pipeline with full Omniscience usage

```yaml
pipeline:
  name: omniscience-full-example

  stages:
    - id: scope
      agent: planner
      allowed_connections: [omniscience]
      prompt: |
        Analyze {task}. Use omniscience.list_sources() to understand what
        data is available. Then use omniscience.search() to scope the work:
        find related features, prior attempts, and relevant documentation.

    - id: research
      agent: researcher
      allowed_connections: [omniscience]
      depends_on: [scope]
      prompt: |
        Deep research for {task}. Run at least 5 targeted searches with
        different query angles. Use omniscience.get_document() to expand
        the most relevant chunks. Compile all citations.

    - id: design
      agent: architect
      allowed_connections: [omniscience]
      depends_on: [research]
      prompt: |
        Design {task}. For each design decision, query omniscience for
        existing patterns. Prefer extending what exists over adding new
        abstractions. Output: design doc with inline citations.

    - id: implement
      agent: developer
      allowed_connections: [omniscience, gitlab]
      depends_on: [design]
      prompt: |
        Implement {task}. Use omniscience.search() to find helpers and
        utilities before writing new code. Commit with references to
        cited chunk_ids in the PR description.
```

## Benefits

- **One connection, broad retrieval**: Omniscience indexes code, docs, tickets, and wiki simultaneously. A single `search` call spans all of them.
- **Freshness SLOs**: Each chunk includes `indexed_at` and `is_stale` metadata. Pipeline agents can check this and warn when citing stale sources.
- **Citations propagate**: `chunk_id` and `uri` fields flow through pipeline artifacts automatically, giving reviewers traceable provenance.
- **Source filtering**: Agents can restrict retrieval to specific sources with `sources=["main-gitlab"]` or source types with `types=["git"]`.

## Token scope recommendations

For multiqlti pipeline agents, use `search` + `sources:read`. Never pass `admin` or `sources:write` to pipeline tokens. Create a dedicated token per environment:

```bash
docker compose exec app omniscience tokens create --name multiqlti-prod --scopes search,sources:read
docker compose exec app omniscience tokens create --name multiqlti-dev  --scopes search,sources:read
```

## Troubleshooting

### Connection test fails

- Confirm the Omniscience URL is reachable from the multiqlti host (not just from your local machine)
- For Docker-based multiqlti, use `host.docker.internal` or the Docker bridge IP as the hostname
- Confirm TLS if using `https://`: the certificate must be valid (or configure multiqlti to accept self-signed certs for dev)

### Agent produces no citations

- The agent may be ignoring tool results. Add explicit instructions to the stage prompt: "You MUST call omniscience.search() and cite results using chunk_id and uri before producing output."
- Confirm `allowed_connections` includes `omniscience` for that stage

### Rate limiting

Default 60 rpm per token. For high-throughput pipelines with many parallel stages, create a token with a higher rate limit or use separate tokens per stage.

## See also

- [MCP API reference](../api/mcp.md) — full tool contracts
- [REST API](../api/rest.md) — token and source management
- [Python client guide](python-client.md) — direct SDK usage for custom pipeline steps
