# MCP Apps UI Cards

Omniscience implements the **Jan 2026 MCP Apps spec** (experimental) so
compatible clients — Claude Code, Cursor, Cline — render tool responses
as structured cards instead of raw JSON.  This document describes the
wire contract.

> Status: **experimental**.  The schemas are versioned (see
> `apps_version`) and may evolve in lockstep with the upstream spec.
> Existing fields will not be removed without a major version bump;
> new fields may be added at any time.

## Capability negotiation

The MCP Apps capability is namespaced under the
`ClientCapabilities.experimental` slot:

```jsonc
{
  "capabilities": {
    "experimental": {
      "omniscience/apps": {}
    }
  }
}
```

* **Clients** advertise the key on `initialize` to opt in.
* **Servers** mirror the same key under
  `ServerCapabilities.experimental` to indicate they can emit cards.
* **A bare `{}` value is sufficient.**  Future spec revisions may add
  sub-keys (e.g. a list of card schemas the client knows how to
  render); a client that omits sub-keys is taken to support all
  current schemas.

When a client does **not** advertise `omniscience/apps`, every tool
response is byte-for-byte identical to the pre-Apps wire format.  This
is a non-negotiable invariant: a legacy client must never see a wire
shape it does not understand.

## Where the card lives

Cards are attached under the standard MCP `_meta` slot, keyed by the
capability namespace:

```jsonc
{
  // all legacy top-level fields preserved verbatim
  "alert": { ... },
  "target_resource": { ... },
  "responsible_pr": { ... },
  "slack_threads": [ ... ],
  "confidence_score": 0.9,
  "effective_as_of": "2026-05-22T12:00:00+00:00",
  "_meta": {
    "omniscience/apps": {
      "apps_version": "1.0",
      "card_type": "resolve_incident",
      "summary": { ... },
      "candidates": [ ... ],
      "similar_past": [ ... ]
    }
  }
}
```

Renderers MUST ignore unknown top-level keys (per the MCP base spec)
and SHOULD ignore `_meta.omniscience/apps` if `apps_version` is
greater than the schema version they implement, falling back to the
legacy rendering.

## Card: `resolve_incident`

