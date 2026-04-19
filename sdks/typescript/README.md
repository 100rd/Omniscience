# @omniscience/sdk

TypeScript client SDK for [Omniscience](https://github.com/omniscience-ai/omniscience) — a self-hosted knowledge retrieval service with a Model Context Protocol (MCP) first API.

## Installation

```bash
npm install @omniscience/sdk
# or
pnpm add @omniscience/sdk
# or
yarn add @omniscience/sdk
```

**Runtime requirements**: Node.js 18+ (uses built-in `fetch`). Works in browsers, Deno, and Bun too. stdio transport requires Node.js.

## Quick start

### REST client

Use `OmniscienceClient` when you want to call the REST API directly — from a backend, a CLI tool, or any non-MCP context.

```ts
import { OmniscienceClient } from "@omniscience/sdk";

const client = new OmniscienceClient({
  baseUrl: "https://omniscience.example.com",
  token: process.env.OMNISCIENCE_TOKEN!,
});

// Search
const result = await client.search({
  query: "connection pooling best practices",
  top_k: 5,
  retrieval_strategy: "hybrid",
});

for (const hit of result.hits) {
  console.log(`[${hit.score.toFixed(3)}] ${hit.citation.uri}`);
  console.log(hit.text.slice(0, 200));
}
```

### MCP client — HTTP transport

Use `OmniscienceMCP` when working with the Model Context Protocol interface. HTTP transport is suitable for hosted/remote servers.

```ts
import { OmniscienceMCP } from "@omniscience/sdk";

const mcp = new OmniscienceMCP({
  transport: "http",
  url: "https://omniscience.example.com",
  token: process.env.OMNISCIENCE_TOKEN!,
});

const result = await mcp.search("authentication middleware", { top_k: 3 });
```

### MCP client — stdio transport

stdio transport spawns `omni serve --stdio` as a subprocess. This is what Claude Code, Cursor, and similar editors use for local indexing.

```ts
import { OmniscienceMCP } from "@omniscience/sdk";

// Requires OMNISCIENCE_TOKEN env var or token option
const mcp = new OmniscienceMCP({ transport: "stdio" });

const sources = await mcp.listSources();
console.log(sources.map((s) => s.name));

await mcp.close(); // Always close when done to terminate the subprocess
```

## API reference

### `OmniscienceClient`

Full REST client. All methods require an appropriate token scope.

```ts
const client = new OmniscienceClient({ baseUrl, token });

// Search
await client.search(params: SearchParams): Promise<SearchResult>

// Sources
await client.listSources(params?: { type?: string; status?: string }): Promise<Source[]>
await client.getSource(id: string): Promise<Source>
await client.createSource(params: CreateSourceParams): Promise<Source>
await client.updateSource(id: string, params: UpdateSourceParams): Promise<Source>
await client.deleteSource(id: string): Promise<void>
await client.syncSource(id: string): Promise<SyncResponse>
await client.getSourceStats(id: string): Promise<SourceStats>

// Documents
await client.getDocument(id: string): Promise<DocumentWithChunks>

// Ingestion runs
await client.listIngestionRuns(params?: ListRunsParams): Promise<IngestionRun[]>
await client.getIngestionRun(id: string): Promise<IngestionRun>

// Tokens (admin scope)
await client.createToken(params: CreateTokenParams): Promise<TokenCreateResponse>
await client.listTokens(): Promise<ApiToken[]>
await client.deleteToken(id: string): Promise<void>

// Health (no auth required)
await client.health(): Promise<HealthResponse>

// Webhooks (signature-authenticated)
await client.deliverWebhook(sourceName: string, payload: unknown, headers?: Record<string, string>): Promise<SyncResponse>
```

#### Constructor options

| Option | Type | Required | Default | Description |
|--------|------|----------|---------|-------------|
| `baseUrl` | `string` | Yes | — | Server URL (no trailing slash) |
| `token` | `string` | Yes | — | API bearer token |
| `fetch` | `typeof fetch` | No | `globalThis.fetch` | Custom fetch implementation |
| `timeoutMs` | `number` | No | `30000` | Request timeout in ms. Set `0` to disable. |

### `OmniscienceMCP`

MCP tool client. Wraps the four tools the server exposes.

```ts
const mcp = new OmniscienceMCP({ transport, url?, token? });

await mcp.search(query: string, options?: SearchOptions): Promise<SearchResult>
await mcp.getDocument(id: string): Promise<DocumentWithChunks>
await mcp.listSources(): Promise<Source[]>
await mcp.sourceStats(sourceId: string): Promise<SourceStats>

await mcp.close(): Promise<void>  // for stdio transport
```

#### Constructor options

| Option | Type | Required | Description |
|--------|------|----------|-------------|
| `transport` | `"stdio" \| "http"` | Yes | Transport to use |
| `url` | `string` | http only | Server base URL |
| `token` | `string` | http only (stdio: env var) | API token |

### Error handling

All errors extend `OmniscienceError`:

```ts
import {
  OmniscienceError,
  ApiError,
  NetworkError,
  ConfigurationError,
} from "@omniscience/sdk";

try {
  const result = await client.search({ query: "..." });
} catch (err) {
  if (err instanceof ApiError) {
    // 4xx / 5xx from the server
    console.error(err.code, err.status, err.message);
    // e.g. "unauthorized" 401 "Token missing or invalid"
  } else if (err instanceof NetworkError) {
    // Could not reach the server
    console.error("Network failure:", err.message);
  } else if (err instanceof ConfigurationError) {
    // Bad SDK setup (missing required options)
    console.error("Config error:", err.message);
  }
}
```

Common `ApiError.code` values:

| Code | HTTP | Meaning |
|------|------|---------|
| `unauthorized` | 401 | Token missing, invalid, or expired |
| `forbidden` | 403 | Token lacks required scope |
| `source_not_found` | 404 | Requested source does not exist |
| `rate_limited` | 429 | Too many requests — check `Retry-After` |
| `embedding_provider_unavailable` | 503 | Can't embed query — retry later |
| `internal` | 500 | Unexpected server failure |

### Types

All types are importable from `@omniscience/sdk` or `@omniscience/sdk/types`:

```ts
import type {
  SearchParams,
  SearchResult,
  SearchHit,
  Source,
  SourceStats,
  Document,
  DocumentWithChunks,
  Chunk,
  IngestionRun,
  ApiToken,
  TokenCreateResponse,
  RetrievalStrategy,
  SourceType,
  SourceStatus,
} from "@omniscience/sdk";
```

## Examples

### Create a source and trigger a sync

```ts
const source = await client.createSource({
  type: "git",
  name: "main-repo",
  config: {
    url: "https://github.com/your-org/your-repo.git",
    branch: "main",
  },
  freshness_sla_seconds: 300,
});

const { run_id } = await client.syncSource(source.id);
console.log(`Syncing — run: ${run_id}`);

// Poll until done
while (true) {
  const run = await client.getIngestionRun(run_id);
  if (run.status !== "running") {
    console.log(`Done: ${run.status} — ${run.docs_new} new, ${run.docs_updated} updated`);
    break;
  }
  await new Promise((r) => setTimeout(r, 2000));
}
```

### Mint an API token

```ts
const { token, secret } = await client.createToken({
  name: "ci-pipeline",
  scopes: ["search", "sources:read"],
});

// secret is shown once — store it securely
console.log("Token prefix:", token.token_prefix);
console.log("Secret (save this!):", secret);
```

### MCP search with filters

```ts
const result = await mcp.search("database migrations", {
  top_k: 10,
  sources: ["main-repo", "docs-repo"],
  filters: { language: "python" },
  retrieval_strategy: "hybrid",
});
```

### Source statistics

```ts
const sources = await client.listSources({ status: "active" });
for (const source of sources) {
  const stats = await client.getSourceStats(source.id);
  console.log(
    `${source.name}: ${stats.active_documents} docs, ${stats.total_chunks} chunks`
  );
}
```

## Build from source

```bash
cd sdks/typescript
npm install
npm run build      # outputs to dist/
npm run typecheck  # type-check without emitting
```

## License

Apache-2.0 — see [LICENSE](../../LICENSE).
