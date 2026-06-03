/*
 * DeltaStrip — Document activity panel with selectable time window.
 *
 * Replaces the original fixed-24h view (Issue #114 + follow-up).
 *
 * Changes vs. original:
 *   - Added a 24h / 7d / 30d / 90d range selector in the panel header.
 *   - On window change the panel calls the new `GET /api/v1/stats/activity`
 *     endpoint via `client.statsActivity(hours, signal)` instead of reusing
 *     the overview payload.
 *   - Added inline legend / tooltip descriptions for all three metric names
 *     (New / Updated / Tombstoned) so operators understand exact semantics.
 *   - Panel subtitle and loading text adapt to the selected window.
 *   - Maintains the existing `PanelFrame` + `PanelBody` chrome and color
 *     coding from Issue #114.
 *
 * Accessibility:
 *   - Selector is a labelled <fieldset>/<legend> containing three
 *     visually-styled radio buttons so it is keyboard-navigable and
 *     announced correctly by screen readers.
 *   - Each metric cell's descriptive tooltip uses the HTML `title`
 *     attribute for universal screen-reader support while the visible
 *     label carries an aria-label that includes the description.
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiClient, StatsActivity } from "../../api/client";
import { useTokenContext } from "../../context/TokenContext";
import { usePanelFetch } from "../../hooks/usePanelFetch";
import { PanelBody, PanelFrame } from "./PanelFrame";
import { PanelErrorBoundary } from "./PanelErrorBoundary";

const REFRESH_MS = 30_000;

// ---------------------------------------------------------------------------
// Window options
// ---------------------------------------------------------------------------

type WindowOption = {
  /** Hours forwarded to the API. */
  hours: number;
  /** Short label shown in the selector button. */
  label: string;
  /** Longer subtitle shown in the panel header. */
  subtitle: string;
};

const WINDOW_OPTIONS: readonly WindowOption[] = [
  { hours: 24, label: "24h", subtitle: "Last 24 hours" },
  { hours: 168, label: "7d", subtitle: "Last 7 days" },
  { hours: 720, label: "30d", subtitle: "Last 30 days" },
  { hours: 2160, label: "90d", subtitle: "Last 90 days" },
] as const;

const DEFAULT_WINDOW = WINDOW_OPTIONS[0];

// ---------------------------------------------------------------------------
// Metric descriptions (used for tooltip title + aria-label)
// ---------------------------------------------------------------------------

const METRIC_DESCRIPTIONS = {
  new: "Documents indexed for the first time in the selected window (first time this external ID was seen).",
  updated:
    "Existing documents re-indexed because their content changed (content hash differs) within the window.",
  tombstoned:
    "Documents soft-deleted in the window because the source stopped reporting them; they remain recoverable until the retention janitor hard-purges them.",
} as const;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

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

  const [selectedWindow, setSelectedWindow] =
    useState<WindowOption>(DEFAULT_WINDOW);

  /* Re-fetch whenever the window changes because usePanelFetch rebuilds its
   * interval when the fetcher closure identity changes (fetcherRef approach).
   * The closure captures `selectedWindow.hours`, so a window change produces
   * a new function reference on the next render, which the effect picks up. */
  const { data, error, refreshing, lastRefreshedAt } =
    usePanelFetch<StatsActivity>(
      (signal) => client.statsActivity(selectedWindow.hours, signal),
      refreshMs
    );

  return (
    <PanelFrame
      title={selectedWindow.subtitle}
      subtitle="Document activity"
      refreshing={refreshing}
      lastRefreshedAt={lastRefreshedAt}
      refreshMs={refreshMs}
    >
      <PanelErrorBoundary panelTitle="Document activity">
        {/* Window selector — placed inside the panel body above the metric
            cells so it sits close to the content it controls. */}
        <WindowSelector
          options={WINDOW_OPTIONS}
          selected={selectedWindow}
          onChange={setSelectedWindow}
        />

        <PanelBody
          data={data}
          error={error}
          isEmpty={(d) => d.new === 0 && d.updated === 0 && d.tombstoned === 0}
          emptyTitle={`No document changes in ${selectedWindow.subtitle.toLowerCase()}`}
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
          skeleton={<DeltaSkeleton window={selectedWindow.subtitle} />}
        >
          {(d) => <DeltaContent data={d} />}
        </PanelBody>
      </PanelErrorBoundary>
    </PanelFrame>
  );
}

