# MCP catalog submission recipes

This document captures, for each public MCP discovery surface we target,
**exactly** what to submit, where, in what format, and how long it
typically takes the maintainers to merge or approve.

The metadata payloads live next to the repo root under `.mcp/`. They
are the source of truth — when content drifts, edit those files first
and re-submit, do not edit text inside the registries' web forms.

## Positioning

We submit under the **broad** positioning: *"Self-hosted MCP retrieval
with replay & audit"*. Rationale: H1 (retrieval) + H5 (audit) fusion;
broader TAM at first listing. We may add a second narrow listing
(*"Bitemporal knowledge for K8s/SRE"*) later — it is cheap to add and
cannot collide with the broad entry.

## Catalogs

### 1. modelcontextprotocol.io official registry

- **URL**: <https://registry.modelcontextprotocol.io/>
- **Source of truth**: [.mcp/registry.json](../.mcp/registry.json)
- **Schema**: `https://static.modelcontextprotocol.io/schemas/2025-09-29/server.schema.json`
- **Submission flow**:
  1. Install the publisher CLI: `npm i -g @modelcontextprotocol/registry-publisher`
     (or use the GitHub Action `modelcontextprotocol/publish-mcp-server@v1`).
  2. From the repo root run:
     ```bash
     mcp-publisher publish --file .mcp/registry.json
     ```
     It will OIDC-authenticate against GitHub (org `100rd`, repo
     `Omniscience`) — no API keys to manage.
  3. The publish call returns a server ID. Pin it as
     `OMNISCIENCE_MCP_REGISTRY_ID` in repo secrets so future CI runs
     can update the entry instead of creating duplicates.
- **Approval**: automatic on successful schema-validation + repo-ownership proof.
- **Update cadence**: bump `version` in `.mcp/registry.json` on every
  release tag — the CI workflow at `.github/workflows/release.yml`
  should run `mcp-publisher publish` on tag push.

### 2. PulseMCP

- **URL**: <https://www.pulsemcp.com/>
- **Source of truth**: [.mcp/pulsemcp.json](../.mcp/pulsemcp.json)
- **Submission flow**:
  1. Open <https://www.pulsemcp.com/submit>.
  2. Pick **"Self-hosted server"**.
  3. Paste fields from `.mcp/pulsemcp.json` into the form:
     - `name`, `short_description`, `long_description`
     - `repository`, `homepage`, `license`
     - **Categories** (multi-select): `devtools`, `observability`,
       `knowledge-management`, `audit`, `retrieval`.
     - **Tags**: paste the tag list from the JSON.
     - **Install** snippets: copy `install.one_line`, `install.docker`,
       `install.uvx` verbatim.
     - **Config example**: paste the `mcp_config_example` JSON block.
     - **Tools**: add each row from the `tools[]` array.
     - **Screenshots**: upload the three files from
       `docs/assets/screenshots/` (must exist in repo before submission —
       see "Screenshot assets" section below).
  4. Submit.
- **Approval SLA**: ~2 business days (PulseMCP curates manually).
- **Contact**: `hello@pulsemcp.com` if the listing stalls more than 5
  business days.

### 3. Cline marketplace

- **URL**: <https://github.com/cline/mcp-marketplace>
- **Source of truth**: [.mcp/cline-marketplace.json](../.mcp/cline-marketplace.json)
- **Submission flow**:
  1. Fork <https://github.com/cline/mcp-marketplace>.
  2. Add `omniscience.json` under `servers/` with the contents of
     `.mcp/cline-marketplace.json`.
  3. Append an entry to `marketplace.json` referencing the new file.
  4. Open a PR titled `Add Omniscience (self-hosted retrieval with replay)`.
  5. Reviewer is `@saoudrizwan` or another Cline maintainer.
- **Approval SLA**: 3-10 business days. Most rejections are for
  missing `llmsInstallationContent` (we include it) or stale
  `logoUrl` (we point at `main` branch — keep that asset stable).
- **Notes**:
  - `requiresApiKey: true` — Cline displays a warning to users; that is
    expected and correct (we mint per-workspace tokens).
  - `autoApprove` list in the install content is intentionally narrow:
    only read-only retrieval tools.

### 4. mcp.directory

- **URL**: <https://mcp.directory/>
- **Source of truth**: [.mcp/pulsemcp.json](../.mcp/pulsemcp.json) (same
  shape works — mcp.directory mostly mirrors PulseMCP fields).
- **Submission flow**:
  1. Open <https://mcp.directory/submit>.
  2. Paste GitHub URL `https://github.com/100rd/Omniscience`.
  3. The form pre-fills from the repo; manually correct categories
     to match `.mcp/pulsemcp.json`.
  4. Submit.
- **Approval SLA**: 1-3 business days.

### 5. Awesome MCP Servers

