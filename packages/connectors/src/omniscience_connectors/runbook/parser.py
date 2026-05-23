"""Markdown runbook parser (issue #231).

Self-contained: no new dependencies beyond the existing pydantic/httpx/etc.
that the connector package already pulls.  We deliberately avoid a full
CommonMark parser — runbooks live in `.md` files that are hand-authored,
not machine-generated, and the only structural pieces we need are:

* YAML front-matter at the top of the document (between ``---`` fences).
* ATX headings (``#`` … ``######``).
* Fenced code blocks (```````).

A full CommonMark library (e.g. ``markdown-it-py``) would add 200kB to the
wheel for features we never use.  This parser is documented as a "best
effort over real runbooks" — malformed inputs return a partially-populated
:class:`RunbookDocument` with warnings rather than raising, so a single bad
runbook does not stall the ingest pipeline.

YAML subset
-----------
We parse a deliberately small YAML subset:

* ``key: value`` scalars
* ``key:`` followed by ``- item`` block-style lists (one item per line)
* Inline ``key: [a, b, c]`` flow-style lists
* ``"`` and ``'`` quoted scalars
* ``#`` line comments

Block mappings, anchors, tags, multi-line scalars, and JSON-style flow
mappings are *not* supported.  Runbook authors who need richer YAML can
keep using the raw passthrough in :attr:`RunbookDocument.raw_front_matter`
but they will be ignored by the matcher.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError

from omniscience_connectors.runbook.models import (
    RunbookDocument,
    RunbookFrontMatter,
    RunbookStep,
    canonical_runbook_name,
)

__all__ = ["ParseError", "parse_runbook"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module constants — no magic numbers
# ---------------------------------------------------------------------------

# Front-matter fence — three or more dashes on a line by themselves.  Trailing
# whitespace allowed because hand-edited runbooks frequently include it.
_FENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^-{3,}\s*$")

# ATX heading: 1-6 ``#`` followed by space and inline text.  Closing ``#``
# tail (per CommonMark) is stripped.
_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)(?:\s+#+\s*)?$"
)

# Fenced-code-block start/end — three backticks or three tildes, optional info
# string.  Mixed fence chars (start ``` end ~~~) are *not* matched.
_CODE_FENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# Inline list literal: ``[a, b, "c d"]``.
_INLINE_LIST_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\[(?P<inner>.*)\]$")

# Slugifier — collapses any run of non-alphanum to a single dash.
_SLUG_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9]+")

# Largest accepted front-matter block (chars).  Defends against pathological
# input where the closing fence is missing — without this we would scan the
# entire document looking for a fence that never arrives.
_MAX_FRONTMATTER_CHARS: Final[int] = 32 * 1024


class ParseError(Exception):
    """Raised only for unrecoverable parser failures (e.g. empty input).

    Recoverable issues — malformed YAML, duplicate slugs, missing closing
    code fences — are captured as :attr:`RunbookDocument.parse_warnings`.
    """


# ---------------------------------------------------------------------------
# Front-matter extraction
# ---------------------------------------------------------------------------


def _extract_front_matter(text: str) -> tuple[str, str | None, list[str]]:
    """Split front-matter from the body.

    Returns ``(body, raw_fm_text or None, warnings)``.  Body is the text
    with the front-matter fences removed.  ``raw_fm_text`` is the inner
    YAML block (without the fences); ``None`` when no front-matter was
    detected.
    """
    warnings: list[str] = []
    lines = text.splitlines()
    if not lines:
        return text, None, warnings

    # Front-matter must start on the very first non-empty line.  Allow
    # a UTF-8 BOM and leading blank lines to be lenient with hand-edited
    # files saved by Windows editors.
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines):
        return text, None, warnings

    first = lines[idx].lstrip("﻿")
    if not _FENCE_PATTERN.match(first):
        return text, None, warnings

    # Walk forward until the closing fence.
    start = idx + 1
    end: int | None = None
    char_budget = 0
    for j in range(start, len(lines)):
        char_budget += len(lines[j]) + 1
        if char_budget > _MAX_FRONTMATTER_CHARS:
            warnings.append("front_matter_unterminated_within_budget")
            return text, None, warnings
        if _FENCE_PATTERN.match(lines[j]):
            end = j
            break

    if end is None:
        warnings.append("front_matter_missing_closing_fence")
        return text, None, warnings

    fm_text = "\n".join(lines[start:end])
    body_lines = lines[end + 1 :]
    return "\n".join(body_lines), fm_text, warnings


# ---------------------------------------------------------------------------
# Tiny YAML subset parser
# ---------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """Remove trailing ``#`` comments outside quoted strings."""
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i].rstrip()
    return line


