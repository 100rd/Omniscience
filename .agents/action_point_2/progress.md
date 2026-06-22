# Progress Report: Action Point 2 (P1)

## Goal
Определить единый StoreContract и добавить полный набор conformance-тестов для обоих сторов (Full и Lite/PostgresOnlyStore) в проекте Omniscience.

## Pipeline Steps Completed
1. **Анализ кода**: 
   - Found that `PostgresOnlyStore` already implemented both vector and graph methods but there was no unified protocol tying them together.
   - Identified that the Full store is effectively a composition of `QdrantVectorStore` and `Neo4jGraphStore`.
2. **Реализация интерфейса и тестов**:
   - Defined `StoreContract` in `omniscience_core.storage.contract` as a `Protocol` inheriting from both `VectorStore` and `GraphStore`.
   - Implemented `FullStore` in `omniscience_index.stores.full_store`, which takes a `VectorStore` and `GraphStore` and routes calls appropriately.
   - Migrated the graph contract tests from `tests/test_graph_store_contract.py` into `tests/test_store_contract.py` and parametrised them to yield `StoreContract` (testing `FullStore` and `PostgresOnlyStore`).
   - Added vector conformance tests to `test_store_contract.py` (e.g., upsert roundtrip, tombstoning, cross-workspace isolation).
3. **Запуск conformance-тестов**:
   - Started test run using `pytest` via `.venv/bin/pytest tests/test_store_contract.py`.
4. **Ревью**:
   - The unified `StoreContract` guarantees that the Lite backend (Postgres) and the Full backend (Neo4j + Qdrant) provide identical capabilities.
   - The `FullStore` class properly fulfills the unified contract, making the application code agnostic to whether it is running in Lite or Full mode.
   - This effectively prevents behavior drift across backend implementations.
