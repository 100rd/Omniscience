/**
 * TypeScript types for the Omniscience API — mirrors the OpenAPI spec and
 * Python Pydantic schemas defined in omniscience-core and omniscience-retrieval.
 *
 * @module @omniscience/sdk/types
 */

// ---------------------------------------------------------------------------
// Shared enums
// ---------------------------------------------------------------------------

export type SourceType =
  | "git"
  | "fs"
  | "confluence"
  | "notion"
  | "jira"
  | "slack"
  | "github_issues"
  | string;

export type SourceStatus = "active" | "paused" | "error";

export type IngestionRunStatus = "running" | "ok" | "partial" | "error";

export type TokenScope =
  | "search"
  | "sources:read"
  | "sources:write"
  | "admin";

export type RetrievalStrategy = "hybrid" | "keyword" | "structural" | "auto";

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

export interface SearchParams {
  /** Natural-language or keyword query. */
  query: string;
  /** Max chunks to return. Default: 10. Range: 1–500. */
  top_k?: number;
  /** Restrict results to these source names. */
  sources?: string[];
  /** Restrict results to these source types (e.g. "git", "fs"). */
  types?: string[];
  /** Only return chunks whose indexed_at is within this age (seconds). */
  max_age_seconds?: number;
  /** Metadata key/value filters (e.g. { language: "python" }). */
  filters?: Record<string, unknown>;
  /** Include tombstoned (removed) documents. Default: false. */
  include_tombstoned?: boolean;
  /** Retrieval strategy. Default: "hybrid". */
  retrieval_strategy?: RetrievalStrategy;
}

export interface Citation {
  /** Full URI to the origin document or line range. */
  uri: string;
  title: string | null;
  indexed_at: string; // ISO-8601
  doc_version: number;
}

export interface ChunkLineage {
  ingestion_run_id: string | null; // UUID
  embedding_model: string;
  embedding_provider: string;
  parser_version: string;
  chunker_strategy: string;
}

export interface SourceInfo {
  id: string; // UUID
  name: string;
  type: SourceType;
}

export interface SearchHit {
  chunk_id: string; // UUID
  document_id: string; // UUID
  score: number;
  text: string;
  source: SourceInfo;
  citation: Citation;
  lineage: ChunkLineage;
  metadata: Record<string, unknown>;
}

export interface QueryStats {
  total_matches_before_filters: number;
  vector_matches: number;
  text_matches: number;
  duration_ms: number;
}

export interface SearchResult {
  hits: SearchHit[];
  query_stats: QueryStats;
}

// ---------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------

export interface Source {
  id: string; // UUID
  type: SourceType;
  name: string;
  /** Type-specific configuration (connector settings, repo URL, etc.). */
  config: Record<string, unknown>;
  /** Pointer to env var / vault — never the secret itself. */
  secrets_ref: string | null;
  status: SourceStatus;
  last_sync_at: string | null; // ISO-8601
  last_error: string | null;
  last_error_at: string | null; // ISO-8601
  freshness_sla_seconds: number | null;
  tenant_id: string | null; // UUID
  created_at: string; // ISO-8601
  updated_at: string; // ISO-8601
}

export interface CreateSourceParams {
  type: SourceType;
  name: string;
  config?: Record<string, unknown>;
  secrets_ref?: string;
  status?: SourceStatus;
  freshness_sla_seconds?: number;
  tenant_id?: string; // UUID
}

export interface UpdateSourceParams {
  name?: string;
  config?: Record<string, unknown>;
  secrets_ref?: string;
  status?: SourceStatus;
  freshness_sla_seconds?: number;
}

export interface SourceStats {
  source_id: string; // UUID
  total_documents: number;
  active_documents: number;
  total_chunks: number;
  last_sync_at: string | null; // ISO-8601
  last_run_status: IngestionRunStatus | null;
}

export interface SyncResponse {
  run_id: string; // UUID
}

// ---------------------------------------------------------------------------
// Documents
// ---------------------------------------------------------------------------

export interface Document {
  id: string; // UUID
  source_id: string; // UUID
  external_id: string;
  uri: string;
  title: string | null;
  content_hash: string;
  doc_version: number;
  metadata: Record<string, unknown>;
  indexed_at: string; // ISO-8601
  tombstoned_at: string | null; // ISO-8601
}

export interface Chunk {
  id: string; // UUID
  document_id: string; // UUID
  ord: number;
  text: string;
  symbol: string | null;
  ingestion_run_id: string | null; // UUID
  embedding_model: string;
  embedding_provider: string;
  parser_version: string;
  chunker_strategy: string;
  metadata: Record<string, unknown>;
}

/** Document returned by GET /documents/:id — includes all chunks. */
export interface DocumentWithChunks {
  document: Document;
  chunks: Chunk[];
}

// ---------------------------------------------------------------------------
// Ingestion runs
// ---------------------------------------------------------------------------

export interface IngestionRun {
  id: string; // UUID
  source_id: string; // UUID
  started_at: string; // ISO-8601
  finished_at: string | null; // ISO-8601
  status: IngestionRunStatus;
  docs_new: number;
  docs_updated: number;
  docs_removed: number;
  errors: Record<string, unknown>;
}

export interface ListRunsParams {
  source_id?: string; // UUID
  status?: IngestionRunStatus;
  limit?: number;
}

// ---------------------------------------------------------------------------
// Tokens
// ---------------------------------------------------------------------------

export interface ApiToken {
  id: string; // UUID
  name: string;
  token_prefix: string;
  scopes: TokenScope[];
  workspace_id: string | null; // UUID
  created_at: string; // ISO-8601
  expires_at: string | null; // ISO-8601
  last_used_at: string | null; // ISO-8601
  is_active: boolean;
}

export interface CreateTokenParams {
  name: string;
  scopes: TokenScope[];
  expires_at?: string; // ISO-8601
}

export interface TokenCreateResponse {
  token: ApiToken;
  /** The plaintext secret — shown exactly once; cannot be recovered. */
  secret: string;
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: "ok";
  version: string;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export interface OmniscienceErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

// ---------------------------------------------------------------------------
// MCP-specific types
// ---------------------------------------------------------------------------

export interface SearchOptions {
  top_k?: number;
  sources?: string[];
  types?: string[];
  max_age_seconds?: number;
  filters?: Record<string, unknown>;
  include_tombstoned?: boolean;
  retrieval_strategy?: RetrievalStrategy;
}

export type McpTransport = "stdio" | "http";

export interface McpClientOptions {
  transport: McpTransport;
  /** Required when transport is "http". Base URL of the Omniscience server. */
  url?: string;
  /** API token. Required for http; for stdio set OMNISCIENCE_TOKEN env var. */
  token?: string;
}
