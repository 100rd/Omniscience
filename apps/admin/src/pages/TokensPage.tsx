import { useEffect, useState, FormEvent } from "react";
import { useTokenContext } from "../context/TokenContext";
import { ApiToken, ApiError } from "../api/client";
import { ConfirmDialog } from "../components/ConfirmDialog";

const AVAILABLE_SCOPES = ["search", "sources:read", "sources:write", "admin"];

interface ToastFn {
  (msg: string, type?: "success" | "error" | "info"): void;
}

interface Props {
  addToast: ToastFn;
}

function CreateTokenForm({
  onCreated,
  addToast,
}: {
  onCreated: (token: ApiToken, secret: string) => void;
  addToast: ToastFn;
}) {
  const { client } = useTokenContext();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["search"]);
  const [submitting, setSubmitting] = useState(false);

  const toggleScope = (scope: string) => {
    setScopes((prev) =>
      prev.includes(scope) ? prev.filter((s) => s !== scope) : [...prev, scope]
    );
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      addToast("Name is required.", "error");
      return;
    }
    if (scopes.length === 0) {
      addToast("Select at least one scope.", "error");
      return;
    }
    setSubmitting(true);
    try {
      const resp = await client.createToken({
        name: name.trim(),
        scopes,
      });
      onCreated(resp.token, resp.secret);
      setName("");
      setScopes(["search"]);
      setOpen(false);
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      addToast(`Failed to create token: ${msg}`, "error");
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors"
      >
        Create token
      </button>
    );
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 mb-6">
      <h2 className="text-base font-medium text-gray-900 mb-4">Create token</h2>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Name
          </label>
          <input
            type="text"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-service-token"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          />
        </div>

        <div>
          <p className="block text-sm font-medium text-gray-700 mb-2">
            Scopes
          </p>
          <div className="flex flex-wrap gap-3">
            {AVAILABLE_SCOPES.map((scope) => (
              <label key={scope} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={scopes.includes(scope)}
                  onChange={() => toggleScope(scope)}
                  className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                />
                <span className="font-mono text-xs">{scope}</span>
              </label>
            ))}
          </div>
        </div>

        <div className="flex gap-3">
          <button
            type="submit"
            disabled={submitting}
            className="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
          >
            {submitting ? "Creating..." : "Create"}
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="px-4 py-2 text-sm border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50 transition-colors"
          >
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}

function SecretReveal({
  secret,
  onDismiss,
}: {
  secret: string;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const copy = () => {
    navigator.clipboard.writeText(secret).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="bg-yellow-50 border border-yellow-300 rounded-xl p-4 mb-6">
      <p className="text-sm font-medium text-yellow-800 mb-2">
        Copy this token now — it will not be shown again.
      </p>
      <div className="flex items-center gap-2">
        <code className="flex-1 bg-white border border-yellow-200 rounded px-3 py-2 text-xs font-mono break-all">
          {secret}
        </code>
        <button
          onClick={copy}
          className="px-3 py-2 text-xs bg-yellow-200 rounded hover:bg-yellow-300 transition-colors whitespace-nowrap"
        >
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
      <button
        onClick={onDismiss}
        className="mt-3 text-xs text-yellow-700 hover:underline"
      >
        I have saved it
      </button>
    </div>
  );
}

export function TokensPage({ addToast }: Props) {
  const { client } = useTokenContext();
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [loading, setLoading] = useState(true);
  const [revokeTarget, setRevokeTarget] = useState<ApiToken | null>(null);
  const [newSecret, setNewSecret] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    client
      .listTokens()
      .then((t) => {
        if (!cancelled) setTokens(t);
      })
      .catch((e) => addToast(String(e), "error"))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [client, addToast]);

  const handleCreated = (token: ApiToken, secret: string) => {
    setTokens((prev) => [token, ...prev]);
    setNewSecret(secret);
  };

  const handleRevoke = async () => {
    if (!revokeTarget) return;
    try {
      await client.deleteToken(revokeTarget.id);
      setTokens((prev) => prev.filter((t) => t.id !== revokeTarget.id));
      addToast(`Token "${revokeTarget.name}" revoked.`, "success");
    } catch (err) {
      const msg = err instanceof ApiError ? err.detail : String(err);
      addToast(`Revoke failed: ${msg}`, "error");
    } finally {
      setRevokeTarget(null);
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-gray-900">API Tokens</h1>
        <CreateTokenForm onCreated={handleCreated} addToast={addToast} />
      </div>

      {newSecret && (
        <SecretReveal
          secret={newSecret}
          onDismiss={() => setNewSecret(null)}
        />
      )}

      {loading ? (
        <p className="text-sm text-gray-500 py-12 text-center">Loading...</p>
      ) : tokens.length === 0 ? (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm px-6 py-12 text-center text-sm text-gray-400">
          No active tokens.
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
          <table className="min-w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-gray-600">
                  Name
                </th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">
                  Prefix
                </th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">
                  Scopes
                </th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">
                  Created
                </th>
                <th className="px-4 py-3 text-left font-medium text-gray-600">
                  Last used
                </th>
                <th className="px-4 py-3 text-right font-medium text-gray-600">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {tokens.map((tok) => (
                <tr key={tok.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium text-gray-900">
                    {tok.name}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">
                    {tok.token_prefix}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {tok.scopes.map((s) => (
                        <span
                          key={s}
                          className="inline-flex items-center rounded px-1.5 py-0.5 bg-indigo-50 text-indigo-700 text-xs font-mono"
                        >
                          {s}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs tabular-nums">
                    {new Date(tok.created_at).toLocaleDateString()}
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs tabular-nums">
                    {tok.last_used_at
                      ? new Date(tok.last_used_at).toLocaleString()
                      : "Never"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => setRevokeTarget(tok)}
                      className="px-3 py-1.5 text-xs border border-red-200 text-red-600 rounded-lg hover:bg-red-50 transition-colors"
                    >
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {revokeTarget && (
        <ConfirmDialog
          message={`Revoke token "${revokeTarget.name}"? Any services using this token will lose access.`}
          onConfirm={handleRevoke}
          onCancel={() => setRevokeTarget(null)}
        />
      )}
    </div>
  );
}
