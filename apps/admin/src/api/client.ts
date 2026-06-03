// API client using httpOnly cookie auth + CSRF double-submit pattern.
//
// Auth strategy:
//   - All requests include `credentials: 'include'` so the browser sends the
//     httpOnly `omniscience_admin_session` cookie automatically.
//   - For mutating methods (POST/PUT/PATCH/DELETE) the client reads the
//     non-httpOnly `csrf_token` cookie and forwards it as `X-CSRF-Token`.
//   - No Authorization header is sourced from localStorage or any JS storage.
//   - The `createSession` / `deleteSession` methods are the only callers that
//     touch session lifecycle; all other methods are auth-agnostic.

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

export interface SourceUpdate {
  config?: Record<string, unknown>;
  secrets_ref?: string | null;
  status?: SourceStatus;
  freshness_sla_seconds?: number | null;
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
  /** Optional UUID of the workspace this token is scoped to.
   *  Required for stats/retention endpoints.
   *  Default workspace: 00000000-0000-0000-0000-000000000001 */
  workspace_id?: string;
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

export interface RelatedEntitiesResponse {
  seed: {
    name: string;
    kind: string;
    source: string;
    chunk_text: string | null;
  };
  related: Array<{
    name: string;
    kind: string;
    source: string;
    depth: number;
    edge_type: string;
  }>;
  edges: Array<{
    from: string;
    to: string;
    type: string;
  }>;
}

export interface HealthResponse {
  status: string;
  version?: string;
}

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

export interface KindHistogramEntry {
  kind: string;
  count: number;
}

export interface EntitiesByKindResponse {
  entries: KindHistogramEntry[];
  total: number;
}

export interface EdgeTypeHistogramEntry {
  edge_type: string;
  count: number;
}

export interface EdgesByTypeResponse {
  entries: EdgeTypeHistogramEntry[];
  total: number;
}

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

export interface RetentionRunNowResponse {
  run_id: string;
  workspace_id: string;
  started_at: string;
  finished_at: string;
  duration_seconds: number;
  dry_run: boolean;
  lag_seconds: number;
}

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

export interface ReplayEnvelope {
  tool_name: string;
  at_time: string;
  state_fingerprint: string;
  fingerprint_algorithm: string;
  original_state_fingerprint: string | null;
  fingerprint_match: boolean | null;
  audit_log_id: string | null;
  response: Record<string, unknown>;
}

export interface ReplayInlineRequest {
  at_time: string;
  query: {
    tool_name: string;
    arguments: Record<string, unknown>;
  };
}

export interface ReplayByAuditIdRequest {
  audit_log_id: string;
}

export type ReplayRequest = ReplayInlineRequest | ReplayByAuditIdRequest;

export interface Workspace {
  id: string;
  name: string;
  metadata: Record<string, unknown>;
}


export type ComponentStatus = "ok" | "degraded" | "error";

export interface PostgresMetrics {
  size_bytes: number;
  table_counts: Record<string, number>;
}

export interface PostgresComponent {
  status: ComponentStatus;
  metrics: PostgresMetrics | null;
  error: string | null;
}

export interface Neo4jMetrics {
  total_nodes: number;
  total_relationships: number;
  entity_nodes: number;
  entity_state_nodes: number;
}

export interface Neo4jComponent {
  status: ComponentStatus;
  metrics: Neo4jMetrics | null;
  error: string | null;
}

export interface QdrantMetrics {
  collection_name: string;
  vectors_count: number;
  points_count: number;
  collection_status: string;
}

export interface QdrantComponent {
  status: ComponentStatus;
  metrics: QdrantMetrics | null;
  error: string | null;
}

export interface NatsConsumerMetrics {
  name: string;
  num_pending: number;
  num_ack_pending: number;
  num_redelivered: number;
}

export interface NatsStreamMetrics {
  name: string;
  messages: number;
  bytes: number;
  consumers: NatsConsumerMetrics[];
}

export interface NatsMetrics {
  streams: NatsStreamMetrics[];
}

export interface NatsComponent {
  status: ComponentStatus;
  metrics: NatsMetrics | null;
  error: string | null;
}

export interface EmbeddingMetrics {
  provider: string;
  model: string;
  dim: number;
}

export interface EmbeddingComponent {
  status: ComponentStatus;
  metrics: EmbeddingMetrics | null;
  error: string | null;
}

export interface ComponentsResponse {
  status: ComponentStatus;
  version: string;
  postgres: PostgresComponent;
  neo4j: Neo4jComponent;
  qdrant: QdrantComponent;
  nats: NatsComponent;
  embedding: EmbeddingComponent;
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

const _MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** Read the csrf_token cookie value (non-httpOnly, set by the backend). */
function _readCsrfCookie(): string | null {
  const match = document.cookie
    .split(";")
    .map((c) => c.trim())
    .find((c) => c.startsWith("csrf_token="));
  return match ? decodeURIComponent(match.slice("csrf_token=".length)) : null;
}

export class ApiClient {
  constructor() {}

