/*
 * DashboardPage — single-pane operator view (Issue #114).
 *
 * Composed of six independent panels. Each panel:
 *   - owns its own data fetch + auto-refresh cadence (30s for fast-
 *     moving overview/clients, 60s for slower-moving sources/schema)
 *   - renders its own loading / empty / populated / error / 403 states
 *     via `PanelBody` so a single failing panel cannot crash the page
 *   - is wrapped in `PanelErrorBoundary` to also catch render-time
 *     exceptions (`PanelFrame` does this internally for the panels we
 *     own; FreshnessPanel from #112 is wrapped here at the call site)
 *
 * Layout (Tailwind grid, dark theme tokens only):
 *
 *   [ 24h delta strip                                                ]   ← thin top strip
 *   [ Overview (totals)                                              ]
 *
 *   [ Schema volume                              ][ Clients         ]   ← split at xl
 *   [ Sources                                    ][ Freshness       ]   ← split at xl
 *
 * Breakpoints — Tailwind defaults:
 *   - default (< md, < 768): single column, every panel full width
 *   - md (>= 768)         : DeltaStrip 3-col internal; overview tiles 3-col
 *   - lg (>= 1024)        : Clients body splits its tokens/tools list
 *   - xl (>= 1280, ~1440) : main grid splits 7/5 — schema vs clients,
 *                           sources vs freshness
 *   - 2xl (>= 1536, ~1920): unchanged from xl; column ratios stay
 *                           operator-readable on ultra-wide
 *
 * Issue #114 specifies sanity at 1440 / 1920 / 2560. The layout uses
 * a 12-column grid with `xl:` breakpoint so 1440 picks up the
 * two-column split; at 1920 and 2560 the same split applies
 * (panels grow with viewport via the layout's max-width container).
 */

import { ApiError } from "../api/client";
import { Component, ReactNode } from "react";
import { ClientsPanel } from "../components/dashboard/ClientsPanel";
import { DeltaStrip } from "../components/dashboard/DeltaStrip";
import { OverviewHeader } from "../components/dashboard/OverviewHeader";
import { PanelErrorBoundary } from "../components/dashboard/PanelErrorBoundary";
import { SchemaVolumePanel } from "../components/dashboard/SchemaVolumePanel";
import { SourcesPanel } from "../components/dashboard/SourcesPanel";
import { FreshnessPanel } from "../components/FreshnessPanel";

export function DashboardPage() {
  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-text">Dashboard</h1>
        <FreshnessHealthIndicator />
      </header>

      {/*
       * Top strip: 24h activity. Thin row above the main grid so the
       * three numbers are the first thing the operator scans.
       */}
      <DeltaStrip />

      {/* Overview header strip (totals). */}
      <OverviewHeader />

      {/* Main 12-col grid — splits at xl. */}
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-6">
        <div className="xl:col-span-7">
          <SchemaVolumePanel />
        </div>
        <div className="xl:col-span-5">
          <ClientsPanel />
        </div>
        <div className="xl:col-span-7">
          <SourcesPanel />
        </div>
        <div className="xl:col-span-5">
          {/*
           * FreshnessPanel from #112 is embedded verbatim. It already
           * handles 403 / loading / empty / populated / fetch-error
           * states internally. Wrap with PanelErrorBoundary to also
           * catch render-time crashes — keeps the per-panel
           * isolation contract uniform across the whole page.
           */}
          <PanelErrorBoundary panelTitle="Freshness">
            <FreshnessPanel />
          </PanelErrorBoundary>
        </div>
      </div>
    </div>
  );
}

/*
 * FreshnessHealthIndicator — small "API ok" badge in the page header.
 *
 * The previous DashboardPage rendered an "API ok" pill driven by a
 * GET /health call. We keep the affordance — it's the at-a-glance
 * "is the server up at all" signal — but isolate it from any of the
 * data panels: a flapping /health does not affect any panel below.
 *
 * Because the badge is the very first thing in the page header and
 * has its own tiny lifecycle, we keep its implementation inline
 * rather than promoting it to its own file.
 */
class FreshnessHealthIndicator extends Component<
  Record<string, never>,
  { status: "ok" | "down" | "loading"; statusCode: number | null }
> {
  private intervalId: number | null = null;
  private controller: AbortController | null = null;

  constructor(props: Record<string, never>) {
    super(props);
    this.state = { status: "loading", statusCode: null };
  }

  componentDidMount(): void {
    void this.poll();
    this.intervalId = window.setInterval(() => {
      void this.poll();
    }, 30_000);
  }

  componentWillUnmount(): void {
    if (this.intervalId != null) window.clearInterval(this.intervalId);
    this.controller?.abort();
  }

  private async poll(): Promise<void> {
    this.controller?.abort();
    const c = new AbortController();
    this.controller = c;
    try {
      const res = await fetch("/health", { signal: c.signal });
      if (c.signal.aborted) return;
      if (res.ok) {
        this.setState({ status: "ok", statusCode: res.status });
      } else {
        this.setState({ status: "down", statusCode: res.status });
      }
    } catch (e) {
      if (c.signal.aborted) return;
      if (e instanceof DOMException && e.name === "AbortError") return;
      this.setState({ status: "down", statusCode: null });
    }
  }

  render(): ReactNode {
    const { status, statusCode } = this.state;
    if (status === "loading") {
      return (
        <span className="inline-flex items-center gap-1.5 text-sm text-text-muted">
          <span className="h-2 w-2 rounded-full bg-text-muted" aria-hidden />
          API checking…
        </span>
      );
    }
    const okay = status === "ok";
    return (
      <span
        className={`inline-flex items-center gap-1.5 text-sm font-medium ${
          okay ? "text-success-fg" : "text-danger-fg"
        }`}
      >
        <span
          aria-hidden="true"
          className={`h-2 w-2 rounded-full ${
            okay ? "bg-success-fg" : "bg-danger-fg"
          }`}
        />
        API {okay ? "ok" : statusCode ?? "down"}
      </span>
    );
  }
}

/* Re-export ApiError so dashboard tooling can typecheck against the
 * exact error class used in panel error branches. Avoids consumers
 * having to deep-import. */
export { ApiError };
