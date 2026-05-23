import { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useTokenContext } from "../context/TokenContext";

interface LayoutProps {
  children: ReactNode;
}

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/sources", label: "Sources" },
  { to: "/freshness", label: "Freshness" },
  { to: "/retention", label: "Retention" },
  { to: "/incidents", label: "Incidents" },
  { to: "/tokens", label: "Tokens" },
  { to: "/ingestion", label: "Ingestion" },
  { to: "/search", label: "Search Playground" },
  { to: "/replay", label: "Replay" },
];

export function Layout({ children }: LayoutProps) {
  const { logout } = useTokenContext();

  return (
    <div className="flex h-screen bg-surface text-text">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 bg-elevation-1 text-text-secondary border-r border-border flex flex-col">
        <div className="px-5 py-5 border-b border-border">
          <span className="text-text font-semibold text-lg tracking-tight">
            Omniscience
          </span>
          <span className="ml-2 text-xs text-text-muted font-mono">admin</span>
        </div>

        <nav className="flex-1 py-4 px-2 flex flex-col gap-1">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `rounded px-3 py-2 text-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
                  isActive
                    ? "bg-accent text-accent-fg"
                    : "text-text-secondary hover:bg-elevation-2 hover:text-text"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="px-4 py-4 border-t border-border">
          <button
            onClick={logout}
            className="w-full text-left text-xs text-text-muted hover:text-text transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
          >
            Sign out
          </button>
        </div>
      </aside>

      {/* Main content
       *
       * The container caps at `max-w-screen-2xl` (= 1536px). On a 1440px
       * monitor minus the 224px sidebar that leaves 1216px of usable
       * content area; on 1920 it's 1696px (still room to spare under
       * the cap); on 2560 it's 2336px and the cap kicks in to keep
       * line-lengths readable on ultra-wide. The dashboard's 12-col
       * grid (Issue #114) splits at the `xl` (>= 1280) breakpoint, so
       * the panels reach their 7/5 layout on every supported width.
       *
       * Other pages were sized for `max-w-6xl` (1152px) and look fine
       * inside the wider container — none of them hard-rely on the
       * narrower max — verified by reading through each /pages file.
       */}
      <main className="flex-1 overflow-auto bg-surface">
        <div className="max-w-screen-2xl mx-auto px-6 py-8">{children}</div>
      </main>
    </div>
  );
}
