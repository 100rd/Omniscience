import { useState, FormEvent } from "react";
import { useTokenContext } from "../context/TokenContext";

export function LoginScreen() {
  const { setToken } = useTokenContext();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = value.trim();
    if (!trimmed) {
      setError("Please enter an API token.");
      return;
    }
    setError(null);
    setToken(trimmed);
  };

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center">
      <div className="bg-white rounded-xl shadow-md w-full max-w-sm px-8 py-10">
        <h1 className="text-2xl font-semibold text-gray-900 mb-1">
          Omniscience Admin
        </h1>
        <p className="text-sm text-gray-500 mb-8">
          Enter your API token to continue.
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label
              htmlFor="token"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              API Token
            </label>
            <input
              id="token"
              type="password"
              autoComplete="current-password"
              placeholder="omni_dev_..."
              value={value}
              onChange={(e) => setValue(e.target.value)}
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
            />
          </div>

          {error && <p className="text-sm text-red-600">{error}</p>}

          <button
            type="submit"
            className="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-medium rounded-lg px-4 py-2 text-sm transition-colors"
          >
            Sign in
          </button>
        </form>
      </div>
    </div>
  );
}
