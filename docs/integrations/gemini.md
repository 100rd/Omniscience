# Connecting Omniscience to Gemini

Connect Omniscience to Gemini-powered agents and applications via MCP. This guide covers the Gemini CLI (Gemini Code Assist / `gemini` CLI tool) and the Google Gen AI Python SDK.

## Prerequisites

- Running Omniscience instance
- Omniscience API token with `search` + `sources:read` scopes
- Gemini CLI installed, or `google-genai` SDK for Python integration

## Step 1 — Create an API token

```bash
docker compose exec app omniscience tokens create \
  --name gemini \
  --scopes search,sources:read
```

Copy the printed token (`omni_dev_...`).

## Option A — Gemini CLI (MCP server)

The Gemini CLI supports MCP servers through its settings file.

### Configure MCP server

Edit `~/.gemini/settings.json` (create if it does not exist):

**stdio transport (local Omniscience):**

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

**streamable-http transport (hosted Omniscience):**

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

### Test from the Gemini CLI

Start the CLI:

```bash
gemini
```

List connected tools:

```
/mcp
```

Expected output:

```
Connected MCP servers:
  omniscience — tools: search, get_document, list_sources, source_stats
```

Ask a question that requires retrieval:

```
How does the payments service handle authentication?
```

Gemini will call `omniscience.search` and include citations in its answer.

## Option B — Google Gen AI Python SDK with MCP

Use this when building custom Gemini-powered applications that need retrieval.

### Install

```bash
pip install google-genai mcp httpx
```

### Example — Gemini agent with Omniscience tools

```python
#!/usr/bin/env python3
"""
Gemini agent with Omniscience retrieval via MCP.
"""

import asyncio
import os
import json

import google.generativeai as genai
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


OMNISCIENCE_URL = os.environ["OMNISCIENCE_URL"]
OMNISCIENCE_TOKEN = os.environ["OMNISCIENCE_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)


def mcp_tool_to_genai_declaration(tool) -> dict:
    """Convert an MCP tool definition to a Gemini function declaration."""
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.inputSchema,
    }


async def run_gemini_with_omniscience(user_question: str) -> str:
    url = f"{OMNISCIENCE_URL}/mcp"
    headers = {"Authorization": f"Bearer {OMNISCIENCE_TOKEN}"}

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as mcp_session:
            await mcp_session.initialize()

            # Get tools from Omniscience
            tools_resp = await mcp_session.list_tools()
            genai_tools = [
                mcp_tool_to_genai_declaration(t) for t in tools_resp.tools
            ]

            model = genai.GenerativeModel(
                model_name="gemini-2.5-flash",
                tools=[{"function_declarations": genai_tools}],
                system_instruction=(
                    "You are a code-aware assistant. Use omniscience.search() "
                    "to retrieve grounded context before answering. "
                    "Cite chunk_id and uri for every claim."
                ),
            )

            chat = model.start_chat()
            response = await asyncio.to_thread(chat.send_message, user_question)

            # Handle tool calls in a loop
            while response.candidates[0].content.parts:
                part = response.candidates[0].content.parts[0]

                if not hasattr(part, "function_call") or not part.function_call:
                    # Text response — done
                    return part.text

                fn_call = part.function_call
                tool_args = dict(fn_call.args)

                # Call the tool via MCP
                tool_result = await mcp_session.call_tool(
                    fn_call.name, arguments=tool_args
                )
                result_data = json.loads(tool_result.content[0].text)

                # Feed the result back to Gemini
                response = await asyncio.to_thread(
                    chat.send_message,
                    genai.protos.Content(
                        parts=[
                            genai.protos.Part(
                                function_response=genai.protos.FunctionResponse(
                                    name=fn_call.name,
                                    response={"result": result_data},
                                )
                            )
                        ]
                    ),
                )

            return response.text


async def main() -> None:
    answer = await run_gemini_with_omniscience(
        "How does the authentication middleware work in the server app?"
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
```

Run it:

```bash
export OMNISCIENCE_URL=http://localhost:8000
export OMNISCIENCE_TOKEN=omni_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export GEMINI_API_KEY=your-gemini-api-key
python gemini_omniscience.py
```

## Option C — Using PydanticAI with Gemini backend

PydanticAI handles the tool-call loop for you and supports Gemini as a backend. See the [PydanticAI guide](pydantic-ai.md) — just swap the model string:

```python
agent = Agent(
    "google-gla:gemini-2.5-flash",   # Gemini instead of Anthropic
    mcp_servers=[omniscience],
    system_prompt="Use omniscience.search for grounding. Cite every claim.",
)
```

This is the simplest option if you do not need low-level control over the Gemini API.

## Scope recommendations

Use `search` + `sources:read`. Never pass `admin` to Gemini agent tokens.

## Troubleshooting

### MCP server not appearing in Gemini CLI

- Confirm `~/.gemini/settings.json` is valid JSON
- Restart the Gemini CLI after editing the config
- For stdio: confirm `omniscience` is on your `$PATH` — `which omniscience`
- Test the MCP server standalone: `OMNISCIENCE_URL=http://localhost:8000 OMNISCIENCE_TOKEN=omni_dev_... omniscience mcp serve --transport stdio`

### Gemini not calling search tool

Gemini may decide not to call tools for some queries. Add explicit instruction in the system prompt:

```
"Always call omniscience.search() before answering questions about the codebase."
```

### Token errors

See the [Claude Code troubleshooting section](claude-code.md#token-invalid-or-unauthorized) — the token management process is identical.

## See also

- [MCP API reference](../api/mcp.md) — full tool contracts
- [PydanticAI integration](pydantic-ai.md) — Gemini-compatible, handles tool loops automatically
- [LangGraph integration](langgraph.md) — for complex stateful agent workflows
- [Python client](python-client.md) — direct MCP access without an agent framework