// ---------------------------------------------------------------------------
// WindowSelector
// ---------------------------------------------------------------------------

interface WindowSelectorProps {
  options: readonly WindowOption[];
  selected: WindowOption;
  onChange: (option: WindowOption) => void;
}

function WindowSelector({ options, selected, onChange }: WindowSelectorProps) {
  return (
    /* fieldset + legend gives the group a proper accessible name.
     * We visually hide the legend text but keep it in the a11y tree. */
    <fieldset className="mb-4">
      <legend className="sr-only">Activity time window</legend>
      <div
        className="inline-flex rounded-md border border-border overflow-hidden"
        role="group"
      >
        {options.map((opt) => {
          const isSelected = opt.hours === selected.hours;
          return (
            <label key={opt.hours} className="relative cursor-pointer">
              <input
                type="radio"
                name="activity-window"
                value={String(opt.hours)}
                checked={isSelected}
                onChange={() => onChange(opt)}
                className="sr-only"
                aria-label={`Show activity for ${opt.subtitle}`}
              />
              <span
                className={
                  "block px-3 py-1 text-xs font-medium select-none transition-colors " +
                  (isSelected
                    ? "bg-accent text-accent-fg"
                    : "bg-elevation-1 text-text-muted hover:bg-elevation-2")
                }
              >
                {opt.label}
              </span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

// ---------------------------------------------------------------------------
// DeltaContent
// ---------------------------------------------------------------------------

function DeltaContent({ data }: { data: StatsActivity }) {
  return (
    <div className="grid grid-cols-3 gap-3">
      <Cell
        glyph="▲"
        label="New"
        value={data.new}
        toneClass="text-success-fg bg-success-bg"
        description={METRIC_DESCRIPTIONS.new}
      />
      <Cell
        glyph="●"
        label="Updated"
        value={data.updated}
        toneClass="text-info-fg bg-info-bg"
        description={METRIC_DESCRIPTIONS.updated}
      />
      <Cell
        glyph="▼"
        label="Tombstoned"
        value={data.tombstoned}
        toneClass="text-danger-fg bg-danger-bg"
        description={METRIC_DESCRIPTIONS.tombstoned}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cell
// ---------------------------------------------------------------------------

interface CellProps {
  glyph: string;
  label: string;
  value: number;
  toneClass: string;
  /** Human-readable description shown as tooltip + screen-reader annotation. */
  description: string;
}

function Cell({ glyph, label, value, toneClass, description }: CellProps) {
  return (
    /* title provides the tooltip on hover and is read by most screen readers
     * as supplementary information when the element receives focus. */
    <div
      className={`rounded-lg px-4 py-3 flex items-center gap-3 ${toneClass}`}
      title={description}
    >
      <span aria-hidden="true" className="text-lg leading-none">
        {glyph}
      </span>
      <div className="min-w-0">
        {/* aria-label combines the metric name and its description so the
            count reads in full context to assistive technology. */}
        <p
          className="text-xs uppercase tracking-wide opacity-80"
          aria-label={`${label}: ${description}`}
        >
          {label}
          {/* Inline help indicator — visually signals that a tooltip is
              available; decorative only (aria-hidden). */}
          <span
            aria-hidden="true"
            className="ml-1 opacity-50 text-[10px] font-normal normal-case"
          >
            (?)
          </span>
        </p>
        <p className="text-xl font-semibold tabular-nums">
          {value.toLocaleString()}
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DeltaSkeleton
// ---------------------------------------------------------------------------

interface DeltaSkeletonProps {
  window: string;
}

function DeltaSkeleton({ window }: DeltaSkeletonProps) {
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
        Loading document activity for {window}…
      </span>
    </div>
  );
}
