/*
 * DeltaStrip — 24h activity strip (Issue #114, panel #6).
 *
 * Surfaces the three rolling-24h fields from the overview payload:
 *   - documents_added_24h
 *   - documents_updated_24h
 *   - documents_tombstoned_24h
 *
 * The component takes the overview as a prop instead of fetching it
 * again — the OverviewHeader has already paid the network cost and
 * having two panels poll the same endpoint would double load with no
 * extra information. The strip therefore renders inline based on the
 * overview's lifecycle (loading/error/populated) using the supplied
 * helpers.
 *
 * Color encoding follows the issue's "color is not the only channel"
 * rule: each delta has both a color and an icon-shaped glyph (▲ ▼ ●).
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

export function DeltaStrip({
  client: clientOverride,
  refreshMs = REFRESH_MS,
}: Props = {}) {
  const ctx = useTokenContext();
  const client = clientOverride ?? ctx.client;

  /* Independent fetch from OverviewHeader: while there is some data
   * overlap, decoupling cadence and error state per panel matches the
   * issue's "ONE panel failing must NOT crash the page" requirement.
   * Server-side cache (5-10s) deduplicates the load anyway. */
  const { data, error, refreshing, lastRefreshedAt } =
    usePanelFetch<StatsOverview>(
      (signal) => client.statsOverview(signal),
      refreshMs
    );

  return (
    <PanelFrame
      title="Last 24 hours"
      subtitle="Document activity"
      refreshing={refreshing}
      lastRefreshedAt={lastRefreshedAt}
      refreshMs={refreshMs}
    >
      <PanelErrorBoundary panelTitle="Last 24 hours">
        <PanelBody
          data={data}
          error={error}
          isEmpty={(d) =>
            d.documents_added_24h === 0 &&
            d.documents_updated_24h === 0 &&
            d.documents_tombstoned_24h === 0
          }
          emptyTitle="No document changes in the last 24h"
          emptyHint={
            <>
              Trigger a sync from{" "}
              <Link
                to="/sources"
                className="text-accent hover:text-accent-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
              >
                Sources
              </Link>{" "}
              to populate.
            </>
          }
          skeleton={<DeltaSkeleton />}
        >
          {(d) => <DeltaContent data={d} />}
        </PanelBody>
      </PanelErrorBoundary>
    </PanelFrame>
  );
}

function DeltaContent({ data }: { data: StatsOverview }) {
  return (
    <div className="grid grid-cols-3 gap-3">
      <Cell
        glyph="▲"
        label="New"
        value={data.documents_added_24h}
        toneClass="text-success-fg bg-success-bg"
      />
      <Cell
        glyph="●"
        label="Updated"
        value={data.documents_updated_24h}
        toneClass="text-info-fg bg-info-bg"
      />
      <Cell
        glyph="▼"
        label="Tombstoned"
        value={data.documents_tombstoned_24h}
        toneClass="text-danger-fg bg-danger-bg"
      />
    </div>
  );
}

interface CellProps {
  glyph: string;
  label: string;
  value: number;
  toneClass: string;
}

function Cell({ glyph, label, value, toneClass }: CellProps) {
  return (
    <div
      className={`rounded-lg px-4 py-3 flex items-center gap-3 ${toneClass}`}
    >
      <span aria-hidden="true" className="text-lg leading-none">
        {glyph}
      </span>
      <div className="min-w-0">
        <p className="text-xs uppercase tracking-wide opacity-80">{label}</p>
        <p className="text-xl font-semibold tabular-nums">
          {value.toLocaleString()}
        </p>
      </div>
    </div>
  );
}

function DeltaSkeleton() {
  return (
    <div className="grid grid-cols-3 gap-3">
      {Array.from({ length: 3 }).map((_, i) => (
        <div
          key={i}
          className="rounded-lg bg-elevation-2 border border-border h-16 motion-safe:animate-pulse"
          aria-hidden="true"
        />
      ))}
      <span className="sr-only" role="status">
        Loading 24h activity…
      </span>
    </div>
  );
}
