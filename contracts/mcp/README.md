# Omniscience MCP v1 contract binding (task-sp-86-management-readonly-release)

**Status:** producer release evidence / non-live. No active consumer pin, production
token, live registry push, or provider/model call lives here. See
[ADR-0022](../../docs/decisions/0022-management-readonly-runtime-release.md) and
[genai-enablement ADR-0017](../../../genai-enablement/docs/decisions/0017-omniscience-mcp-v1-contract-and-severance.md).

Implements the MCP-facing slice of `task-sp-86-management-readonly-release`, scoped to
`contracts/mcp/**` only.

## This is a binding, not a fork

`apps/server/src/omniscience_server/mcp/contracts/v1/` is the SP-10 ground truth: the
already-merged, human-verified `McpContractV1` manifest, tool registry, and 32 wire
schemas (`specs/SPEC-MCP-stable-contract-v1.md`). That implementation is outside
task-sp-86's writable scope and stays untouched. `contracts/mcp/` vendors a
byte-identical copy at a flat, language-neutral, top-level path so a non-Python
consumer (Go, or any other language) can validate against the contract without
importing `omniscience_server` or running Node.js — see
`contracts/releases/management-readonly-v1/consumer-kit-go/`.

```text
contracts/mcp/
  pin.json               per-file sha256 this binding claims to match, plus the SP-10
                         ground-truth path and its own schema_set_sha256/tool_count
  manifest.json           byte-identical copy of the SP-10 contract manifest
  tool-registry.json      byte-identical copy of the 15-tool canonical registry
  schemas/v1/             byte-identical copies of the 32 SP-10 wire schemas
  validator/              dependency-free JSON Schema subset validator (schema_check.py)
  tooling/verify_pin.py   recomputes every vendored file's sha256 against pin.json AND
                         against the live SP-10 ground-truth file at the same path
```

## Running the probe

```bash
python3 contracts/mcp/tooling/verify_pin.py
```

`scripts/qualify_management_readonly_release.py` calls the same check as part of
AC-SP86-1 (release-lock digest re-derivation) and folds `manifest.json`'s
`schema_set_sha256` into the release lock's `mcp_manifest_digest`.

## Non-activation boundary

- No credential, cloud key, live consumer pin, provider/model call, Barbarossa
  decision, deployment, or production-readiness claim.
- `contracts/mcp/validator/` only re-exports the vendored structural
  `schema_check.validate`; it issues no permit and mints no token.
- This binding does not change `apps/server/src/omniscience_server/mcp/**` -- SP-86
  packages the already-merged SP-10 contract, it does not reinterpret or widen it.
- A drift between this copy and the live SP-10 source (caught by
  `tooling/verify_pin.py`'s ground-truth check) is a release-lock RED, not a silent
  re-vendor -- task-sp-86 cannot edit the SP-10 implementation to make it match.
