/*
 * SourceEditModal — inline modal for editing an existing source's config.
 *
 * Editable fields: status, freshness_sla_seconds, config (JSON textarea
 * with parse-time validation), and secrets_ref.
 *
 * On save: calls PATCH /api/v1/sources/{id} via client.updateSource() then
 * invokes onSaved(updatedSource) so the parent can do an optimistic refresh
 * without a full re-fetch.
 *
 * Accessibility: dialog role, labelled by heading, focus-trapped on open,
 * Escape closes, no secrets logged to console.
 */

import {
  FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { ApiError, Source, SourceStatus, SourceUpdate } from "../api/client";
import { useTokenContext } from "../context/TokenContext";

const STATUS_OPTIONS: SourceStatus[] = ["active", "paused", "error"];

interface Props {
  source: Source;
  onSaved: (updated: Source) => void;
  onClose: () => void;
}

export function SourceEditModal({ source, onSaved, onClose }: Props) {
  const { client } = useTokenContext();

  const [status, setStatus] = useState<SourceStatus>(source.status);
  const [freshnessRaw, setFreshnessRaw] = useState<string>(
    source.freshness_sla_seconds != null
      ? String(source.freshness_sla_seconds)
      : ""
  );
  const [configRaw, setConfigRaw] = useState<string>(
    JSON.stringify(source.config, null, 2)
  );
  const [secretsRef, setSecretsRef] = useState<string>(
    source.secrets_ref ?? ""
  );

  const [configError, setConfigError] = useState<string | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Close on Escape key
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    },
    [onClose]
  );

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  // Focus the first focusable element on open
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setConfigError(null);
    setApiError(null);

    // Validate JSON config
    let parsedConfig: Record<string, unknown>;
    try {
      parsedConfig = JSON.parse(configRaw);
    } catch {
      setConfigError("Config must be valid JSON.");
      return;
    }

    // Build patch — only include fields present in SourceUpdate
    const patch: SourceUpdate = {
      status,
      config: parsedConfig,
      secrets_ref: secretsRef.trim() !== "" ? secretsRef.trim() : null,
    };

    const freshnessNum = freshnessRaw.trim() !== "" ? Number(freshnessRaw) : null;
    if (freshnessRaw.trim() !== "") {
      if (!Number.isInteger(freshnessNum) || (freshnessNum as number) < 0) {
        setConfigError("Freshness SLA must be a non-negative integer (seconds).");
        return;
      }
      patch.freshness_sla_seconds = freshnessNum as number;
    } else {
      patch.freshness_sla_seconds = null;
    }

    setSaving(true);
    try {
      const updated = await client.updateSource(source.id, patch);
      onSaved(updated);
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      setApiError(msg);
    } finally {
      setSaving(false);
    }
  };

  return (
    /* Backdrop */
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="source-edit-heading"
    >
      {/* Panel */}
      <div className="bg-elevation-1 border border-border rounded-xl shadow-xl w-full max-w-2xl mx-4 flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border flex-shrink-0">
          <div>
            <h2
              id="source-edit-heading"
              ref={headingRef}
              tabIndex={-1}
              className="text-base font-medium text-text focus:outline-none"
            >
              Edit source
            </h2>
            <p className="text-xs text-text-muted mt-0.5">
              <span className="font-mono">{source.name}</span>
              <span className="ml-2 text-text-secondary">({source.type})</span>
            </p>
          </div>
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="p-1.5 rounded-lg text-text-muted hover:text-text hover:bg-elevation-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <svg
              aria-hidden="true"
              className="h-4 w-4"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>

        {/* Form body — scrollable */}
        <form
          onSubmit={(e) => void handleSubmit(e)}
          className="flex flex-col flex-1 overflow-hidden"
        >
          <div className="px-6 py-5 space-y-5 overflow-y-auto flex-1">
            {/* Status */}
            <div>
              <label
                htmlFor="source-edit-status"
                className="block text-sm font-medium text-text-secondary mb-1"
              >
                Status
              </label>
              <select
                id="source-edit-status"
                value={status}
                onChange={(e) => setStatus(e.target.value as SourceStatus)}
                className="w-full border border-border bg-elevation-2 text-text rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
              >
                {STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>

            {/* Freshness SLA */}
            <div>
              <label
                htmlFor="source-edit-freshness"
                className="block text-sm font-medium text-text-secondary mb-1"
              >
                Freshness SLA{" "}
                <span className="text-text-muted font-normal">(seconds, leave blank for none)</span>
              </label>
              <input
                id="source-edit-freshness"
                type="number"
                min={0}
                step={1}
                value={freshnessRaw}
                onChange={(e) => setFreshnessRaw(e.target.value)}
                placeholder="e.g. 3600"
                className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
              />
            </div>

            {/* Secrets ref */}
            <div>
              <label
                htmlFor="source-edit-secrets"
                className="block text-sm font-medium text-text-secondary mb-1"
              >
                Secrets ref{" "}
                <span className="text-text-muted font-normal">(leave blank to clear)</span>
              </label>
              <input
                id="source-edit-secrets"
                type="text"
                value={secretsRef}
                onChange={(e) => setSecretsRef(e.target.value)}
                placeholder="k8s:omniscience/source-secret"
                className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
              />
            </div>

            {/* Config JSON */}
            <div>
              <label
                htmlFor="source-edit-config"
                className="block text-sm font-medium text-text-secondary mb-1"
              >
                Config{" "}
                <span className="text-text-muted font-normal">(JSON)</span>
              </label>
              <textarea
                id="source-edit-config"
                value={configRaw}
                onChange={(e) => {
                  setConfigRaw(e.target.value);
                  setConfigError(null);
                }}
                rows={10}
                spellCheck={false}
                className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent resize-y"
                placeholder='{"cluster_name": "my-cluster"}'
                aria-describedby={configError ? "source-edit-config-error" : undefined}
                aria-invalid={configError != null}
              />
              {configError && (
                <p
                  id="source-edit-config-error"
                  role="alert"
                  className="text-xs text-danger-fg mt-1"
                >
                  {configError}
                </p>
              )}
            </div>

            {/* API error banner */}
            {apiError && (
              <div
                role="alert"
                className="rounded-md border border-danger-border bg-danger-bg text-danger-fg px-4 py-3 text-sm"
              >
                <p className="font-medium">Save failed</p>
                <p className="mt-1 break-words">{apiError}</p>
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex justify-end gap-3 px-6 py-4 border-t border-border flex-shrink-0">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm border border-border rounded-lg text-text-secondary hover:bg-elevation-2 hover:text-text transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="px-4 py-2 text-sm bg-accent text-accent-fg rounded-lg hover:bg-accent-hover disabled:opacity-50 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
            >
              {saving ? "Saving..." : "Save changes"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
