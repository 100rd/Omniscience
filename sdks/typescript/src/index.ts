/**
 * @omniscience/sdk — TypeScript client for the Omniscience knowledge retrieval service.
 *
 * ## Quick start
 *
 * ### REST client
 * ```ts
 * import { OmniscienceClient } from "@omniscience/sdk";
 *
 * const client = new OmniscienceClient({
 *   baseUrl: "https://omniscience.example.com",
 *   token: process.env.OMNISCIENCE_TOKEN!,
 * });
 *
 * const results = await client.search({ query: "connection pooling" });
 * console.log(results.hits[0]?.text);
 * ```
 *
 * ### MCP client (HTTP transport)
 * ```ts
 * import { OmniscienceMCP } from "@omniscience/sdk";
 *
 * const mcp = new OmniscienceMCP({
 *   transport: "http",
 *   url: "https://omniscience.example.com",
 *   token: process.env.OMNISCIENCE_TOKEN!,
 * });
 *
 * const results = await mcp.search("connection pooling", { top_k: 5 });
 * ```
 *
 * ### MCP client (stdio transport — local)
 * ```ts
 * import { OmniscienceMCP } from "@omniscience/sdk";
 *
 * // Requires OMNISCIENCE_TOKEN env var or token option.
 * // Spawns `omni serve --stdio` as a subprocess.
 * const mcp = new OmniscienceMCP({ transport: "stdio" });
 * const sources = await mcp.listSources();
 * await mcp.close(); // Terminate the subprocess when done
 * ```
 *
 * @module @omniscience/sdk
 */

// ---------------------------------------------------------------------------
// REST client
// ---------------------------------------------------------------------------
export { OmniscienceClient } from "./rest.js";
export type { OmniscienceClientOptions } from "./rest.js";

// ---------------------------------------------------------------------------
// MCP client
// ---------------------------------------------------------------------------
export { OmniscienceMCP } from "./mcp.js";

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------
export {
  ApiError,
  ConfigurationError,
  NetworkError,
  OmniscienceError,
} from "./errors.js";

// ---------------------------------------------------------------------------
// Types — re-exported from types.ts for direct import convenience
// ---------------------------------------------------------------------------
export type {
  ApiToken,
  Chunk,
  ChunkLineage,
  Citation,
  CreateSourceParams,
  CreateTokenParams,
  Document,
  DocumentWithChunks,
  HealthResponse,
  IngestionRun,
  IngestionRunStatus,
  ListRunsParams,
  McpClientOptions,
  McpTransport,
  OmniscienceErrorBody,
  QueryStats,
  RetrievalStrategy,
  SearchHit,
  SearchOptions,
  SearchParams,
  SearchResult,
  Source,
  SourceInfo,
  SourceStats,
  SourceStatus,
  SourceType,
  SyncResponse,
  TokenCreateResponse,
  TokenScope,
  UpdateSourceParams,
} from "./types.js";
