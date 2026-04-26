/*
 * usePanelFetch — shared auto-refreshing panel data hook (Issue #114).
 *
 * Mirrors the `setInterval` + `AbortController` pattern from FreshnessPanel
 * (Issue #112). Centralized so every dashboard panel uses the same fetch
 * lifecycle and refresh-indicator state without copy-paste:
 *
 *   - First load happens immediately.
 *   - Subsequent loads run on a fixed `refreshMs` interval.
 *   - Each fetch is wrapped in an `AbortController`. The previous
 *     in-flight request is aborted before the next one starts so a slow
 *     response from interval N-1 cannot race over a fast response from N.
 *   - On unmount, any in-flight fetch is aborted and the interval is
 *     cleared.
 *   - `lastRefreshedAt` is set on every successful response — panel
 *     headers use it to render "refreshed Ns ago".
 *   - `error` is sticky until the next successful refresh; panels
 *     decide whether to show stale data alongside the error or hide it.
 *
 * No external state library. The hook is intentionally tiny — panels
 * stay readable and bundle delta stays minimal (project rule).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";

export interface PanelFetchState<T> {
  data: T | null;
  error: ApiError | Error | null;
  refreshing: boolean;
  lastRefreshedAt: number | null;
  /* Imperative refresh — useful for "Retry" buttons after errors. */
  refresh: () => void;
}

export function usePanelFetch<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  refreshMs: number
): PanelFetchState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<number | null>(null);

  const inFlightRef = useRef<AbortController | null>(null);
  /* Hold the latest fetcher in a ref so the polling interval doesn't
   * tear down and rebuild every time the parent re-renders with a new
   * inline closure. The interval reads the current fetcher on each tick. */
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const fetchOnce = useCallback(async () => {
    inFlightRef.current?.abort();
    const controller = new AbortController();
    inFlightRef.current = controller;

    setRefreshing(true);
    try {
      const resp = await fetcherRef.current(controller.signal);
      if (controller.signal.aborted) return;
      setData(resp);
      setError(null);
      setLastRefreshedAt(Date.now());
    } catch (e) {
      if (controller.signal.aborted) return;
      if (e instanceof DOMException && e.name === "AbortError") return;
      if (e instanceof Error) setError(e);
      else setError(new Error(String(e)));
    } finally {
      if (inFlightRef.current === controller) {
        inFlightRef.current = null;
      }
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void fetchOnce();
    const id = window.setInterval(() => {
      if (cancelled) return;
      void fetchOnce();
    }, refreshMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      inFlightRef.current?.abort();
      inFlightRef.current = null;
    };
  }, [fetchOnce, refreshMs]);

  return { data, error, refreshing, lastRefreshedAt, refresh: fetchOnce };
}
