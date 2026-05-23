"""MCP Apps capability negotiation (issue #238).

The Jan 2026 MCP Apps spec piggybacks on the standard MCP
``ClientCapabilities.experimental`` slot rather than introducing a
brand-new top-level capability — this is a backwards-compatible
extension path documented in the spec.  Servers declare support by
including an entry under their own ``ServerCapabilities.experimental``
namespace; clients reciprocate the same shape on the way in.

We use the dotted-namespaced key ``omniscience/apps`` to avoid
colliding with any future official ``apps`` capability.  When the
official spec stabilises we will mirror that key in addition to ours.

The functions in this module are deliberately tolerant: an unknown,
malformed, or absent capability declaration must always degrade to
"client does NOT support Apps" so the legacy text response is shipped.
**There is no path where a wire-format change can break a legacy
client.**
"""

from __future__ import annotations

from typing import Any, Final

#: Namespaced experimental capability key for MCP Apps.  Matches the
#: dotted-namespace convention used by other Omniscience extensions
#: (e.g. ``omniscience/replay`` in #243).  Pinned as ``Final`` so
#: misspellings at call sites are a typecheck error, not a silent miss.
APPS_CAPABILITY_KEY: Final[str] = "omniscience/apps"

#: Wire-format version of the card schemas defined in
#: :mod:`omniscience_server.mcp.apps.card_schemas`.  Bumped when the
#: schemas change in a way clients must be aware of.  Clients that
#: receive a higher version than they know are expected to fall back
#: to the legacy text payload (mirrored in every card-wrapped response
#: as ``content[0].text``).
APPS_SCHEMA_VERSION: Final[str] = "1.0"


def client_supports_apps(client_capabilities: Any) -> bool:
    """Return ``True`` iff the client advertised ``omniscience/apps``.

    Accepts either a ``ClientCapabilities`` pydantic model (the live
    type from ``mcp.types``) or a plain ``dict`` (what the MCP test
    client and our own integration test harness produce).  Anything
    else — including ``None`` — degrades to ``False``.

    The capability *value* is intentionally not interrogated here; a
    bare ``{"omniscience/apps": {}}`` declaration is sufficient to
    opt in.  Future spec revisions may add sub-keys (e.g. supported
    schema versions); this function will be extended additively.
    """
    if client_capabilities is None:
        return False

    experimental: Any
    if isinstance(client_capabilities, dict):
        experimental = client_capabilities.get("experimental")
    else:
        experimental = getattr(client_capabilities, "experimental", None)

    if not isinstance(experimental, dict):
        return False
    return APPS_CAPABILITY_KEY in experimental


def should_emit_card(ctx: Any) -> bool:
    """Decide whether to attach a card to the outgoing tool response.

    Looks up the live ``ClientCapabilities`` via the FastMCP context's
    server session and delegates to :func:`client_supports_apps`.
    Wrapped in defensive ``try``/``except``: the contract is "never
    block a legacy client", so any error in capability lookup must
    degrade to ``False``.

    ``ctx`` is typed as ``Any`` because :class:`mcp.server.fastmcp.Context`
    is generic in three type parameters that vary per tool registration
    and we do not want this lookup to leak that generic into every
    call site.
    """
    if ctx is None:
        return False
    try:
        session = getattr(ctx, "session", None)
        if session is None:
            return False
        client_params = getattr(session, "client_params", None)
        if client_params is None:
            return False
        return client_supports_apps(client_params.capabilities)
    except Exception:  # pragma: no cover - defensive only
        return False
