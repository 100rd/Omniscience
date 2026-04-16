# Connecting Omniscience to Cursor

**Status**: design doc. Implementation lands in [M3](../roadmap.md).

## Overview

Cursor supports MCP servers as tool providers. Omniscience plugs in and gives Cursor grounded retrieval across your indexed sources.

## Setup

1. Run Omniscience and create a token (see [Claude Code guide](claude-code.md) steps 1–2).

2. In Cursor settings → MCP → Add server:

   ```json
   {
     "name": "omniscience",
     "command": "omniscience",
     "args": ["mcp", "serve", "--transport", "stdio"],
     "env": {
       "OMNISCIENCE_URL": "https://your-omniscience-host",
       "OMNISCIENCE_TOKEN": "sk_..."
     }
   }
   ```

3. Restart Cursor. `omniscience` should appear in the MCP server list.

4. Cursor's agent will now call `search` when relevant. You can also invoke tools directly via `@omniscience search`.

## Notes

Cursor supports both stdio and http MCP transports. Stdio is simpler for local setups; streamable-http is better for shared hosted Omniscience deployments.

See [MCP API](../api/mcp.md) for full tool contracts.
