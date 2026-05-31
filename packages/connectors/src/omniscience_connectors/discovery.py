"""Base interfaces for source discovery (GitHub/GitLab Org scanning)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

@dataclass(frozen=True)
class DiscoveredSource:
    """A source candidate found by a discovery connector."""
    name: str
    type: str  # e.g. "git"
    uri: str
    external_id: str  # e.g. github repo ID or full path
    metadata: dict[str, Any] = field(default_factory=dict)

class DiscoveryConnector(Protocol):
    """Protocol for connectors that can discover new sources automatically."""
    
    async def discover(self, config: dict[str, Any], secrets: dict[str, str]) -> list[DiscoveredSource]:
        """Scan the remote platform and return a list of discovered sources."""
        ...