def _parse_scalar(raw: str) -> str:
    """Parse a YAML scalar, stripping matched quotes."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in {'"', "'"}:
        return s[1:-1]
    return s


def _parse_inline_list(raw: str) -> list[str]:
    """Parse a flow-style YAML list literal — best effort split on commas.

    Respects single/double quoted strings; does not handle nested lists
    or mappings (which the matcher would ignore anyway).
    """
    m = _INLINE_LIST_PATTERN.match(raw.strip())
    if m is None:
        return []
    inner = m.group("inner")
    items: list[str] = []
    buf: list[str] = []
    in_single = in_double = False
    for ch in inner:
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
        elif ch == "," and not in_single and not in_double:
            items.append(_parse_scalar("".join(buf)))
            buf = []
        else:
            buf.append(ch)
    if buf:
        items.append(_parse_scalar("".join(buf)))
    return [item for item in items if item != ""]


def _parse_yaml_subset(text: str) -> tuple[dict[str, object], list[str]]:
    """Parse the subset of YAML that runbook front-matter is allowed to use.

    Returns ``(mapping, warnings)``.  Unknown structural constructs are
    skipped with a warning rather than raising — a malformed line should
    not poison the rest of the front-matter.
    """
    warnings: list[str] = []
    result: dict[str, object] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for raw_line in text.splitlines():
        stripped = _strip_comment(raw_line)
        if stripped.strip() == "":
            continue

        # Block-style list continuation: ``  - item``.
        if stripped.lstrip().startswith("- "):
            if current_key is None or current_list is None:
                warnings.append("list_item_without_parent_key")
                continue
            item = _parse_scalar(stripped.lstrip()[2:])
            current_list.append(item)
            continue

        # ``key: value`` or ``key:``.
        if ":" not in stripped:
            warnings.append(f"unparseable_line:{stripped[:64]}")
            continue
        key, _, rhs = stripped.partition(":")
        key = key.strip()
        rhs = rhs.strip()
        if not key:
            warnings.append("empty_key")
            continue

        if rhs == "":
            # Open a list.  We don't know yet whether the next lines are
            # list items or a nested mapping; we initialise a list and
            # downgrade to scalar later if no items arrive.
            current_key = key
            current_list = []
            result[key] = current_list
        elif rhs.startswith("["):
            result[key] = _parse_inline_list(rhs)
            current_key = key
            current_list = None
        else:
            result[key] = _parse_scalar(rhs)
            current_key = key
            current_list = None

    return result, warnings


# ---------------------------------------------------------------------------
# Heading + code-block extraction
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Slugify heading text for use as a step id."""
    slug = _SLUG_PATTERN.sub("-", text.strip().lower()).strip("-")
    return slug or "step"


@dataclass(slots=True)
class _StepBuilder:
    """Mutable accumulator for an in-progress step, typed for mypy --strict."""

    step_id: str
    heading: str
    heading_level: int


