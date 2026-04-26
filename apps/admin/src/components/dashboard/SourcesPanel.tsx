/*
 * SourcesPanel — compact sources table on the dashboard (Issue #114, panel #3).
 *
 * Renders the top N sources from `GET /api/v1/stats/sources` with
 * freshness color badges (mirroring the rules used by FreshnessPanel
 * but compressed: each row gets a single pill — fresh / approaching /
 * stale / never / no-sla).
 *
 * The dashboard view is intentionally read-only: no add / sync /
 * delete buttons (those live on /sources). A "view all" link sits at
 * the bottom and routes to /sources when there are more rows than
 * we display.
 *
 * Refresh cadence: 60s. Sources change slowly (one entry per
 * configured source per workspace).
 */

import { Link } from "react-router-dom";
import {
  ApiClient,
  SourceStatsRow,
  SourcesStatsResponse,
} from "../../api/client";
import { useTokenContext } from "../../context/TokenContext";
import { usePanelFetch } from "../../hooks/usePanelFetch";
import { PanelBody, PanelFrame } from "./PanelFrame";
import { PanelErrorBoundary } from "./PanelErrorBoundary";

const REFRESH_MS = 60_000;
const TOP_N = 10;
/* Mirrors FreshnessPanel's sentinel logic — `1e15` from the server,
 * defensively threshold below it to dodge float drift. */
const NEVER_SYNCED_SENTINEL = 1e14;

type Bucket = "fresh" | "approaching" | "stale" | "never" | "no-sla";

interface BucketSpec {
  label: string;
  pillCls: string;
}

const BUCKET_SPECS: Record<Bucket, BucketSpec> = {
  fresh: {
    label: "Fresh",
    pillCls: "bg-success-bg text-success-fg",
  },
  approaching: {
    label: "Approaching",
    pillCls: "bg-warning-bg text-warning-fg",
  },
  stale: {
    label: "Stale",
    pillCls: "bg-danger-bg text-danger-fg",
  },
  never: {
    label: "Never",
    pillCls: "bg-info-bg text-info-fg",
  },
  "no-sla": {
    label: "No SLA",
    pillCls: "bg-elevation-2 text-text-secondary",
  },
};

function classify(row: SourceStatsRow): Bucket {
  if (row.age_seconds >= NEVER_SYNCED_SENTINEL) return "never";
  if (row.freshness_sla_seconds == null) return "no-sla";
  if (row.is_stale) return "stale";
  if (row.age_seconds >= row.freshness_sla_seconds * 0.5) return "approaching";
  return "fresh";
}

function formatAge(seconds: number): string {
  if (seconds >= NEVER_SYNCED_SENTINEL) return "never";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

interface Props {
  client?: ApiClient;
  refreshMs?: number;
  topN?: number;
}

export function SourcesPanel({
  client: clientOverride,
  refreshMs = REFRESH_MS,
  topN = TOP_N,
}: Props = {}) {
  const ctx = useTokenContext();
  const client = clientOverride ?? ctx.client;

  const { data, error, refreshing, lastRefreshedAt } =
    usePanelFetch<SourcesStatsResponse>(
      (signal) => client.statsSources(signal),
      refreshMs
    );

  return (
    <PanelFrame
      title="Sources"
      subtitle="Per-source freshness and volume"
      refreshing={refreshing}
      lastRefreshedAt={lastRefreshedAt}
      refreshMs={refreshMs}
    >
      <PanelErrorBoundary panelTitle="Sources">
        <PanelBody
          data={data}
          error={error}
          isEmpty={(d) => d.total === 0}
          emptyTitle="No sources configured yet"
          emptyHint={
            <>
              Add a source on the{" "}
              <Link
                to="/sources"
                className="text-accent hover:text-accent-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
              >
                Sources
              </Link>{" "}
              page.
            </>
          }
          skeleton={<TableSkeleton />}
        >
          {(d) => <CompactTable data={d} topN={topN} />}
        </PanelBody>
      </PanelErrorBoundary>
    </PanelFrame>
  );
}

interface TableProps {
  data: SourcesStatsResponse;
  topN: number;
}

function CompactTable({ data, topN }: TableProps) {
  /* Sort: most-stale first (matches FreshnessPanel default). For the
   * dashboard surface, stale-first is what an operator wants to see
   * — health-of-a-glance. */
  const rows = data.sources
    .slice()
    .sort((a, b) => {
      const ra = classify(a);
      const rb = classify(b);
      const order: Bucket[] = [
        "stale",
        "approaching",
        "fresh",
        "never",
        "no-sla",
      ];
      const ai = order.indexOf(ra);
      const bi = order.indexOf(rb);
      if (ai !== bi) return ai - bi;
      return b.age_seconds - a.age_seconds;
    })
    .slice(0, topN);

  return (
    <>
      <div className="overflow-hidden rounded-lg border border-border">
        <table className="min-w-full table-fixed text-sm">
          <colgroup>
            <col className="w-[36%]" />
            <col className="w-[14%]" />
            <col className="w-[16%]" />
            <col className="w-[16%]" />
            <col className="w-[18%]" />
          </colgroup>
          <thead className="bg-elevation-2 border-b border-border">
            <tr>
              <th
                scope="col"
                className="px-4 py-2 text-left font-medium text-text-secondary"
              >
                Source
              </th>
              <th
                scope="col"
                className="px-4 py-2 text-left font-medium text-text-secondary"
              >
                Type
              </th>
              <th
                scope="col"
                className="px-4 py-2 text-right font-medium text-text-secondary"
              >
                Docs
              </th>
              <th
                scope="col"
                className="px-4 py-2 text-left font-medium text-text-secondary"
              >
                Last sync
              </th>
              <th
                scope="col"
                className="px-4 py-2 text-left font-medium text-text-secondary"
              >
                Status
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.map((row) => {
              const spec = BUCKET_SPECS[classify(row)];
              return (
                <tr
                  key={row.id}
                  tabIndex={0}
                  className="hover:bg-elevation-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset"
                >
                  <td
                    className="px-4 py-2 text-text truncate"
                    title={row.name}
                  >
                    {row.name}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-text-secondary">
                    {row.type}
                  </td>
                  <td className="px-4 py-2 text-right text-text-muted tabular-nums">
                    {row.documents.toLocaleString()}
                  </td>
                  <td className="px-4 py-2 text-text-muted tabular-nums">
                    {formatAge(row.age_seconds)}
                  </td>
                  <td className="px-4 py-2">
                    <span
                      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${spec.pillCls}`}
                    >
                      {spec.label}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data.total > rows.length && (
        <div className="mt-3 text-right">
          <Link
            to="/sources"
            className="text-sm text-accent hover:text-accent-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
          >
            View all {data.total} sources →
          </Link>
        </div>
      )}
    </>
  );
}

function TableSkeleton() {
  return (
    <div className="rounded-lg border border-border" aria-hidden="true">
      {Array.from({ length: 5 }).map((_, i) => (
        <div
          key={i}
          className="px-4 py-3 border-b border-border last:border-b-0 grid grid-cols-5 gap-2"
        >
          {Array.from({ length: 5 }).map((__, j) => (
            <div
              key={j}
              className="h-3 bg-elevation-2 rounded motion-safe:animate-pulse"
            />
          ))}
        </div>
      ))}
      <span className="sr-only" role="status">
        Loading sources…
      </span>
    </div>
  );
}
