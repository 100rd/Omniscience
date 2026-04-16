# Vision

## What Omniscience is

Omniscience is a **self-hosted retrieval service** that turns your organization's knowledge — code repositories, infrastructure configs, documentation, tickets, chats, metrics — into a queryable substrate for AI agents.

It speaks **MCP** (Model Context Protocol) as its primary API. Any MCP-compatible AI client — Claude Code, Cursor, Gemini, custom LangChain agents, AI pipelines like multiqlti — connects in minutes and gains grounded retrieval across everything Omniscience has indexed.

## What it isn't

- **Not a chatbot.** No embedded LLM. No opinionated synthesis. Callers get chunks with citations; they craft the answer.
- **Not a replacement for Glean / Sourcegraph Cody / Onyx.** Smaller, opinionated for AI-tool integration. Trades features for surface-area and operational simplicity.
- **Not a vector database.** Uses one (pgvector) but solves the ingestion, freshness, ACL, and retrieval problems around it.

## Primary users

1. **Engineers using AI coding tools.** Claude Code / Cursor / Gemini users who want their agent to actually know their codebase and docs, not just the current file.
2. **Teams building AI pipelines.** multiqlti / LangChain / custom orchestrators that need a retrieval step grounded in the org's knowledge.
3. **Platform teams.** Self-hosters who can't send internal code to third-party services and want one retrieval endpoint for every AI tool their org adopts.

## Core problems solved

| Problem | How Omniscience addresses it |
|---|---|
| AI tools hallucinate about your codebase | Retrieval returns real chunks with `origin_url` + `indexed_at`; caller grounds the answer |
| Each AI tool ships its own incomplete indexer | One indexer, many consumers via MCP |
| Stale indexes produce wrong answers | Freshness SLO per source, tombstones, incremental ingestion |
| Multiple data sources (code + docs + tickets) | Pluggable connector SDK, unified retrieval |
| Can't send code to SaaS knowledge tools | Self-hosted, local embeddings by default (Ollama), no outbound required |
| Access control differs per source | Per-query ACL filter with source-level token scoping |

## Non-goals

- **Running inference.** Embeddings yes; completion no. The calling LLM does completions.
- **UI-first product.** There will be an admin UI but the product is the API.
- **Training custom models.** We use existing embedding models.
- **Becoming a knowledge graph database.** We emit graph relations where useful but don't replace Neo4j/Cypher.

## Design principles

1. **MCP-first, REST-second.** Tool schemas are the contract. REST and CLI are conveniences.
2. **Retrieval-only.** One responsibility, done well.
3. **Citations are non-negotiable.** Every chunk carries provenance.
4. **Freshness is measurable.** Every source has an SLO; staleness is a first-class signal in responses.
5. **Incremental everything.** Full reindex is a recovery operation, not a routine one.
6. **Local-first defaults.** Zero external calls out of the box.
7. **Pluggable where it matters.** Connectors, embedding providers, parsers.
