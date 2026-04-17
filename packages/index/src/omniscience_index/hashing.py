"""Content normalization and hashing utilities for change-detection dedup.

Normalization rules (from docs/schema.md):
- Strip BOM (U+FEFF) from the start of the text
- Trim trailing whitespace per line
- Collapse consecutive blank lines to a single blank line

These rules ensure cosmetic-only edits (editor trailing-space cleanup, extra
blank lines) don't trigger unnecessary re-indexing.
"""

from __future__ import annotations

import hashlib


def normalize_content(text: str) -> str:
    """Return *text* after applying cosmetic normalization.

    Steps applied in order:
    1. Strip a leading BOM (U+FEFF) if present.
    2. Strip trailing whitespace from every line.
    3. Collapse runs of more than one consecutive blank line to exactly one.
    """
    text = text.lstrip("\ufeff")
    lines = [line.rstrip() for line in text.splitlines()]

    normalized: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
        else:
            if blank_run > 0:
                normalized.append("")
            blank_run = 0
            normalized.append(line)
    return "\n".join(normalized)


def compute_content_hash(text: str) -> str:
    """Return the SHA-256 hex digest of *text* after normalization."""
    normalized = normalize_content(text)
    return hashlib.sha256(normalized.encode()).hexdigest()


__all__ = ["compute_content_hash", "normalize_content"]
