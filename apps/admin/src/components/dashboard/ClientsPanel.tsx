/*
 * ClientsPanel — connected clients view (Issue #114, panel #5).
 *
 * Surfaces three things from `GET /api/v1/stats/clients`:
 *   - active MCP sessions (process-global counter pair)
 *   - top tokens by `requests_last_15m` (workspace-scoped)
 *   - top tools by `invocations_last_hour` (process-global)
 *
 * The panel is split into a session header strip + two list bodies
 * (tokens and tools). Each list defaults to top 5 with names + counts;
 * the issue does not require pagination here.
 *
 * Refresh cadence: 30s. Client telemetry moves quickly.
 */

import { Link } from "react-router-dom";
import {
  ApiClient,
  ClientsStatsResponse,
  TokenClientStats,
  ToolUsageEntry,
} from "../../api/client";
import { useTokenContext } from "../../context/TokenContext";
import { usePanelFetch } from "../../hooks/usePanelFetch";
import { PanelBody, PanelFrame } from "./PanelFrame";
import { PanelErrorBoundary } from "./PanelErrorBoundary";

const REFRESH_MS = 30_000;
const TOP_TOKENS = 5;
const TOP_TOOLS = 5;

interface Props {
  client?: ApiClient;
  refreshMs?: number;
}

export function ClientsPanel({
  client: clientOverride,
  refreshMs = REFRESH_MS,
}: Props = {}) {
  const ctx = useTokenContext();
  const client = clientOverride ?? ctx.client;

  const { data, error, refreshing, lastRefreshedAt } =
    usePanelFetch<ClientsStatsResponse>(
      (signal) => client.statsClients(signal),
      refreshMs
    );

  return (
    <PanelFrame
      title="Clients"
      subtitle="MCP sessions, token activity, top tools"
      refreshing={refreshing}
      lastRefreshedAt={lastRefreshedAt}
      refreshMs={refreshMs}
    >
      <PanelErrorBoundary panelTitle="Clients">
        <PanelBody
          data={data}
          error={error}
          isEmpty={(d) =>
            d.mcp_sessions_active === 0 &&
            d.mcp_sessions_last_hour === 0 &&
            d.tokens.length === 0 &&
            d.top_tools_last_hour.length === 0
          }
          emptyTitle="No client activity yet"
          emptyHint={
            <>
              Issue tokens on the{" "}
              <Link
                to="/tokens"
                className="text-accent hover:text-accent-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
              >
                Tokens
              </Link>{" "}
              page to see traffic here.
            </>
          }
          skeleton={<ClientsSkeleton />}
        >
          {(d) => <ClientsBody data={d} />}
        </PanelBody>
      </PanelErrorBoundary>
    </PanelFrame>
  );
}

function ClientsBody({ data }: { data: ClientsStatsResponse }) {
  return (
    <div className="space-y-5">
      <SessionStrip
        active={data.mcp_sessions_active}
        lastHour={data.mcp_sessions_last_hour}
      />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <TokensList tokens={data.tokens} />
        <ToolsList tools={data.top_tools_last_hour} />
      </div>
    </div>
  );
}

function SessionStrip({
  active,
  lastHour,
}: {
  active: number;
  lastHour: number;
}) {
  return (
    <div className="grid grid-cols-2 gap-3">
      <div className="rounded-lg bg-elevation-2 border border-border px-4 py-3">
        <p className="text-xs text-text-muted uppercase tracking-wide">
          Active MCP sessions
        </p>
        <p className="text-2xl font-semibold text-text mt-1 tabular-nums">
          {active.toLocaleString()}
        </p>
      </div>
      <div className="rounded-lg bg-elevation-2 border border-border px-4 py-3">
        <p className="text-xs text-text-muted uppercase tracking-wide">
          MCP sessions last hour
        </p>
        <p className="text-2xl font-semibold text-text mt-1 tabular-nums">
          {lastHour.toLocaleString()}
        </p>
      </div>
    </div>
  );
}

function TokensList({ tokens }: { tokens: TokenClientStats[] }) {
  const sorted = tokens
    .slice()
    .sort((a, b) => b.requests_last_15m - a.requests_last_15m)
    .slice(0, TOP_TOKENS);

  return (
    <div>
      <h3 className="text-sm font-medium text-text-secondary mb-2">
        Top tokens (last 15m)
      </h3>
      {sorted.length === 0 ? (
        <p className="text-sm text-text-muted">
          No token activity in this window.
        </p>
      ) : (
        <ul className="space-y-1">
          {sorted.map((t) => (
            <li
              key={t.token_id}
              className="flex items-center justify-between gap-3 text-sm py-1.5 px-2 rounded hover:bg-elevation-2 focus-within:bg-elevation-2"
              title={t.name}
            >
              <span className="truncate text-text">{t.name}</span>
              <span className="tabular-nums text-text-muted shrink-0">
                {t.requests_last_15m.toLocaleString()}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ToolsList({ tools }: { tools: ToolUsageEntry[] }) {
  const sorted = tools
    .slice()
    .sort((a, b) => b.invocations_last_hour - a.invocations_last_hour)
    .slice(0, TOP_TOOLS);

  return (
    <div>
      <h3 className="text-sm font-medium text-text-secondary mb-2">
        Top tools (last hour)
      </h3>
      {sorted.length === 0 ? (
        <p className="text-sm text-text-muted">
          No tool invocations in this window.
        </p>
      ) : (
        <ul className="space-y-1">
          {sorted.map((t) => (
            <li
              key={t.tool_name}
              className="flex items-center justify-between gap-3 text-sm py-1.5 px-2 rounded hover:bg-elevation-2 focus-within:bg-elevation-2"
              title={t.tool_name}
            >
              <span className="truncate font-mono text-xs text-text-secondary">
                {t.tool_name}
              </span>
              <span className="tabular-nums text-text-muted shrink-0">
                {t.invocations_last_hour.toLocaleString()}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ClientsSkeleton() {
  return (
    <div className="space-y-5" aria-hidden="true">
      <div className="grid grid-cols-2 gap-3">
        {Array.from({ length: 2 }).map((_, i) => (
          <div
            key={i}
            className="rounded-lg bg-elevation-2 border border-border h-16 motion-safe:animate-pulse"
          />
        ))}
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {Array.from({ length: 2 }).map((_, i) => (
          <div key={i}>
            <div className="h-3 w-32 bg-elevation-2 rounded mb-2 motion-safe:animate-pulse" />
            <div className="space-y-1">
              {Array.from({ length: 4 }).map((__, j) => (
                <div
                  key={j}
                  className="h-5 w-full bg-elevation-2 rounded motion-safe:animate-pulse"
                />
              ))}
            </div>
          </div>
        ))}
      </div>
      <span className="sr-only" role="status">
        Loading clients…
      </span>
    </div>
  );
}
