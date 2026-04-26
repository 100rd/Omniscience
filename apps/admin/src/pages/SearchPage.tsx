import { useState, FormEvent } from "react";
import { useTokenContext } from "../context/TokenContext";
import { SearchResult, SearchHit, ApiError, Source } from "../api/client";
import { useEffect } from "react";

interface ToastFn {
  (msg: string, type?: "success" | "error" | "info"): void;
}

interface Props {
  addToast: ToastFn;
}

function HitCard({ hit }: { hit: SearchHit }) {
  return (
    <div className="bg-elevation-1 rounded-xl border border-border shadow-sm p-5">
      <div className="flex items-start justify-between gap-4 mb-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-text truncate">
            {hit.citation.title ?? hit.citation.uri}
          </p>
          <a
            href={hit.citation.uri}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-accent hover:text-accent-hover hover:underline truncate block focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
          >
            {hit.citation.uri}
          </a>
        </div>
        <div className="flex-shrink-0 text-right">
          <span className="text-sm font-semibold text-text tabular-nums">
            {(hit.score * 100).toFixed(1)}
          </span>
          <p className="text-xs text-text-muted">score</p>
        </div>
      </div>

      <div className="flex items-center gap-3 mb-3 text-xs text-text-muted">
        <span className="font-mono bg-elevation-2 text-text-secondary rounded px-1.5 py-0.5">
          {hit.source.type}
        </span>
        <span>{hit.source.name}</span>
        <span>v{hit.citation.doc_version}</span>
      </div>

      <p className="text-sm text-text-secondary leading-relaxed line-clamp-4">
        {hit.text}
      </p>
    </div>
  );
}

export function SearchPage({ addToast }: Props) {
  const { client } = useTokenContext();
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(10);
  const [sourceFilter, setSourceFilter] = useState("");
  const [strategy, setStrategy] = useState<
    "hybrid" | "keyword" | "structural" | "auto"
  >("hybrid");
  const [result, setResult] = useState<SearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [sources, setSources] = useState<Source[]>([]);

  useEffect(() => {
    client
      .listSources()
      .then(setSources)
      .catch(() => {});
  }, [client]);

  const handleSearch = async (e: FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    try {
      const r = await client.search({
        query: query.trim(),
        top_k: topK,
        sources: sourceFilter ? [sourceFilter] : undefined,
        retrieval_strategy: strategy,
      });
      setResult(r);
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      addToast(`Search failed: ${msg}`, "error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <h1 className="text-2xl font-semibold text-text mb-6">
        Search Playground
      </h1>

      <form
        onSubmit={handleSearch}
        className="bg-elevation-1 rounded-xl border border-border shadow-sm p-5 mb-6 space-y-4"
      >
        <div>
          <label className="block text-sm font-medium text-text-secondary mb-1">
            Query
          </label>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="How does authentication work?"
            className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          />
        </div>

        <div className="grid grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              Top K: <span className="font-semibold text-text">{topK}</span>
            </label>
            <input
              type="range"
              min={1}
              max={50}
              value={topK}
              onChange={(e) => setTopK(Number(e.target.value))}
              className="w-full accent-accent"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              Strategy
            </label>
            <select
              value={strategy}
              onChange={(e) => setStrategy(e.target.value as typeof strategy)}
              className="w-full border border-border bg-elevation-2 text-text rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
            >
              <option value="hybrid">hybrid</option>
              <option value="keyword">keyword</option>
              <option value="structural">structural</option>
              <option value="auto">auto</option>
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              Source filter
            </label>
            <select
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              className="w-full border border-border bg-elevation-2 text-text rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
            >
              <option value="">All sources</option>
              {sources.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <button
          type="submit"
          disabled={loading || !query.trim()}
          className="px-5 py-2 text-sm bg-accent text-accent-fg rounded-lg hover:bg-accent-hover disabled:opacity-50 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {loading ? "Searching..." : "Search"}
        </button>
      </form>

      {result && (
        <div>
          <div className="flex items-center justify-between mb-4">
            <p className="text-sm text-text-secondary">
              <span className="font-semibold text-text">
                {result.hits.length}
              </span>{" "}
              hits &middot;{" "}
              <span className="tabular-nums">
                {result.query_stats.duration_ms.toFixed(0)}ms
              </span>{" "}
              &middot; {result.query_stats.vector_matches} vector /{" "}
              {result.query_stats.text_matches} text matches
            </p>
          </div>

          {result.hits.length === 0 ? (
            <div className="bg-elevation-1 rounded-xl border border-border shadow-sm px-6 py-12 text-center text-sm text-text-muted">
              No results found.
            </div>
          ) : (
            <div className="space-y-4">
              {result.hits.map((hit) => (
                <HitCard key={hit.chunk_id} hit={hit} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
