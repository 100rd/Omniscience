/*
 * FreshnessPanel — operator view of source freshness (Issue #112).
 *
 * Consumes `GET /api/v1/stats/sources` (Issue #111) and renders one row per
 * source with its sync age vs configured SLA.  Color-coded buckets:
 *
 *   fresh        — age < SLA × 0.5                           (success)
 *   approaching  — SLA × 0.5 ≤ age < SLA                     (warning)
 *   stale        — age ≥ SLA && is_stale: true               (danger)
 *   never-synced — age_seconds sentinel (>= NEVER_SENTINEL)  (info / muted)
 *   no-sla       — freshness_sla_seconds is null             (info / muted)
 *
 * Auto-refresh: every 30s via `setInterval`, with a per-fetch `AbortController`
 * to cancel the in-flight request on unmount or interval tick. We deliberately
 * avoid replacing the rendered table on refresh — only on initial load and on
 * hard error — so column widths stay stable (no layout shift).
 *
 * No new dependencies (project rule for #112). State is held in plain `useState`
 * + `useEffect`; no SWR/react-query. The pulse dot in the panel header is the
 * visible refresh indicator.
 *
 * ACL: the `stats:read` scope is enforced server-side. If the API returns 403
 * we render a clear banner surfacing the server's actual reason (e.g. missing
 * scope vs missing workspace binding) instead of a hardcoded message.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiClient,
  ApiError,
  SourceStatsRow,
  SourcesStatsResponse,
} from "../api/client";
import { useTokenContext } from "../context/TokenContext";

/* The server uses `1e15` as "never synced" — see SourceStatsRow doc.
 * Use a slightly lower threshold to be defensive against float precision. */
const NEVER_SYNCED_SENTINEL = 1e14;

const REFRESH_MS = 30_000;

/** Fallback message when the server returns 403 but the body carries no detail. */
const FALLBACK_403_MSG =
  "Your token doesn't have stats:read. Ask an administrator to issue a token with the freshness scope to view this panel.";

type Bucket = "fresh" | "approaching" | "stale" | "never-synced" | "no-sla";

interface BucketSpec {
  bucket: Bucket;
  label: string;
  /* Tailwind classes for the row pill / distribution segment. */
  pillCls: string;
  /* Bar segment background. */
  segmentCls: string;
  /* Legend swatch. */
  swatchCls: string;
}

const BUCKET_SPECS: Record<Bucket, BucketSpec> = {
  fresh: {
    bucket: "fresh",
    label: "Fresh",
    pillCls: "bg-success-bg text-success-fg",
    segmentCls: "bg-chart-2",
    swatchCls: "bg-chart-2",
  },
  approaching: {
    bucket: "approaching",
    label: "Approaching",
    pillCls: "bg-warning-bg text-warning-fg",
    segmentCls: "bg-chart-3",
    swatchCls: "bg-chart-3",
  },
  stale: {
    bucket: "stale",
    label: "Stale",
    pillCls: "bg-danger-bg text-danger-fg",
    segmentCls: "bg-chart-4",
    swatchCls: "bg-chart-4",
  },
  "never-synced": {
    bucket: "never-synced",
    label: "Never synced",
    pillCls: "bg-info-bg text-info-fg",
    segmentCls: "bg-chart-5",
    swatchCls: "bg-chart-5",
  },
  "no-sla": {
    bucket: "no-sla",
    label: "No SLA",
    pillCls: "bg-elevation-2 text-text-secondary",
    segmentCls: "bg-chart-6",
    swatchCls: "bg-chart-6",
  },
};

const BUCKET_ORDER: Bucket[] = [
  "stale",
  "approaching",
  "fresh",
  "never-synced",
  "no-sla",
];

function classifyRow(row: SourceStatsRow): Bucket {
  if (row.age_seconds >= NEVER_SYNCED_SENTINEL) return "never-synced";
  if (row.freshness_sla_seconds == null) return "no-sla";
  if (row.is_stale) return "stale";
  if (row.age_seconds >= row.freshness_sla_seconds * 0.5) return "approaching";
  return "fresh";
}

/* Human-readable duration. Sentinel -> "never". Sub-minute ages collapse to
 * "just now" so the table doesn't churn between "12s" / "13s" each refresh. */
function formatAge(seconds: number): string {
  if (seconds >= NEVER_SYNCED_SENTINEL) return "never";
  if (seconds < 60) return "just now";
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60);
    return `${m}m ago`;
  }
  if (seconds < 86_400) {
    const h = Math.floor(seconds / 3600);
    return `${h}h ago`;
  }
  const d = Math.floor(seconds / 86_400);
  return `${d}d ago`;
}

