/*
 * TimeAgo — live "refreshed Ns ago" label (Issue #114).
 *
 * Issue #114 explicitly requires that every panel header show "last
 * refreshed N seconds ago" and that the timestamp updates *every
 * second*, even though the actual data refresh is per-panel cadence
 * (30s / 60s). Splitting the render-tick from the fetch-tick keeps
 * operator perception of freshness accurate without re-fetching.
 *
 * Implementation: a 1-second interval re-renders the label. Single
 * shared interval per mounted instance — bundle and runtime cost is
 * negligible vs the perceptual benefit. Component honours
 * `prefers-reduced-motion`: the displayed text still updates (text
 * change is content, not motion), but no animation is ever attached.
 */

import { useEffect, useState } from "react";

interface TimeAgoProps {
  timestamp: number;
}

function format(deltaMs: number): string {
  if (deltaMs < 0) return "just now";
  const s = Math.floor(deltaMs / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    return `${m}m ago`;
  }
  if (s < 86_400) {
    const h = Math.floor(s / 3600);
    return `${h}h ago`;
  }
  const d = Math.floor(s / 86_400);
  return `${d}d ago`;
}

export function TimeAgo({ timestamp }: TimeAgoProps) {
  const [now, setNow] = useState<number>(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  return <>{format(now - timestamp)}</>;
}
