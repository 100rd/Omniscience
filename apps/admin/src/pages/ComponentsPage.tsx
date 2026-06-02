/*
 * ComponentsPage — System Status / Components admin page.
 *
 * Fetches GET /api/v1/admin/components every 10 s and renders one card
 * per infrastructure component (postgres, neo4j, qdrant, nats, embedding)
 * with a status pill and its metrics.
 *
 * Auto-refresh uses an AbortController to cancel the in-flight request on
 * unmount — the same pattern used in FreshnessPanel.tsx.
 *
 * On 403 the server's detail message is surfaced verbatim.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiClient,
  ApiError,
  ComponentStatus,
  ComponentsResponse,
  NatsStreamMetrics,
} from "../api/client";
import { useTokenContext } from "../context/TokenContext";

const REFRESH_MS = 10_000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format byte counts for display (KB / MB / GB). */
function formatBytes(n: number): string {
  if (n < 1_024) return `${n} B`;
  if (n < 1_048_576) return `${(n / 1_024).toFixed(1)} KB`;
  if (n < 1_073_741_824) return `${(n / 1_048_576).toFixed(1)} MB`;
  return `${(n / 1_073_741_824).toFixed(2)} GB`;
}

// ---------------------------------------------------------------------------
// Status pill
// ---------------------------------------------------------------------------

interface StatusPillProps {
  status: ComponentStatus;
}

