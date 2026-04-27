/*
 * Sparkline — hand-rolled SVG mini chart (Issue #136).
 *
 * Used by the Retention page to render an at-a-glance trend for each
 * of the four ADR-0009 §8 metrics. Per Issue #114's "no charting
 * heavyweights" rule we add zero new npm dependencies and keep the
 * implementation tiny — the bundle delta budget for #136 is the same
 * as for #114.
 *
 * A11y notes
 * ----------
 *   - The SVG carries a `role="img"` and `aria-label` describing the
 *     metric + current value; screen readers get a meaningful summary
 *     without rendering the visualisation.
 *   - Color comes from the chart palette tokens defined in
 *     tailwind.config.js — never raw hex.
 *   - Static SVG, no animation; satisfies prefers-reduced-motion by
 *     construction.
 *
 * Empty / single-point safety
 * ---------------------------
 *   - 0 points  → renders an inert placeholder strip.
 *   - 1 point   → renders a single dot at the midline (no spurious
 *     line connecting one point to nothing).
 *   - all-equal → renders a flat line at the midline.
 */

interface SparklineProps {
  /* Numeric series, in chronological order. Newest sample LAST so the
   * line trends rightward (the conventional reading order). */
  values: number[];
  /* Accessible label — "Lag (s) trend over the last hour" etc. */
  caption: string;
  /* Width / height in CSS pixels. Defaults match the dashboard card
   * shape; the page-level metric cards override for a wider canvas. */
  width?: number;
  height?: number;
  /* Stroke color CSS variable — defaults to the chart-1 palette token.
   * Callers pass `--chart-2` etc. to differentiate concurrent
   * sparklines without leaving the palette. */
  strokeVar?: string;
}

const DEFAULT_WIDTH = 120;
const DEFAULT_HEIGHT = 32;

export function Sparkline({
  values,
  caption,
  width = DEFAULT_WIDTH,
  height = DEFAULT_HEIGHT,
  strokeVar = "--chart-1",
}: SparklineProps) {
  if (values.length === 0) {
    return (
      <svg
        role="img"
        aria-label={`${caption} (no data yet)`}
        width={width}
        height={height}
        className="block"
      >
        <line
          x1={0}
          y1={height / 2}
          x2={width}
          y2={height / 2}
          stroke="var(--text-muted)"
          strokeOpacity={0.3}
          strokeDasharray="2 3"
          strokeWidth={1}
        />
      </svg>
    );
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min;
  const xStep = values.length > 1 ? width / (values.length - 1) : 0;

  /* Map each value to an SVG y coordinate. With range == 0 we draw a
   * flat line at the vertical midpoint — a no-op series should look
   * stable, not collapsed to the top or bottom. */
  const yFor = (v: number): number => {
    if (range === 0) return height / 2;
    /* Pad 2px top/bottom so the stroke does not clip at extremes. */
    const usable = height - 4;
    return 2 + ((max - v) / range) * usable;
  };

  if (values.length === 1) {
    return (
      <svg
        role="img"
        aria-label={`${caption} (current ${values[0]})`}
        width={width}
        height={height}
        className="block"
      >
        <circle
          cx={width / 2}
          cy={yFor(values[0])}
          r={2.5}
          fill={`var(${strokeVar})`}
        />
      </svg>
    );
  }

  const points = values
    .map((v, i) => `${(i * xStep).toFixed(2)},${yFor(v).toFixed(2)}`)
    .join(" ");

  const last = values[values.length - 1];
  return (
    <svg
      role="img"
      aria-label={`${caption} (current ${last})`}
      width={width}
      height={height}
      className="block"
    >
      <polyline
        points={points}
        fill="none"
        stroke={`var(${strokeVar})`}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* Trailing dot anchors the eye on the latest sample — the
       * conventional sparkline endpoint emphasis. */}
      <circle
        cx={(values.length - 1) * xStep}
        cy={yFor(last)}
        r={2}
        fill={`var(${strokeVar})`}
      />
    </svg>
  );
}
