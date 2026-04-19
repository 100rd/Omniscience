"""Database source connector for Omniscience.

Discovers and fetches table/view schemas from relational databases via
SQLAlchemy's information_schema queries.  Supports any SQLAlchemy-compatible
database (PostgreSQL, MySQL, SQLite, etc.).
"""

from omniscience_connectors.database.connector import DatabaseConfig, DatabaseConnector

__all__ = ["DatabaseConfig", "DatabaseConnector"]
