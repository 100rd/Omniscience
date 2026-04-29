"""GitHub webhook handler for the GitHub PR/MR connector.

Subscribed events
-----------------
* ``pull_request`` — opened/edited/closed/synchronize/reopened
* ``pull_request_review`` — submitted/edited/dismissed
* ``pull_request_review_comment`` — created/edited/deleted
* ``push`` — only used to keep the PR head SHA fresh; repo file content stays
  on :class:`GitConnector`'s webhook.

Signature verification is HMAC-SHA256 over the raw body, with the digest
delivered in ``X-Hub-Signature-256: sha256=<hex>``.  Constant-time comparison.

ACL invariant
-------------
Workspace identity comes from ``Source.tenant_id`` resolved by the FastAPI
receiver on the ``/webhooks/{source_name}`` path — *not* from any field in
the webhook payload (``installation.id``, ``repository.owner.login``, etc.).
This handler intentionally does **not** read the installation ID for ACL
purposes; it only reads payload fields to synthesise the affected PR URL.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from omniscience_connectors.base import DocumentRef, WebhookHandler, WebhookPayload

__all__ = ["GithubPrWebhookHandler"]

logger = logging.getLogger(__name__)

# GitHub signature header (lower-cased for case-insensitive lookup).
_GITHUB_SIG_HEADER = "x-hub-signature-256"

# Event type header.
_GITHUB_EVENT_HEADER = "x-github-event"

# Events we react to.  Anything else is acknowledged but produces no refs.
_HANDLED_EVENTS = frozenset(
    {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "push",
    }
)


def _verify_github_signature(payload: bytes, secret: str, signature_header: str) -> bool:
    """Verify a GitHub HMAC-SHA256 signature using constant-time comparison.

    Mirrors :func:`apps.server.omniscience_server.rest.webhooks._verify_github_signature`
    but is duplicated here only because importing from ``apps/server`` would
    create a layering violation (connectors must not depend on the FastAPI
    server).  The two implementations are kept byte-equivalent.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    received = signature_header[len("sha256=") :]
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


class GithubPrWebhookHandler(WebhookHandler):
    """Webhook handler for GitHub pull-request-related events."""

    async def verify_signature(
        self,
        payload: bytes,
        headers: dict[str, str],
        secret: str,
    ) -> bool:
        """Return ``True`` if the request is authentic.

        Constant-time comparison; never raises on malformed input.
        """
        lower_headers = {k.lower(): v for k, v in headers.items()}
        sig = lower_headers.get(_GITHUB_SIG_HEADER, "")
        if not sig or not secret:
            return False
        return _verify_github_signature(payload, secret, sig)

    async def parse_payload(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookPayload:
        """Translate a GitHub event into a :class:`WebhookPayload`.

        For PR-touching events, returns a single :class:`DocumentRef` with
        the canonical PR URL so the ingestion pipeline re-fetches the
        full PR (including any updated reviews/comments).
        """
        try:
            data: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"GitHub webhook payload is not valid JSON: {exc}") from exc

        lower_headers = {k.lower(): v for k, v in headers.items()}
        event = lower_headers.get(_GITHUB_EVENT_HEADER, "")
        repo_obj = data.get("repository") or {}
        owner = ((repo_obj.get("owner") or {}).get("login") or "").lower()
        repo = repo_obj.get("name") or ""
        source_name = f"{owner}/{repo}" if owner and repo else "github_pr"

        affected_refs: list[DocumentRef] = []
        if event in _HANDLED_EVENTS:
            affected_refs = _extract_pr_refs(event, data, owner, repo)

        return WebhookPayload(
            source_name=source_name,
            affected_refs=affected_refs,
            raw_headers=lower_headers,
        )


def _extract_pr_refs(
    event: str,
    data: dict[str, Any],
    owner: str,
    repo: str,
) -> list[DocumentRef]:
    """Extract a single PR ``DocumentRef`` per affected pull request.

    For ``push`` events we walk the commits and emit refs only when the
    payload references PR-bearing branches (we cannot know the PR number
    from a push payload alone, so push triggers no PR refs — repo content
    stays on GitConnector's webhook).
    """
    if not owner or not repo:
        return []

    if event == "pull_request":
        pr = data.get("pull_request") or {}
        number = pr.get("number")
        if isinstance(number, int):
            return [_pr_ref(owner, repo, number, action=str(data.get("action", "")))]
        return []

    if event == "pull_request_review":
        pr = data.get("pull_request") or {}
        number = pr.get("number")
        if isinstance(number, int):
            return [
                _pr_ref(
                    owner,
                    repo,
                    number,
                    action=str(data.get("action", "")),
                    review_id=_safe_int((data.get("review") or {}).get("id")),
                )
            ]
        return []

    if event == "pull_request_review_comment":
        pr = data.get("pull_request") or {}
        number = pr.get("number")
        if isinstance(number, int):
            return [
                _pr_ref(
                    owner,
                    repo,
                    number,
                    action=str(data.get("action", "")),
                    comment_id=_safe_int((data.get("comment") or {}).get("id")),
                )
            ]
        return []

    # `push` — head SHA refresh hook; we emit no PR refs (PR linkage comes
    # via pull_request events).  Returning empty keeps the no-op invariant.
    return []


def _pr_ref(
    owner: str,
    repo: str,
    number: int,
    *,
    action: str = "",
    review_id: int | None = None,
    comment_id: int | None = None,
) -> DocumentRef:
    """Build the canonical PR :class:`DocumentRef` for re-ingestion."""
    uri = f"https://github.com/{owner}/{repo}/pull/{number}"
    metadata: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "number": number,
        "action": action,
    }
    if review_id is not None:
        metadata["review_id"] = review_id
    if comment_id is not None:
        metadata["comment_id"] = comment_id
    return DocumentRef(external_id=uri, uri=uri, metadata=metadata)


def _safe_int(value: Any) -> int | None:
    """Coerce *value* to ``int`` if possible; return ``None`` on failure."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
