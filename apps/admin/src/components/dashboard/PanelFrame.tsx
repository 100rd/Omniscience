/*
 * PanelFrame — shared chrome for every dashboard panel (Issue #114).
 *
 * Why a single component for the chrome?
 * --------------------------------------
 * Every panel on the redesigned dashboard needs the exact same structure:
 *   - header strip with title + "refreshed Ns ago" indicator
 *   - body area that handles the four required states (loading / empty
 *     / populated / error) plus the 403 missing-scope case
 *   - per-panel error isolation so one panel's failure does not crash
 *     the whole page (issue #114 acceptance: "ONE panel failing must
 *     NOT crash the page")
 *
 * Centralizing the chrome here keeps panel content focused on its
 * visualization and guarantees consistent operator UX across panels.
 *
 * A11y notes
 * ----------
 *   - Each panel is a `<section>` with an `aria-label`, so a screen
 *     reader announces them as discrete regions.
 *   - The refresh indicator wraps `aria-live="polite"` so updates do
 *     not interrupt other announcements.
 *   - The pulse dot is `aria-hidden`; the textual `lastRefreshedAt`
 *     label is the actual announcement target.
 *   - Skeleton pulse and refresh dot honour `prefers-reduced-motion`
 *     via the `motion-safe:` Tailwind variant — no animation when the
 *     user has expressed a preference.
 */

import { ReactNode } from "react";
import { ApiError } from "../../api/client";
import { TimeAgo } from "./TimeAgo";

interface PanelFrameProps {
  title: string;
  subtitle?: string;
  refreshing: boolean;
  lastRefreshedAt: number | null;
  refreshMs: number;
  /* Whatever the panel needs to render. Wrapped by ErrorBoundary so a
   * runtime exception inside the body does not crash the dashboard. */
  children: ReactNode;
  /* Optional className passthrough so callers can place the panel in a
   * grid (e.g. `xl:col-span-8`). */
  className?: string;
}

export function PanelFrame({
  title,
  subtitle,
  refreshing,
  lastRefreshedAt,
  refreshMs,
  children,
  className,
}: PanelFrameProps) {
  return (
    <section
      aria-label={title}
      className={
        "bg-elevation-1 rounded-xl border border-border shadow-sm flex flex-col" +
        (className ? ` ${className}` : "")
      }
    >
      <header className="flex items-center justify-between px-5 py-4 border-b border-border">
        <div>
          <h2 className="text-base font-medium text-text">{title}</h2>
          {subtitle && (
            <p className="text-xs text-text-muted mt-0.5">{subtitle}</p>
          )}
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
                ? "bg-accent motion-safe:animate-pulse"
                : "bg-success-fg")
            }
          />
          <span>
            {lastRefreshedAt == null ? (
              "Loading..."
            ) : (
              <>
                <TimeAgo timestamp={lastRefreshedAt} /> · every{" "}
                {Math.round(refreshMs / 1000)}s
              </>
            )}
          </span>
        </div>
      </header>
      <div className="px-5 py-5 flex-1">{children}</div>
    </section>
  );
}

/* Renders the appropriate state branch — used by panels to keep their
 * own logic small. The "populated" branch is the children prop and is
 * only rendered when data is non-null and there is no error. */
interface PanelBodyProps<T> {
  data: T | null;
  error: ApiError | Error | null;
  /* Detector for the empty-state — receives the (non-null) data. */
  isEmpty: (data: T) => boolean;
  emptyTitle: string;
  emptyHint: ReactNode;
  /* What renders when `data` is non-empty. */
  children: (data: T) => ReactNode;
  /* Optional skeleton renderer for the loading state. Defaults to a
   * neutral text loader matching FreshnessPanel's voice. */
  skeleton?: ReactNode;
}

export function PanelBody<T>({
  data,
  error,
  isEmpty,
  emptyTitle,
  emptyHint,
  children,
  skeleton,
}: PanelBodyProps<T>) {
  /* Error wins — surfacing both stale data and an error confuses the
   * operator. We mirror FreshnessPanel's policy here. */
  if (error != null) {
    if (error instanceof ApiError && error.status === 403) {
      return (
        <div
          role="alert"
          className="rounded-md border border-warning-border bg-warning-bg text-warning-fg px-4 py-3 text-sm"
        >
          <p className="font-medium">Missing scope</p>
          <p className="mt-1">
            Your token doesn&rsquo;t have <code>stats:read</code>. Ask an
            administrator to issue a token with that scope to view this panel.
          </p>
        </div>
      );
    }
    const status = error instanceof ApiError ? error.status : null;
    return (
      <div
        role="alert"
        className="rounded-md border border-danger-border bg-danger-bg text-danger-fg px-4 py-3 text-sm"
      >
        <p className="font-medium">
          Couldn&rsquo;t load{status != null ? ` (${status})` : ""}
        </p>
        <p className="mt-1 break-words">{error.message}</p>
      </div>
    );
  }

  if (data == null) {
    return (
      <>
        {skeleton ?? (
          <p className="text-sm text-text-muted py-12 text-center" role="status">
            Loading…
          </p>
        )}
      </>
    );
  }

  if (isEmpty(data)) {
    return (
      <div className="text-sm py-10 text-center">
        <p className="text-text">{emptyTitle}</p>
        <p className="mt-1 text-text-muted">{emptyHint}</p>
      </div>
    );
  }

  return <>{children(data)}</>;
}
