"""Loader for ``bench/vendors/versions.json`` — pinned vendor versions.

Keeping the on-disk file as JSON (rather than YAML or a Python dict)
makes it trivial for the CI matrix to ``jq`` out image tags, and for
external researchers to verify exactly which vendor builds produced a
historical leaderboard row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DEFAULT_VERSIONS_PATH: Final[Path] = Path(__file__).resolve().parent / "versions.json"


@dataclass(frozen=True, slots=True)
class VendorPin:
    """One vendor's pinned reference.

    Attributes match the keys in ``versions.json`` minus the optional
    ``$comment`` / ``notes`` fields. We keep ``raw`` around so unusual
    per-vendor metadata (image digest, commit, auth env var) is still
    reachable without forcing every adapter into the same shape.
    """

    name: str
    kind: str
    ref: str
    endpoint_env: str
    endpoint_default: str
    raw: dict[str, str]


def load_vendor_pins(path: Path = DEFAULT_VERSIONS_PATH) -> dict[str, VendorPin]:
    """Load and shallow-validate ``versions.json``.

    Raises :class:`FileNotFoundError` / :class:`KeyError` with the
    offending field name. We intentionally do *not* fail on unknown
    top-level vendors — adding a new vendor to the file should be
    backwards-compatible.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):  # pragma: no cover - guards malformed file
        raise ValueError(f"{path}: top-level must be an object")
    vendors_raw = payload.get("vendors")
    if not isinstance(vendors_raw, dict):
        raise KeyError(f"{path}: missing 'vendors' object")

    pins: dict[str, VendorPin] = {}
    for name, entry in vendors_raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: vendor {name!r} entry must be an object")
        try:
            pin = VendorPin(
                name=name,
                kind=str(entry["kind"]),
                ref=str(entry["ref"]),
                endpoint_env=str(entry["endpoint_env"]),
                endpoint_default=str(entry["endpoint_default"]),
                raw={k: str(v) for k, v in entry.items() if isinstance(v, str)},
            )
        except KeyError as exc:  # pragma: no cover - guards malformed file
            raise KeyError(f"{path}: vendor {name!r} missing field {exc.args[0]!r}") from exc
        pins[name] = pin
    return pins


#: Module-level cache so importers don't pay disk I/O on every lookup.
VENDOR_VERSIONS: Final[dict[str, VendorPin]] = load_vendor_pins()
