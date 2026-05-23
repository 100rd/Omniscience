"""Runbook entity models and canonical name helpers (issue #231).

Kept in a tiny module so the matcher (``linker.py``), the parser
(``parser.py``), and the server-side handlers
(``apps/server/.../runbook.py``) can all import the value objects without
pulling the network-touching connector class.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "RUNBOOK_NAME_PATTERN",
    "RUNBOOK_STEP_NAME_PATTERN",
    "RunbookDocument",
    "RunbookFrontMatter",
    "RunbookStep",
    "canonical_runbook_name",
    "canonical_runbook_step_name",
]


# ---------------------------------------------------------------------------
# Canonical name pattern.  Permissive enough to cover ``a/b/runbook.md``,
# ``team/payments/db-conn.md``, ``my_dir/file.markdown``, but forbids the
# fragment marker ``#`` in the base name slot — ``#`` is reserved for the
# step suffix on :func:`canonical_runbook_step_name`.
# ---------------------------------------------------------------------------
_NAME_SAFE_CHARS: Final[str] = r"[A-Za-z0-9._\-/]"
RUNBOOK_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^runbook://{_NAME_SAFE_CHARS}+/{_NAME_SAFE_CHARS}+$"
)
RUNBOOK_STEP_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^runbook://{_NAME_SAFE_CHARS}+/{_NAME_SAFE_CHARS}+#[A-Za-z0-9._\-]+$"
)

# Step ids are slugified heading text — `/` and `#` are forbidden so the
# canonical name parser can split on them unambiguously.
_STEP_ID_SAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._\-]+")
_SOURCE_UID_SAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._\-]+")


def _slugify_source_uid(source_uid: str) -> str:
    """Return a canonical-name-safe slug from a free-form source uid.

    The connector accepts arbitrary source labels (``"git://repo"``,
    ``"team-runbooks"``, ``"fs:/var/lib/runbooks"``).  We strip the URL
    scheme separator and replace anything outside ``[A-Za-z0-9._-]`` with
    a single ``-`` so the canonical name shape stays deterministic.
    """
    cleaned = _SOURCE_UID_SAFE.sub("-", source_uid).strip("-")
    if not cleaned:
        raise ValueError("source_uid must contain at least one safe character")
    return cleaned


def _normalise_rel_path(rel_path: str) -> str:
    """Return a POSIX-style relative path with no leading slashes or dots.

    Defends against ``./foo.md`` and ``/abs/path/foo.md`` arriving from
    different filesystems — the canonical name must be stable across
    callers regardless of how they expressed the path.
    """
    rel = rel_path.replace("\\", "/").lstrip("./")
    rel = rel.lstrip("/")
    if not rel:
        raise ValueError("rel_path must be non-empty")
    if "#" in rel:
        raise ValueError("rel_path must not contain '#' (reserved for step suffix)")
    return rel


def canonical_runbook_name(source_uid: str, rel_path: str) -> str:
    """Return the canonical entity name for a runbook document.

    >>> canonical_runbook_name("team-runbooks", "payments/db-conn.md")
    'runbook://team-runbooks/payments/db-conn.md'
    """
    slug = _slugify_source_uid(source_uid)
    rel = _normalise_rel_path(rel_path)
    return f"runbook://{slug}/{rel}"


def canonical_runbook_step_name(
    source_uid: str,
    rel_path: str,
    step_id: str,
) -> str:
    """Return the canonical name for an individual runbook step.

    >>> canonical_runbook_step_name(
    ...     "team-runbooks", "payments/db-conn.md", "Restart-the-pool"
    ... )
    'runbook://team-runbooks/payments/db-conn.md#restart-the-pool'
    """
    base = canonical_runbook_name(source_uid, rel_path)
    slug = _STEP_ID_SAFE.sub("-", step_id).strip("-").lower()
    if not slug:
        raise ValueError("step_id slugifies to empty string")
    return f"{base}#{slug}"


# ---------------------------------------------------------------------------
# Pydantic value objects.  These are pure data — no behaviour — so they
# round-trip through JSON cleanly for storage in DocumentRef.metadata.
# ---------------------------------------------------------------------------


class RunbookFrontMatter(BaseModel):
    """YAML front-matter block parsed off the head of a runbook file.

    Unknown keys are silently dropped by Pydantic to keep ``mypy --strict``
    happy with the typed surface; raw passthrough lives in
    :attr:`RunbookDocument.raw_front_matter`.
    """

    title: str | None = None
    alert_names: list[str] = Field(default_factory=list)
    alert_patterns: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    severity: str | None = None
    owners: list[str] = Field(default_factory=list)

    @field_validator("alert_names", "alert_patterns", "tags", "owners", mode="before")
    @classmethod
    def _coerce_to_string_list(cls, value: object) -> list[str]:
        """Front-matter is YAML — be defensive about scalar vs list.

        Hand-edited runbooks frequently provide a single string where a
        list is expected (``tags: service:payments``).  We accept either
        shape so authors do not get stuck on YAML quirks.
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(v) for v in value if v is not None]
        # Numeric/bool/etc. — coerce to string for forward-compat.
        return [str(value)]


class RunbookStep(BaseModel):
    """One actionable step extracted from a runbook (== one heading).

    The step's ``body`` carries the markdown beneath the heading verbatim
    so the responder UI can render code fences, lists, and links without
    a second pass through the parser.
    """

    step_id: str = Field(description="Slug derived from heading text.")
    heading: str = Field(description="Original heading text (CommonMark inline).")
    heading_level: int = Field(ge=1, le=6)
    body: str = Field(description="Verbatim markdown body beneath the heading.")
    code_blocks: list[str] = Field(
        default_factory=list,
        description="Fenced-code-block contents inside ``body`` (info string stripped).",
    )


class RunbookDocument(BaseModel):
    """Parsed runbook ready for indexing or matching.

    Constructed by :func:`omniscience_connectors.runbook.parse_runbook`; the
    suggest path reads this back out of the indexed entity metadata.
    """

    source_uid: str
    rel_path: str
    canonical_name: str
    front_matter: RunbookFrontMatter
    raw_front_matter: dict[str, object] = Field(default_factory=dict)
    title: str = Field(description="Human-readable title (front-matter or first H1).")
    body_markdown: str = Field(description="Full markdown body with front-matter stripped.")
    steps: list[RunbookStep] = Field(default_factory=list)
    parse_warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Non-fatal warnings encountered during parsing — e.g. malformed "
            "front-matter that was skipped, duplicate step slugs that were "
            "disambiguated, etc.  Surfaced in suggest_runbook output so "
            "authors can fix their runbooks."
        ),
    )
