/**
 * REST client for the Omniscience API.
 *
 * Covers all /api/v1 endpoints documented in docs/api/rest.md.
 *
 * @module @omniscience/sdk/rest
 *
 * @example
 * ```ts
 * import { OmniscienceClient } from "@omniscience/sdk";
 *
 * const client = new OmniscienceClient({
 *   baseUrl: "https://omniscience.example.com",
 *   token: process.env["OMNISCIENCE_TOKEN"]!,
 * });
 *
 * const results = await client.search({ query: "authentication middleware" });
 * ```
 */

import { NetworkError, parseApiError } from "./errors.js";
import type {
  ApiToken,
  CreateSourceParams,
  CreateTokenParams,
  DocumentWithChunks,
  HealthResponse,
  IngestionRun,
  ListRunsParams,
  SearchParams,
  SearchResult,
  Source,
  SourceStats,
  SyncResponse,
  TokenCreateResponse,
  UpdateSourceParams,
} from "./types.js";

// ---------------------------------------------------------------------------
// Client options
// ---------------------------------------------------------------------------

/** Fetch function type — compatible with the global fetch API. */
export type FetchFn = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

export interface OmniscienceClientOptions {
  /**
   * Base URL of the Omniscience server (no trailing slash).
   * Example: "https://omniscience.example.com"
   */
  baseUrl: string;
  /**
   * API bearer token. Required for all endpoints except GET /health.
   * Generate one with `omni tokens create` or via the admin UI.
   */
  token: string;
  /**
   * Optional custom fetch implementation.
   * Defaults to the global `fetch` (Node 18+, browsers, Deno, Bun).
   */
  fetch?: FetchFn;
  /**
   * Request timeout in milliseconds. Default: 30 000.
   * Set to 0 to disable.
   */
  timeoutMs?: number;
}

// ---------------------------------------------------------------------------
// Internal raw-request options
// ---------------------------------------------------------------------------

interface RawRequestOpts {
  authenticated: boolean;
  body?: unknown;
  extraHeaders?: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Main client
// ---------------------------------------------------------------------------

/**
 * REST client for the Omniscience API.
 *
 * All methods throw {@link ApiError} on 4xx/5xx responses and
 * {@link NetworkError} when the request cannot be sent.
 */
export class OmniscienceClient {
  private readonly baseUrl: string;
  private readonly token: string;
  private readonly _fetch: FetchFn;
  private readonly timeoutMs: number;

  constructor(options: OmniscienceClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, "");
    this.token = options.token;
    this._fetch = options.fetch ?? (fetch as FetchFn);
    this.timeoutMs = options.timeoutMs ?? 30_000;
  }

  // -------------------------------------------------------------------------
  // Search
  // -------------------------------------------------------------------------

  /**
   * Execute a hybrid (vector + BM25) search query.
   *
   * Requires `search` scope.
   *
   * @example
   * ```ts
   * const result = await client.search({
   *   query: "connection pooling",
   *   top_k: 5,
   *   sources: ["main-repo"],
   *   retrieval_strategy: "hybrid",
   * });
   * console.log(result.hits[0]?.text);
   * ```
   */
  async search(params: SearchParams): Promise<SearchResult> {
    return this._request<SearchResult>("POST", "/api/v1/search", params);
  }

  // -------------------------------------------------------------------------
  // Sources
  // -------------------------------------------------------------------------

  /**
   * List all configured sources.
   *
   * Requires `sources:read` scope.
   *
   * @param params - Optional filters: `type` and/or `status`.
   */
  async listSources(params?: {
    type?: string;
    status?: string;
  }): Promise<Source[]> {
    const qs = params !== undefined ? buildQueryString(params) : "";
    return this._request<Source[]>("GET", `/api/v1/sources${qs}`);
  }

  /**
   * Get a single source by ID.
   *
   * Requires `sources:read` scope.
   */
  async getSource(id: string): Promise<Source> {
    return this._request<Source>("GET", `/api/v1/sources/${id}`);
  }

  /**
   * Create a new ingestion source.
   *
   * Requires `sources:write` scope.
   *
   * @example
   * ```ts
   * const source = await client.createSource({
   *   type: "git",
   *   name: "main-repo",
   *   config: { url: "https://github.com/org/repo.git", branch: "main" },
   * });
   * ```
   */
  async createSource(params: CreateSourceParams): Promise<Source> {
    return this._request<Source>("POST", "/api/v1/sources", params);
  }

  /**
   * Partially update a source.
   *
   * Requires `sources:write` scope.
   */
  async updateSource(id: string, params: UpdateSourceParams): Promise<Source> {
    return this._request<Source>("PATCH", `/api/v1/sources/${id}`, params);
  }

  /**
   * Delete a source (tombstones associated documents; janitor purges later).
   *
   * Requires `sources:write` scope.
   */
  async deleteSource(id: string): Promise<void> {
    await this._request<void>("DELETE", `/api/v1/sources/${id}`);
  }

  /**
   * Trigger a manual sync for a source.
   *
   * Returns a `run_id` you can poll via {@link getIngestionRun}.
   *
   * Requires `sources:write` scope.
   */
  async syncSource(id: string): Promise<SyncResponse> {
    return this._request<SyncResponse>("POST", `/api/v1/sources/${id}/sync`);
  }

  /**
   * Get statistics (document counts, chunk count, last sync) for a source.
   *
   * Requires `sources:read` scope.
   */
  async getSourceStats(id: string): Promise<SourceStats> {
    return this._request<SourceStats>("GET", `/api/v1/sources/${id}/stats`);
  }

  // -------------------------------------------------------------------------
  // Documents
  // -------------------------------------------------------------------------