function StatusPill({ status }: StatusPillProps) {
  const cls =
    status === "ok"
      ? "bg-success-bg text-success-fg"
      : status === "degraded"
        ? "bg-warning-bg text-warning-fg"
        : "bg-danger-bg text-danger-fg";
  const label = status === "ok" ? "OK" : status === "degraded" ? "Degraded" : "Error";
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-semibold uppercase tracking-wide ${cls}`}
      aria-label={`Status: ${label}`}
    >
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Component cards
// ---------------------------------------------------------------------------

interface CardProps {
  title: string;
  status: ComponentStatus;
  error: string | null;
  children?: React.ReactNode;
}

function ComponentCard({ title, status, error, children }: CardProps) {
  return (
    <section
      className="rounded-lg border border-border bg-elevation-1 p-5 flex flex-col gap-3"
      aria-label={`${title} component status`}
    >
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-base font-semibold text-text">{title}</h2>
        <StatusPill status={status} />
      </div>
      {error && (
        <p className="text-xs text-danger-fg bg-danger-bg rounded px-2 py-1 break-all" role="alert">
          {error}
        </p>
      )}
      {children}
    </section>
  );
}

interface MetricRowProps {
  label: string;
  value: string | number;
}

function MetricRow({ label, value }: MetricRowProps) {
  return (
    <div className="flex items-center justify-between text-sm gap-4">
      <span className="text-text-secondary">{label}</span>
      <span className="text-text font-mono text-xs">{value}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-component sections
// ---------------------------------------------------------------------------

function PostgresCard({
  component,
}: {
  component: ComponentsResponse["postgres"];
}) {
  return (
    <ComponentCard title="PostgreSQL" status={component.status} error={component.error}>
      {component.metrics && (
        <dl className="flex flex-col gap-1">
          <MetricRow label="DB size" value={formatBytes(component.metrics.size_bytes)} />
          {Object.entries(component.metrics.table_counts).map(([table, count]) => (
            <MetricRow key={table} label={table} value={count.toLocaleString()} />
          ))}
        </dl>
      )}
    </ComponentCard>
  );
}

function Neo4jCard({ component }: { component: ComponentsResponse["neo4j"] }) {
  return (
    <ComponentCard title="Neo4j" status={component.status} error={component.error}>
      {component.metrics && (
        <dl className="flex flex-col gap-1">
          <MetricRow
            label="Total nodes"
            value={component.metrics.total_nodes.toLocaleString()}
          />
          <MetricRow
            label="Total relationships"
            value={component.metrics.total_relationships.toLocaleString()}
          />
          <MetricRow
            label="Entity nodes"
            value={component.metrics.entity_nodes.toLocaleString()}
          />
          <MetricRow
            label="EntityState nodes"
            value={component.metrics.entity_state_nodes.toLocaleString()}
          />
        </dl>
      )}
    </ComponentCard>
  );
}

function QdrantCard({ component }: { component: ComponentsResponse["qdrant"] }) {
  return (
    <ComponentCard title="Qdrant" status={component.status} error={component.error}>
      {component.metrics && (
        <dl className="flex flex-col gap-1">
          <MetricRow label="Collection" value={component.metrics.collection_name} />
          <MetricRow
            label="Vectors"
            value={component.metrics.vectors_count.toLocaleString()}
          />
          <MetricRow
            label="Points"
            value={component.metrics.points_count.toLocaleString()}
          />
          <MetricRow label="Status" value={component.metrics.collection_status} />
        </dl>
      )}
    </ComponentCard>
  );
}

function NatsStreamBlock({ stream }: { stream: NatsStreamMetrics }) {
  return (
    <div className="rounded border border-border bg-elevation-2 p-3 flex flex-col gap-1">
      <p className="text-xs font-semibold text-text mb-1">{stream.name}</p>
      <MetricRow label="Messages" value={stream.messages.toLocaleString()} />
      <MetricRow label="Bytes" value={formatBytes(stream.bytes)} />
      {stream.consumers.length > 0 && (
        <div className="mt-2 flex flex-col gap-1">
          {stream.consumers.map((c) => (
            <div
              key={c.name}
              className="pl-2 border-l-2 border-border flex flex-col gap-0.5"
              aria-label={`Consumer ${c.name}`}
            >
              <p className="text-xs text-text-secondary font-mono truncate">{c.name}</p>
              <MetricRow label="Queue depth (pending)" value={c.num_pending.toLocaleString()} />
              <MetricRow label="Ack pending" value={c.num_ack_pending.toLocaleString()} />
              <MetricRow label="Redelivered" value={c.num_redelivered.toLocaleString()} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function NatsCard({ component }: { component: ComponentsResponse["nats"] }) {
  return (
    <ComponentCard title="NATS JetStream" status={component.status} error={component.error}>
      {component.metrics && (
        <div className="flex flex-col gap-2">
          {component.metrics.streams.length === 0 ? (
            <p className="text-xs text-text-muted">No streams found.</p>
          ) : (
            component.metrics.streams.map((s) => <NatsStreamBlock key={s.name} stream={s} />)
          )}
        </div>
      )}
    </ComponentCard>
  );
}

function EmbeddingCard({ component }: { component: ComponentsResponse["embedding"] }) {
  return (
    <ComponentCard title="Embedding" status={component.status} error={component.error}>
      {component.metrics && (
        <dl className="flex flex-col gap-1">
          <MetricRow label="Provider" value={component.metrics.provider} />
          <MetricRow label="Model" value={component.metrics.model} />
          <MetricRow label="Dimensions" value={component.metrics.dim.toLocaleString()} />
        </dl>
      )}
    </ComponentCard>
  );
}

// ---------------------------------------------------------------------------
// Overall status banner
// ---------------------------------------------------------------------------

function OverallStatusBanner({
  status,
  version,
}: {
  status: ComponentStatus;
  version: string;
}) {
  const cls =
    status === "ok"
      ? "bg-success-bg text-success-fg border-success-fg"
      : status === "degraded"
        ? "bg-warning-bg text-warning-fg border-warning-fg"
        : "bg-danger-bg text-danger-fg border-danger-fg";
  const label =
    status === "ok"
      ? "All systems operational"
      : status === "degraded"
        ? "Partial degradation"
        : "One or more components are failing";
  return (
    <div
      className={`rounded-lg border px-4 py-3 flex items-center justify-between ${cls}`}
      role="status"
      aria-live="polite"
    >
      <span className="font-semibold text-sm">{label}</span>
      <span className="text-xs font-mono opacity-70">v{version}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function ComponentsPage() {
  const ctx = useTokenContext();
  const client: ApiClient = ctx.client;

  const [data, setData] = useState<ComponentsResponse | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const inFlightRef = useRef<AbortController | null>(null);

  const fetchOnce = useCallback(async () => {
    inFlightRef.current?.abort();
    const controller = new AbortController();
    inFlightRef.current = controller;
    setRefreshing(true);
    try {
      const resp = await client.getComponents(controller.signal);
      if (controller.signal.aborted) return;
      setData(resp);
      setError(null);
    } catch (e) {
      if (controller.signal.aborted) return;
      if (e instanceof DOMException && e.name === "AbortError") return;
      if (e instanceof Error) setError(e);
      else setError(new Error(String(e)));
    } finally {
      if (inFlightRef.current === controller) inFlightRef.current = null;
      setRefreshing(false);
    }
  }, [client]);

  useEffect(() => {
    let cancelled = false;
    void fetchOnce();
    const id = window.setInterval(() => {
      if (cancelled) return;
      void fetchOnce();
    }, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      inFlightRef.current?.abort();
      inFlightRef.current = null;
    };
  }, [fetchOnce]);

  // 403 error — surface server reason
  if (error instanceof ApiError && error.status === 403) {
    return (
      <div>
        <h1 className="text-2xl font-semibold text-text mb-8">System Status</h1>
        <div
          className="rounded-lg border border-danger-fg bg-danger-bg text-danger-fg px-4 py-3 text-sm"
          role="alert"
        >
          <strong>Access denied:</strong> {error.detail}
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-2xl font-semibold text-text">System Status</h1>
        <span
          className={`text-xs text-text-muted transition-opacity ${refreshing ? "opacity-100" : "opacity-0"}`}
          aria-live="polite"
          aria-label={refreshing ? "Refreshing" : ""}
        >
          Refreshing…
        </span>
      </div>

      {/* Generic fetch error (not 403) */}
      {error && !(error instanceof ApiError && error.status === 403) && (
        <div
          className="mb-6 rounded-lg border border-danger-fg bg-danger-bg text-danger-fg px-4 py-3 text-sm"
          role="alert"
        >
          {error instanceof ApiError ? error.detail : error.message}
        </div>
      )}

      {/* Loading skeleton */}
      {!data && !error && (
        <p className="text-text-secondary text-sm">Loading component status…</p>
      )}

      {data && (
        <div className="flex flex-col gap-6">
          <OverallStatusBanner status={data.status} version={data.version} />
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            <PostgresCard component={data.postgres} />
            <Neo4jCard component={data.neo4j} />
            <QdrantCard component={data.qdrant} />
            <NatsCard component={data.nats} />
            <EmbeddingCard component={data.embedding} />
          </div>
        </div>
      )}
    </div>
  );
}
