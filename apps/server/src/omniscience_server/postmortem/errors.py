"""Error codes for the post-mortem generator surface (issue #232).

All codes follow the convention already established by
``resolve_incident`` and ``incident_timeline`` — a stable string
identifier that REST/MCP error envelopes can pattern-match on.
"""

from __future__ import annotations

from typing import Final

#: Caller supplied a template id we don't know about.
INVALID_TEMPLATE_CODE: Final[str] = "invalid_template"

#: Caller supplied a render format other than 'markdown' / 'html'.
INVALID_FORMAT_CODE: Final[str] = "invalid_format"

#: Caller supplied an empty / oversized / non-string incident id.
INVALID_INCIDENT_ID_CODE: Final[str] = "invalid_incident_id"

#: The alert id referenced by the incident id was not found in the
#: caller's workspace (or does not exist at all — no existence leak).
INCIDENT_NOT_FOUND_CODE: Final[str] = "incident_not_found"


class PostmortemError(ValueError):
    """Structured error raised by the post-mortem generator.

    Subclass of :class:`ValueError` so the existing REST/MCP error
    machinery (which pattern-matches on ``str(exc)``) treats it
    identically to the existing handlers.  The ``code`` attribute is
    exposed for handlers that want the structured field directly.
    """

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}:{message}")
        self.code = code
        self.message = message


__all__ = [
    "INCIDENT_NOT_FOUND_CODE",
    "INVALID_FORMAT_CODE",
    "INVALID_INCIDENT_ID_CODE",
    "INVALID_TEMPLATE_CODE",
    "PostmortemError",
]
