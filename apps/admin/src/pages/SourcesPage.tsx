import { useEffect, useState, FormEvent } from "react";
import { useTokenContext } from "../context/TokenContext";
import { Source, SourceCreate, SourceType, ApiError } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { SourceEditModal } from "../components/SourceEditModal";
import { getSourceTemplate } from "../api/sourceTemplates";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format a last_sync_at ISO timestamp as a human-readable relative time,
 *  falling back to "Never" when null. */
function formatLastSync(isoString: string | null): string {
  if (isoString === null) return "Never";
  const ts = Date.parse(isoString);
  if (Number.isNaN(ts)) return isoString; // malformed — show raw
  const diffMs = Date.now() - ts;
  if (diffMs < 0) return "just now";
  const s = Math.floor(diffMs / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86_400)}d ago`;
}

// ---------------------------------------------------------------------------
// Discovery settings
// ---------------------------------------------------------------------------

function DiscoverySettings({ addToast }: { addToast: ToastFn }) {
  const { client } = useTokenContext();
  const [metadata, setMetadata] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSubmitting] = useState(false);

  useEffect(() => {
    client
      .getWorkspace()
      .then((ws) => setMetadata(ws.metadata))
      .catch(() => addToast("Failed to load workspace settings", "error"))
      .finally(() => setLoading(false));
  }, [client, addToast]);

  const handleSave = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    try {
      await client.updateWorkspace(metadata);
      addToast("Discovery settings saved.", "success");
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      addToast(msg || "Save failed", "error");
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) return null;

  const discovery = metadata.discovery as Record<string, unknown> | undefined;
  const github = (discovery?.github ?? {}) as Record<string, string>;

  return (
    <div className="bg-elevation-1 rounded-xl border border-border shadow-sm p-6 mb-8">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-base font-medium text-text">
          Auto-Discovery (v0.4)
        </h2>
        <div className="text-[10px] bg-accent/10 text-accent px-2 py-0.5 rounded uppercase font-bold tracking-wider">
          Experimental
        </div>
      </div>
      <form onSubmit={(e) => void handleSave(e)} className="space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label
              htmlFor="disc-github-org"
              className="block text-xs font-medium text-text-secondary uppercase mb-1"
            >
              GitHub Org
            </label>
            <input
              id="disc-github-org"
              type="text"
              value={github.org ?? ""}
              onChange={(e) =>
                setMetadata({
                  ...metadata,
                  discovery: {
                    ...discovery,
                    github: { ...github, org: e.target.value },
                  },
                })
              }
              placeholder="my-org"
              className="w-full border border-border bg-elevation-2 text-text rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
            />
          </div>
          <div>
            <label
              htmlFor="disc-github-token"
              className="block text-xs font-medium text-text-secondary uppercase mb-1"
            >
              GitHub Token
            </label>
            <input
              id="disc-github-token"
              type="password"
              value={github.token ?? ""}
              onChange={(e) =>
                setMetadata({
                  ...metadata,
                  discovery: {
                    ...discovery,
                    github: { ...github, token: e.target.value },
                  },
                })
              }
              placeholder="ghp_***"
              className="w-full border border-border bg-elevation-2 text-text rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
            />
          </div>
          <div>
            <label
              htmlFor="disc-github-pattern"
              className="block text-xs font-medium text-text-secondary uppercase mb-1"
            >
              Include Pattern (Regex)
            </label>
            <input
              id="disc-github-pattern"
              type="text"
              value={github.include_pattern ?? ""}
              onChange={(e) =>
                setMetadata({
                  ...metadata,
                  discovery: {
                    ...discovery,
                    github: { ...github, include_pattern: e.target.value },
                  },
                })
              }
              placeholder="-(service|api)$"
              className="w-full border border-border bg-elevation-2 text-text rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
            />
          </div>
        </div>
        <div className="flex justify-end">
          <button
            type="submit"
            disabled={saving}
            className="px-4 py-2 text-sm bg-accent text-accent-fg rounded-lg hover:bg-accent-hover disabled:opacity-50 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {saving ? "Saving..." : "Save Discovery Config"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add source form
// ---------------------------------------------------------------------------

const SOURCE_TYPES: SourceType[] = [
  "git",
  "fs",
  "confluence",
  "notion",
  "slack",
  "jira",
  "grafana",
  "k8s",
  "terraform",
  "s3",
  "aws",
  "alerts",
  "otel",
  "k8s_operator",
];

interface ToastFn {
  (msg: string, type?: "success" | "error" | "info"): void;
}

interface Props {
  addToast: ToastFn;
}

function AddSourceForm({
  onCreated,
  addToast,
}: {
  onCreated: (s: Source) => void;
  addToast: ToastFn;
}) {
  const { client } = useTokenContext();
  const [open, setOpen] = useState(false);
  const [type, setType] = useState<SourceType>("git");
  const [configRaw, setConfigRaw] = useState(
    () => getSourceTemplate("git").configJson
  );
  const [configHint, setConfigHint] = useState(
    () => getSourceTemplate("git").hint
  );
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);

  const handleTypeChange = (newType: SourceType) => {
    setType(newType);
    const tmpl = getSourceTemplate(newType);
    setConfigRaw(tmpl.configJson);
    setConfigHint(tmpl.hint);
    setConfigError(null);
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setConfigError(null);

    let config: Record<string, unknown>;
    try {
      config = JSON.parse(configRaw);
    } catch {
      setConfigError("Config must be valid JSON.");
      return;
    }

    setSubmitting(true);
    try {
      const payload: SourceCreate = { type, name: name.trim(), config };
      const created = await client.createSource(payload);
      onCreated(created);
      addToast(`Source "${created.name}" created.`, "success");
      setName("");
      const resetTmpl = getSourceTemplate(type);
      setConfigRaw(resetTmpl.configJson);
      setOpen(false);
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      addToast(`Failed to create source: ${msg}`, "error");
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="px-4 py-2 text-sm bg-accent text-accent-fg rounded-lg hover:bg-accent-hover transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
      >
        Add source
      </button>
    );
  }

  return (
    <div className="bg-elevation-1 rounded-xl border border-border shadow-sm p-6 mb-6">
      <h2 className="text-base font-medium text-text mb-4">Add source</h2>
      <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label
              htmlFor="add-source-type"
              className="block text-sm font-medium text-text-secondary mb-1"
            >
              Type
            </label>
            <select
              id="add-source-type"
              value={type}
              onChange={(e) =>
                handleTypeChange(e.target.value as SourceType)
              }
              className="w-full border border-border bg-elevation-2 text-text rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
            >
              {SOURCE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label
              htmlFor="add-source-name"
              className="block text-sm font-medium text-text-secondary mb-1"
            >
              Name
            </label>
            <input
              id="add-source-name"
              type="text"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-repo"
              className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
            />
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between mb-1">
            <label
              htmlFor="add-source-config"
              className="block text-sm font-medium text-text-secondary"
            >
              Config (JSON)
            </label>
            <button
              type="button"
              onClick={() => {
                const tmpl = getSourceTemplate(type);
                setConfigRaw(tmpl.configJson);
                setConfigHint(tmpl.hint);
                setConfigError(null);
              }}
              className="text-xs text-accent hover:text-accent-hover transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
            >
              Load example
            </button>
          </div>
          <textarea
            id="add-source-config"
            value={configRaw}
            onChange={(e) => {
              setConfigRaw(e.target.value);
              setConfigError(null);
            }}
            rows={8}
            spellCheck={false}
            className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent resize-y"
            aria-describedby={
              configError ? "add-source-config-error" : "add-source-config-hint"
            }
            aria-invalid={configError != null}
          />
          {configError ? (
            <p
              id="add-source-config-error"
              role="alert"
              className="text-xs text-danger-fg mt-1"
            >
              {configError}
            </p>
          ) : (
            <p
              id="add-source-config-hint"
              className="text-xs text-text-muted mt-1"
            >
              {configHint}
            </p>
          )}
        </div>

        <div className="flex gap-3">
          <button
            type="submit"
            disabled={submitting}
            className="px-4 py-2 text-sm bg-accent text-accent-fg rounded-lg hover:bg-accent-hover disabled:opacity-50 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {submitting ? "Creating..." : "Create"}
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="px-4 py-2 text-sm border border-border rounded-lg text-text-secondary hover:bg-elevation-2 hover:text-text transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SourcesPage
// ---------------------------------------------------------------------------

export function SourcesPage({ addToast }: Props) {
  const { client } = useTokenContext();
  const [sources, setSources] = useState<Source[]>([]);
  const [loading, setLoading] = useState(true);
  const [deleteTarget, setDeleteTarget] = useState<Source | null>(null);
  const [editTarget, setEditTarget] = useState<Source | null>(null);
  const [syncingIds, setSyncingIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    client
      .listSources()
      .then((s) => {
        if (!cancelled) setSources(s);
      })
      .catch((e) => addToast(String(e), "error"))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [client, addToast]);

  const handleCreated = (s: Source) => setSources((prev) => [s, ...prev]);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await client.deleteSource(deleteTarget.id);
      setSources((prev) => prev.filter((s) => s.id !== deleteTarget.id));
      addToast(`Source "${deleteTarget.name}" deleted.`, "success");
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      addToast(`Delete failed: ${msg}`, "error");
    } finally {
      setDeleteTarget(null);
    }
  };

  const handleSync = async (source: Source) => {
    setSyncingIds((prev) => new Set(prev).add(source.id));
    try {
      const { run_id } = await client.triggerSync(source.id);
      addToast(`Sync triggered (run ${run_id.slice(0, 8)}).`, "success");
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      addToast(`Sync failed: ${msg}`, "error");
    } finally {
      setSyncingIds((prev) => {
        const next = new Set(prev);
        next.delete(source.id);
        return next;
      });
    }
  };

  const handleSaved = (updated: Source) => {
    setSources((prev) =>
      prev.map((s) => (s.id === updated.id ? updated : s))
    );
    addToast(`Source "${updated.name}" updated.`, "success");
    setEditTarget(null);
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-text">Sources</h1>
        <AddSourceForm onCreated={handleCreated} addToast={addToast} />
      </div>

      <DiscoverySettings addToast={addToast} />

      {loading ? (
        <p className="text-sm text-text-muted py-12 text-center">Loading...</p>
      ) : sources.length === 0 ? (
        <div className="bg-elevation-1 rounded-xl border border-border shadow-sm px-6 py-12 text-center text-sm text-text-muted">
          No sources configured yet.
        </div>
      ) : (
        <div className="bg-elevation-1 rounded-xl border border-border shadow-sm overflow-hidden">
          <table className="min-w-full text-sm">
            <thead className="bg-elevation-2 border-b border-border">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Name
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Type
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Status
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Last sync
                </th>
                <th className="px-4 py-3 text-right font-medium text-text-secondary">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {sources.map((src) => (
                <tr key={src.id} className="hover:bg-elevation-2">
                  <td className="px-4 py-3 font-medium text-text">
                    {src.name}
                    {src.last_error && (
                      <p className="text-xs text-danger-fg mt-0.5 truncate max-w-xs">
                        {src.last_error}
                      </p>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-text-secondary">
                    {src.type}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge value={src.status} />
                  </td>
                  <td
                    className="px-4 py-3 text-text-muted tabular-nums text-xs"
                    title={
                      src.last_sync_at
                        ? new Date(src.last_sync_at).toLocaleString()
                        : undefined
                    }
                  >
                    {formatLastSync(src.last_sync_at)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => setEditTarget(src)}
                        className="px-3 py-1.5 text-xs border border-border rounded-lg text-text-secondary hover:bg-elevation-2 hover:text-text transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => void handleSync(src)}
                        disabled={syncingIds.has(src.id)}
                        className="px-3 py-1.5 text-xs border border-border rounded-lg text-text-secondary hover:bg-elevation-2 hover:text-text disabled:opacity-50 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        {syncingIds.has(src.id) ? "Syncing..." : "Sync"}
                      </button>
                      <button
                        onClick={() => setDeleteTarget(src)}
                        className="px-3 py-1.5 text-xs border border-danger-border text-danger-fg rounded-lg hover:bg-danger-bg transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editTarget && (
        <SourceEditModal
          source={editTarget}
          onSaved={handleSaved}
          onClose={() => setEditTarget(null)}
        />
      )}

      {deleteTarget && (
        <ConfirmDialog
          message={`Delete source "${deleteTarget.name}"? This action cannot be undone.`}
          onConfirm={() => void handleDelete()}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