The card emitted for the
[`resolve_incident`](mcp.md#resolve_incident) tool.

### Top-level schema

| Field          | Type                       | Required | Description                                                          |
|----------------|----------------------------|----------|----------------------------------------------------------------------|
| `apps_version` | `"1.0"`                    | yes      | Schema version.                                                       |
| `card_type`    | `"resolve_incident"`       | yes      | Discriminator for renderer dispatch.                                  |
| `summary`      | `IncidentSummary`          | yes      | Header block.                                                         |
| `candidates`   | `RankedCandidate[]` (`<=3`)| yes      | Up to three ranked rows, ordered descending by confidence.            |
| `similar_past` | `SimilarPastIncident[]`    | yes      | May be empty.  Surfaced when the inner response carries the field.  |

### `IncidentSummary`

| Field                   | Type            | Required | Description                                                                                  |
|-------------------------|-----------------|----------|----------------------------------------------------------------------------------------------|
| `headline`              | `string`        | yes      | One-line headline.  Falls back to the alert URI tail if no `chunk_text` on the alert.        |
| `summary`               | `string`        | yes      | Paragraph-length human summary.                                                              |
| `alert_uri`             | `string`        | yes      | Canonical alert URI (`alert://provider/id`).                                                 |
| `fired_at`              | `string \| null`| no       | ISO-8601 UTC of when the alert fired.                                                        |
| `below_trust_threshold` | `boolean`       | yes      | Mirrors `meta.below_trust_threshold` from the inner response (#155).                          |

### `RankedCandidate`

| Field        | Type             | Required | Description                                                                  |
|--------------|------------------|----------|------------------------------------------------------------------------------|
| `rank`       | `integer >= 1`   | yes      | 1-indexed rank within the card.                                              |
| `title`      | `string`         | yes      | Primary headline (e.g. `acme/api#42`, `#C12345 / 1716383700.000100`).        |
| `subtitle`   | `string \| null` | no       | Muted caption text (e.g. `merged 2026-05-22T11:30:00+00:00`).                |
| `kind`       | enum             | yes      | One of `alert`, `resource`, `pull_request`, `slack_thread`, `other`.         |
| `confidence` | `ConfidenceBar`  | yes      | Calibrated score with discrete bucket.                                       |
| `citations`  | `CitationLink[]` | yes      | May be empty.  Each link is a canonical entity URI.                          |

### `ConfidenceBar`

| Field   | Type           | Required | Description                                                                                                            |
|---------|----------------|----------|------------------------------------------------------------------------------------------------------------------------|
| `value` | `float [0,1]`  | yes      | Calibrated score.                                                                                                      |
| `bucket`| enum           | yes      | One of `very_low`, `low`, `medium`, `high`.  Derived via `bucket_for_score`: `>=0.8`->high, `>=0.5`->medium, `>=0.2`->low, else very_low. |
| `label` | `string`       | yes      | Short human label for screen readers, e.g. `"high confidence (0.92)"`.                                                 |

### `CitationLink`

| Field   | Type             | Required | Description                                                            |
|---------|------------------|----------|------------------------------------------------------------------------|
| `title` | `string`         | yes      | Human-readable label for the link.                                     |
| `uri`   | `string`         | yes      | Canonical entity URI (`https://...`, `slack://...`, `alert://...`).    |
| `kind`  | `string \| null` | no       | Optional entity-kind hint for icon selection.                          |

### `SimilarPastIncident`

Rendered only when the inner `resolve_incident` response carries an
optional `similar_past: list[SimilarIncident]` field (added by the
parallel work in [#233](https://github.com/100rd/Omniscience/issues/233)).
Absence yields an empty list; **the card builder never raises on a
missing or malformed field**.

| Field              | Type             | Required | Description                                                       |
|--------------------|------------------|----------|-------------------------------------------------------------------|
| `alert_id`         | `string`         | yes      | Canonical alert URI of the past incident.                         |
| `title`            | `string`         | yes      | Short human-readable summary.                                     |
| `occurred_at`      | `string \| null` | no       | ISO-8601 UTC timestamp of when the past incident fired.           |
| `similarity_score` | `float [0,1]?`   | no       | Optional clusterer similarity in `[0, 1]`.  Out-of-range -> null. |
| `resolution_hint`  | `string \| null` | no       | One-line note on how the past incident was resolved.              |

## Backwards-compatibility guarantees

1. **Legacy clients are unaffected.**  No `_meta` key, no shape
   change.  Every test exercising the pre-#238 wire format keeps
   passing.
2. **The inner response is immutable.**  The card is composed at the
   MCP tool boundary by reading the inner dict; it never mutates it.
3. **Malformed inputs degrade gracefully.**  If the card builder
   raises for any reason, the wrapper returns the legacy response
   unchanged.  An Apps-aware client sees no card; it does not see a
   broken tool.
4. **Forward-compat with new fields.**  Optional fields (e.g.
   `similar_past`) are picked up automatically when present.  Removing
   a field would require a major version bump.

## Implementation map

| File                                                                        | Role                                                          |
|-----------------------------------------------------------------------------|---------------------------------------------------------------|
| `apps/server/src/omniscience_server/mcp/apps/card_schemas.py`               | Pydantic schemas (this document is generated from them).      |
| `apps/server/src/omniscience_server/mcp/apps/card_builders.py`              | Pure builder + wire-format wrapper.                           |
| `apps/server/src/omniscience_server/mcp/apps/capability.py`                 | Capability key + client-capability detection.                 |
| `apps/server/src/omniscience_server/mcp/server.py::resolve_incident`        | Wire-up: tool body calls `wrap_resolve_incident_response`.    |
| `tests/test_mcp_apps_card.py`                                               | Unit tests (builder, capability detection, bucket bounds).    |
| `tests/integration/test_mcp_apps_card_render.py`                            | End-to-end test via the registered MCP tool.                  |

## Renderer screenshots

The branch carrying this work attaches screenshots for the three
supported renderers in the PR description:

* Claude Code: `docs/api/screenshots/mcp-apps-claude-code.png`
* Cursor: `docs/api/screenshots/mcp-apps-cursor.png`
* Cline: `docs/api/screenshots/mcp-apps-cline.png`

(Files are not checked in to the repo — they live in the PR body.)
