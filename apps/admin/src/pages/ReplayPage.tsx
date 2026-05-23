// Replay page (Issue #243) — admin time-machine view.
//
// The page lets an auditor paste an audit_log_id and renders:
//
//   * the original response (loaded from the audit row), and
//   * the replayed response at the recorded T, and
//   * the state_fingerprint of each, plus a match badge.
//
// Drift (fingerprint_match === false) is highlighted with a yellow
// banner — that is the legitimate ADR-0008 §2 retroactive-correction
// scenario, not an error.  See docs/api/replay.md.

import { FormEvent, useState } from "react";
import { useTokenContext } from "../context/TokenContext";
import { ApiError, ReplayEnvelope } from "../api/client";

interface ToastFn {
  (msg: string, type?: "success" | "error" | "info"): void;
}

interface Props {
  addToast: ToastFn;
}

function FingerprintBadge({
  match,
}: {
  match: boolean | null;
}) {
  if (match === null) {
    return (
      <span className="rounded bg-elevation-2 px-2 py-0.5 font-mono text-xs text-text-muted">
        inline replay — no original to compare
      </span>
    );
  }
  if (match) {
    return (
      <span className="rounded bg-emerald-900/40 px-2 py-0.5 font-mono text-xs text-emerald-300">
        fingerprint match — reproducible
      </span>
    );
  }
  return (
    <span className="rounded bg-amber-900/40 px-2 py-0.5 font-mono text-xs text-amber-300">
      fingerprint differs — retroactive correction landed
    </span>
  );
}

function ResponsePanel({
  title,
  payload,
  fingerprint,
}: {
  title: string;
  payload: unknown;
  fingerprint: string | null;
}) {
  return (
    <div className="bg-elevation-1 rounded-xl border border-border shadow-sm p-5">
      <div className="flex items-baseline justify-between mb-3">
        <h2 className="text-sm font-semibold text-text">{title}</h2>
        {fingerprint && (
          <code className="text-[10px] text-text-muted font-mono truncate max-w-[60%]">
            {fingerprint}
          </code>
        )}
      </div>
      <pre className="text-xs text-text-secondary bg-elevation-2 rounded p-3 overflow-auto max-h-[420px]">
        {JSON.stringify(payload, null, 2)}
      </pre>
    </div>
  );
}

export function ReplayPage({ addToast }: Props) {
  const { client } = useTokenContext();
  const [auditId, setAuditId] = useState("");
  const [envelope, setEnvelope] = useState<ReplayEnvelope | null>(null);
  const [loading, setLoading] = useState(false);

  const handleReplay = async (e: FormEvent) => {
    e.preventDefault();
    if (!auditId.trim()) return;
    setLoading(true);
    setEnvelope(null);
    try {
      const result = await client.replayByAuditId(auditId.trim());
      setEnvelope(result);
      if (result.fingerprint_match === false) {
        addToast(
          "Replay completed; state has drifted since the original call",
          "info"
        );
      } else {
        addToast("Replay completed", "success");
      }
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      addToast(`Replay failed: ${msg}`, "error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <h1 className="text-2xl font-semibold text-text mb-2">
        Time-Machine Replay
      </h1>
      <p className="text-sm text-text-muted mb-6">
        Paste an <code className="font-mono">audit_log_id</code> from a
        previous tool invocation to re-run it against the bitemporal
        graph state at the recorded time. The two responses and their
        state-fingerprints are shown side-by-side. See{" "}
        <a
          className="text-accent hover:text-accent-hover hover:underline"
          href="/docs/api/replay.md"
          target="_blank"
          rel="noreferrer"
        >
          docs/api/replay.md
        </a>{" "}
        for the ADP rationale.
      </p>

      <form
        onSubmit={handleReplay}
        className="bg-elevation-1 rounded-xl border border-border shadow-sm p-5 mb-6 flex gap-3 items-end"
      >
        <div className="flex-1">
          <label className="block text-sm font-medium text-text-secondary mb-1">
            audit_log_id
          </label>
          <input
            type="text"
            value={auditId}
            onChange={(e) => setAuditId(e.target.value)}
            placeholder="8a7e5e1d-...-1e3c"
            className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent"
          />
        </div>
        <button
          type="submit"
          disabled={loading || !auditId.trim()}
          className="px-4 py-2 rounded-lg bg-accent text-accent-fg text-sm font-medium hover:bg-accent-hover disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {loading ? "Replaying..." : "Replay"}
        </button>
      </form>

      {envelope && (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-3 text-xs text-text-secondary">
            <span>
              Tool:{" "}
              <code className="font-mono text-text">{envelope.tool_name}</code>
            </span>
            <span>
              At:{" "}
              <code className="font-mono text-text">{envelope.at_time}</code>
            </span>
            <span>
              Algorithm:{" "}
              <code className="font-mono text-text-muted">
                {envelope.fingerprint_algorithm}
              </code>
            </span>
            <FingerprintBadge match={envelope.fingerprint_match} />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <ResponsePanel
              title="Replayed (at recorded T)"
              payload={envelope.response}
              fingerprint={envelope.state_fingerprint}
            />
            <ResponsePanel
              title="Original (from audit row)"
              payload={
                envelope.original_state_fingerprint
                  ? {
                      note:
                        "Original response body is preserved in the " +
                        "audit_log row.  Fetch with a separate audit-log " +
                        "browser when needed.",
                      original_state_fingerprint:
                        envelope.original_state_fingerprint,
                    }
                  : { note: "Inline replay — no audit row" }
              }
              fingerprint={envelope.original_state_fingerprint}
            />
          </div>
        </>
      )}
    </div>
  );
}
