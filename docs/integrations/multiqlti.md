# Connecting Omniscience to multiqlti

**Status**: design doc. Implementation lands in [M3](../roadmap.md), requires multiqlti [External Connections](https://github.com/100rd/multiqlti/issues/277).

## Overview

[multiqlti](https://github.com/100rd/multiqlti) is an AI pipeline platform. Pipeline stages can consume Omniscience as an **External Connection** (MCP source), giving agents grounded retrieval during Planning, Architecture, Development, Code Review, and Research stages.

## Setup

1. Run Omniscience and create a token. Scopes: `search` + `sources:read`.

2. In multiqlti, go to your workspace → **Connections** → **Add → Generic MCP**:

   ```
   Name:      omniscience
   Transport: streamable-http
   URL:       https://your-omniscience-host/mcp
   Auth:      Bearer sk_...
   ```

3. Test the connection. multiqlti will list the tools exposed by Omniscience.

4. In your pipeline, grant this connection to the stages that need retrieval:

   - **Research stage** — search docs/code for context
   - **Architecture stage** — lookup prior decisions and patterns
   - **Code Review stage** — pull related code for context
   - **Development stage** — find examples in the existing codebase

## Example pipeline snippet

```yaml
stages:
  - id: research
    agent: researcher
    allowed_connections: [omniscience, gitlab]
    prompt: |
      Research {task}. Use omniscience.search() to find prior decisions,
      existing code, and documentation. Cite every claim.

  - id: architecture
    agent: architect
    allowed_connections: [omniscience]
    depends_on: research
    prompt: |
      Propose an architecture for {task}. Query omniscience for existing
      patterns in our codebase before introducing new ones.
```

## Benefits

- Omniscience indexes many sources (code + docs + tickets) — one connection, broad retrieval
- Single source of truth for organization knowledge across multiple pipelines
- Freshness SLOs enforced at source level; stages get `indexed_at` per chunk
- Citations flow into pipeline artifacts automatically

See [MCP API](../api/mcp.md) for tool contracts.
