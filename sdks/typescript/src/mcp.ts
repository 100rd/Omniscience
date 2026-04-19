/**
 * MCP (Model Context Protocol) client for Omniscience.
 *
 * Supports two transports:
 *   - **stdio** — for local CLI-style clients (Claude Code, Cursor).
 *     The server binary must be on PATH (e.g. `omni serve --stdio`).
 *   - **http** — for hosted clients using streamable-HTTP behind a TLS proxy.
 *
 * Authentication:
 *   - stdio: pass `token` option or set `OMNISCIENCE_TOKEN` env var.
 *   - http: `token` is required.
 *
 * @module @omniscience/sdk/mcp
 *
 * @example HTTP transport
 * ```ts
 * import { OmniscienceMCP } from "@omniscience/sdk/mcp";
 *
 * const mcp = new OmniscienceMCP({
 *   transport: "http",
 *   url: "https://omniscience.example.com",
 *   token: process.env["OMNISCIENCE_TOKEN"]!,
 * });
 *
 * const results = await mcp.search("connection pooling", { top_k: 5 });
 * ```
 *
 * @example stdio transport (local)
 * ```ts
 * import { OmniscienceMCP } from "@omniscience/sdk/mcp";
 *
 * // Requires OMNISCIENCE_TOKEN env var or token option
 * const mcp = new OmniscienceMCP({ transport: "stdio" });
 * const sources = await mcp.listSources();
 * await mcp.close(); // Terminate subprocess when done
 * ```
 */

import type { ChildProcess } from "child_process";
import { ConfigurationError, NetworkError, parseApiError } from "./errors.js";
import type { FetchFn } from "./rest.js";
import type {
  McpClientOptions,
  SearchOptions,
  SearchResult,
  Source,
  SourceStats,
} from "./types.js";

// ---------------------------------------------------------------------------
// MCP protocol types (minimal — enough to implement the tools we expose)
// ---------------------------------------------------------------------------

interface McpRequest {
  jsonrpc: "2.0";
  id: number;
  method: "tools/call";
  params: {
    name: string;
    arguments: Record<string, unknown>;
  };
}

interface McpSuccessResponse {
  jsonrpc: "2.0";
  id: number;
  result: {
    content: Array<{ type: "text"; text: string }>;
    isError?: false;
  };
}

interface McpErrorResponse {
  jsonrpc: "2.0";
  id: number;
  error: {
    code: number;
    message: string;
    data?: unknown;
  };
}

type McpResponse = McpSuccessResponse | McpErrorResponse;

// MCP document shape returned by get_document tool
export interface McpDocument {
  document: {
    id: string;
    source_id: string;
    external_id: string;
    uri: string;
    title: string | null;
    content_hash: string;
    doc_version: number;
    metadata: Record<string, unknown>;
    indexed_at: string;
    tombstoned_at: string | null;
  };
  chunks: Array<{
    id: string;
    document_id: string;
    ord: number;
    text: string;
    symbol: string | null;
    ingestion_run_id: string | null;
    embedding_model: string;
    embedding_provider: string;
    parser_version: string;
    chunker_strategy: string;
    metadata: Record<string, unknown>;
  }>;
}

// ---------------------------------------------------------------------------
// OmniscienceMCP
// ---------------------------------------------------------------------------

/**
 * MCP client for Omniscience.
 *
 * Wraps the four MCP tools exposed by the server:
 * - `search`
 * - `get_document`
 * - `list_sources`
 * - `source_stats`
 *
 * All methods return fully-typed results. Errors are thrown as
 * {@link OmniscienceError} subclasses.
 */
export class OmniscienceMCP {
  private readonly options: McpClientOptions;
  private readonly _fetch: FetchFn;
  private _requestId = 0;

  /** stdio subprocess handle (Node.js only; set after first call). */
  private _proc: StdioProcess | null = null;

