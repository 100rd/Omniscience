"""Filesystem source connector for Omniscience.

Discovers and fetches files from a local filesystem path.
Uses inotify/kqueue/FSEvents via watchfiles for future watcher support.
"""

from omniscience_connectors.fs.connector import FsConfig, FsConnector

__all__ = [
    "FsConfig",
    "FsConnector",
]
