# Connecting Omniscience to Claude Code

**Status**: design doc. Implementation lands in [M3](../roadmap.md).

## Overview

Claude Code uses MCP to extend its tool set. Once Omniscience exposes its MCP server, Claude Code can call `search`, `get_document`, etc. during a conversation — giving it grounded retrieval across your indexed sources.

## Setup

1. Start Omniscience:

   ```bash
   docker compose up -d
   ```

2. Create an API token with `search` + `sources:read` scopes:

   ```bash
   omniscience tokens create --name "claude-code" --scopes search,sources:read
   ```

3. Add to Claude Code's MCP config (`~/.claude/mcp-servers.json` or equivalent):

   ```json
   {
     "mcpServers": {
       "omniscience": {
         "command": "omniscience",
         "args": ["mcp", "serve", "--transport", "stdio"],
         "env": {
           "OMNISCIENCE_URL": "https://your-omniscience-host",
           "OMNISCIENCE_TOKEN": "sk_..."
         }
       }
     }
   }
   ```

   Or for streamable-http:

   ```json
   {
     "mcpServers": {
       "omniscience": {
         "transport": "streamable-http",
         "url": "https://your-omniscience-host/mcp",
         "headers": {
           "Authorization": "Bearer sk_..."
         }
       }
     }
   }
   ```

4. Verify:

   ```bash
   claude
   # Inside Claude Code:
   > /mcp   # list connected MCP servers; `omniscience` should appear
   ```

5. Ask something:

   ```
   > How is authentication handled in this codebase?
   ```

   Claude Code now has `search` as a tool and will call it for grounding.

## Scope recommendations

For day-to-day use: `search` + `sources:read`. Never give `admin` or `sources:write` to a client token.

## Troubleshooting

See [REST API](../api/rest.md) for `/health` and token endpoints.
