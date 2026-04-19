// API client wrapping fetch() with Bearer token auth.

export type SourceType =
  | "git"
  | "fs"
  | "confluence"
  | "notion"
  | "slack"
  | "jira"
  | "grafana"
  | "k8s"
  | "terraform";

export type SourceStatus = "active" | "paused" | "error";

export type IngestionRunStatus = "running" | "ok" | "partial" | "error";

export interface Source {
  id: string;
  type: SourceType;
  name: string;
  config: Record<string, unknown>;
  secrets_ref: string | null;
  status: SourceStatus;
  last_sync_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  freshness_sla_seconds: number | null;
  tenant_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourceStats {
  source_id: string;
  total_documents: number;
  active_documents: number;
  total_chunks: number;
  last_sync_at: string | null;
  last_run_status: string | null;
}

export interface SourceCreate {
  type: SourceType;
  name: string;
  config?: Record<string, unknown>;
  secrets_ref?: string;
  status?: SourceStatus;
  freshness_sla_seconds?: number;
}

export interface IngestionRun {
  id: string;
  source_id: string;
  started_at: string;
  finished_at: string | null;
  status: IngestionRunStatus;
  docs_new: number;
  docs_updated: number;
  docs_removed: number;
  errors: Record<string, unknown>;
}

export interface ApiToken {
  id: string;
  name: string;
  token_prefix: string;
  scopes: string[];
  workspace_id: string | null;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  is_active: boolean;
}

export interface TokenCreateRequest {
  name: string;
  scopes: string[];
  expires_at?: string;
}

export interface TokenCreateResponse {
  token: ApiToken;
  secret: string;
}

export interface SearchRequest {
  query: string;
  top_k?: number;
  sources?: string[];
  retrieval_strategy?: "hybrid" | "keyword" | "structural" | "auto";
}

export interface SearchHit {
  chunk_id: string;
  document_id: string;
  score: number;
  text: string;
  source: { id: string; name: string; type: string };
  citation: {
    uri: string;
    title: string | null;
    indexed_at: string;
    doc_version: number;
  };
  lineage: {
    ingestion_run_id: string | null;
    embedding_model: string;
    embedding_provider: string;
    parser_version: string;
    chunker_strategy: string;
  };
  metadata: Record<string, unknown>;
}

export interface SearchResult {
  hits: SearchHit[];
  query_stats: {
    total_matches_before_filters: number;
    vector_matches: number;
    text_matches: number;
    duration_ms: number;
  };
}

export interface HealthResponse {
  status: string;
  version?: string;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string
  ) {
    super(`API error ${status}: ${detail}`);
    this.name = "ApiError";
  }
}

export class ApiClient {
  private token: string | null;

  constructor(token: string | null = null) {
    this.token = token;
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  private headers(): HeadersInit {
    const h: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (this.token) {
      h["Authorization"] = `Bearer ${this.token}`;
    }
    return h;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown
  ): Promise<T> {
    const res = await fetch(path, {
      method,
      headers: this.headers(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

    if (res.status === 204) {
      return undefined as T;
    }

    const data = await res.json().catch(() => ({ detail: res.statusText }));

    if (!res.ok) {
      const detail =
        typeof data?.detail === "string"
          ? data.detail
          : typeof data?.detail?.message === "string"
            ? data.detail.message
            : JSON.stringify(data?.detail ?? data);
      throw new ApiError(res.status, detail);
    }

    return data as T;
  }

  // Health
  async health(): Promise<HealthResponse> {
    return this.request<HealthResponse>("GET", "/health");
  }

  // Sources
  async listSources(params?: {
    source_type?: SourceType;
    status?: SourceStatus;
  }): Promise<Source[]> {
    const qs = new URLSearchParams();
    if (params?.source_type) qs.set("source_type", params.source_type);
    if (params?.status) qs.set("status", params.status);
    const suffix = qs.toString() ? `?${qs}` : "";
    return this.request<Source[]>("GET", `/api/v1/sources${suffix}`);
  }

  async getSource(id: string): Promise<Source> {
    return this.request<Source>("GET", `/api/v1/sources/${id}`);
  }

  async createSource(payload: SourceCreate): Promise<Source> {
    return this.request<Source>("POST", "/api/v1/sources", payload);
  }

  async deleteSource(id: string): Promise<void> {
    return this.request<void>("DELETE", `/api/v1/sources/${id}`);
  }

  async triggerSync(id: string): Promise<{ run_id: string }> {
    return this.request<{ run_id: string }>(
      "POST",
      `/api/v1/sources/${id}/sync`
    );
  }

  async sourceStats(id: string): Promise<SourceStats> {
    return this.request<SourceStats>("GET", `/api/v1/sources/${id}/stats`);
  }

  // Ingestion runs
  async listIngestionRuns(params?: {
    source_id?: string;
    status?: IngestionRunStatus;
    limit?: number;
  }): Promise<IngestionRun[]> {
    const qs = new URLSearchParams();
    if (params?.source_id) qs.set("source_id", params.source_id);
    if (params?.status) qs.set("status", params.status);
    if (params?.limit) qs.set("limit", String(params.limit));
    const suffix = qs.toString() ? `?${qs}` : "";
    return this.request<IngestionRun[]>("GET", `/api/v1/ingestion-runs${suffix}`);
  }

  // Tokens
  async listTokens(): Promise<ApiToken[]> {
    return this.request<ApiToken[]>("GET", "/api/v1/tokens");
  }

  async createToken(payload: TokenCreateRequest): Promise<TokenCreateResponse> {
    return this.request<TokenCreateResponse>("POST", "/api/v1/tokens", payload);
  }

  async deleteToken(id: string): Promise<void> {
    return this.request<void>("DELETE", `/api/v1/tokens/${id}`);
  }

  // Search
  async search(payload: SearchRequest): Promise<SearchResult> {
    return this.request<SearchResult>("POST", "/api/v1/search", payload);
  }
}
