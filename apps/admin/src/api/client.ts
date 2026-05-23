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

/*
 * Wire format for `GET /api/v1/stats/sources` (Issue #111).
 *
 * Mirrors the Pydantic `SourceStatsRow` / `SourcesStatsResponse` defined in
 * `packages/core/src/omniscience_core/stats/models.py`. Notes on fields:
 *   - `last_sync_at` is ISO-8601 or null when the source has never synced.
 *   - `age_seconds` uses a `1e15` sentinel to mean "never synced". Renderers
 *     should special-case that value rather than displaying "11574 days".
 *   - `freshness_sla_seconds` is null when the source has no SLA configured.
 *   - `is_stale` is computed server-side: true when age >= SLA.
 */
export interface SourceStatsRow {
  id: string;
  name: string;
  type: string;
  status: string;
  documents: number;
  chunks: number;
  entities: number;
  last_sync_at: string | null;
  freshness_sla_seconds: number | null;
  age_seconds: number;
  is_stale: boolean;
}

export interface SourcesStatsResponse {
  sources: SourceStatsRow[];
  total: number;
}

/*
 * Wire format for `GET /api/v1/stats/overview` (Issue #111).
 *
 * Mirrors the Pydantic `StatsOverview` defined in
 * `packages/core/src/omniscience_core/stats/models.py`. All counts are
 * scoped to the caller's workspace.
 */
export interface StatsOverview {
  sources: number;
  active_sources: number;
  documents: number;
  active_documents: number;
  tombstoned_documents: number;
  chunks: number;
  entities: number;
  edges_by_type: Record<string, number>;
  total_indexed_bytes: number;
  documents_added_24h: number;
  documents_updated_24h: number;
  documents_tombstoned_24h: number;
}

/*
 * Wire format for `GET /api/v1/stats/entities-by-kind` (Issue #111).
 * Mirrors `KindHistogramEntry` / `EntitiesByKindResponse`.
 */
export interface KindHistogramEntry {
  kind: string;
  count: number;
}

export interface EntitiesByKindResponse {
  entries: KindHistogramEntry[];
  total: number;
}

/*
 * Wire format for `GET /api/v1/stats/edges-by-type` (Issue #111).
 * Mirrors `EdgeTypeHistogramEntry` / `EdgesByTypeResponse`.
 */
export interface EdgeTypeHistogramEntry {
  edge_type: string;
  count: number;
}

export interface EdgesByTypeResponse {
  entries: EdgeTypeHistogramEntry[];
  total: number;
}

/*
 * Wire format for `GET /api/v1/stats/clients` (Issue #113).
 *
 * Mirrors the Pydantic `ClientsStatsResponse`. `mcp_sessions_active` and
 * `mcp_sessions_last_hour` are process-global; `tokens` is workspace-scoped.
 * `last_seen_at` is null when the token has been issued but has not made a
 * request since process start.
 */
export interface TokenClientStats {
  token_id: string;
  name: string;
  last_seen_at: string | null;
  requests_last_15m: number;
  requests_last_24h: number;
}

export interface ToolUsageEntry {
  tool_name: string;
  invocations_last_hour: number;
}

export interface ClientsStatsResponse {
  mcp_sessions_active: number;
  mcp_sessions_last_hour: number;
  tokens: TokenClientStats[];
  top_tools_last_hour: ToolUsageEntry[];
}

/*
 * Wire format for `GET /api/v1/admin/retention/status` (Issue #136, ADR-0009 §8).
 *
 * Mirrors the Pydantic `RetentionStatus`. All counts are scoped to the
 * caller's workspace; the response carries IDs and counts only — no
 * entity bodies, no chunk text (ACL invariant from ADR-0009 §Consequences-
 * security).
 *
 * `last_run_at` is null when the worker has not completed its first
 * tick since process start — the admin UI renders that as "never run"
 * rather than displaying a stale value.
 */
export interface RetentionStatusResponse {
  workspace_id: string;
  neo4j_hot: number;
  neo4j_warm: number;
  qdrant_hot: number;
  qdrant_warm: number;
  last_run_at: string | null;
  lag_seconds: number;
  dry_run: boolean;
}

/*
 * Wire format for the dry-run `GET /api/v1/admin/retention/report`
 * (Issue #135, ADR-0009 §3). Returned when an operator wants to
 * preview what the next worker tick would evict without mutating any
 * store. Sample size is bounded by `Settings.retention_sample_size`
 * (default 20).
 */
export interface RetentionSampleEntry {
  id: string | null;
  valid_from: string | null;
  recorded_at: string | null;
}

export interface RetentionReportResponse {
  workspace_id: string;
  dry_run: boolean;
  eligible_hot_to_warm_entity_states: number;
  eligible_hot_to_warm_edges: number;
  eligible_hot_to_warm_chunks: number;
  eligible_warm_to_archive_entity_snapshots: number;
  eligible_warm_to_archive_dates: string[];
  sampled_eligible: RetentionSampleEntry[];
  oldest_eligible_recorded_at: string | null;
  lag_seconds: number;
}

/*
 * Wire format for `POST /api/v1/admin/retention/run-now` (Issue #136).
 * 202 Accepted with a server-generated `run_id`; the run executes
 * synchronously inside the request handler and is scoped to the
 * caller's workspace by the structural ACL invariant on the worker.
 */
