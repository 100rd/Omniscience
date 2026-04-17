# Connecting Omniscience to Claude Code

Give Claude Code grounded retrieval across your indexed code, docs, and infrastructure configs. Once connected, Claude Code can call `search`, `get_document`, and `list_sources` during any conversation.

## Prerequisites

- Docker and Docker Compose v2 installed
- Claude Code installed (`npm install -g @anthropic-ai/claude-code` or the standalone installer)
- Outbound network access from Claude Code to your Omniscience host (localhost for local setups)

## Step 1 — Deploy Omniscience

Create your environment file:

```bash
cat > .env << 'EOF'
POSTGRES_PASSWORD=change-me-strong-password
OMNISCIENCE_SECRET_KEY=change-me-32-char-secret-key-here
EOF
```

Start the stack:

```bash
docker compose up -d
```

Wait for all services to become healthy:

```bash
docker compose ps
# Expected: app, postgres, nats all show "healthy"
```

Verify the API is reachable:

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"0.1.0"}
```

If `app` is not healthy after 30 seconds, check logs: `docker compose logs app`.

## Step 2 — Create an API token

```bash
docker compose exec app omniscience tokens create \
  --name claude \
  --scopes search,sources:read
```

Expected output:

```
Created token: omni_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Scopes: search, sources:read
Name: claude
```

Copy the token. It is shown only once.

## Step 3 — Configure MCP in Claude Code

Claude Code reads MCP server config from `.claude/settings.json` in your project directory, or from `~/.claude/settings.json` for a user-wide config.

**Option A — stdio transport (local Omniscience, recommended for local dev)**

```json
{
  "mcpServers": {
    "omniscience": {
      "command": "omniscience",
      "args": ["mcp", "serve", "--transport", "stdio"],
      "env": {
        "OMNISCIENCE_URL": "http://localhost:8000",
        "OMNISCIENCE_TOKEN": "omni_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

**Option B — streamable-http transport (hosted/shared Omniscience)**

```json
{
  "mcpServers": {
    "omniscience": {
      "transport": "streamable-http",
      "url": "https://your-omniscience-host/mcp",
      "headers": {
        "Authorization": "Bearer omni_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

Replace `omni_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` with the token from Step 2.

## Step 4 — Verify the connection

Start Claude Code in your project:

```bash
claude
```

List connected MCP servers:

```
/mcp
```

You should see:

```
Connected MCP servers:
  omniscience — tools: search, get_document, list_sources, source_stats
```

Test retrieval:

```
How is authentication implemented in this service?
```

Claude Code will call `omniscience.search` with your question and return grounded results with citations. You should see it invoke the tool and show source URIs in the response.

List available sources:

```
What sources does Omniscience have indexed?
```

Claude Code will call `omniscience.list_sources()` and show source names, types, and freshness.

## Environment variables reference

| Variable | Required | Description |
|---|---|---|
| `OMNISCIENCE_URL` | Yes | Base URL of your Omniscience instance |
| `OMNISCIENCE_TOKEN` | Yes | API token with at minimum `search` scope |

## Scope recommendations

For day-to-day use: `search` + `sources:read`.

- `search` — allows calling the `search` tool
- `sources:read` — allows `list_sources` and `source_stats`
- `sources:write` — not needed for Claude Code
- `admin` — never give this to a client token

## Troubleshooting

### Connection refused

```
Error: connect ECONNREFUSED 127.0.0.1:8000
```

Omniscience is not running or not healthy.

```bash
docker compose ps                # check status
docker compose logs app          # check startup errors
curl http://localhost:8000/health  # confirm reachability
```

If using a remote host, confirm the URL in `.claude/settings.json` matches your deployment.

### Token invalid or unauthorized

```
Error: unauthorized — Token missing or invalid
```

- Confirm the token value in settings.json matches what was printed by `tokens create`
- Tokens are shown only once. If lost, create a new one and revoke the old one:

```bash
docker compose exec app omniscience tokens list
docker compose exec app omniscience tokens revoke <token-id>
docker compose exec app omniscience tokens create --name claude --scopes search,sources:read
```

### MCP server not appearing in `/mcp`

- Confirm `.claude/settings.json` is valid JSON (no trailing commas, no comments)
- Restart Claude Code after editing the config
- For stdio transport, confirm the `omniscience` CLI binary is on `$PATH`:

```bash
which omniscience
omniscience --version
```

If not installed: `pip install omniscience-cli` or see [releases](https://github.com/omniscience/omniscience/releases).

### No search results

- Confirm at least one source is configured and has been synced:

```bash
curl -H "Authorization: Bearer omni_dev_..." http://localhost:8000/api/v1/sources
```

- Check if sources are stale:

```bash
curl -H "Authorization: Bearer omni_dev_..." http://localhost:8000/api/v1/sources | jq '.[] | {name, is_stale, last_sync_at}'
```

- Trigger a manual sync if needed:

```bash
curl -X POST -H "Authorization: Bearer omni_dev_..." \
  http://localhost:8000/api/v1/sources/<source-id>/sync
```

### Rate limited

Default limit is 60 requests per minute per token. If you hit this during heavy use, create a token with a higher limit via the admin API or contact your Omniscience admin.

## See also

- [MCP API reference](../api/mcp.md) — full tool contracts
- [REST API](../api/rest.md) — health, token, and source management endpoints
- [Cursor integration](cursor.md) — same MCP setup for Cursor IDE