def _split_steps(body: str) -> tuple[list[RunbookStep], list[str]]:
    """Split the markdown body into one step per ATX heading.

    The first heading begins step 1.  Content before the first heading is
    discarded (the runbook entity itself carries the full body in
    :attr:`RunbookDocument.body_markdown`, so no information is lost).

    Returns ``(steps, warnings)``.
    """
    warnings: list[str] = []
    steps: list[RunbookStep] = []
    lines = body.splitlines()

    current: _StepBuilder | None = None
    buf: list[str] = []
    in_fence = False
    fence_char: str | None = None
    code_buf: list[str] = []
    current_code_blocks: list[str] = []

    def _flush() -> None:
        if current is None:
            return
        step_text = "\n".join(buf).rstrip()
        steps.append(
            RunbookStep(
                step_id=current.step_id,
                heading=current.heading,
                heading_level=current.heading_level,
                body=step_text,
                code_blocks=list(current_code_blocks),
            )
        )

    seen_slugs: dict[str, int] = {}

    for raw in lines:
        # Track fenced code blocks so headings inside them don't open a
        # new step (markdown convention; CommonMark §4.5).
        fence_match = _CODE_FENCE_PATTERN.match(raw)
        if fence_match is not None:
            ch = fence_match.group("fence")[0]
            if not in_fence:
                in_fence = True
                fence_char = ch
                code_buf = []
                buf.append(raw)
                continue
            if fence_char == ch:
                in_fence = False
                fence_char = None
                if current is not None:
                    current_code_blocks.append("\n".join(code_buf))
                code_buf = []
                buf.append(raw)
                continue
            # Mismatched fence char — keep collecting as content.

        if in_fence:
            code_buf.append(raw)
            buf.append(raw)
            continue

        heading_match = _HEADING_PATTERN.match(raw)
        if heading_match is not None:
            # Close out the previous step.
            _flush()

            heading_text = heading_match.group("text").strip()
            level = len(heading_match.group("hashes"))
            slug = _slugify(heading_text)
            if slug in seen_slugs:
                seen_slugs[slug] += 1
                disambiguated = f"{slug}-{seen_slugs[slug]}"
                warnings.append(f"duplicate_step_slug:{slug}->{disambiguated}")
                slug = disambiguated
            else:
                seen_slugs[slug] = 1

            current = _StepBuilder(
                step_id=slug,
                heading=heading_text,
                heading_level=level,
            )
            current_code_blocks = []
            buf = []
            continue

        if current is not None:
            buf.append(raw)

    if in_fence:
        warnings.append("unterminated_code_fence")
    _flush()
    return steps, warnings


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_runbook(
    *,
    source_uid: str,
    rel_path: str,
    text: str,
) -> RunbookDocument:
    """Parse a markdown runbook into a :class:`RunbookDocument`.

    Never raises on malformed input — all recoverable issues are accumulated
    on :attr:`RunbookDocument.parse_warnings`.  The only raised exception is
    :class:`ParseError` when the input is empty / non-textual, which the
    connector treats as a hard skip (one log line, no entity emitted).
    """
    if text == "":
        raise ParseError("empty runbook text")

    warnings: list[str] = []
    body, fm_text, fm_warnings = _extract_front_matter(text)
    warnings.extend(fm_warnings)

    raw_fm: dict[str, object] = {}
    fm: RunbookFrontMatter
    if fm_text is not None:
        raw_fm, yaml_warnings = _parse_yaml_subset(fm_text)
        warnings.extend(yaml_warnings)
        try:
            fm = RunbookFrontMatter.model_validate(raw_fm)
        except ValidationError as exc:
            warnings.append(f"front_matter_validation_error:{exc.error_count()}")
            fm = RunbookFrontMatter()
    else:
        fm = RunbookFrontMatter()

    steps, step_warnings = _split_steps(body)
    warnings.extend(step_warnings)

    # Title precedence: front-matter > first H1 in body > rel_path basename.
    title: str
    if fm.title:
        title = fm.title
    elif steps and steps[0].heading_level == 1:
        title = steps[0].heading
    else:
        title = rel_path.rsplit("/", 1)[-1]

    return RunbookDocument(
        source_uid=source_uid,
        rel_path=rel_path,
        canonical_name=canonical_runbook_name(source_uid, rel_path),
        front_matter=fm,
        raw_front_matter=raw_fm,
        title=title,
        body_markdown=body,
        steps=steps,
        parse_warnings=warnings,
    )
