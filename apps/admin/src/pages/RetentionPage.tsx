/*
 * RetentionPage — dedicated `/retention` deep-dive (Issue #136, ADR-0009 §8).
 *
 * Shows the four ADR-0009 §8 metrics with sparklines + current values,
 * a per-tenant tier table (one row in v1 — the caller's own workspace
 * — because the admin UI is workspace-scoped per the ACL invariant),
 * and the two action buttons:
 *
 *   - **Run now**       → POST /api/v1/admin/retention/run-now
 *   - **Dry run report** → GET  /api/v1/admin/retention/report
 *
 * The dry-run report is rendered inline on the page when triggered;
 * it shows the would-evict counts + sample rows. The page does NOT
 * surface entity bodies or chunk text — only IDs and counts (ADR-0009
 * §Consequences-security ACL invariant).
 *
 * Sparkline data
 * --------------
 * The /status endpoint returns point-in-time observations, not a
 * series. To populate the sparklines without scraping Prometheus
 * directly, the page maintains a small in-memory ring buffer (last
 * 30 samples = 15min at the 30s refresh cadence) — fed by every
 * successful poll. Sparkline cleared on workspace switch / page
 * unmount; matches FreshnessPanel's "current state, not historical
 * series" posture.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  ApiClient,
  RetentionReportResponse,
  RetentionRunNowResponse,
  RetentionStatusResponse,
} from "../api/client";
import { useTokenContext } from "../context/TokenContext";
import { usePanelFetch } from "../hooks/usePanelFetch";
import { Sparkline } from "../components/dashboard/Sparkline";

const REFRESH_MS = 30_000;
const SPARKLINE_HISTORY = 30;

const LAG_WARNING_SECONDS = 86_400;
const LAG_CRITICAL_SECONDS = 604_800;

type LagBucket = "ok" | "warning" | "critical" | "never";

function classifyLag(s: RetentionStatusResponse): LagBucket {
  if (s.last_run_at == null) return "never";
  if (s.lag_seconds > LAG_CRITICAL_SECONDS) return "critical";
  if (s.lag_seconds > LAG_WARNING_SECONDS) return "warning";
  return "ok";
}

function formatLag(seconds: number): string {
  if (seconds <= 0) return "0s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86_400)}d`;
}

function formatCount(n: number): string {
  return n.toLocaleString();
}

function formatTimestamp(s: string | null): string {
  if (s == null) return "never";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  return d.toLocaleString();
}

interface Props {
  client?: ApiClient;
  refreshMs?: number;
}

export function RetentionPage({
  client: clientOverride,
  refreshMs = REFRESH_MS,
}: Props = {}) {
  const ctx = useTokenContext();
  const client = clientOverride ?? ctx.client;

  const { data, error, refreshing, lastRefreshedAt, refresh } =
    usePanelFetch<RetentionStatusResponse>(
      (signal) => client.retentionStatus(signal),
      refreshMs
    );

  /* Sparkline ring buffers — one per metric. Re-allocated on workspace
   * switch (workspace_id change) so cross-tenant data does not leak
   * into the chart. */
  const seenWorkspaceRef = useRef<string | null>(null);
  const [hotHistory, setHotHistory] = useState<number[]>([]);
  const [warmHistory, setWarmHistory] = useState<number[]>([]);
  const [lagHistory, setLagHistory] = useState<number[]>([]);
  const [chunkHotHistory, setChunkHotHistory] = useState<number[]>([]);

  useEffect(() => {
    if (data == null) return;
    if (seenWorkspaceRef.current !== data.workspace_id) {
      seenWorkspaceRef.current = data.workspace_id;
      setHotHistory([data.neo4j_hot]);
      setWarmHistory([data.neo4j_warm]);
      setLagHistory([data.lag_seconds]);
      setChunkHotHistory([data.qdrant_hot]);
      return;
    }
    setHotHistory((prev) =>
      [...prev, data.neo4j_hot].slice(-SPARKLINE_HISTORY)
    );
    setWarmHistory((prev) =>
      [...prev, data.neo4j_warm].slice(-SPARKLINE_HISTORY)
    );
    setLagHistory((prev) =>
      [...prev, data.lag_seconds].slice(-SPARKLINE_HISTORY)
    );
    setChunkHotHistory((prev) =>
      [...prev, data.qdrant_hot].slice(-SPARKLINE_HISTORY)
    );
  }, [data]);

  /* Action state — Run-now and Dry-run report are independent
   * synchronous actions; each carries its own pending / success /
   * error UI affordances. */
  const [runNowState, setRunNowState] = useState<ActionState<RetentionRunNowResponse>>({
    status: "idle",
  });
  const [reportState, setReportState] = useState<ActionState<RetentionReportResponse>>({
    status: "idle",
  });

  const handleRunNow = useCallback(async () => {
    setRunNowState({ status: "pending" });
    try {
      const result = await client.retentionRunNow();
      setRunNowState({ status: "success", data: result });
      /* Refresh the status panel so the new last_run_at + lag values
       * appear without waiting for the next 30s tick. */
      refresh();
    } catch (e) {
      setRunNowState({ status: "error", error: toError(e) });
    }
  }, [client, refresh]);

  const handleDryRun = useCallback(async () => {
    setReportState({ status: "pending" });
    try {
      const result = await client.retentionReport();
      setReportState({ status: "success", data: result });
    } catch (e) {
      setReportState({ status: "error", error: toError(e) });
    }
  }, [client]);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-text">Retention</h1>
          <p className="text-sm text-text-muted mt-1">
            Hot / warm / archive eviction observability — ADR-0009 §8.
          </p>
        </div>
        <RefreshIndicator
          refreshing={refreshing}
          lastRefreshedAt={lastRefreshedAt}
          refreshMs={refreshMs}
        />
      </div>

      {error != null ? (
        <ErrorBanner error={error} />
      ) : data == null ? (
        <PageSkeleton />
      ) : (
        <div className="space-y-8">
          <MetricCards
            data={data}
            hotHistory={hotHistory}
            warmHistory={warmHistory}
            lagHistory={lagHistory}
            chunkHotHistory={chunkHotHistory}
          />

          <Section title="Per-tenant retention">
            <TenantTable data={data} />
          </Section>

          <Section title="Operator actions">
            <Actions
              dryRun={data.dry_run}
              runNowState={runNowState}
              reportState={reportState}
              onRunNow={handleRunNow}
              onDryRun={handleDryRun}
            />
          </Section>

          {reportState.status === "success" && (
            <Section title="Dry-run report">
              <DryRunReport data={reportState.data} />
            </Section>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface MetricCardsProps {
  data: RetentionStatusResponse;
  hotHistory: number[];
  warmHistory: number[];
  lagHistory: number[];
  chunkHotHistory: number[];
}

function MetricCards({
  data,
  hotHistory,
  warmHistory,
  lagHistory,
  chunkHotHistory,
}: MetricCardsProps) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
      <MetricCard
        title="Records (Neo4j hot)"
        currentValue={formatCount(data.neo4j_hot)}
        sparkline={hotHistory}
        caption="Hot-tier record count over the last 15 minutes"
        strokeVar="--chart-1"
      />
      <MetricCard
        title="Records (Neo4j warm)"
        currentValue={formatCount(data.neo4j_warm)}
        sparkline={warmHistory}
        caption="Warm-tier (snapshot) count over the last 15 minutes"
        strokeVar="--chart-2"
      />
      <MetricCard
        title="Lag SLO"
        currentValue={
          data.last_run_at == null ? "no runs yet" : formatLag(data.lag_seconds)
        }
        sparkline={lagHistory}
        caption="Lag in seconds over the last 15 minutes"
        strokeVar="--chart-3"
        emphasised={classifyLag(data) !== "ok" && data.last_run_at != null}
      />
      <MetricCard
        title="Chunks (Qdrant hot)"
        currentValue={formatCount(data.qdrant_hot)}
        sparkline={chunkHotHistory}
        caption="Qdrant hot-tier chunk count over the last 15 minutes"
        strokeVar="--chart-4"
      />
    </div>
  );
}

interface MetricCardProps {
  title: string;
  currentValue: string;
  sparkline: number[];
  caption: string;
  strokeVar: string;
  emphasised?: boolean;
}

function MetricCard({
  title,
  currentValue,
  sparkline,
  caption,
  strokeVar,
  emphasised = false,
}: MetricCardProps) {
  return (
    <section
      aria-label={title}
      className={`bg-elevation-1 rounded-xl border ${
        emphasised ? "border-warning-border" : "border-border"
      } shadow-sm p-4 flex flex-col gap-3`}
    >
      <div className="flex items-start justify-between gap-2">
        <h2 className="text-xs text-text-muted uppercase tracking-wide">
          {title}
        </h2>
        <Sparkline
          values={sparkline}
          caption={caption}
          width={80}
          height={24}
          strokeVar={strokeVar}
        />
      </div>
      <p
        className={`text-2xl font-semibold tabular-nums ${
          emphasised ? "text-warning-fg" : "text-text"
        }`}
      >
        {currentValue}
      </p>
    </section>
  );
}

function TenantTable({ data }: { data: RetentionStatusResponse }) {
  /* v1 admin UI is workspace-scoped per the ACL invariant — the
   * /status endpoint returns the caller's workspace only. The table
   * therefore renders one row in v1; we keep the table shape so a
   * future cross-workspace admin token (out-of-scope for #136) can
   * extend rendering without restructuring the component. */
  const lagBucket = classifyLag(data);
  const lagSpec = LAG_PILL_SPECS[lagBucket];
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <table className="min-w-full text-sm">
        <thead className="bg-elevation-2 border-b border-border">
          <tr>
            <Th>Workspace ID</Th>
            <Th align="right">Neo4j hot</Th>
            <Th align="right">Neo4j warm</Th>
            <Th align="right">Qdrant hot</Th>
            <Th align="right">Qdrant warm</Th>
            <Th>Last run</Th>
            <Th>Lag</Th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          <tr>
            <td
              className="px-4 py-2 font-mono text-xs text-text-secondary truncate"
              title={data.workspace_id}
            >
              {data.workspace_id}
            </td>
            <td className="px-4 py-2 text-right tabular-nums text-text">
              {formatCount(data.neo4j_hot)}
            </td>
            <td className="px-4 py-2 text-right tabular-nums text-text">
              {formatCount(data.neo4j_warm)}
            </td>
            <td className="px-4 py-2 text-right tabular-nums text-text">
              {formatCount(data.qdrant_hot)}
            </td>
            <td className="px-4 py-2 text-right tabular-nums text-text">
              {formatCount(data.qdrant_warm)}
            </td>
            <td className="px-4 py-2 text-text-muted">
              {formatTimestamp(data.last_run_at)}
            </td>
            <td className="px-4 py-2">
              <span
                className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${lagSpec.pillCls}`}
              >
                {data.last_run_at == null
                  ? lagSpec.label
                  : `${formatLag(data.lag_seconds)} · ${lagSpec.label}`}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

interface ActionsProps {
  dryRun: boolean;
  runNowState: ActionState<RetentionRunNowResponse>;
  reportState: ActionState<RetentionReportResponse>;
  onRunNow: () => void;
  onDryRun: () => void;
}

function Actions({
  dryRun,
  runNowState,
  reportState,
  onRunNow,
  onDryRun,
}: ActionsProps) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          onClick={onRunNow}
          disabled={runNowState.status === "pending"}
          className="inline-flex items-center rounded-md bg-accent text-accent-fg px-4 py-2 text-sm font-medium hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {runNowState.status === "pending" ? "Running…" : "Run now"}
        </button>
        <button
          type="button"
          onClick={onDryRun}
          disabled={reportState.status === "pending"}
          className="inline-flex items-center rounded-md border border-border bg-elevation-2 text-text px-4 py-2 text-sm font-medium hover:border-border-strong disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {reportState.status === "pending" ? "Computing…" : "Dry run report"}
        </button>
        <p className="text-xs text-text-muted self-center">
          Worker is in <strong className="text-text">{dryRun ? "dry-run" : "live"}</strong> mode.{" "}
          {dryRun
            ? "“Run now” will compute eligibility but not mutate stores."
            : "“Run now” will mutate stores within the configured boundaries."}
        </p>
      </div>

      {runNowState.status === "success" && (
        <Banner intent="success" role="status">
          Run completed in {runNowState.data.duration_seconds.toFixed(2)}s · run_id{" "}
          <code className="text-xs">{runNowState.data.run_id}</code>
        </Banner>
      )}
      {runNowState.status === "error" && (
        <Banner intent="error" role="alert">
          Run failed: {runNowState.error.message}
        </Banner>
      )}
      {reportState.status === "error" && (
        <Banner intent="error" role="alert">
          Dry-run report failed: {reportState.error.message}
        </Banner>
      )}
    </div>
  );
}

function DryRunReport({ data }: { data: RetentionReportResponse }) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <ReportTile label="Hot → warm: entity states" value={data.eligible_hot_to_warm_entity_states} />
        <ReportTile label="Hot → warm: edges" value={data.eligible_hot_to_warm_edges} />
        <ReportTile label="Hot → warm: chunks" value={data.eligible_hot_to_warm_chunks} />
        <ReportTile
          label="Warm → archive: snapshots"
          value={data.eligible_warm_to_archive_entity_snapshots}
        />
      </div>
      {data.eligible_warm_to_archive_dates.length > 0 && (
        <p className="text-sm text-text-secondary">
          Snapshot dates queued for archive:{" "}
          <span className="font-mono text-text-muted">
            {data.eligible_warm_to_archive_dates.join(", ")}
          </span>
        </p>
      )}
      {data.sampled_eligible.length > 0 ? (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="min-w-full text-sm">
            <thead className="bg-elevation-2 border-b border-border">
              <tr>
                <Th>Entity ID</Th>
                <Th>valid_from</Th>
                <Th>recorded_at</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {data.sampled_eligible.map((s, i) => (
                <tr key={`${s.id ?? "?"}-${i}`}>
                  <td className="px-4 py-2 font-mono text-xs text-text-secondary truncate">
                    {s.id ?? "—"}
                  </td>
                  <td className="px-4 py-2 text-text-muted tabular-nums">{s.valid_from ?? "—"}</td>
                  <td className="px-4 py-2 text-text-muted tabular-nums">
                    {s.recorded_at ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-sm text-text-muted">
          No sampled rows — workspace has nothing eligible for eviction.
        </p>
      )}
    </div>
  );
}

function ReportTile({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-elevation-2 rounded-lg border border-border px-4 py-3">
      <p className="text-xs text-text-muted uppercase tracking-wide">{label}</p>
      <p className="text-2xl font-semibold text-text mt-1 tabular-nums">
        {formatCount(value)}
      </p>
    </div>
  );
}

interface SectionProps {
  title: string;
  children: React.ReactNode;
}

function Section({ title, children }: SectionProps) {
  return (
    <section aria-label={title} className="space-y-3">
      <h2 className="text-sm font-medium text-text-secondary uppercase tracking-wide">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Th({
  children,
  align = "left",
}: {
  children: React.ReactNode;
  align?: "left" | "right";
}) {
  return (
    <th
      scope="col"
      className={`px-4 py-2 font-medium text-text-secondary ${
        align === "right" ? "text-right" : "text-left"
      }`}
    >
      {children}
    </th>
  );
}

function RefreshIndicator({
  refreshing,
  lastRefreshedAt,
  refreshMs,
}: {
  refreshing: boolean;
  lastRefreshedAt: number | null;
  refreshMs: number;
}) {
  return (
    <div
      className="flex items-center gap-2 text-xs text-text-muted"
      aria-live="polite"
    >
      <span
        aria-hidden="true"
        className={`h-2 w-2 rounded-full ${
          refreshing ? "bg-accent motion-safe:animate-pulse" : "bg-success-fg"
        }`}
      />
      <span>
        {lastRefreshedAt == null
          ? "Loading…"
          : `Refreshed ${formatRelative(lastRefreshedAt)} · every ${Math.round(
              refreshMs / 1000
            )}s`}
      </span>
    </div>
  );
}

function formatRelative(ts: number): string {
  const elapsed = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (elapsed < 60) return `${elapsed}s ago`;
  return `${Math.round(elapsed / 60)}m ago`;
}

function ErrorBanner({ error }: { error: ApiError | Error }) {
  if (error instanceof ApiError && error.status === 403) {
    return (
      <div
        role="alert"
        className="rounded-md border border-warning-border bg-warning-bg text-warning-fg px-4 py-3 text-sm"
      >
        <p className="font-medium">Missing scope</p>
        <p className="mt-1">
          Your token doesn’t have <code>stats:read</code>. Ask an administrator
          to issue a token with that scope to view retention status.
        </p>
      </div>
    );
  }
  const status = error instanceof ApiError ? error.status : null;
  return (
    <div
      role="alert"
      className="rounded-md border border-danger-border bg-danger-bg text-danger-fg px-4 py-3 text-sm"
    >
      <p className="font-medium">
        Couldn’t load retention status{status != null ? ` (${status})` : ""}
      </p>
      <p className="mt-1 break-words">{error.message}</p>
    </div>
  );
}

function PageSkeleton() {
  return (
    <div className="space-y-6" aria-hidden="true">
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div
            key={i}
            className="bg-elevation-1 rounded-xl border border-border shadow-sm p-4 motion-safe:animate-pulse"
          >
            <div className="h-3 w-32 bg-elevation-2 rounded" />
            <div className="h-8 w-24 bg-elevation-2 rounded mt-3" />
          </div>
        ))}
      </div>
      <span className="sr-only" role="status">
        Loading retention…
      </span>
    </div>
  );
}

function Banner({
  intent,
  role,
  children,
}: {
  intent: "success" | "error";
  role: "status" | "alert";
  children: React.ReactNode;
}) {
  const cls =
    intent === "success"
      ? "border-success-fg bg-success-bg text-success-fg"
      : "border-danger-border bg-danger-bg text-danger-fg";
  return (
    <div role={role} className={`rounded-md border px-4 py-3 text-sm ${cls}`}>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type ActionState<T> =
  | { status: "idle" }
  | { status: "pending" }
  | { status: "success"; data: T }
  | { status: "error"; error: ApiError | Error };

function toError(e: unknown): ApiError | Error {
  if (e instanceof Error) return e;
  return new Error(String(e));
}

interface PillSpec {
  label: string;
  pillCls: string;
}

const LAG_PILL_SPECS: Record<LagBucket, PillSpec> = {
  ok: { label: "Within 24h SLO", pillCls: "bg-success-bg text-success-fg" },
  warning: { label: ">24h", pillCls: "bg-warning-bg text-warning-fg" },
  critical: { label: ">7d (P1)", pillCls: "bg-danger-bg text-danger-fg" },
  never: { label: "Not run yet", pillCls: "bg-info-bg text-info-fg" },
};
