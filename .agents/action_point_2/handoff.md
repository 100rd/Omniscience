# Handoff: Action Point 2 (P1)

## Summary of Work
I have completed Action Point 2. The unified `StoreContract` protocol was defined, along with a `FullStore` implementation and a unified suite of conformance tests.

- **`omniscience_core/storage/contract.py`**: Added `StoreContract` which inherits from `GraphStore` and `VectorStore`.
- **`omniscience_index/stores/full_store.py`**: Created `FullStore` which implements `StoreContract` by composing `VectorStore` and `GraphStore`.
- **`tests/test_store_contract.py`**: Replaced the previous graph-only contract test with a combined suite that tests both graph and vector operations. The tests run against both the `PostgresOnlyStore` (Lite-mode) and the `FullStore` (Full-mode, using Neo4j and Qdrant testcontainers).

## Next Steps
- Verify the CI test runs once the docker testcontainers pull and execute.
- Review and merge the changes.