  /**
   * Retrieve a full document (all chunks) by ID.
   *
   * Requires `sources:read` scope.
   */
  async getDocument(id: string): Promise<DocumentWithChunks> {
    return this._request<DocumentWithChunks>(
      "GET",
      `/api/v1/documents/${id}`,
    );
  }

  // -------------------------------------------------------------------------
  // Ingestion runs
  // -------------------------------------------------------------------------

  /**
   * List recent ingestion runs.
   *
   * Requires `sources:read` scope.
   */
  async listIngestionRuns(params?: ListRunsParams): Promise<IngestionRun[]> {
    const qs =
      params !== undefined
        ? buildQueryString(params as Record<string, unknown>)
        : "";
    return this._request<IngestionRun[]>("GET", `/api/v1/ingestion-runs${qs}`);
  }

  /**
   * Get a single ingestion run by ID.
   *
   * Requires `sources:read` scope.
   */
  async getIngestionRun(id: string): Promise<IngestionRun> {
    return this._request<IngestionRun>("GET", `/api/v1/ingestion-runs/${id}`);
  }

  // -------------------------------------------------------------------------
  // Tokens
  // -------------------------------------------------------------------------

  /**
   * Mint a new API token.
   *
   * The plaintext `secret` in the response is shown exactly once and
   * cannot be recovered. Store it securely.
   *
   * Requires `admin` scope (or unauthenticated during bootstrap).
   *
   * @example
   * ```ts
   * const { token, secret } = await client.createToken({
   *   name: "ci-pipeline",
   *   scopes: ["search", "sources:read"],
   * });
   * // Save `secret` — it won't be shown again.
   * ```
   */
  async createToken(params: CreateTokenParams): Promise<TokenCreateResponse> {
    return this._request<TokenCreateResponse>(
      "POST",
      "/api/v1/tokens",
      params,
    );
  }

  /**
   * List all active API tokens (secrets are never exposed).
   *
   * Requires `admin` scope.
   */
  async listTokens(): Promise<ApiToken[]> {
    return this._request<ApiToken[]>("GET", "/api/v1/tokens");
  }

  /**
   * Deactivate an API token by ID.
   *
   * Requires `admin` scope.
   */
  async deleteToken(id: string): Promise<void> {
    await this._request<void>("DELETE", `/api/v1/tokens/${id}`);
  }

  // -------------------------------------------------------------------------
  // Health
  // -------------------------------------------------------------------------

  /**
   * Unauthenticated health check.
   *
   * Returns `{ status: "ok", version: "..." }` when the service is healthy.
   * Does **not** require a token — useful for liveness probes.
   */
  async health(): Promise<HealthResponse> {
    return this._rawRequest<HealthResponse>("GET", "/api/v1/health", {
      authenticated: false,
    });
  }

  // -------------------------------------------------------------------------
  // Webhooks
  // -------------------------------------------------------------------------

  /**
   * Deliver a webhook payload to a named source.
   *
   * Used by source systems (GitHub, GitLab, Confluence) that push events.
   * The payload is validated and signature-checked per source type.
   * Returns the enqueued ingestion run ID.
   *
   * No bearer token required — authentication is via HMAC signature.
   */
  async deliverWebhook(
    sourceName: string,
    payload: unknown,
    headers?: Record<string, string>,
  ): Promise<SyncResponse> {
    const opts: RawRequestOpts = {
      authenticated: false,
      body: payload,
    };
    if (headers !== undefined) {
      opts.extraHeaders = headers;
    }
    return this._rawRequest<SyncResponse>(
      "POST",
      `/api/v1/ingest/webhook/${encodeURIComponent(sourceName)}`,
      opts,
    );
  }

  // -------------------------------------------------------------------------
  // Internal
  // -------------------------------------------------------------------------

  private async _request<T>(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<T> {
    return this._rawRequest<T>(method, path, { authenticated: true, body });
  }

  private async _rawRequest<T>(
    method: string,
    path: string,
    opts: RawRequestOpts,
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "application/json",
    };

    if (opts.extraHeaders !== undefined) {
      Object.assign(headers, opts.extraHeaders);
    }

    if (opts.authenticated) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    const init: RequestInit = { method, headers };

    if (opts.body !== undefined) {
      init.body = JSON.stringify(opts.body);
    }

    // Attach AbortSignal for timeout
    let abortController: AbortController | undefined;
    let timerId: ReturnType<typeof setTimeout> | undefined;

    if (this.timeoutMs > 0) {
      abortController = new AbortController();
      timerId = setTimeout(() => {
        abortController!.abort();
      }, this.timeoutMs);
      init.signal = abortController.signal;
    }

    let response: Response;
    try {
      response = await this._fetch(url, init);
    } catch (err) {
      throw new NetworkError(
        `Request to ${method} ${path} failed: ${String(err)}`,
        err instanceof Error ? err : new Error(String(err)),
      );
    } finally {
      if (timerId !== undefined) {
        clearTimeout(timerId);
      }
    }

    if (!response.ok) {
      throw await parseApiError(response);
    }

    // 204 No Content — no body
    if (response.status === 204) {
      return undefined as unknown as T;
    }

    return response.json() as Promise<T>;
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function buildQueryString(params: Record<string, unknown>): string {
  const pairs: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) {
      pairs.push(
        `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`,
      );
    }
  }
  return pairs.length > 0 ? `?${pairs.join("&")}` : "";
}

export type {
  ApiToken,
  CreateSourceParams,
  CreateTokenParams,
  DocumentWithChunks,
  HealthResponse,
  IngestionRun,
  ListRunsParams,
  SearchParams,
  SearchResult,
  Source,
  SourceStats,
  SyncResponse,
  TokenCreateResponse,
  UpdateSourceParams,
};
