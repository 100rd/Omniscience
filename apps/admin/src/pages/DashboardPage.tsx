import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useTokenContext } from "../context/TokenContext";
import { Source, IngestionRun } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { FreshnessPanel } from "../components/FreshnessPanel";

function StatCard({
  label,
  value,
  to,
}: {
  label: string;
  value: string | number;
  to: string;
}) {
  return (
    <Link
      to={to}
      className="bg-elevation-1 rounded-xl border border-border shadow-sm px-6 py-5 hover:shadow-md hover:border-border-strong transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      <p className="text-sm text-text-muted">{label}</p>
      <p className="text-3xl font-semibold text-text mt-1">{value}</p>
    </Link>
  );
}

export function DashboardPage() {
  const { client } = useTokenContext();
  const [sources, setSources] = useState<Source[]>([]);
  const [runs, setRuns] = useState<IngestionRun[]>([]);
  const [health, setHealth] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [s, r, h] = await Promise.allSettled([
          client.listSources(),
          client.listIngestionRuns({ limit: 10 }),
          client.health(),
        ]);
        if (cancelled) return;

        if (s.status === "fulfilled") setSources(s.value);
        if (r.status === "fulfilled") setRuns(r.value);
        if (h.status === "fulfilled") setHealth(h.value.status);
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [client]);

  if (loading) {
    return (
      <div className="text-sm text-text-muted py-12 text-center">
        Loading...
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-sm text-danger-fg py-12 text-center">{error}</div>
    );
  }

  const runningCount = sources.filter((s) => s.status === "active").length;
  const errorCount = sources.filter((s) => s.status === "error").length;
  const recentRuns = runs.slice(0, 5);

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-2xl font-semibold text-text">Dashboard</h1>
        {health && (
          <span
            className={`inline-flex items-center gap-1.5 text-sm font-medium ${
              health === "ok" ? "text-success-fg" : "text-danger-fg"
            }`}
          >
            <span
              className={`h-2 w-2 rounded-full ${
                health === "ok" ? "bg-success-fg" : "bg-danger-fg"
              }`}
            />
            API {health}
          </span>
        )}
      </div>

      <div className="grid grid-cols-3 gap-4 mb-10">
        <StatCard label="Total sources" value={sources.length} to="/sources" />
        <StatCard label="Active sources" value={runningCount} to="/sources" />
        <StatCard
          label="Sources with errors"
          value={errorCount}
          to="/sources"
        />
      </div>

      <section className="mb-10">
        <FreshnessPanel />
      </section>

      <section>
        <h2 className="text-base font-medium text-text-secondary mb-4">
          Recent ingestion runs
        </h2>
        {recentRuns.length === 0 ? (
          <p className="text-sm text-text-muted">No ingestion runs yet.</p>
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
                    Docs new
                  </th>
                  <th className="px-4 py-3 text-left font-medium text-text-secondary">
                    Started
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {recentRuns.map((run) => {
                  const src = sources.find((s) => s.id === run.source_id);
                  return (
                    <tr key={run.id} className="hover:bg-elevation-2">
                      <td className="px-4 py-3 font-mono text-xs text-text-secondary">
                        {src?.name ?? run.source_id.slice(0, 8)}
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge value={run.status} />
                      </td>
                      <td className="px-4 py-3 text-text">{run.docs_new}</td>
                      <td className="px-4 py-3 text-text-muted tabular-nums">
                        {new Date(run.started_at).toLocaleString()}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {runs.length > 5 && (
          <div className="mt-3">
            <Link
              to="/ingestion"
              className="text-sm text-accent hover:text-accent-hover hover:underline"
            >
              View all runs
            </Link>
          </div>
        )}
      </section>
    </div>
  );
}
