/*
 * SchemaVolumePanel — entity-kind + edge-type histograms (Issue #114, panel #2).
 *
 * Two side-by-side bar charts: one for entity kinds, one for edge
 * types. They live in a single panel so they share the refresh
 * indicator and stack vertically at narrow breakpoints (md and below).
 *
 * Each histogram has its own fetch + lifecycle. We fan them out
 * concurrently inside one effect so a slow `entities-by-kind` does
 * not delay `edges-by-type` rendering — independent error state.
 *
 * Refresh cadence: 60s. Schema histograms are slower-moving than the
 * overview totals; the issue suggests this exact cadence.
 */

import { Link } from "react-router-dom";
import {
  ApiClient,
  EdgesByTypeResponse,
  EntitiesByKindResponse,
} from "../../api/client";
import { useTokenContext } from "../../context/TokenContext";
import { usePanelFetch } from "../../hooks/usePanelFetch";
import { HorizontalBarChart } from "./Charts";
import { PanelBody, PanelFrame } from "./PanelFrame";
import { PanelErrorBoundary } from "./PanelErrorBoundary";

const REFRESH_MS = 60_000;

interface Props {
  client?: ApiClient;
  refreshMs?: number;
}

export function SchemaVolumePanel({
  client: clientOverride,
  refreshMs = REFRESH_MS,
}: Props = {}) {
  const ctx = useTokenContext();
  const client = clientOverride ?? ctx.client;

  const entities = usePanelFetch<EntitiesByKindResponse>(
    (signal) => client.statsEntitiesByKind(signal),
    refreshMs
  );
  const edges = usePanelFetch<EdgesByTypeResponse>(
    (signal) => client.statsEdgesByType(signal),
    refreshMs
  );

  /* For the panel-level refresh badge we report whichever child
   * refreshed most recently. The "any-refreshing" signal is OR'd so
   * the pulse dot animates while either underlying request is
   * in-flight. */
  const lastRefreshedAt =
    entities.lastRefreshedAt != null && edges.lastRefreshedAt != null
      ? Math.max(entities.lastRefreshedAt, edges.lastRefreshedAt)
      : (entities.lastRefreshedAt ?? edges.lastRefreshedAt);
  const refreshing = entities.refreshing || edges.refreshing;

  return (
    <PanelFrame
      title="Schema volume"
      subtitle="Entities by kind and edges by type"
      refreshing={refreshing}
      lastRefreshedAt={lastRefreshedAt}
      refreshMs={refreshMs}
    >
      <PanelErrorBoundary panelTitle="Schema volume">
        {/*
         * Each subchart manages its own state branches independently —
         * a 403 on entities still allows edges to render normally, and
         * vice versa.
         */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <SubPanel
            title="Entities by kind"
            data={entities.data}
            error={entities.error}
            renderChart={(d) => (
              <HorizontalBarChart
                data={d.entries.map((e) => ({
                  label: e.kind,
                  value: e.count,
                }))}
                caption="Entity kinds"
              />
            )}
            isEmpty={(d) => d.total === 0}
          />
          <SubPanel
            title="Edges by type"
            data={edges.data}
            error={edges.error}
            renderChart={(d) => (
              <HorizontalBarChart
                data={d.entries.map((e) => ({
                  label: e.edge_type,
                  value: e.count,
                }))}
                caption="Edge types"
              />
            )}
            isEmpty={(d) => d.total === 0}
          />
        </div>
      </PanelErrorBoundary>
    </PanelFrame>
  );
}

interface SubPanelProps<T> {
  title: string;
  data: T | null;
  error: Error | null;
  renderChart: (data: T) => React.ReactNode;
  isEmpty: (data: T) => boolean;
}

function SubPanel<T>({
  title,
  data,
  error,
  renderChart,
  isEmpty,
}: SubPanelProps<T>) {
  return (
    <div>
      <h3 className="text-sm font-medium text-text-secondary mb-2">{title}</h3>
      <PanelBody
        data={data}
        error={error}
        isEmpty={isEmpty}
        emptyTitle="No data yet"
        emptyHint={
          <>
            Trigger a sync from{" "}
            <Link
              to="/sources"
              className="text-accent hover:text-accent-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
            >
              Sources
            </Link>{" "}
            to populate the graph.
          </>
        }
        skeleton={<ChartSkeleton />}
      >
        {(d) => <>{renderChart(d)}</>}
      </PanelBody>
    </div>
  );
}

function ChartSkeleton() {
  return (
    <ul className="space-y-1.5" aria-hidden="true">
      {Array.from({ length: 5 }).map((_, i) => (
        <li
          key={i}
          className="grid grid-cols-[minmax(7rem,9rem)_1fr_auto] items-center gap-2"
        >
          <div className="h-3 w-20 bg-elevation-2 rounded motion-safe:animate-pulse" />
          <div className="h-3 w-full bg-elevation-2 rounded motion-safe:animate-pulse" />
          <div className="h-3 w-8 bg-elevation-2 rounded motion-safe:animate-pulse" />
        </li>
      ))}
      <li className="sr-only" role="status">
        Loading chart…
      </li>
    </ul>
  );
}
