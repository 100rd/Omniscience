import { useEffect, useState, FormEvent } from "react";
import { useTokenContext } from "../context/TokenContext";
import {
  IngestionRun,
  IngestionRunStatus,
  Source,
  ApiError,
} from "../api/client";
import { StatusBadge } from "../components/StatusBadge";

interface ToastFn {
  (msg: string, type?: "success" | "error" | "info"): void;
}

interface Props {
  addToast: ToastFn;
}

const STATUS_OPTIONS: IngestionRunStatus[] = [
  "running",
  "ok",
  "partial",
  "error",
];

export function IngestionPage({ addToast }: Props) {
  const { client } = useTokenContext();
  const [runs, setRuns] = useState<IngestionRun[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [loading, setLoading] = useState(true);
  const [sourceFilter, setSourceFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");

  const loadRuns = async (params?: {
    source_id?: string;
    status?: IngestionRunStatus;
  }) => {
    setLoading(true);
    try {
      const [r, s] = await Promise.all([
        client.listIngestionRuns({ ...params, limit: 100 }),
        client.listSources(),
      ]);
      setRuns(r);
      setSources(s);
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      addToast(`Failed to load ingestion runs: ${msg}`, "error");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadRuns();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleFilter = (e: FormEvent) => {
    e.preventDefault();
    loadRuns({
      source_id: sourceFilter || undefined,
      status: (statusFilter as IngestionRunStatus) || undefined,
    });
  };

  const sourceMap = Object.fromEntries(sources.map((s) => [s.id, s.name]));

  const duration = (run: IngestionRun): string => {
    if (!run.finished_at) return "—";
    const ms =
      new Date(run.finished_at).getTime() -
      new Date(run.started_at).getTime();
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  };

  return (
    <div>
      <h1 className="text-2xl font-semibold text-text mb-6">Ingestion Runs</h1>

      {/* Filters */}
      <form
        onSubmit={handleFilter}
        className="bg-elevation-1 rounded-xl border border-border shadow-sm px-4 py-4 mb-6 flex flex-wrap items-end gap-4"
      >
        <div>
          <label className="block text-xs font-medium text-text-secondary mb-1">
            Source
          </label>
          <select
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            className="border border-border bg-elevation-2 text-text rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent min-w-40"
          >
            <option value="">All sources</option>
            {sources.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-medium text-text-secondary mb-1">
            Status
          </label>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="border border-border bg-elevation-2 text-text rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          >
            <option value="">All statuses</option>
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <button
          type="submit"
          className="px-4 py-2 text-sm bg-accent text-accent-fg rounded-lg hover:bg-accent-hover transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Apply
        </button>
        <button
          type="button"
          onClick={() => {
            setSourceFilter("");
            setStatusFilter("");
            loadRuns();
          }}
          className="px-4 py-2 text-sm border border-border rounded-lg text-text-secondary hover:bg-elevation-2 hover:text-text transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Reset
        </button>
      </form>

      {loading ? (
        <p className="text-sm text-text-muted py-12 text-center">Loading...</p>
      ) : runs.length === 0 ? (
        <div className="bg-elevation-1 rounded-xl border border-border shadow-sm px-6 py-12 text-center text-sm text-text-muted">
          No ingestion runs found.
        </div>
      ) : (
        <div className="bg-elevation-1 rounded-xl border border-border shadow-sm overflow-hidden">
          <table className="min-w-full text-sm">
            <thead className="bg-elevation-2 border-b border-border">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Source
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Status
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  New
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Updated
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Removed
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Duration
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Started
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {runs.map((run) => (
                <tr key={run.id} className="hover:bg-elevation-2">
                  <td className="px-4 py-3 text-text">
                    {sourceMap[run.source_id] ?? (
                      <span className="font-mono text-xs text-text-muted">
                        {run.source_id.slice(0, 8)}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge value={run.status} />
                    {run.status === "error" &&
                      Object.keys(run.errors).length > 0 && (
                        <details className="mt-1">
                          <summary className="text-xs text-danger-fg cursor-pointer">
                            Errors
                          </summary>
                          <pre className="text-xs text-danger-fg bg-danger-bg rounded p-2 mt-1 max-w-xs overflow-auto">
                            {JSON.stringify(run.errors, null, 2)}
                          </pre>
                        </details>
                      )}
                  </td>
                  <td className="px-4 py-3 text-text tabular-nums">
                    {run.docs_new}
                  </td>
                  <td className="px-4 py-3 text-text tabular-nums">
                    {run.docs_updated}
                  </td>
                  <td className="px-4 py-3 text-text tabular-nums">
                    {run.docs_removed}
                  </td>
                  <td className="px-4 py-3 text-text-muted tabular-nums text-xs">
                    {duration(run)}
                  </td>
                  <td className="px-4 py-3 text-text-muted text-xs tabular-nums">
                    {new Date(run.started_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