  constructor(options: McpClientOptions, fetchImpl?: FetchFn) {
    this.options = options;
    this._fetch = fetchImpl ?? (fetch as FetchFn);

    if (options.transport === "http" && !options.url) {
      throw new ConfigurationError(
        "OmniscienceMCP: `url` is required when transport is 'http'.",
      );
    }
    if (options.transport === "http" && !options.token) {
      throw new ConfigurationError(
        "OmniscienceMCP: `token` is required when transport is 'http'.",
      );
    }
  }

  // -------------------------------------------------------------------------
  // Public tool methods
  // -------------------------------------------------------------------------

  /**
   * Execute a hybrid semantic + keyword search.
   *
   * @param query - Natural-language or keyword query.
   * @param options - Optional search parameters.
   */
  async search(
    query: string,
    options: SearchOptions = {},
  ): Promise<SearchResult> {
    const args: Record<string, unknown> = { query, ...options };
    const raw = await this._call("search", args);
    return raw as SearchResult;
  }

  /**
   * Retrieve a full document (all chunks) by document ID.
   *
   * @param id - Document UUID.
   */
  async getDocument(id: string): Promise<McpDocument> {
    const raw = await this._call("get_document", { document_id: id });
    return raw as McpDocument;
  }

  /**
   * List all configured sources with freshness information.
   */
  async listSources(): Promise<Source[]> {
    const raw = (await this._call("list_sources", {})) as {
      sources: Source[];
    };
    return raw.sources;
  }

  /**
   * Get per-source statistics: document counts, chunk count, last sync.
   *
   * @param sourceId - Source UUID.
   */
  async sourceStats(sourceId: string): Promise<SourceStats> {
    const raw = await this._call("source_stats", { source_id: sourceId });
    return raw as SourceStats;
  }

  // -------------------------------------------------------------------------
  // Transport dispatch
  // -------------------------------------------------------------------------

  private async _call(
    toolName: string,
    args: Record<string, unknown>,
  ): Promise<unknown> {
    const request: McpRequest = {
      jsonrpc: "2.0",
      id: ++this._requestId,
      method: "tools/call",
      params: {
        name: toolName,
        arguments: args,
      },
    };

    if (this.options.transport === "http") {
      return this._callHttp(request);
    }
    return this._callStdio(request);
  }

  // -------------------------------------------------------------------------
  // HTTP transport
  // -------------------------------------------------------------------------

  private async _callHttp(request: McpRequest): Promise<unknown> {
    const url = `${this.options.url!.replace(/\/$/, "")}/mcp`;
    const token = this.options.token!;

    let response: Response;
    try {
      response = await this._fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json, text/event-stream",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(request),
      });
    } catch (err) {
      throw new NetworkError(
        `MCP HTTP request failed: ${String(err)}`,
        err instanceof Error ? err : new Error(String(err)),
      );
    }

    if (!response.ok) {
      throw await parseApiError(response);
    }

    const mcpResponse = (await response.json()) as McpResponse;
    return this._unwrap(mcpResponse);
  }

  // -------------------------------------------------------------------------
  // stdio transport
  // -------------------------------------------------------------------------

  /**
   * Send a JSON-RPC request over stdio to a locally running `omni serve --stdio`
   * process.
   *
   * Uses Node.js `child_process` (dynamically imported so the module stays
   * importable in browsers/edge runtimes — those should use http transport).
   */
  private async _callStdio(request: McpRequest): Promise<unknown> {
    const proc = await this._ensureStdioProcess();
    const mcpResponse = (await proc.call(request)) as McpResponse;
    return this._unwrap(mcpResponse);
  }

  private async _ensureStdioProcess(): Promise<StdioProcess> {
    if (this._proc !== null) {
      return this._proc;
    }

    const token =
      this.options.token ??
      (typeof process !== "undefined"
        ? process.env["OMNISCIENCE_TOKEN"]
        : undefined);

    if (!token) {
      throw new ConfigurationError(
        "OmniscienceMCP (stdio): token is required. " +
          "Pass `token` option or set the OMNISCIENCE_TOKEN environment variable.",
      );
    }

    this._proc = await StdioProcess.spawn(token);
    return this._proc;
  }

  // -------------------------------------------------------------------------
  // Cleanup
  // -------------------------------------------------------------------------

  /**
   * Terminate the stdio subprocess (if one was started).
   *
   * Call this when you are done using the client to avoid leaving orphan
   * processes. Not needed for http transport.
   */
  async close(): Promise<void> {
    if (this._proc !== null) {
      await this._proc.terminate();
      this._proc = null;
    }
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  private _unwrap(response: McpResponse): unknown {
    if ("error" in response) {
      const { code, message } = response.error;
      throw new NetworkError(
        `MCP error ${code}: ${message}`,
        new Error(message),
      );
    }

    const content = response.result.content[0];
    if (content === undefined || content.type !== "text") {
      throw new NetworkError(
        "MCP response contained no text content.",
        new Error("empty content"),
      );
    }

    try {
      return JSON.parse(content.text) as unknown;
    } catch {
      // The tool returned plain text — wrap it
      return { text: content.text };
    }
  }
}

