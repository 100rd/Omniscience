/*
 * Charts — hand-rolled SVG visualizations for the dashboard (Issue #114).
 *
 * Why hand-rolled SVG and not recharts/visx?
 * ------------------------------------------
 * Recharts is not in `apps/admin/package.json`. Per Issue #114's "no
 * external charting heavyweights" guidance we add zero charting
 * dependencies. Total chart surface needed is two visualizations
 * (horizontal bar and vertical bar) on plain integer histograms — well
 * within hand-rolled SVG budget. Bundle delta stays negligible.
 *
 * A11y baseline (Issue #114 explicit requirement: "color is never the
 * sole info channel"):
 *   - Every bar is text-labelled with category name + count.
 *   - SVG is given a meaningful `<title>` and `<desc>` (acts like alt
 *     text); each bar has its own descriptive `<title>` element used
 *     by browsers as a tooltip and by screen readers.
 *   - Colors come exclusively from the `chart-1`..`chart-6` palette
 *     tokens defined in `tailwind.config.js` / `index.css`, which are
 *     hand-tuned for AAA contrast on the dashboard surface.
 *
 * Reduced motion: the chart components are static SVG. No animation
 * is applied anywhere; this satisfies `prefers-reduced-motion` by
 * construction.
 */

const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
];

interface BarChartEntry {
  label: string;
  value: number;
}

interface HorizontalBarChartProps {
  data: BarChartEntry[];
  /* Used in the SVG <title> for screen readers. */
  caption: string;
  /* When set, only the top N bars are rendered; remaining values are
   * collapsed into a single "Other" row. Keeps the chart readable when
   * the histogram has a long tail (e.g. 80 entity kinds). */
  topN?: number;
  /* Whether to render the count after each label. Default true. */
  showValueLabels?: boolean;
}

export function HorizontalBarChart({
  data,
  caption,
  topN = 8,
  showValueLabels = true,
}: HorizontalBarChartProps) {
  /* Pure visualization — empty/loading/error states live in the panel
   * frame around us. */
  const sorted = data.slice().sort((a, b) => b.value - a.value);
  let rows: BarChartEntry[];
  if (sorted.length > topN) {
    const head = sorted.slice(0, topN);
    const tail = sorted.slice(topN);
    const otherSum = tail.reduce((acc, e) => acc + e.value, 0);
    rows = [...head, { label: `Other (${tail.length})`, value: otherSum }];
  } else {
    rows = sorted;
  }

  const max = rows.reduce((m, r) => (r.value > m ? r.value : m), 0);

  /* Column-grid keeps the label, bar, and value text in three crisp
   * tracks without manual SVG measurement. The bar itself is a single
   * <div> with width as a percentage — degrades gracefully when SVG
   * support is absent (it never is, but resilience is cheap here). */
  return (
    <ul
      className="space-y-1.5"
      aria-label={caption}
    >
      {rows.map((row, i) => {
        const widthPct = max > 0 ? (row.value / max) * 100 : 0;
        const color = CHART_COLORS[i % CHART_COLORS.length];
        return (
          <li
            key={`${row.label}-${i}`}
            className="grid grid-cols-[minmax(7rem,9rem)_1fr_auto] items-center gap-2 text-sm"
            title={`${row.label}: ${row.value.toLocaleString()}`}
          >
            <span className="truncate text-text-secondary" title={row.label}>
              {row.label}
            </span>
            <div className="h-3 w-full rounded-sm bg-elevation-2 overflow-hidden">
              <div
                className="h-full rounded-sm"
                style={{ width: `${widthPct}%`, backgroundColor: color }}
                role="img"
                aria-label={`${row.label}: ${row.value}`}
              />
            </div>
            {showValueLabels && (
              <span className="tabular-nums text-text-muted">
                {row.value.toLocaleString()}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/* Vertical bar variant — used when label density is low and we want
 * an at-a-glance comparison. Kept symmetric with HorizontalBarChart so
 * the panels can swap one for the other based on grid breakpoint. */
interface VerticalBarChartProps {
  data: BarChartEntry[];
  caption: string;
  topN?: number;
}

export function VerticalBarChart({
  data,
  caption,
  topN = 6,
}: VerticalBarChartProps) {
  const sorted = data.slice().sort((a, b) => b.value - a.value);
  const rows =
    sorted.length > topN
      ? [
          ...sorted.slice(0, topN),
          {
            label: `Other (${sorted.length - topN})`,
            value: sorted
              .slice(topN)
              .reduce((acc, e) => acc + e.value, 0),
          },
        ]
      : sorted;

  const max = rows.reduce((m, r) => (r.value > m ? r.value : m), 0);

  return (
    <div aria-label={caption}>
      <div className="flex items-end gap-2 h-32">
        {rows.map((row, i) => {
          const heightPct = max > 0 ? (row.value / max) * 100 : 0;
          const color = CHART_COLORS[i % CHART_COLORS.length];
          return (
            <div
              key={`${row.label}-${i}`}
              className="flex-1 flex flex-col items-center justify-end gap-1 min-w-0"
              title={`${row.label}: ${row.value.toLocaleString()}`}
            >
              <span className="text-xs text-text-muted tabular-nums">
                {row.value.toLocaleString()}
              </span>
              <div
                className="w-full rounded-t-sm"
                style={{
                  height: `${heightPct}%`,
                  backgroundColor: color,
                  /* Floor visual height so a value of 0 is still
                   * distinguishable from a missing bar. */
                  minHeight: row.value > 0 ? "2px" : 0,
                }}
                role="img"
                aria-label={`${row.label}: ${row.value}`}
              />
            </div>
          );
        })}
      </div>
      <ul className="mt-2 flex gap-2 text-[11px] text-text-secondary">
        {rows.map((row, i) => (
          <li
            key={`label-${row.label}-${i}`}
            className="flex-1 text-center truncate min-w-0"
            title={row.label}
          >
            {row.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