- **URL**: <https://github.com/punkpeye/awesome-mcp-servers>
- **Source of truth**: no JSON — single-line markdown entry.
- **Submission flow**:
  1. Fork the repo.
  2. Under the **"Retrieval / Search"** section (and additionally under
     **"Observability"**), add the line, alphabetically:
     ```markdown
     - [100rd/Omniscience](https://github.com/100rd/Omniscience) - Self-hosted MCP retrieval with bitemporal replay and audit. Indexes code, docs, infra, Datadog, PagerDuty, runbooks; returns citations.
     ```
  3. PR title: `Add Omniscience to Retrieval and Observability sections`.
- **Approval SLA**: 1-5 business days. Reviewer enforces alphabetical
  order and ≤1-sentence description.

## Screenshot assets

The registry + PulseMCP entries reference three images that need to
exist at the stable URL `https://raw.githubusercontent.com/100rd/Omniscience/main/docs/assets/...`:

| File | What it shows |
|---|---|
| `docs/assets/icon-256.png` | Square logo, 256x256, transparent PNG |
| `docs/assets/screenshots/search.png` | Claude Code calling `search()` and rendering results |
| `docs/assets/screenshots/replay.png` | The `replay()` tool diff'ing now vs as-of T |
| `docs/assets/screenshots/blast-radius.png` | `blast_radius()` graph visualization |

**Status**: as of this PR these assets do **not yet exist**. The
README badges will 404 until they are committed. Track creation in
issue #237; design lead to deliver.

## Install-flow validation

The pinned install advertised everywhere (README, PulseMCP, Cline
marketplace) downloads the installer at an immutable release tag,
verifies its SHA-256 against the in-repo sidecar
`.mcp/install.sh.sha256`, and only then executes it:

```bash
curl -fsSL -o install.sh https://raw.githubusercontent.com/100rd/Omniscience/v0.5.0/.mcp/install.sh
echo "080551789e472af099aa9e60554a3ac28a30a7fa98dd1c80c9a0698b5eff7ca1  install.sh" | sha256sum -c -
# macOS (no sha256sum): shasum -a 256 -c - instead
bash install.sh
```

Cutting a release that touches `.mcp/install.sh` must regenerate
`.mcp/install.sh.sha256` and the digests inlined in the README, this
doc, `.mcp/pulsemcp.json`, and `.mcp/cline-marketplace.json`
(`tests/test_install_pinning.py` fails otherwise).

Public installs use the GHCR-image variant **`docker-compose.prod.yml`** (no local build context required); images are published from tag `v*` by `.github/workflows/release.yml` to `ghcr.io/100rd/omniscience-{app,admin}`. Pin a specific image tag with `OMNISCIENCE_VERSION=v0.5.0 bash install.sh`; default is `:latest`. The dev compose (`docker-compose.yml`) still uses `build:` for local hot-reload and is not consumed by the installer.

It must pass on **both** macOS and Linux. Validation procedure:

1. **macOS** (Intel + Apple Silicon): fresh shell, fresh `$PWD`:
   ```bash
   curl -fsSL -o install.sh https://raw.githubusercontent.com/100rd/Omniscience/v0.5.0/.mcp/install.sh
   sha256sum -c install.sh.sha256   # after fetching the sidecar; macOS: shasum -a 256 -c
   bash install.sh
   curl http://localhost:8000/health
   ```
2. **Linux** (Ubuntu 22.04 + Debian 12): same procedure inside
   `docker run -it --rm ubuntu:22.04 bash` after installing docker.
   For ergonomics use a vanilla EC2 t3.small.
3. **Verify** that re-running the script is idempotent (does not
   overwrite `.env`, does not duplicate containers).

The script is held in `.mcp/install.sh` and is the single source of
truth — registries link to it, the README links to it.

## Tracking checklist (for the human owner)

Open this PR ("chore(mcp): submit to MCP catalogs"), then walk this
list. Tick each box as the external submission lands. The issue closes
when all five are checked.

- [ ] modelcontextprotocol.io — run `mcp-publisher publish --file .mcp/registry.json`; record server ID in repo secrets.
- [ ] PulseMCP — submit form at <https://www.pulsemcp.com/submit>; record listing URL.
- [ ] Cline marketplace — open PR against `cline/mcp-marketplace`; record PR URL.
- [ ] mcp.directory — submit form at <https://mcp.directory/submit>; record listing URL.
- [ ] Awesome MCP Servers — open PR against `punkpeye/awesome-mcp-servers`; record PR URL.
- [ ] Update README badges to point at the resolved listing URLs (remove `pending` markers).
- [ ] Add `.github/workflows/mcp-publish.yml` to re-publish to modelcontextprotocol.io on every tag.
- [ ] Run install-flow validation on macOS + Linux post-submission (the script links to `main`, so any breakage is immediately visible to new users).
