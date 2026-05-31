import React, { useState } from "react";
import { useTokenContext } from "../context/TokenContext";
import { RelatedEntitiesResponse } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { GraphCanvas } from "../components/GraphCanvas";

interface GraphPageProps {
  addToast: (message: string, type: "success" | "error") => void;
}

export function GraphPage({ addToast }: GraphPageProps) {
  const { client } = useTokenContext();
  const [entityName, setEntityName] = useState("");
  const [data, setData] = useState<RelatedEntitiesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [depth, setDepth] = useState(1);
  const [asOf, setAsOf] = useState("");

  const graphNodes = data
    ? [
        { id: data.seed.name, name: data.seed.name, kind: data.seed.kind },
        ...data.related.map((r) => ({ id: r.name, name: r.name, kind: r.kind })),
      ]
    : [];

  const graphLinks = data
    ? data.edges.map((e) => ({ source: e.from, target: e.to, type: e.type }))
    : [];

  const fetchGraph = async (name: string) => {
    if (!name) return;
    setLoading(true);
    try {
      let formattedAsOf = asOf;
      if (
        formattedAsOf &&
        !formattedAsOf.includes("Z") &&
        !formattedAsOf.includes("+")
      ) {
        formattedAsOf = new Date(formattedAsOf).toISOString();
      }
      const res = await client.getRelatedEntities(name, {
        depth,
        as_of: formattedAsOf || undefined,
      });
      setData(res);
    } catch (err: unknown) {
      const detail =
        err instanceof Error
          ? err.message
          : typeof err === "object" && err !== null && "detail" in err
            ? String((err as { detail: unknown }).detail)
            : "Failed to fetch graph";
      addToast(detail, "error");
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    void fetchGraph(entityName);
  };

  const navigateTo = (name: string) => {
    setEntityName(name);
    void fetchGraph(name);
  };

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-gray-900">Graph Explorer</h1>
        <p className="text-gray-500">
          Traverse semantic relationships across sources.
        </p>
      </header>

      <div className="bg-elevation-1 p-6 rounded-lg border border-border shadow-md">
        <form
          onSubmit={handleSearch}
          className="flex flex-col md:flex-row gap-6 items-end"
        >
          <div className="flex-1 w-full">
            <label className="block text-xs font-bold text-text-muted uppercase mb-1">
              Entity Name (FQN)
            </label>
            <input
              type="text"
              value={entityName}
              onChange={(e) => setEntityName(e.target.value)}
              placeholder="e.g. payments.process_payment"
              className="w-full px-4 py-2 bg-elevation-2 border border-border text-text rounded-md focus:ring-2 focus:ring-accent outline-none transition-all"
            />
          </div>

          <div className="w-full md:w-32">
            <label className="block text-xs font-bold text-text-muted uppercase mb-1">
              Depth
            </label>
            <select
              value={depth}
              onChange={(e) => setDepth(Number(e.target.value))}
              className="w-full px-3 py-2 bg-elevation-2 border border-border text-text rounded-md focus:ring-2 focus:ring-accent outline-none"
            >
              {[1, 2, 3, 4, 5].map((d) => (
                <option key={d} value={d} className="bg-elevation-2 text-text">
                  {d} hops
                </option>
              ))}
            </select>
          </div>

          <div className="w-full md:w-64">
            <label className="block text-xs font-bold text-text-muted uppercase mb-1">
              As Of (UTC Time)
            </label>
            <input
              type="datetime-local"
              step="1"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
              className="w-full px-3 py-2 bg-elevation-2 border border-border text-text rounded-md focus:ring-2 focus:ring-accent outline-none"
            />
          </div>

          <button
            type="submit"
            disabled={loading || !entityName}
            className="w-full md:w-auto px-8 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 font-bold shadow-lg shadow-blue-200 transition-all"
          >
            {loading ? "Exploring..." : "Explore"}
          </button>
        </form>
      </div>

      {data && (
        <div className="space-y-6">
          <div className="bg-white p-4 rounded-lg border border-gray-200 shadow-sm overflow-hidden">
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-sm font-bold text-gray-900 uppercase tracking-tighter">
                Visual Semantic Map
              </h2>
              <div className="text-[10px] text-gray-400 font-mono">
                Found {graphNodes.length} nodes and {graphLinks.length} edges
              </div>
            </div>
            <GraphCanvas
              nodes={graphNodes}
              links={graphLinks}
              onNodeClick={navigateTo}
            />
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-1 space-y-6">
              <div className="bg-white p-6 rounded-lg border border-gray-200 shadow-sm">
                <h2 className="text-sm font-bold text-gray-900 uppercase mb-4">
                  Selected Entity
                </h2>
                <div className="space-y-4">
                  <div>
                    <div className="text-[10px] font-bold text-gray-400 uppercase">
                      FQN
                    </div>
                    <div className="text-sm font-mono text-blue-800 break-all">
                      {data.seed.name}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] font-bold text-gray-400 uppercase">
                      Type
                    </div>
                    <div className="inline-block mt-1">
                      <StatusBadge value={data.seed.kind as Parameters<typeof StatusBadge>[0]["value"]} />
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] font-bold text-gray-400 uppercase">
                      Source Identifier
                    </div>
                    <div className="text-xs text-gray-500 font-mono mt-1 italic">
                      {data.seed.source}
                    </div>
                  </div>
                  {data.seed.chunk_text && (
                    <div>
                      <div className="text-[10px] font-bold text-gray-400 uppercase">
                        AST Snippet
                      </div>
                      <pre className="mt-2 p-4 bg-gray-900 text-green-400 text-[11px] rounded-lg border border-gray-800 font-mono overflow-x-auto shadow-inner">
                        {data.seed.chunk_text}
                      </pre>
                    </div>
                  )}
                </div>
              </div>
            </div>

            <div className="lg:col-span-2">
              <div className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-6 py-3 text-left text-[10px] font-bold text-gray-400 uppercase">
                        Relation
                      </th>
                      <th className="px-6 py-3 text-left text-[10px] font-bold text-gray-400 uppercase">
                        Target Entity
                      </th>
                      <th className="px-6 py-3 text-right text-[10px] font-bold text-gray-400 uppercase">
                        Hops
                      </th>
                    </tr>
                  </thead>
                  <tbody className="bg-white divide-y divide-gray-100">
                    {data.related.map((rel, idx) => (
                      <tr
                        key={idx}
                        className="hover:bg-blue-50/50 transition-colors group"
                      >
                        <td className="px-6 py-4">
                          <span className="px-2 py-0.5 rounded text-[10px] font-bold uppercase bg-blue-100 text-blue-700 border border-blue-200">
                            {rel.edge_type}
                          </span>
                        </td>
                        <td className="px-6 py-4">
                          <button
                            onClick={() => navigateTo(rel.name)}
                            className="text-sm font-mono text-blue-600 hover:text-blue-800 hover:underline text-left block"
                          >
                            {rel.name}
                          </button>
                          <div className="text-[10px] text-gray-400 font-sans mt-0.5">
                            {rel.kind} &bull; {rel.source.slice(0, 12)}...
                          </div>
                        </td>
                        <td className="px-6 py-4 text-right text-xs text-gray-500 font-mono">
                          {rel.depth}
                        </td>
                      </tr>
                    ))}
                    {data.related.length === 0 && (
                      <tr>
                        <td
                          colSpan={3}
                          className="px-6 py-12 text-center text-sm text-gray-400 italic bg-gray-50/50"
                        >
                          Isolated entity. No relationships found at this depth.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
