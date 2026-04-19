"""Federation configuration: registry of remote Omniscience instances.

The ``FederatedInstance`` model describes a single peer.  ``FederationConfig``
holds the full list of peers and shared tuning parameters.  Both are plain
Pydantic models loaded from Settings (JSON env var or object construction).
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class FederatedInstance(BaseModel):
    """A remote Omniscience instance to include in federated search.

    Args:
        name:     Human-readable label used in logging and ``source_instance`` annotations.
        url:      Base URL of the remote instance (e.g. ``https://other-omni.corp.example``).
        token:    Bearer token for the remote instance's ``/api/v1/search`` endpoint.
        enabled:  When ``False`` the instance is skipped at query time without removal.
        priority: Tie-breaking order when re-ranking.  Lower value = higher priority.
                  Used to pick among hits with identical scores from different remotes.
    """

    name: str
    url: str
    token: str
    enabled: bool = True
    priority: int = 0


class FederationConfig(BaseModel):
    """Collection of federated peers and shared request parameters.

    Args:
        instances:          List of remote Omniscience instances to query.
        timeout_seconds:    Per-remote HTTP timeout applied to each search call.
        max_remote_results: Maximum ``top_k`` forwarded to each remote.
                            Caps bandwidth per peer; merged results are still
                            sliced to the caller's ``top_k`` after merge.
    """

    instances: list[FederatedInstance] = Field(default_factory=list)
    timeout_seconds: float = Field(default=5.0, ge=0.1, le=300.0)
    max_remote_results: int = Field(default=20, ge=1, le=500)

    @property
    def enabled_instances(self) -> list[FederatedInstance]:
        """Return only instances where ``enabled=True``."""
        return [inst for inst in self.instances if inst.enabled]

    @classmethod
    def from_json(cls, json_str: str) -> FederationConfig:
        """Parse a JSON string representing a list of instance dicts.

        Accepts:
            - A JSON array of instance objects: ``[{"name": ..., "url": ..., "token": ...}]``
            - An empty string → returns an empty config.

        Args:
            json_str: Raw JSON string, typically from an environment variable.

        Returns:
            A populated ``FederationConfig`` instance.

        Raises:
            ValueError: If *json_str* is non-empty but not valid JSON.
        """
        stripped = json_str.strip()
        if not stripped:
            return cls()

        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"federation_instances is not valid JSON: {exc}") from exc

        if not isinstance(raw, list):
            raise ValueError("federation_instances JSON must be a list of instance objects")

        instances = [FederatedInstance.model_validate(item) for item in raw]
        logger.debug("parsed %d federated instances from JSON", len(instances))
        return cls(instances=instances)


__all__ = [
    "FederatedInstance",
    "FederationConfig",
]
