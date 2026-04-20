"""Runtime secrets resolution for source connectors.

:class:`SecretsResolver` maps a ``secrets_ref`` string stored on a
:class:`~omniscience_core.db.models.Source` row to a plain
``dict[str, str]`` of secret key-value pairs consumed by connectors at
fetch time.

Supported ``secrets_ref`` formats
----------------------------------

``None`` or ``""``
    Returns an empty dict — the source requires no secrets.

``env:PREFIX_``
    Reads **all** environment variables whose names start with ``PREFIX_``
    and returns them as ``{"SUFFIX": value, ...}`` where ``SUFFIX`` is the
    part of the name after the prefix.

    Example::

        # Environment:  GITHUB_TOKEN=ghp_abc123  GITHUB_ORG=myorg
        resolver.resolve("env:GITHUB_")
        # -> {"TOKEN": "ghp_abc123", "ORG": "myorg"}

``env:VAR_NAME=key``
    Reads the single environment variable ``VAR_NAME`` and returns it as
    ``{"key": value}``.

    Example::

        # Environment:  GH_TOKEN=ghp_abc123
        resolver.resolve("env:GH_TOKEN=token")
        # -> {"token": "ghp_abc123"}

``file:/path/to/secrets.json``
    Parses the JSON file at the given path and returns its top-level
    key-value pairs as strings.  Only flat ``{"key": "value", ...}``
    objects are supported; nested structures raise :class:`ConfigError`.

Errors
------

:class:`~omniscience_core.errors.ConfigError` is raised when:

- The ``secrets_ref`` format is unrecognised.
- An ``env:VAR_NAME=key`` reference points to an undefined variable.
- A ``file:`` reference cannot be opened or is not valid flat JSON.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog

from omniscience_core.errors import ConfigError

log = structlog.get_logger(__name__)


class SecretsResolver:
    """Resolves ``secrets_ref`` strings to concrete secret dictionaries.

    The resolver is stateless and thread-safe; a single instance can be
    reused across many resolutions.
    """

    def resolve(self, secrets_ref: str | None) -> dict[str, str]:
        """Resolve *secrets_ref* to a ``dict[str, str]`` of secrets.

        Args:
            secrets_ref: The opaque reference stored on the
                :class:`~omniscience_core.db.models.Source` row, or ``None``
                when the source requires no secrets.

        Returns:
            A mapping of secret names to values.  Empty dict when
            *secrets_ref* is ``None`` or an empty string.

        Raises:
            ConfigError: When the reference is malformed, an environment
                variable is missing, or a secrets file cannot be read.
        """
        if not secrets_ref:
            return {}

        if secrets_ref.startswith("env:"):
            return self._resolve_env(secrets_ref[len("env:") :])

        if secrets_ref.startswith("file:"):
            return self._resolve_file(secrets_ref[len("file:") :])

        raise ConfigError(
            f"Unrecognised secrets_ref format: {secrets_ref!r}. "
            "Supported formats: 'env:PREFIX_', 'env:VAR=key', 'file:/path/to/secrets.json'."
        )

    # ------------------------------------------------------------------
    # Private resolution backends
    # ------------------------------------------------------------------

    def _resolve_env(self, spec: str) -> dict[str, str]:
        """Resolve ``env:`` references.

        *spec* is the portion after the ``env:`` prefix, either:
        - ``PREFIX_``  — scan all env vars beginning with the prefix
        - ``VAR=key``  — read one env var and rename it to *key*
        """
        if "=" in spec:
            return self._resolve_env_single(spec)
        return self._resolve_env_prefix(spec)

    def _resolve_env_prefix(self, prefix: str) -> dict[str, str]:
        """Return all env vars that start with *prefix*, keyed by the suffix."""
        result: dict[str, str] = {}
        for name, value in os.environ.items():
            if name.startswith(prefix):
                key = name[len(prefix) :]
                result[key] = value

        log.debug(
            "secrets_resolved_env_prefix",
            prefix=prefix,
            keys_found=len(result),
        )
        return result

    def _resolve_env_single(self, spec: str) -> dict[str, str]:
        """Resolve ``VAR_NAME=key`` spec.

        Reads ``os.environ[VAR_NAME]`` and returns ``{key: value}``.

        Raises:
            ConfigError: When ``VAR_NAME`` is not set.
        """
        var_name, _, key = spec.partition("=")
        var_name = var_name.strip()
        key = key.strip()

        value = os.environ.get(var_name)
        if value is None:
            raise ConfigError(
                f"Environment variable {var_name!r} referenced in secrets_ref is not set."
            )

        log.debug("secrets_resolved_env_single", var_name=var_name, key=key)
        return {key: value}

    def _resolve_file(self, path_str: str) -> dict[str, str]:
        """Parse a JSON secrets file and return its flat key-value pairs.

        Args:
            path_str: Filesystem path (after the ``file:`` prefix).

        Raises:
            ConfigError: When the file cannot be opened, is not valid JSON,
                or contains non-string values.
        """
        path = Path(path_str)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Cannot read secrets file {path_str!r}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Secrets file {path_str!r} is not valid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(
                f"Secrets file {path_str!r} must contain a JSON object at the top level, "
                f"got {type(data).__name__}."
            )

        result: dict[str, str] = {}
        for k, v in data.items():
            if not isinstance(v, str):
                raise ConfigError(
                    f"Secrets file {path_str!r}: value for key {k!r} must be a string, "
                    f"got {type(v).__name__}."
                )
            result[str(k)] = v

        log.debug("secrets_resolved_file", path=path_str, keys_found=len(result))
        return result


__all__ = ["SecretsResolver"]
