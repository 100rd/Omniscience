import { useEffect, useState, FormEvent } from "react";
import { useTokenContext } from "../context/TokenContext";
import { Source, SourceCreate, SourceType, ApiError } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { ConfirmDialog } from "../components/ConfirmDialog";

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
  const [name, setName] = useState("");
  const [configRaw, setConfigRaw] = useState("{}");
  const [submitting, setSubmitting] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);

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
      setConfigRaw("{}");
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
        className="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors"
      >
        Add source
      </button>
    );
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 mb-6">
      <h2 className="text-base font-medium text-gray-900 mb-4">Add source</h2>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Type
            </label>
            <select
              value={type}
              onChange={(e) => setType(e.target.value as SourceType)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            >
              {SOURCE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Name
            </label>
            <input
              type="text"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-repo"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Config (JSON)
          </label>
          <textarea
            value={configRaw}
            onChange={(e) => setConfigRaw(e.target.value)}
            rows={5}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500"
            placeholder='{"repo_url": "https://github.com/..."}'
          />
          {configError && (
            <p className="text-xs text-red-600 mt-1">{configError}</p>
          )}
        </div>

        <div className="flex gap-3">
          <button
            type="submit"
            disabled={submitting}
            className="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
          >
            {submitting ? "Creating..." : "Create"}
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50 transition-colors"
          >
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}

export function SourcesPage({ addToast }: Props) {
  const { client } = useTokenContext();
  const [sources, setSources] = useState<Source[]>([]);
  const [loading, setLoading] = useState(true);
  const [deleteTarget, setDeleteTarget] = useState<Source | null>(null);
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

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-gray-900">Sources</h1>
        <AddSourceForm onCreated={handleCreated} addToast={addToast} />
      </div>

      {loading ? (
        <p className="text-sm text-gray-500 py-12 text-center">Loading...</p>
      ) : sources.length === 0 ? (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm px-6 py-12 text-center text-sm text-gray-400">
          No sources configured yet.
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
          <table className="min-w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-gray-600">
                  Name
                </th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">
                  Type
                </th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">
                  Status
                </th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">
                  Last sync
                </th>
                <th className="px-4 py-3 text-right font-medium text-gray-600">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {sources.map((src) => (
                <tr key={src.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium text-gray-900">
                    {src.name}
                    {src.last_error && (
                      <p className="text-xs text-red-500 mt-0.5 truncate max-w-xs">
                        {src.last_error}
                      </p>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">
                    {src.type}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge value={src.status} />
                  </td>
                  <td className="px-4 py-3 text-gray-500 tabular-nums text-xs">
                    {src.last_sync_at
                      ? new Date(src.last_sync_at).toLocaleString()
                      : "Never"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => handleSync(src)}
                        disabled={syncingIds.has(src.id)}
                        className="px-3 py-1.5 text-xs border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-100 disabled:opacity-50 transition-colors"
                      >
                        {syncingIds.has(src.id) ? "Syncing..." : "Sync"}
                      </button>
                      <button
                        onClick={() => setDeleteTarget(src)}
                        className="px-3 py-1.5 text-xs border border-red-200 text-red-600 rounded-lg hover:bg-red-50 transition-colors"
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

      {deleteTarget && (
        <ConfirmDialog
          message={`Delete source "${deleteTarget.name}"? This action cannot be undone.`}
          onConfirm={handleDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
