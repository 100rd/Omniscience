# omniscience-client

Python client SDK for the [Omniscience](https://github.com/omniscience-project/omniscience) knowledge retrieval service.

## Installation

```bash
pip install omniscience-client
```

## Quick start

```python
import asyncio
from omniscience_client import OmniscienceClient

async def main():
    async with OmniscienceClient(
        base_url="http://localhost:8000",
        token="omni_your_token_here",
    ) as client:
        result = await client.search("retrieval augmented generation", top_k=5)
        for hit in result.hits:
            print(f"[{hit.score:.3f}] {hit.citation.title or hit.citation.uri}")
            print(hit.text[:200])

asyncio.run(main())
```

## MCP transport

```python
from omniscience_client import OmniscienceMCP

async with OmniscienceMCP() as mcp:
    await mcp.connect("http://localhost:8000/mcp", token="omni_...")
    result = await mcp.search("vector database comparison")
```

## API

### `OmniscienceClient`

| Method | Description |
|--------|-------------|
| `search(query, top_k, **kwargs)` | Hybrid vector + keyword search |
| `list_sources(**filters)` | List configured ingestion sources |
| `create_source(type, name, config)` | Create a new source |
| `get_document(document_id)` | Retrieve document with all chunks |
| `list_ingestion_runs(**filters)` | List ingestion run records |
| `create_token(name, scopes)` | Mint a new API token |
| `close()` | Release the HTTP connection pool |

### `OmniscienceMCP`

| Method | Description |
|--------|-------------|
| `connect(url, token)` | Establish MCP session |
| `search(query, **kwargs)` | Search via MCP tool |
| `get_document(document_id)` | Fetch document via MCP tool |
| `close()` | Tear down the MCP session |

## License

Apache-2.0
