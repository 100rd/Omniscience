"""Neo4j subpackage — internal decomposition of the neo4j_store god-module.

Public surface is unchanged: all symbols remain importable from
``omniscience_index.stores.neo4j_store`` via its re-export sheet.
This ``__init__.py`` re-exports the subpackage's own public surface for
any caller that imports directly from the subpackage path.
"""

from omniscience_index.stores.neo4j.store import Neo4jGraphStore, Neo4jStoreConfig

__all__ = ["Neo4jGraphStore", "Neo4jStoreConfig"]
