# `omniscience init`

One-shot bootstrap for MCP-aware editors. Writes the per-IDE configuration
file, provisions a workspace token, and runs a `tools/list` smoke test
against the running Omniscience server — typically taking the manual
onboarding flow from ~30 minutes to ~60 seconds.

## Usage

```bash
omniscience init --client <ide> [options]
```

### Supported clients

| `--client`     | Config file written                          | Scope     | Spec |
|----------------|----------------------------------------------|-----------|------|
| `claude-code`  | `~/.claude/mcp_servers.json`                 | global    | https://docs.claude.com/en/docs/claude-code/mcp |
| `cursor`       | `.cursor/mcp.json` (current project)         | workspace | https://docs.cursor.com/context/mcp |
| `cline`        | `.vscode/cline_mcp_settings.json`            | workspace | https://docs.cline.bot/mcp/configuring-mcp-servers |
| `continue`     | `~/.continue/config.json`                    | global    | https://docs.continue.dev/customize/deep-dives/mcp |
| `zed`          | `~/.config/zed/settings.json`                | global    | https://zed.dev/docs/assistant/model-context-protocol |

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--client / -c` | _required_ | Target editor (see table above). |
| `--name / -n`   | `omniscience` | Server name (the key inside the IDE config). |
| `--url`         | `$OMNISCIENCE_URL` or `http://localhost:8000` | Omniscience server base URL. |
| `--token`       | _unset_ | Use an existing token instead of provisioning a new one. |
| `--scopes`      | `search,sources:read` | Comma-separated scopes for the provisioned token. |
| `--print`       | `false` | Emit the resulting config to stdout (no files touched). |
| `--force / -f`  | `false` | Overwrite an existing entry with the same `--name`. |
| `--no-smoke-test` | `false` | Skip the post-write `tools/list` round-trip. |

## What it does

1. **Resolves connection params.** `--url` > `$OMNISCIENCE_URL` > `http://localhost:8000`.
2. **Provisions a workspace token** via `POST /api/v1/tokens` unless one is supplied via `--token` or `$OMNISCIENCE_TOKEN`.
3. **Renders + merges** the per-client config entry, preserving any
   unrelated servers already in the file.
4. **Writes atomically** (`write-tmp + rename`); a crashed run never
   leaves a half-written `settings.json`.
5. **Smoke-tests** the install by issuing a JSON-RPC `tools/list` call
   against `<url>/mcp`. Failures emit a warning but don't roll back the
   config — re-run `omniscience doctor` once the server is reachable.

## Examples

Write the Claude Code entry, provisioning a fresh token against a remote
server:

```bash
omniscience init --client claude-code --url https://omni.example.com
```

Pin an existing token (useful in CI):

```bash
omniscience init --client cursor --token "$OMNISCIENCE_TOKEN"
```

Preview without writing:

```bash
omniscience init --client zed --print | jq .
```

Re-issue a server entry over an older one:

```bash
omniscience init --client claude-code --force
```

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Config written (smoke-test result is advisory only). |
| `1`  | Unknown `--client`, token provisioning failed, existing entry without `--force`, or corrupt config file. |

## Troubleshooting

* **"Existing config is not valid JSON"** — your IDE config file is
  corrupted. Fix it manually or move it aside; the CLI refuses to
  silently overwrite unknown contents.
* **"An MCP entry named 'omniscience' already exists"** — re-run with
  `--force` or pick a different `--name`.
* **Smoke test failed: network error** — server is unreachable. The
  config was still written; run `omniscience doctor` for diagnostics.
