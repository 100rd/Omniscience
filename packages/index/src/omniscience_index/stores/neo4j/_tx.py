"""Low-level Neo4j async-transaction runner helpers.

These are module-level functions (not methods) because the Neo4j Python
driver's ``execute_read`` / ``execute_write`` managed-transaction API
requires a plain callable as the transaction body.  Keeping them here
avoids the overhead of defining a new closure on every call site and
makes them importable by integration tests that need to exercise raw
Cypher round-trips.
"""

from __future__ import annotations

from typing import Any

from neo4j import AsyncManagedTransaction


async def _run_write_stmt(
    tx: AsyncManagedTransaction,
    cypher: str,
    params: dict[str, Any],
) -> None:
    """Run a single write statement inside a managed transaction."""
    await tx.run(cypher, params)


async def _run_write_returning(
    tx: AsyncManagedTransaction,
    cypher: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run a write statement and materialise its result rows."""
    result = await tx.run(cypher, params)
    return [record.data() async for record in result]


async def _run_read_stmt(
    tx: AsyncManagedTransaction,
    cypher: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run a read statement and materialise its result rows."""
    result = await tx.run(cypher, params)
    return [record.data() async for record in result]