// ---------------------------------------------------------------------------
// stdio subprocess abstraction (Node.js only)
// ---------------------------------------------------------------------------

interface PendingCall {
  resolve: (v: unknown) => void;
  reject: (e: Error) => void;
}

/**
 * Manages a single `omni serve --stdio` child process, multiplexing
 * JSON-RPC requests over its stdin/stdout.
 *
 * Dynamically imports `child_process` so the module can be imported on
 * non-Node runtimes without errors (those will fail at runtime if stdio
 * transport is actually used, which is expected).
 */
class StdioProcess {
  private readonly pending = new Map<number, PendingCall>();

  private constructor(private readonly proc: ChildProcess) {}

  static async spawn(token: string): Promise<StdioProcess> {
    let spawnFn: typeof import("child_process").spawn;

    try {
      const cp = await import("child_process");
      spawnFn = cp.spawn;
    } catch {
      throw new ConfigurationError(
        "stdio transport requires Node.js. " +
          "Use transport: 'http' in browser and edge environments.",
      );
    }

    const childProc = spawnFn("omni", ["serve", "--stdio"], {
      env: {
        ...process.env,
        OMNISCIENCE_TOKEN: token,
      },
      stdio: ["pipe", "pipe", "inherit"],
    });

    const instance = new StdioProcess(childProc);
    instance._listen();
    return instance;
  }

  call(request: McpRequest): Promise<unknown> {
    return new Promise<unknown>((resolve, reject) => {
      this.pending.set(request.id, { resolve, reject });
      this.proc.stdin!.write(JSON.stringify(request) + "\n");
    });
  }

  async terminate(): Promise<void> {
    this.proc.kill("SIGTERM");
    for (const { reject } of this.pending.values()) {
      reject(new Error("MCP stdio process terminated."));
    }
    this.pending.clear();
  }

  private _listen(): void {
    let buffer = "";

    this.proc.stdout!.on("data", (data: Buffer) => {
      buffer += data.toString();
      const lines = buffer.split("\n");
      // All lines except the last are complete
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        let parsed: McpResponse;
        try {
          parsed = JSON.parse(trimmed) as McpResponse;
        } catch {
          // Non-JSON output from the server — ignore (e.g. startup banner)
          continue;
        }

        const handler = this.pending.get(parsed.id);
        if (handler === undefined) continue;

        this.pending.delete(parsed.id);

        if ("error" in parsed) {
          handler.reject(
            new Error(
              `MCP error ${parsed.error.code}: ${parsed.error.message}`,
            ),
          );
        } else {
          handler.resolve(parsed);
        }
      }
    });
  }
}

export type { McpClientOptions, SearchOptions };
