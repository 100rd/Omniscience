/*
 * IncidentTimelinePage — `/incidents` deep-dive (Issue #235, H5 Track 1).
 *
 * Operators enter an alert id and an optional from/to/entity_types
 * filter; the page calls `GET /api/v1/incidents/{id}/timeline?format=mermaid`
 * and renders the returned block via Mermaid (loaded from the
 * `unpkg.com` CDN on demand to avoid adding a build-time npm dep).
 *
 * The page is read-only. Workspace scoping is implicit — every call
 * goes through the bearer token in `TokenContext`.
 *
 * No raw colour literals — Tailwind theme tokens only (per the
 * `check-no-raw-colors.mjs` lint).
 */

import { useState, FormEvent, useEffect, useRef } from "react";
import { useTokenContext } from "../context/TokenContext";
import {
  ApiError,
  IncidentTimelineResponse,
  TimelineEvent,
} from "../api/client";

interface ToastFn {
  (msg: string, type?: "success" | "error" | "info"): void;
}

interface Props {
  addToast: ToastFn;
}

// Mermaid is loaded lazily from CDN the first time the page renders a
// non-empty diagram. The script tag is appended to <head> and the
// `mermaid` global becomes available; we then call `mermaid.run()` over
// the `.mermaid` blocks on the page. Failures are surfaced as a toast
// but do not block the JSON view.
const MERMAID_CDN = "https://unpkg.com/mermaid@10/dist/mermaid.min.js";

interface MermaidAPI {
  initialize: (cfg: Record<string, unknown>) => void;
  run: (opts?: { nodes?: Element[] }) => Promise<void>;
}

declare global {
  interface Window {
    mermaid?: MermaidAPI;
  }
}

let mermaidLoadPromise: Promise<MermaidAPI> | null = null;

function loadMermaid(): Promise<MermaidAPI> {
  if (typeof window === "undefined") {
    return Promise.reject(new Error("window not available"));
  }
  if (window.mermaid) {
    return Promise.resolve(window.mermaid);
  }
  if (mermaidLoadPromise) {
    return mermaidLoadPromise;
  }
  mermaidLoadPromise = new Promise<MermaidAPI>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = MERMAID_CDN;
    script.async = true;
    script.onload = () => {
      const m = window.mermaid;
      if (!m) {
        reject(new Error("mermaid global not registered after script load"));
        return;
      }
      m.initialize({ startOnLoad: false, theme: "dark" });
      resolve(m);
    };
    script.onerror = () => reject(new Error("failed to load mermaid CDN"));
    document.head.appendChild(script);
  });
  return mermaidLoadPromise;
}

