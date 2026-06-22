# Action Point 6 (P1) Report

## Tasks Completed
1. **Property-based tests for concurrent upsert**:
   - Added `test_concurrent_upsert_property` using `hypothesis` to `tests/test_store_contract.py`.
   - The test generates random names for entities, runs `upsert_entity` concurrently using `asyncio.gather`, and asserts that all entities exist.
   - Being in `test_store_contract.py`, this test will automatically run against both stores (Full and Lite) thanks to the existing `@pytest.fixture(params=...)` parameterization of the `store` fixture.

2. **Added 'degraded' flag for Lite**:
   - Modified `packages/index/src/omniscience_index/stores/postgres_only_store.py` (the Lite store backend).
   - Added `degraded: bool = False` flag to the `PostgresOnlyStore.__init__` signature.
   - When `self.degraded` is set to `True`, a relevant discount of `0.8` is applied to the retrieved vector scores in `PostgresOnlyStore.search()` (e.g. `score *= 0.8`), reflecting the degraded mode.

3. **CI Execution on both Stores**:
   - Validated that `tests/test_store_contract.py` is executed against both `full` and `postgres_only` backends within GitHub Actions CI. The `PostgresContainer` and `Neo4jContainer` fixtures handle spawning the required isolated instances during pytest runs.

All instructions for Action Point 6 (P1) have been implemented.
