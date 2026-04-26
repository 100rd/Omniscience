/*
 * PanelErrorBoundary — per-panel runtime exception isolation (Issue #114).
 *
 * Issue #114 acceptance: "ONE panel failing must NOT crash the page.
 * The other panels render normally; the failed panel shows an inline
 * error message."
 *
 * `usePanelFetch` already turns API errors into the `error` prop
 * handled by `PanelBody`. This boundary handles a *different* class of
 * failure: a runtime exception thrown during render of the panel body
 * (e.g. an unexpected null deref on a malformed response that slipped
 * past our types). React requires class components for boundaries.
 *
 * The boundary renders the same visual error treatment as the API
 * error path so operators see one consistent error UX regardless of
 * the failure mode.
 */

import { Component, ReactNode } from "react";

interface Props {
  panelTitle: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class PanelErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error): void {
    /* Console-only — no remote logging plumbing in this PR. The boundary's
     * job here is to prevent page-wide crash; observability for runtime
     * panel errors will land separately if/when we add a logger. */
    // eslint-disable-next-line no-console
    console.error(`[dashboard panel] ${this.props.panelTitle} crashed:`, error);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div
          role="alert"
          className="rounded-md border border-danger-border bg-danger-bg text-danger-fg px-4 py-3 text-sm"
        >
          <p className="font-medium">Panel crashed</p>
          <p className="mt-1 break-words">{this.state.error.message}</p>
        </div>
      );
    }
    return this.props.children;
  }
}
