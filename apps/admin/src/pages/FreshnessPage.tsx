/*
 * FreshnessPage — dedicated `/freshness` route hosting the FreshnessPanel.
 *
 * The same component is also slotted into DashboardPage so operators get
 * an at-a-glance view there. This page is the deep-dive: same panel,
 * full width, accessible from the sidebar nav.
 */

import { FreshnessPanel } from "../components/FreshnessPanel";

export function FreshnessPage() {
  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-2xl font-semibold text-text">Freshness</h1>
      </div>
      <FreshnessPanel />
    </div>
  );
}
