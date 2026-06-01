import { useEffect, useState, FormEvent } from "react";
import { useTokenContext } from "../context/TokenContext";
import { ApiToken, ApiError } from "../api/client";
import { ConfirmDialog } from "../components/ConfirmDialog";

const AVAILABLE_SCOPES = ["search", "sources:read", "sources:write", "admin"];

const DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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
  const [workspaceId, setWorkspaceId] = useState("");
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
    const trimmedWsId = workspaceId.trim();
    if (trimmedWsId && !UUID_RE.test(trimmedWsId)) {
      addToast("Workspace ID must be a valid UUID.", "error");
      return;
    }
    setSubmitting(true);
    try {
      const resp = await client.createToken({
        name: name.trim(),
        scopes,
        ...(trimmedWsId ? { workspace_id: trimmedWsId } : {}),
      });
      onCreated(resp.token, resp.secret);
      setName("");
      setScopes(["search"]);
      setWorkspaceId("");
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
        className="px-4 py-2 text-sm bg-accent text-accent-fg rounded-lg hover:bg-accent-hover transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
      >
        Create token
      </button>
    );
  }

  return (
    <div className="bg-elevation-1 rounded-xl border border-border shadow-sm p-6 mb-6">
      <h2 className="text-base font-medium text-text mb-4">Create token</h2>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-text-secondary mb-1">
            Name
          </label>
          <input
            type="text"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-service-token"
            className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          />
        </div>

        <div>
          <p className="block text-sm font-medium text-text-secondary mb-2">
            Scopes
          </p>
          <div className="flex flex-wrap gap-3">
            {AVAILABLE_SCOPES.map((scope) => (
              <label
                key={scope}
                className="flex items-center gap-2 text-sm text-text"
              >
                <input
                  type="checkbox"
                  checked={scopes.includes(scope)}
                  onChange={() => toggleScope(scope)}
                  className="rounded border-border text-accent focus:ring-accent"
                />
                <span className="font-mono text-xs">{scope}</span>
              </label>
            ))}
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-text-secondary mb-1">
            Workspace ID (UUID){" "}
            <span className="text-text-muted font-normal">— optional</span>
          </label>
          <input
            type="text"
            value={workspaceId}
            onChange={(e) => setWorkspaceId(e.target.value)}
            placeholder={DEFAULT_WORKSPACE_ID}
            className="w-full border border-border bg-elevation-2 text-text placeholder:text-text-muted rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent"
          />
          <p className="mt-1 text-xs text-text-muted">
            Required for stats and retention endpoints. Default workspace:{" "}
            <code className="font-mono">{DEFAULT_WORKSPACE_ID}</code>
          </p>
        </div>

        <div className="flex gap-3">
          <button
            type="submit"
            disabled={submitting}
            className="px-4 py-2 text-sm bg-accent text-accent-fg rounded-lg hover:bg-accent-hover disabled:opacity-50 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {submitting ? "Creating..." : "Create"}
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="px-4 py-2 text-sm border border-border rounded-lg text-text-secondary hover:bg-elevation-2 hover:text-text transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
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
    <div className="bg-warning-bg border border-warning-border rounded-xl p-4 mb-6">
      <p className="text-sm font-medium text-warning-fg mb-2">
        Copy this token now — it will not be shown again.
      </p>
      <div className="flex items-center gap-2">
        <code className="flex-1 bg-elevation-1 border border-border text-text rounded px-3 py-2 text-xs font-mono break-all">
          {secret}
        </code>
        <button
          onClick={copy}
          className="px-3 py-2 text-xs bg-elevation-2 text-text border border-border rounded hover:bg-elevation-1 transition-colors whitespace-nowrap focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
      <button
        onClick={onDismiss}
        className="mt-3 text-xs text-warning-fg hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
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
        <h1 className="text-2xl font-semibold text-text">API Tokens</h1>
        <CreateTokenForm onCreated={handleCreated} addToast={addToast} />
      </div>

      {newSecret && (
        <SecretReveal
          secret={newSecret}
          onDismiss={() => setNewSecret(null)}
        />
      )}

      {loading ? (
        <p className="text-sm text-text-muted py-12 text-center">Loading...</p>
      ) : tokens.length === 0 ? (
        <div className="bg-elevation-1 rounded-xl border border-border shadow-sm px-6 py-12 text-center text-sm text-text-muted">
          No active tokens.
        </div>
      ) : (
        <div className="bg-elevation-1 rounded-xl border border-border shadow-sm overflow-hidden">
          <table className="min-w-full text-sm">
            <thead className="bg-elevation-2 border-b border-border">
              <tr>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Name
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Prefix
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Scopes
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Workspace
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Created
                </th>
                <th className="px-4 py-3 text-left font-medium text-text-secondary">
                  Last used
                </th>
                <th className="px-4 py-3 text-right font-medium text-text-secondary">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {tokens.map((tok) => (
                <tr key={tok.id} className="hover:bg-elevation-2">
                  <td className="px-4 py-3 font-medium text-text">
                    {tok.name}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-text-secondary">
                    {tok.token_prefix}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {tok.scopes.map((s) => (
                        <span
                          key={s}
                          className="inline-flex items-center rounded px-1.5 py-0.5 bg-accent-bg text-accent text-xs font-mono"
                        >
                          {s}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-text-secondary truncate max-w-[10rem]" title={tok.workspace_id ?? undefined}>
                    {tok.workspace_id ?? <span className="text-text-muted">—</span>}
                  </td>
                  <td className="px-4 py-3 text-text-muted text-xs tabular-nums">
                    {new Date(tok.created_at).toLocaleDateString()}
                  </td>
                  <td className="px-4 py-3 text-text-muted text-xs tabular-nums">
                    {tok.last_used_at
                      ? new Date(tok.last_used_at).toLocaleString()
                      : "Never"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => setRevokeTarget(tok)}
                      className="px-3 py-1.5 text-xs border border-danger-border text-danger-fg rounded-lg hover:bg-danger-bg transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
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
