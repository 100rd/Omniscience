"""Authority/active-content/credential/PII conformance scanner (SPEC-MCTX
REQ-MCTX-5/6, probes P-MCTX-3+5).

This is defense-in-depth on top of PW0 field admission (``pii.py``): even a
correctly PW0-admitted field must still never carry a forbidden authority-truth
field name, active content, a credential-shaped value, or a seeded-PII value shape.
``producer.py`` calls ``conformance_scan`` on every assembled bundle before it is
ever returned; any violation forces ``fallback_required=authority_field_detected``
rather than surfacing the bundle. Tests reuse this exact function as the
"independent conformance runner" that replays a seeded attack corpus (AC-SP81-2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omniscience_core.management.taxonomy import (
    ACTIVE_CONTENT_PATTERNS,
    CREDENTIAL_KEY_TOKENS,
    FORBIDDEN_AUTHORITY_FIELD_TOKENS,
    SEEDED_PII_PATTERNS,
)


def _walk(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.append((path, value))
            out.extend(_walk(value, path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            out.extend(_walk(value, f"{prefix}[{index}]"))
    return out


def scan_for_authority_fields(payload: Mapping[str, Any]) -> list[str]:
    """REQ-MCTX-5: forbidden management-truth field names anywhere in the payload."""
    errors: list[str] = []
    for key, _value in _walk(payload):
        leaf_key = key.rsplit(".", 1)[-1].split("[")[0].lower()
        if any(token in leaf_key for token in FORBIDDEN_AUTHORITY_FIELD_TOKENS):
            errors.append(f"authority field detected: '{key}'")
    return errors


def scan_for_credentials(payload: Mapping[str, Any]) -> list[str]:
    """REQ-MCTX-6: credential-shaped field names anywhere in the payload."""
    errors: list[str] = []
    for key, _value in _walk(payload):
        leaf_key = key.rsplit(".", 1)[-1].split("[")[0].lower()
        if any(token in leaf_key for token in CREDENTIAL_KEY_TOKENS):
            errors.append(f"credential-shaped field detected: '{key}'")
    return errors


def scan_text_for_active_content(text: str) -> list[str]:
    return [
        f"active content detected: {pattern.pattern}"
        for pattern in ACTIVE_CONTENT_PATTERNS
        if pattern.search(text)
    ]


def scan_text_for_seeded_pii(text: str) -> list[str]:
    return [
        f"seeded PII shape detected: {pattern.pattern}"
        for pattern in SEEDED_PII_PATTERNS
        if pattern.search(text)
    ]


def scan_text_for_credential_tokens(text: str) -> list[str]:
    """REQ-MCTX-6: credential-shaped tokens inside free text (not just field names) --
    e.g. ``"api_key: sk-..."`` embedded in a candidate citation's body."""
    lowered = text.lower()
    return [
        f"credential token detected: '{token}'"
        for token in CREDENTIAL_KEY_TOKENS
        if token in lowered
    ]


def scan_text(text: str) -> list[str]:
    """Combined active-content + credential + PII scan for one candidate text field,
    used before a citation is ever assembled (``producer.py``)."""
    return (
        scan_text_for_active_content(text)
        + scan_text_for_credential_tokens(text)
        + scan_text_for_seeded_pii(text)
    )


def conformance_scan(bundle_dict: Mapping[str, Any]) -> list[str]:
    """Full defense-in-depth scan over an assembled bundle payload -- authority
    fields, credentials, active content, and seeded PII in every string value."""
    errors = scan_for_authority_fields(bundle_dict) + scan_for_credentials(bundle_dict)
    for _key, value in _walk(bundle_dict):
        if isinstance(value, str):
            errors.extend(scan_text(value))
    return errors