function formatTs(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function EventRow({ event }: { event: TimelineEvent }) {
  const summary =
    event.after_state_summary ?? event.before_state_summary ?? "";
  return (
    <li className="bg-elevation-1 rounded-lg border border-border p-3">
      <div className="flex items-baseline justify-between gap-3 mb-1">
        <span className="text-sm font-medium text-text font-mono">
          {event.entity_id}
        </span>
        <span className="text-xs text-text-muted tabular-nums">
          {formatTs(event.ts)}
        </span>
      </div>
      <div className="flex items-center gap-2 text-xs text-text-muted mb-1">
        <span className="font-mono bg-elevation-2 text-text-secondary rounded px-1.5 py-0.5">
          {event.entity_type}
        </span>
        <span className="font-mono bg-elevation-2 text-text-secondary rounded px-1.5 py-0.5">
          {event.change_kind}
        </span>
        <span>source: {event.source}</span>
      </div>
      {summary && (
        <p className="text-sm text-text-secondary leading-relaxed">
          {summary}
        </p>
      )}
    </li>
  );
}

export function IncidentTimelinePage({ addToast }: Props) {
  const { client } = useTokenContext();

  const [alertId, setAlertId] = useState("");
  const [fromTs, setFromTs] = useState("");
  const [toTs, setToTs] = useState("");
  const [entityTypesRaw, setEntityTypesRaw] = useState("");

  const [json, setJson] = useState<IncidentTimelineResponse | null>(null);
  const [mermaidSrc, setMermaidSrc] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const mermaidRef = useRef<HTMLDivElement | null>(null);

  // Re-render the Mermaid block whenever the source changes.
  useEffect(() => {
    if (!mermaidSrc || !mermaidRef.current) return;
    const node = mermaidRef.current;
    node.innerHTML = `<div class="mermaid">${mermaidSrc
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")}</div>`;
    loadMermaid()
      .then((m) => m.run({ nodes: [node.firstElementChild!] }))
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        addToast(`Mermaid render failed: ${msg}`, "error");
      });
  }, [mermaidSrc, addToast]);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!alertId.trim()) return;
    setLoading(true);
    try {
      const entity_types = entityTypesRaw
        .split(",")
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      const query = {
        from: fromTs || undefined,
        to: toTs || undefined,
        entity_types: entity_types.length ? entity_types : undefined,
      };
      // Fetch JSON + Mermaid in parallel — same query, two formats.
      const [jsonResult, mermaidResult] = await Promise.all([
        client.incidentTimeline(alertId.trim(), query),
        client.incidentTimelineMermaid(alertId.trim(), query),
      ]);
      setJson(jsonResult);
      setMermaidSrc(mermaidResult);
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      addToast(`Timeline failed: ${msg}`, "error");
      setJson(null);
      setMermaidSrc(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <h1 className="text-2xl font-semibold text-text mb-6">
        Incident Timeline
      </h1>

      <form
        onSubmit={handleSubmit}
        className="bg-elevation-1 rounded-xl border border-border shadow-sm p-5 mb-6 space-y-4"
      >
        <div>
          <label className="block text-sm font-medium text-text-secondary mb-1">
            Alert ID
          </label>
          <input
            type="text"
            value={alertId}
            onChange={(e) => setAlertId(e.target.value)}
            placeholder="alert://pagerduty/INC-2026-04-12-001"
            className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent"
          />
        </div>

        <div className="grid grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              From (ISO-8601 UTC)
            </label>
            <input
              type="text"
              value={fromTs}
              onChange={(e) => setFromTs(e.target.value)}
              placeholder="2026-04-12T00:00:00Z"
              className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              To (ISO-8601 UTC)
            </label>
            <input
              type="text"
              value={toTs}
              onChange={(e) => setToTs(e.target.value)}
              placeholder="2026-04-13T00:00:00Z"
              className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              Entity types (comma-separated)
            </label>
            <input
              type="text"
              value={entityTypesRaw}
              onChange={(e) => setEntityTypesRaw(e.target.value)}
              placeholder="k8s_pod, pull_request"
              className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
            />
          </div>
        </div>

        <button
          type="submit"
          disabled={loading || !alertId.trim()}
          className="px-5 py-2 text-sm bg-accent text-accent-fg rounded-lg hover:bg-accent-hover disabled:opacity-50 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {loading ? "Loading…" : "Render timeline"}
        </button>
      </form>

      {json && (
        <div className="space-y-6">
          <div className="bg-elevation-1 rounded-xl border border-border shadow-sm p-5">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-lg font-semibold text-text">Mermaid</h2>
              <span className="text-xs text-text-muted">
                {json.events.length} event{json.events.length === 1 ? "" : "s"}
                {json.truncated ? " (truncated)" : ""}
              </span>
            </div>
            <div
              ref={mermaidRef}
              className="bg-elevation-2 rounded-lg p-4 overflow-x-auto"
            />
            <details className="mt-3">
              <summary className="text-xs text-text-muted cursor-pointer hover:text-text">
                View raw Mermaid source
              </summary>
              <pre className="mt-2 text-xs text-text-secondary bg-elevation-2 rounded-lg p-3 overflow-x-auto whitespace-pre">
                {mermaidSrc}
              </pre>
            </details>
          </div>

          <div className="bg-elevation-1 rounded-xl border border-border shadow-sm p-5">
            <h2 className="text-lg font-semibold text-text mb-3">Events</h2>
            {json.events.length === 0 ? (
              <p className="text-sm text-text-muted">
                No events match the current filters.
              </p>
            ) : (
              <ul className="space-y-2">
                {json.events.map((event, idx) => (
                  <EventRow key={`${event.entity_id}-${event.change_kind}-${idx}`} event={event} />
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