  private headers(method: string): HeadersInit {
    const h: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (_MUTATING_METHODS.has(method.toUpperCase())) {
      const csrf = _readCsrfCookie();
      if (csrf) {
        h["X-CSRF-Token"] = csrf;
      }
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
      headers: this.headers(method),
      credentials: "include",
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

  // Session management (admin SPA auth)

  /** POST /api/v1/admin/session — validate token, receive httpOnly cookie. */
  async createSession(token: string): Promise<void> {
    return this.request<void>("POST", "/api/v1/admin/session", { token });
  }

  /** DELETE /api/v1/admin/session — clear session + CSRF cookies. */
  async deleteSession(): Promise<void> {
    return this.request<void>("DELETE", "/api/v1/admin/session");
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

  async updateSource(id: string, patch: SourceUpdate): Promise<Source> {
    return this.request<Source>("PATCH", `/api/v1/sources/${id}`, patch);
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

  async getRelatedEntities(
    name: string,
    params: { depth?: number; as_of?: string } = {}
  ): Promise<RelatedEntitiesResponse> {
    const qs = new URLSearchParams();
    if (params.depth) qs.set("max_depth", String(params.depth));
    if (params.as_of) qs.set("as_of", params.as_of);
    const suffix = qs.toString() ? `?${qs}` : "";
    return this.request<RelatedEntitiesResponse>(
      "GET",
      `/api/v1/entities/${encodeURIComponent(name)}/related${suffix}`
    );
  }

  // Workspace
  async getWorkspace(): Promise<Workspace> {
    return this.request<Workspace>("GET", "/api/v1/workspace");
  }

  async updateWorkspace(metadata: Record<string, unknown>): Promise<Workspace> {
    return this.request<Workspace>("PATCH", "/api/v1/workspace", { metadata });
  }

  // Stats
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

  // Clients
  async statsClients(signal?: AbortSignal): Promise<ClientsStatsResponse> {
    return this.request<ClientsStatsResponse>(
      "GET",
      "/api/v1/stats/clients",
      undefined,
      signal
    );
  }

  // Retention
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

  // Incident timeline
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
    const h: Record<string, string> = {};
    const csrf = _readCsrfCookie();
    if (csrf) h["X-CSRF-Token"] = csrf;
    const res = await fetch(path, {
      method: "GET",
      headers: Object.keys(h).length > 0 ? h : undefined,
      credentials: "include",
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

  // Replay
  async replay(payload: ReplayRequest): Promise<ReplayEnvelope> {
    return this.request<ReplayEnvelope>("POST", "/api/v1/replay", payload);
  }

  async replayByAuditId(auditLogId: string): Promise<ReplayEnvelope> {
    return this.request<ReplayEnvelope>(
      "GET",
      `/api/v1/replay/audit/${encodeURIComponent(auditLogId)}`
    );
  }

  // Components status
  async getComponents(signal?: AbortSignal): Promise<ComponentsResponse> {
    return this.request<ComponentsResponse>(
      "GET",
      "/api/v1/admin/components",
      undefined,
      signal
    );
  }

}
