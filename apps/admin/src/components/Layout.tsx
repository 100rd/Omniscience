import { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useTokenContext } from "../context/TokenContext";

interface LayoutProps {
  children: ReactNode;
}

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/sources", label: "Sources" },
  { to: "/tokens", label: "Tokens" },
  { to: "/ingestion", label: "Ingestion" },
  { to: "/search", label: "Search Playground" },
];

export function Layout({ children }: LayoutProps) {
  const { logout } = useTokenContext();

  return (
    <div className="flex h-screen bg-gray-50">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 bg-gray-900 text-gray-200 flex flex-col">
        <div className="px-5 py-5 border-b border-gray-700">
          <span className="text-white font-semibold text-lg tracking-tight">
            Omniscience
          </span>
          <span className="ml-2 text-xs text-gray-400 font-mono">admin</span>
        </div>

        <nav className="flex-1 py-4 px-2 flex flex-col gap-1">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `rounded px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? "bg-indigo-600 text-white"
                    : "text-gray-300 hover:bg-gray-700 hover:text-white"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="px-4 py-4 border-t border-gray-700">
          <button
            onClick={logout}
            className="w-full text-left text-xs text-gray-400 hover:text-gray-200 transition-colors"
          >
            Sign out
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto">
        <div className="max-w-6xl mx-auto px-6 py-8">{children}</div>
      </main>
    </div>
  );
}
