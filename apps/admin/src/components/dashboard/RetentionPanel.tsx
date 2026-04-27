/*
 * RetentionPanel — compact retention summary on the dashboard
 * (Issue #136 § "wire into the dashboard home page (#114) as a card").
 *
 * The dashboard card shows hot/warm/archive totals + lag indicator
 * with a link to /retention for the deep-dive page. Refresh cadence
 * is 30s — matches the issue's call-out and fits the OverviewHeader
 * cadence so the dashboard stays visually coherent.
 *
 * The card is intentionally read-only. Action buttons (Run now / Dry
 * run report) live exclusively on /retention so the dashboard surface
 * stays scannable. Operators following the link from the card to the
 * deep-dive page get the full set of affordances + per-tenant table.
 */

import { Link } from "react-router-dom";
import { ApiClient, RetentionStatusResponse } from "../../api/client";
import { useTokenContext } from "../../context/TokenContext";
import { usePanelFetch } from "../../hooks/usePanelFetch";
import { PanelBody, PanelFrame } from "./PanelFrame";
import { PanelErrorBoundary } from "./PanelErrorBoundary";

const REFRESH_MS = 30_000;

/* SLO thresholds in seconds — kept synchronised with the Prometheus
 * alert rules in `monitoring/prometheus/alerts/retention.yaml`. */
const LAG_WARNING_SECONDS = 86_400; // 24h
const LAG_CRITICAL_SECONDS = 604_800; // 7d

type LagBucket = "ok" | "warning" | "critical" | "never";

interface BucketSpec {
  label: string;
  pillCls: string;
}

const LAG_BUCKETS: Record<LagBucket, BucketSpec> = {
  ok: { label: "Within 24h SLO", pillCls: "bg-success-bg text-success-fg" },
  warning: { label: ">24h", pillCls: "bg-warning-bg text-warning-fg" },
  critical: { label: ">7d (P1)", pillCls: "bg-danger-bg text-danger-fg" },
  never: { label: "Not run yet", pillCls: "bg-info-bg text-info-fg" },
};

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

interface Props {
  client?: ApiClient;
  refreshMs?: number;
}

export function RetentionPanel({
  client: clientOverride,
  refreshMs = REFRESH_MS,
}: Props = {}) {
  const ctx = useTokenContext();
  const client = clientOverride ?? ctx.client;

  const { data, error, refreshing, lastRefreshedAt } =
    usePanelFetch<RetentionStatusResponse>(
      (signal) => client.retentionStatus(signal),
      refreshMs
    );

  return (
    <PanelFrame
      title="Retention"
      subtitle="Hot / warm / archive totals & lag SLO"
      refreshing={refreshing}
      lastRefreshedAt={lastRefreshedAt}
      refreshMs={refreshMs}
    >
      <PanelErrorBoundary panelTitle="Retention">
        <PanelBody
          data={data}
          error={error}
          isEmpty={() => false /* totals always render even at zero */}
          emptyTitle=""
          emptyHint=""
          skeleton={<CardSkeleton />}
        >
          {(d) => <Card data={d} />}
        </PanelBody>
      </PanelErrorBoundary>
    </PanelFrame>
  );
}

function Card({ data }: { data: RetentionStatusResponse }) {
  const bucket = classifyLag(data);
  const spec = LAG_BUCKETS[bucket];
  const totalHot = data.neo4j_hot + data.qdrant_hot;
  const totalWarm = data.neo4j_warm + data.qdrant_warm;
  /* Archive count is not surfaced via /status (Qdrant is always 0 by
   * design — re-embedding from archive is unsupported in v1 per
   * ADR-0009 §5; Neo4j archive lives outside the live store entirely
   * per ADR-0009 §1). The card displays "—" so operators know to
   * follow the link to /retention for the dry-run report which lists
   * eligible-for-archive snapshot dates. */
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-3">
        <Tile label="Hot" value={totalHot} colorVar="--chart-1" />
        <Tile label="Warm" value={totalWarm} colorVar="--chart-2" />
        <Tile label="Archive" value={null} colorVar="--chart-3" />
      </div>

      <div className="flex items-center justify-between rounded-md border border-border bg-elevation-2 px-3 py-2">
        <div className="flex flex-col">
          <span className="text-xs text-text-muted uppercase tracking-wide">
            Lag SLO
          </span>
          <span className="text-sm text-text tabular-nums">
            {data.last_run_at == null ? "no runs yet" : formatLag(data.lag_seconds)}
          </span>
        </div>
        <span
          className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${spec.pillCls}`}
        >
          {spec.label}
        </span>
      </div>

      <div className="flex items-center justify-between text-xs text-text-muted">
        <span>
          {data.dry_run ? "Dry-run mode" : "Live mode"}
        </span>
        <Link
          to="/retention"
          className="text-accent hover:text-accent-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
        >
          View retention →
        </Link>
      </div>
    </div>
  );
}

interface TileProps {
  label: string;
  value: number | null;
  colorVar: string;
}

function Tile({ label, value, colorVar }: TileProps) {
  return (
    <div className="bg-elevation-2 rounded-lg border border-border px-3 py-2.5">
      <div className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className="h-2 w-2 rounded-full"
          style={{ backgroundColor: `var(${colorVar})` }}
        />
        <span className="text-xs text-text-muted uppercase tracking-wide">
          {label}
        </span>
      </div>
      <p className="text-xl font-semibold text-text mt-1 tabular-nums">
        {value == null ? "—" : formatCount(value)}
      </p>
    </div>
  );
}

function CardSkeleton() {
  return (
    <div className="space-y-4" aria-hidden="true">
      <div className="grid grid-cols-3 gap-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <div
            key={i}
            className="bg-elevation-2 rounded-lg border border-border px-3 py-2.5 motion-safe:animate-pulse"
          >
            <div className="h-3 w-12 bg-elevation-1 rounded" />
            <div className="h-6 w-16 bg-elevation-1 rounded mt-2" />
          </div>
        ))}
      </div>
      <div className="h-12 rounded-md border border-border bg-elevation-2 motion-safe:animate-pulse" />
      <span className="sr-only" role="status">
        Loading retention…
      </span>
    </div>
  );
}
