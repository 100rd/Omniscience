/*
 * OverviewHeader — totals strip across the top of the dashboard
 * (Issue #114, panel #1).
 *
 * Renders six tiles from `GET /api/v1/stats/overview`:
 *   sources, documents, chunks, entities, edges, total indexed bytes.
 *
 * The header is a self-contained panel — it owns its own fetch and
 * refresh cadence (30s, matches the issue's suggested cadence for
 * fast-moving overview data). Per-panel auto-refresh isolates failure
 * (a flapping overview endpoint does not delay the rest of the page).
 *
 * Edges total comes from the `edges_by_type` histogram: the overview
 * endpoint is the canonical source of truth for the workspace edge
 * count and we sum the histogram values rather than re-issue
 * /edges-by-type just to derive a total.
 */

import { Link } from "react-router-dom";
import { ApiClient, StatsOverview } from "../../api/client";
import { useTokenContext } from "../../context/TokenContext";
import { usePanelFetch } from "../../hooks/usePanelFetch";
import { PanelBody, PanelFrame } from "./PanelFrame";
import { PanelErrorBoundary } from "./PanelErrorBoundary";

const REFRESH_MS = 30_000;

interface Props {
  client?: ApiClient;
  refreshMs?: number;
}

interface TileSpec {
  label: string;
  value: number;
  to?: string;
  /* Optional secondary line — used for "active sources" sub-count. */
  hint?: string;
}

/* Approximate byte formatter — uses base-1000 so the dashboard label
 * lines up with the way operators size dataset growth in tickets. */
function formatBytes(n: number): string {
  if (n < 1000) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let v = n / 1000;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  /* One decimal place when below 100 — keeps "12.3 GB" readable
   * without the noise of "12.34 GB". */
  return `${v < 100 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

function formatCount(n: number): string {
  return n.toLocaleString();
}

export function OverviewHeader({
  client: clientOverride,
  refreshMs = REFRESH_MS,
}: Props = {}) {
  const ctx = useTokenContext();
  const client = clientOverride ?? ctx.client;

  const { data, error, refreshing, lastRefreshedAt } =
    usePanelFetch<StatsOverview>(
      (signal) => client.statsOverview(signal),
      refreshMs
    );

  return (
    <PanelFrame
      title="Overview"
      subtitle="Workspace totals"
      refreshing={refreshing}
      lastRefreshedAt={lastRefreshedAt}
      refreshMs={refreshMs}
    >
      <PanelErrorBoundary panelTitle="Overview">
        <PanelBody
          data={data}
          error={error}
          isEmpty={() => false /* totals always render even at zero */}
          emptyTitle=""
          emptyHint=""
          skeleton={<TilesSkeleton />}
        >
          {(d) => <Tiles data={d} />}
        </PanelBody>
      </PanelErrorBoundary>
    </PanelFrame>
  );
}

function Tiles({ data }: { data: StatsOverview }) {
  const totalEdges = Object.values(data.edges_by_type).reduce(
    (acc, n) => acc + n,
    0
  );
  const tiles: TileSpec[] = [
    {
      label: "Sources",
      value: data.sources,
      to: "/sources",
      hint: `${data.active_sources.toLocaleString()} active`,
    },
    {
      label: "Documents",
      value: data.active_documents,
      hint:
        data.tombstoned_documents > 0
          ? `${data.tombstoned_documents.toLocaleString()} tombstoned`
          : undefined,
    },
    { label: "Chunks", value: data.chunks },
    { label: "Entities", value: data.entities },
    { label: "Edges", value: totalEdges },
    {
      label: "Indexed size",
      value: data.total_indexed_bytes,
      hint: "active chunks",
    },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
      {tiles.map((t) => (
        <Tile key={t.label} {...t} />
      ))}
    </div>
  );
}

function Tile({ label, value, to, hint }: TileSpec) {
  /* Indexed size is the only tile whose raw `value` is bytes; we
   * detect it via the label rather than threading a formatter prop. */
  const display =
    label === "Indexed size" ? formatBytes(value) : formatCount(value);

  const inner = (
    <>
      <p className="text-xs text-text-muted uppercase tracking-wide">{label}</p>
      <p className="text-2xl font-semibold text-text mt-1 tabular-nums">
        {display}
      </p>
      {hint && (
        <p className="text-xs text-text-muted mt-0.5 truncate">{hint}</p>
      )}
    </>
  );

  if (to) {
    return (
      <Link
        to={to}
        className="bg-elevation-2 rounded-lg border border-border px-4 py-3 hover:border-border-strong transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent block"
      >
        {inner}
      </Link>
    );
  }
  return (
    <div className="bg-elevation-2 rounded-lg border border-border px-4 py-3">
      {inner}
    </div>
  );
}

/* Skeleton tiles — render the same grid shape so first-paint layout
 * matches populated state (no jump). The `motion-safe:` variant
 * disables the pulse for users with `prefers-reduced-motion`. */
function TilesSkeleton() {
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
      {Array.from({ length: 6 }).map((_, i) => (
        <div
          key={i}
          className="bg-elevation-2 rounded-lg border border-border px-4 py-3 motion-safe:animate-pulse"
          aria-hidden="true"
        >
          <div className="h-3 w-16 bg-elevation-1 rounded" />
          <div className="h-7 w-24 bg-elevation-1 rounded mt-2" />
          <div className="h-3 w-12 bg-elevation-1 rounded mt-1" />
        </div>
      ))}
      <span className="sr-only" role="status">
        Loading overview…
      </span>
    </div>
  );
}
