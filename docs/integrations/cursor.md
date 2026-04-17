# Connecting Omniscience to Cursor

Give Cursor's AI agent grounded retrieval across your indexed code, docs, and infrastructure configs. Once connected, Cursor can call `search`, `get_document`, and `list_sources` during chat and inline suggestions.

## Prerequisites

- Cursor 0.40+ installed
- Running Omniscience instance with at least one source indexed
- Omniscience API token with `search` + `sources:read` scopes

If Omniscience is not yet running, follow [Step 1 and Step 2 in the Claude Code guide](claude-code.md) first.

## Step 1 — Create an API token

If you already have a token from the Claude Code setup, you can reuse it. Otherwise:

```bash
docker compose exec app omniscience tokens create \
  --name cursor \
  --scopes search,sources:read
```

Copy the printed token (`omni_dev_...`). It is shown only once.

## Step 2 — Configure Omniscience as an MCP server in Cursor

Open Cursor settings: `Cmd+Shift+P` → **Cursor Settings** → **MCP**.

Click **Add new MCP server** and fill in the form, or edit the JSON directly.

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

The config file is stored at `~/.cursor/mcp.json` on macOS/Linux or `%APPDATA%\Cursor\mcp.json` on Windows.

## Step 3 — Restart Cursor and verify

Restart Cursor after saving the config. Open **Cursor Settings → MCP** and confirm `omniscience` shows a green status indicator and lists tools: `search`, `get_document`, `list_sources`, `source_stats`.

## Step 4 — Test from Cursor Chat

Open the Cursor Chat panel (`Cmd+L`) and ask:

```
How is authentication implemented in this service?
```

Cursor's agent will call `omniscience.search` automatically when your question benefits from retrieval. You will see the tool invocation in the response, followed by cited source URIs.

You can also invoke tools explicitly:

```
@omniscience search "rate limiting implementation"
```

Or ask about source freshness:

```
What sources does Omniscience have indexed and when were they last synced?
```

## Environment variables reference

| Variable | Required | Description |
|---|---|---|
| `OMNISCIENCE_URL` | Yes (stdio only) | Base URL of your Omniscience instance |
| `OMNISCIENCE_TOKEN` | Yes (stdio only) | API token with at minimum `search` scope |

For the streamable-http transport, these are passed as URL and header directly in the JSON config instead.

## Transport selection

| Setup | Recommended transport |
|---|---|
| Local Omniscience on the same machine | stdio |
| Shared Omniscience on a remote host | streamable-http |
| Team using the same hosted instance | streamable-http |

stdio is simpler because it requires no network setup and works behind NAT. streamable-http is better for shared deployments because the connection is stateless and can be load-balanced.

## Troubleshooting

### MCP server shows red/error status

- Confirm Omniscience is running: `curl http://localhost:8000/health`
- For stdio: confirm the `omniscience` binary is on your `$PATH`. Open a terminal in Cursor and run `which omniscience`
- For streamable-http: confirm the URL is reachable from Cursor
- Check that the JSON config is valid (no trailing commas, no `//` comments)

### Cursor agent does not use Omniscience

Cursor calls MCP tools when it judges them relevant. To force it, prefix your message with `@omniscience` or mention the tool explicitly:

```
Use omniscience.search to find how the payments service handles retries.
```

### Token errors

```
unauthorized — Token missing or invalid
```

- Confirm the token in the config matches exactly what was printed during creation
- Tokens are single-use display. If lost, revoke and create a new one:

```bash
docker compose exec app omniscience tokens list
docker compose exec app omniscience tokens revoke <token-id>
docker compose exec app omniscience tokens create --name cursor --scopes search,sources:read
```

### No results for queries

- Confirm sources are indexed: open Cursor Chat and ask Omniscience to list sources
- Trigger a manual sync via the REST API if sources are stale:

```bash
curl -X POST -H "Authorization: Bearer omni_dev_..." \
  http://localhost:8000/api/v1/sources/<source-id>/sync
```

## See also

- [MCP API reference](../api/mcp.md) — full tool contracts
- [Claude Code integration](claude-code.md) — identical setup, different IDE
- [REST API](../api/rest.md) — health, token, and source management endpoints
