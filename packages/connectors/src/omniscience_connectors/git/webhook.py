"""Git webhook handler for GitHub and GitLab push events."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from omniscience_connectors.base import DocumentRef, WebhookHandler, WebhookPayload

__all__ = ["GitWebhookHandler"]

logger = logging.getLogger(__name__)

_GITHUB_SIG_HEADER = "x-hub-signature-256"
_GITLAB_TOKEN_HEADER = "x-gitlab-token"  # noqa: S105 — HTTP header name, not a password


class GitWebhookHandler(WebhookHandler):
    """Webhook handler supporting GitHub and GitLab push events.

    Signature verification:
    - **GitHub**: HMAC-SHA256 in ``X-Hub-Signature-256: sha256=<hex>``
    - **GitLab**: shared token in ``X-Gitlab-Token: <token>``
    """

    async def verify_signature(
        self,
        payload: bytes,
        headers: dict[str, str],
        secret: str,
    ) -> bool:
        """Return True if the webhook request is authentic."""
        lower_headers = {k.lower(): v for k, v in headers.items()}

        # GitHub: HMAC-SHA256
        github_sig = lower_headers.get(_GITHUB_SIG_HEADER, "")
        if github_sig:
            return _verify_github_signature(payload, github_sig, secret)

        # GitLab: plain token comparison
        gitlab_token = lower_headers.get(_GITLAB_TOKEN_HEADER, "")
        if gitlab_token:
            return hmac.compare_digest(gitlab_token, secret)

        # No recognised signature header
        return False

    async def parse_payload(
        self,
        payload: bytes,
        headers: dict[str, str],
    ) -> WebhookPayload:
        """Parse a GitHub or GitLab push event into a WebhookPayload."""
        try:
            data: dict[str, object] = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Webhook payload is not valid JSON: {exc}") from exc

        lower_headers = {k.lower(): v for k, v in headers.items()}
        source_name = _detect_source(lower_headers, data)
        affected_refs = _extract_affected_refs(data)

        return WebhookPayload(
            source_name=source_name,
            affected_refs=affected_refs,
            raw_headers=lower_headers,
        )


def _verify_github_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    """Verify a GitHub HMAC-SHA256 signature using constant-time comparison."""
    if not sig_header.startswith("sha256="):
        return False
    provided_hex = sig_header[len("sha256=") :]
    expected_hex = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hex, provided_hex)


def _detect_source(lower_headers: dict[str, str], data: dict[str, object]) -> str:
    """Infer a human-readable source name from headers or payload."""
    if _GITHUB_SIG_HEADER in lower_headers:
        repo = data.get("repository")
        if isinstance(repo, dict):
            full_name = repo.get("full_name")
            if isinstance(full_name, str):
                return full_name
        return "github"

    if _GITLAB_TOKEN_HEADER in lower_headers:
        repo = data.get("project") or data.get("repository")
        if isinstance(repo, dict):
            name = repo.get("path_with_namespace") or repo.get("name")
            if isinstance(name, str):
                return name
        return "gitlab"

    return "git"


def _extract_affected_refs(data: dict[str, object]) -> list[DocumentRef]:
    """Extract added/modified/removed file paths from a push event payload."""
    refs: list[DocumentRef] = []
    commits = data.get("commits")
    if not isinstance(commits, list):
        return refs

    seen: set[str] = set()
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        sha = commit.get("id") or commit.get("sha") or ""
        for key in ("added", "modified", "removed"):
            files = commit.get(key, [])
            if not isinstance(files, list):
                continue
            for file_path in files:
                if not isinstance(file_path, str) or file_path in seen:
                    continue
                seen.add(file_path)
                refs.append(
                    DocumentRef(
                        external_id=f"{sha}:{file_path}" if sha else file_path,
                        uri=file_path,
                        metadata={"action": key, "commit": sha},
                    )
                )
    return refs