/* Human-readable SLA target. Null -> "—". Uses non-relative wording so it
 * reads as a target, not as an event in the past. */
function formatSla(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86_400)}d`;
}

type SortKey = "name" | "type" | "age" | "sla" | "bucket";
type SortDir = "asc" | "desc";

function bucketRank(b: Bucket): number {
  /* For sort-by-bucket we want the most-urgent first when descending. */
  return BUCKET_ORDER.indexOf(b);
}

function compareRows(
  a: SourceStatsRow,
  b: SourceStatsRow,
  key: SortKey,
  dir: SortDir
): number {
  const sign = dir === "asc" ? 1 : -1;
  switch (key) {
    case "name":
      return sign * a.name.localeCompare(b.name);
    case "type":
      return sign * a.type.localeCompare(b.type);
    case "age":
      return sign * (a.age_seconds - b.age_seconds);
    case "sla": {
      /* Treat null SLA as +Infinity so it sorts to the end on ascending. */
      const av = a.freshness_sla_seconds ?? Number.POSITIVE_INFINITY;
      const bv = b.freshness_sla_seconds ?? Number.POSITIVE_INFINITY;
      return sign * (av - bv);
    }
    case "bucket":
      /* Lower rank in BUCKET_ORDER = more urgent. We invert so that
       * descending = most urgent first (matches "most stale first" default). */
      return -sign * (bucketRank(classifyRow(a)) - bucketRank(classifyRow(b)));
  }
}

interface FreshnessPanelProps {
  /* Override the polling interval — primarily for tests / Storybook. */
  refreshMs?: number;
  /* Override the api client — primarily for tests. */
  client?: ApiClient;
  /* Optional className passthrough so the panel can be slotted into grids. */
  className?: string;
}

export function FreshnessPanel({
  refreshMs = REFRESH_MS,
  client: clientOverride,
  className,
}: FreshnessPanelProps = {}) {
  const ctx = useTokenContext();
  const client = clientOverride ?? ctx.client;

  const [data, setData] = useState<SourcesStatsResponse | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<number | null>(null);

  const [sortKey, setSortKey] = useState<SortKey>("age");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  /* Track the latest in-flight controller so we can abort on unmount /
   * on the next interval tick. Using a ref avoids re-rendering on assignment. */
  const inFlightRef = useRef<AbortController | null>(null);

  const fetchOnce = useCallback(async () => {
    /* Cancel any prior in-flight fetch (e.g. a slow response when the
     * 30s tick fires) so we don't race responses into setState. */
    inFlightRef.current?.abort();
    const controller = new AbortController();
    inFlightRef.current = controller;

    setRefreshing(true);
    try {
      const resp = await client.statsSources(controller.signal);
      if (controller.signal.aborted) return;
      setData(resp);
      setError(null);
      setLastRefreshedAt(Date.now());
    } catch (e) {
      if (controller.signal.aborted) return;
      /* DOMException with name "AbortError" can also surface here. */
      if (e instanceof DOMException && e.name === "AbortError") return;
      if (e instanceof Error) setError(e);
      else setError(new Error(String(e)));
    } finally {
      if (inFlightRef.current === controller) {
        inFlightRef.current = null;
      }
      setRefreshing(false);
    }
  }, [client]);

  useEffect(() => {
    let cancelled = false;
    void fetchOnce();
    const id = window.setInterval(() => {
      if (cancelled) return;
      void fetchOnce();
    }, refreshMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      inFlightRef.current?.abort();
      inFlightRef.current = null;
    };
  }, [fetchOnce, refreshMs]);

  /* Per-bucket counts, derived once per data update. */
  const bucketCounts = useMemo<Record<Bucket, number>>(() => {
    const counts: Record<Bucket, number> = {
      fresh: 0,
      approaching: 0,
      stale: 0,
      "never-synced": 0,
      "no-sla": 0,
    };
    for (const row of data?.sources ?? []) {
      counts[classifyRow(row)] += 1;
    }
    return counts;
  }, [data]);

  const sortedRows = useMemo<SourceStatsRow[]>(() => {
    if (!data) return [];
    const copy = data.sources.slice();
    copy.sort((a, b) => compareRows(a, b, sortKey, sortDir));
    return copy;
  }, [data, sortKey, sortDir]);

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      /* Sensible default direction per column: ages descending (most stale
       * first), text columns ascending. */
      setSortDir(key === "age" || key === "bucket" ? "desc" : "asc");
    }
  }

  /* Distinct render cases. The panel header (title + pulse) is always shown so
   * the auto-refresh indicator stays anchored. */
  return (
    <section
      aria-label="Source freshness"
      className={
        "bg-elevation-1 rounded-xl border border-border shadow-sm" +
        (className ? ` ${className}` : "")
      }
    >
      <PanelHeader
        refreshing={refreshing}
        lastRefreshedAt={lastRefreshedAt}
        refreshMs={refreshMs}
      />
      <div className="px-5 pb-5">
        <Body
          data={data}
          error={error}
          sortedRows={sortedRows}
          bucketCounts={bucketCounts}
          sortKey={sortKey}
          sortDir={sortDir}
          onToggleSort={toggleSort}
        />
      </div>
    </section>
  );
}

/* ────────────────────────── header (always visible) ───────────────────────── */

interface PanelHeaderProps {
  refreshing: boolean;
  lastRefreshedAt: number | null;
  refreshMs: number;
}

function PanelHeader({
  refreshing,
  lastRefreshedAt,
  refreshMs,
}: PanelHeaderProps) {
  /* The pulse: a tiny dot that glows while a refresh is in-flight. We use
   * `aria-live="polite"` for the timer text so a screen reader hears the
   * update without being interrupted. */
  const refreshLabel =
    lastRefreshedAt == null
      ? "Loading..."
      : `Refreshes every ${Math.round(refreshMs / 1000)}s`;

  return (
    <header className="flex items-center justify-between px-5 py-4 border-b border-border">
      <div>
        <h2 className="text-base font-medium text-text">Freshness</h2>
        <p className="text-xs text-text-muted mt-0.5">
          Per-source sync age vs SLA
        </p>
      </div>
      <div
        className="flex items-center gap-2 text-xs text-text-muted"
        aria-live="polite"
      >
        <span
          aria-hidden="true"
          className={
            "h-2 w-2 rounded-full " +
            (refreshing
              ? "bg-accent animate-pulse"
              : "bg-success-fg")
          }
        />
        <span>{refreshLabel}</span>
      </div>
    </header>
  );
}

/* ────────────────────────────── body / states ─────────────────────────────── */

interface BodyProps {
  data: SourcesStatsResponse | null;
  error: ApiError | Error | null;
  sortedRows: SourceStatsRow[];
  bucketCounts: Record<Bucket, number>;
  sortKey: SortKey;
  sortDir: SortDir;
  onToggleSort: (key: SortKey) => void;
}

function Body({
  data,
  error,
  sortedRows,
  bucketCounts,
  sortKey,
  sortDir,
  onToggleSort,
}: BodyProps) {
  /* Initial load: show a loading state instead of the table to avoid
   * rendering an empty <table> shell (and a layout pop when data arrives). */
  if (data == null && error == null) {
    return (
      <p className="text-sm text-text-muted py-12 text-center" role="status">
        Loading freshness…
      </p>
    );
  }

  /* Error: distinguish 403 from other errors so the operator isn't left
   * guessing.  For 403 we surface the server's actual error message (e.g.
   * "Stats endpoints require a workspace-scoped token") because different
   * token configurations produce different 403 reasons.  We keep stale data
   * hidden in this branch — surfacing possibly-incorrect cached data alongside
   * an error is worse than nothing. */
  if (error != null) {
    if (error instanceof ApiError && error.status === 403) {
      /* `error.detail` is the server's `data.detail.message` string,
       * already extracted by ApiClient.request().  Fall back to the generic
       * "missing scope" hint only when the body carried no message. */
      const serverMsg = error.detail || FALLBACK_403_MSG;
      return (
        <div
          role="alert"
          className="rounded-md border border-warning-border bg-warning-bg text-warning-fg px-4 py-3 text-sm my-6"
        >
          <p className="font-medium">Access denied (403)</p>
          <p className="mt-1">{serverMsg}</p>
        </div>
      );
    }
    return (
      <div
        role="alert"
        className="rounded-md border border-danger-border bg-danger-bg text-danger-fg px-4 py-3 text-sm my-6"
      >
        <p className="font-medium">Couldn't load freshness</p>
        <p className="mt-1 break-words">{error.message}</p>
      </div>
    );
  }

  /* Empty: data arrived but no sources at all. */
  if (data != null && data.total === 0) {
    return (
      <div className="text-sm text-text-muted py-12 text-center">
        <p className="text-text">No sources configured</p>
        <p className="mt-1">
          Add a source on the Sources page to start tracking freshness.
        </p>
      </div>
    );
  }

  return (
    <>
      <DistributionBar counts={bucketCounts} total={sortedRows.length} />
      <FreshnessTable
        rows={sortedRows}
        sortKey={sortKey}
        sortDir={sortDir}
        onToggleSort={onToggleSort}
      />
    </>
  );
}

/* ─────────────────────────── distribution bar ─────────────────────────────── */

interface DistributionBarProps {
  counts: Record<Bucket, number>;
  total: number;
}

function DistributionBar({ counts, total }: DistributionBarProps) {
  if (total === 0) return null;

  const segments = BUCKET_ORDER.filter((b) => counts[b] > 0).map((b) => ({
    bucket: b,
    spec: BUCKET_SPECS[b],
    count: counts[b],
    /* `flex-grow` proportional to count keeps math simple and avoids a
     * Math.round drift problem where summed widths != 100%. */
    grow: counts[b],
  }));

  return (
    <figure
      className="my-5"
      aria-label={`Freshness distribution across ${total} sources`}
    >
      <div className="flex h-3 w-full overflow-hidden rounded-full border border-border">
        {segments.map((s) => (
          <div
            key={s.bucket}
            className={`${s.spec.segmentCls}`}
            style={{ flexGrow: s.grow }}
            role="img"
            aria-label={`${s.spec.label}: ${s.count}`}
          />
        ))}
      </div>
      <figcaption className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-secondary">
        {BUCKET_ORDER.map((b) => {
          const spec = BUCKET_SPECS[b];
          const count = counts[b];
          if (count === 0) return null;
          return (
            <span key={b} className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className={`h-2.5 w-2.5 rounded-sm ${spec.swatchCls}`}
              />
              <span className="text-text">{spec.label}</span>
              <span className="text-text-muted tabular-nums">({count})</span>
            </span>
          );
        })}
      </figcaption>
    </figure>
  );
}

/* ───────────────────────────────── table ──────────────────────────────────── */

interface FreshnessTableProps {
  rows: SourceStatsRow[];
  sortKey: SortKey;
  sortDir: SortDir;
  onToggleSort: (key: SortKey) => void;
}

function FreshnessTable({
  rows,
  sortKey,
  sortDir,
  onToggleSort,
}: FreshnessTableProps) {
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      {/* `table-fixed` + per-col widths is what keeps column widths stable
       * across auto-refreshes — content changes don't reflow the layout. */}
      <table className="min-w-full table-fixed text-sm">
        <colgroup>
          <col className="w-[34%]" />
          <col className="w-[14%]" />
          <col className="w-[18%]" />
          <col className="w-[14%]" />
          <col className="w-[20%]" />
        </colgroup>
        <thead className="bg-elevation-2 border-b border-border">
          <tr>
            <SortHeader
              label="Source"
              k="name"
              activeKey={sortKey}
              dir={sortDir}
              onClick={onToggleSort}
            />
            <SortHeader
              label="Type"
              k="type"
              activeKey={sortKey}
              dir={sortDir}
              onClick={onToggleSort}
            />
            <SortHeader
              label="Last sync"
              k="age"
              activeKey={sortKey}
              dir={sortDir}
              onClick={onToggleSort}
            />
            <SortHeader
              label="SLA"
              k="sla"
              activeKey={sortKey}
              dir={sortDir}
              onClick={onToggleSort}
            />
            <SortHeader
              label="Status"
              k="bucket"
              activeKey={sortKey}
              dir={sortDir}
              onClick={onToggleSort}
            />
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((row) => {
            const bucket = classifyRow(row);
            const spec = BUCKET_SPECS[bucket];
            return (
              <tr
                key={row.id}
                tabIndex={0}
                className="hover:bg-elevation-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset"
              >
                <td className="px-4 py-3 text-text truncate" title={row.name}>
                  {row.name}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-text-secondary">
                  {row.type}
                </td>
                <td className="px-4 py-3 text-text-muted tabular-nums">
                  {formatAge(row.age_seconds)}
                </td>
                <td className="px-4 py-3 text-text-muted tabular-nums">
                  {formatSla(row.freshness_sla_seconds)}
                </td>
                <td className="px-4 py-3">
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
  );
}

interface SortHeaderProps {
  label: string;
  k: SortKey;
  activeKey: SortKey;
  dir: SortDir;
  onClick: (k: SortKey) => void;
}

function SortHeader({ label, k, activeKey, dir, onClick }: SortHeaderProps) {
  const isActive = activeKey === k;
  const ariaSort: "ascending" | "descending" | "none" = isActive
    ? dir === "asc"
      ? "ascending"
      : "descending"
    : "none";
  /* The arrow is rendered with a stable width box so column widths don't
   * shift when the active sort changes. */
  const arrow = isActive ? (dir === "asc" ? "▲" : "▼") : "";
  return (
    <th
      scope="col"
      aria-sort={ariaSort}
      className="px-4 py-3 text-left font-medium text-text-secondary"
    >
      <button
        type="button"
        onClick={() => onClick(k)}
        className="inline-flex items-center gap-1.5 rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <span>{label}</span>
        <span aria-hidden="true" className="w-3 text-xs text-text-muted">
          {arrow}
        </span>
      </button>
    </th>
  );
}