export interface RetentionRunNowResponse {
  run_id: string;
  workspace_id: string;
  started_at: string;
  finished_at: string;
  duration_seconds: number;
  dry_run: boolean;
  lag_seconds: number;
}

/*
 * Wire format for `GET /api/v1/incidents/{id}/timeline` (Issue #235).
 *
 * Mirrors the Pydantic `IncidentTimelineResponse` /  `TimelineEvent`
 * defined in `apps/server/src/omniscience_server/incident_timeline.py`.
 * Each event represents one bitemporal state change for an entity in
 * the alert's blast radius. `change_kind` is "created" when the row's
 * `valid_from` projects forward, or "ended" when its `valid_to` does.
 */
export type TimelineChangeKind = "created" | "ended";

export interface TimelineEvent {
  ts: string;
  entity_id: string;
  entity_type: string;
  change_kind: TimelineChangeKind;
  before_state_summary: string | null;
  after_state_summary: string | null;
  source: string;
}

export interface IncidentTimelineResponse {
  alert_id: string;
  events: TimelineEvent[];
  effective_as_of: string;
  window_from: string | null;
  window_to: string | null;
  entity_types_filter: string[] | null;
  truncated: boolean;
}

export interface IncidentTimelineQuery {
  from?: string;
  to?: string;
  entity_types?: string[];
  as_of?: string;
  max_depth?: number;
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
    body?: unknown,
    signal?: AbortSignal
  ): Promise<T> {
    const res = await fetch(path, {
      method,
      headers: this.headers(),
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
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

  // Stats (Issue #111)
  async statsSources(signal?: AbortSignal): Promise<SourcesStatsResponse> {
    return this.request<SourcesStatsResponse>(
      "GET",
      "/api/v1/stats/sources",
      undefined,
      signal
    );
  }

  async statsOverview(signal?: AbortSignal): Promise<StatsOverview> {
    return this.request<StatsOverview>(
      "GET",
      "/api/v1/stats/overview",
      undefined,
      signal
    );
  }

  async statsEntitiesByKind(
    signal?: AbortSignal
  ): Promise<EntitiesByKindResponse> {
    return this.request<EntitiesByKindResponse>(
      "GET",
      "/api/v1/stats/entities-by-kind",
      undefined,
      signal
    );
  }

  async statsEdgesByType(signal?: AbortSignal): Promise<EdgesByTypeResponse> {
    return this.request<EdgesByTypeResponse>(
      "GET",
      "/api/v1/stats/edges-by-type",
      undefined,
      signal
    );
  }

  // Stats (Issue #113)
  async statsClients(signal?: AbortSignal): Promise<ClientsStatsResponse> {
    return this.request<ClientsStatsResponse>(
      "GET",
      "/api/v1/stats/clients",
      undefined,
      signal
    );
  }

  // Retention admin (Issue #136, ADR-0009)
  async retentionStatus(
    signal?: AbortSignal
  ): Promise<RetentionStatusResponse> {
    return this.request<RetentionStatusResponse>(
      "GET",
      "/api/v1/admin/retention/status",
      undefined,
      signal
    );
  }

  async retentionReport(
    signal?: AbortSignal
  ): Promise<RetentionReportResponse> {
    return this.request<RetentionReportResponse>(
      "GET",
      "/api/v1/admin/retention/report",
      undefined,
      signal
    );
  }

  async retentionRunNow(): Promise<RetentionRunNowResponse> {
    return this.request<RetentionRunNowResponse>(
      "POST",
      "/api/v1/admin/retention/run-now"
    );
  }

  // Incident timeline (Issue #235)
  async incidentTimeline(
    alertId: string,
    query: IncidentTimelineQuery = {},
    signal?: AbortSignal
  ): Promise<IncidentTimelineResponse> {
    const qs = new URLSearchParams();
    if (query.from) qs.set("from", query.from);
    if (query.to) qs.set("to", query.to);
    if (query.as_of) qs.set("as_of", query.as_of);
    if (query.max_depth != null) qs.set("max_depth", String(query.max_depth));
    if (query.entity_types) {
      for (const t of query.entity_types) qs.append("entity_types", t);
    }
    const suffix = qs.toString() ? `?${qs}` : "";
    return this.request<IncidentTimelineResponse>(
      "GET",
      `/api/v1/incidents/${encodeURIComponent(alertId)}/timeline${suffix}`,
      undefined,
      signal
    );
  }

  async incidentTimelineMermaid(
    alertId: string,
    query: IncidentTimelineQuery = {},
    signal?: AbortSignal
  ): Promise<string> {
    const qs = new URLSearchParams();
    qs.set("format", "mermaid");
    if (query.from) qs.set("from", query.from);
    if (query.to) qs.set("to", query.to);
    if (query.as_of) qs.set("as_of", query.as_of);
    if (query.max_depth != null) qs.set("max_depth", String(query.max_depth));
    if (query.entity_types) {
      for (const t of query.entity_types) qs.append("entity_types", t);
    }
    const path = `/api/v1/incidents/${encodeURIComponent(alertId)}/timeline?${qs}`;
    const res = await fetch(path, {
      method: "GET",
      headers: this.token ? { Authorization: `Bearer ${this.token}` } : undefined,
      signal,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ detail: res.statusText }));
      const detail =
        typeof data?.detail === "string"
          ? data.detail
          : typeof data?.detail?.message === "string"
            ? data.detail.message
            : JSON.stringify(data?.detail ?? data);
      throw new ApiError(res.status, detail);
    }
    return await res.text();
  }
}
