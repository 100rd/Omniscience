import { useState, FormEvent } from "react";
import { useTokenContext } from "../context/TokenContext";

export function LoginScreen() {
  const { login } = useTokenContext();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const trimmed = value.trim();
    if (!trimmed) {
      setError("Please enter an API token.");
      return;
    }
    setError(null);
    setLoading(true);
    try {
      await login(trimmed);
    } catch {
      setError("Token invalid or insufficient scope. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-surface flex items-center justify-center">
      <div className="bg-elevation-1 border border-border rounded-xl shadow-md w-full max-w-sm px-8 py-10">
        <h1 className="text-2xl font-semibold text-text mb-1">
          Omniscience Admin
        </h1>
        <p className="text-sm text-text-muted mb-8">
          Enter your API token to continue.
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label
              htmlFor="token"
              className="block text-sm font-medium text-text-secondary mb-1"
            >
              API Token
            </label>
            <input
              id="token"
              type="password"
              autoComplete="current-password"
              placeholder="sk_dev_..."
              value={value}
              onChange={(e) => setValue(e.target.value)}
              disabled={loading}
              className="w-full rounded-lg border border-border bg-elevation-2 text-text placeholder:text-text-muted px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent focus:border-transparent disabled:opacity-50"
            />
          </div>

          {error && (
            <p role="alert" className="text-sm text-danger-fg">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-accent hover:bg-accent-hover text-accent-fg font-medium rounded-lg px-4 py-2 text-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-elevation-1 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
