"""Tests for the GitHub PR webhook handler (Issue #150).

Coverage:
- HMAC-SHA256 signature verification — positive + negative + missing header
- Constant-time comparison via hmac.compare_digest (boundary cases)
- Event parsing — pull_request, pull_request_review, pull_request_review_comment
- ACL invariant — workspace identity is never inferred from payload fields
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from omniscience_connectors.github_pr.webhook import (
    GithubPrWebhookHandler,
    _verify_github_signature,
)

OWNER = "100rd"
REPO = "Omniscience"
SECRET = "supersecret"


def _sign(payload: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _pr_event(action: str = "opened", number: int = 42) -> bytes:
    return json.dumps(
        {
            "action": action,
            "pull_request": {
                "number": number,
                "html_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}",
            },
            "repository": {"name": REPO, "owner": {"login": OWNER}},
            # Tenant-writable upstream content — MUST NOT be used for ACL:
            "installation": {"id": 12345},
        }
    ).encode()


def _review_event(review_id: int = 99, number: int = 42) -> bytes:
    return json.dumps(
        {
            "action": "submitted",
            "review": {"id": review_id, "state": "APPROVED"},
            "pull_request": {"number": number},
            "repository": {"name": REPO, "owner": {"login": OWNER}},
        }
    ).encode()


def _comment_event(comment_id: int = 7, number: int = 42) -> bytes:
    return json.dumps(
        {
            "action": "created",
            "comment": {"id": comment_id, "body": "nit"},
            "pull_request": {"number": number},
            "repository": {"name": REPO, "owner": {"login": OWNER}},
        }
    ).encode()


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_signature_success() -> None:
    handler = GithubPrWebhookHandler()
    payload = _pr_event()
    headers = {"X-Hub-Signature-256": _sign(payload), "X-GitHub-Event": "pull_request"}
    assert await handler.verify_signature(payload, headers, SECRET) is True


@pytest.mark.asyncio
async def test_verify_signature_wrong_secret() -> None:
    handler = GithubPrWebhookHandler()
    payload = _pr_event()
    headers = {"X-Hub-Signature-256": _sign(payload, "wrong"), "X-GitHub-Event": "pull_request"}
    assert await handler.verify_signature(payload, headers, SECRET) is False


@pytest.mark.asyncio
async def test_verify_signature_missing_header() -> None:
    handler = GithubPrWebhookHandler()
    assert await handler.verify_signature(_pr_event(), {}, SECRET) is False


@pytest.mark.asyncio
async def test_verify_signature_empty_secret() -> None:
    """Empty secret cannot authenticate anything — return False, never True."""
    handler = GithubPrWebhookHandler()
    payload = _pr_event()
    headers = {"X-Hub-Signature-256": _sign(payload, "")}
    assert await handler.verify_signature(payload, headers, "") is False


@pytest.mark.asyncio
async def test_verify_signature_malformed_header() -> None:
    handler = GithubPrWebhookHandler()
    headers = {"X-Hub-Signature-256": "md5=abcdef"}
    assert await handler.verify_signature(_pr_event(), headers, SECRET) is False


def test_verify_helper_uses_constant_time_compare() -> None:
    """Boundary: helper must reject a flipped-bit signature even when same length."""
    payload = b"hello"
    correct = _sign(payload)
    flipped = "sha256=" + ("0" * 64)
    assert _verify_github_signature(payload, SECRET, correct) is True
    assert _verify_github_signature(payload, SECRET, flipped) is False
    # Empty signature
    assert _verify_github_signature(payload, SECRET, "") is False


# ---------------------------------------------------------------------------
# parse_payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_pull_request_event_emits_canonical_pr_ref() -> None:
    handler = GithubPrWebhookHandler()
    headers = {"X-GitHub-Event": "pull_request"}
    result = await handler.parse_payload(_pr_event(action="opened"), headers)

    assert result.source_name == f"{OWNER.lower()}/{REPO}"
    assert len(result.affected_refs) == 1
    ref = result.affected_refs[0]
    assert ref.uri == f"https://github.com/{OWNER.lower()}/{REPO}/pull/42"
    assert ref.metadata["number"] == 42
    assert ref.metadata["action"] == "opened"


@pytest.mark.asyncio
async def test_parse_review_event_carries_review_id() -> None:
    handler = GithubPrWebhookHandler()
    headers = {"X-GitHub-Event": "pull_request_review"}
    result = await handler.parse_payload(_review_event(99), headers)

    assert len(result.affected_refs) == 1
    ref = result.affected_refs[0]
    assert ref.metadata["review_id"] == 99
    assert ref.metadata["number"] == 42


@pytest.mark.asyncio
async def test_parse_review_comment_event_carries_comment_id() -> None:
    handler = GithubPrWebhookHandler()
    headers = {"X-GitHub-Event": "pull_request_review_comment"}
    result = await handler.parse_payload(_comment_event(7), headers)

    assert len(result.affected_refs) == 1
    assert result.affected_refs[0].metadata["comment_id"] == 7


@pytest.mark.asyncio
async def test_parse_push_event_emits_no_pr_refs() -> None:
    """Push events update file content (GitConnector's domain) — no PR refs."""
    handler = GithubPrWebhookHandler()
    payload = json.dumps(
        {"ref": "refs/heads/main", "repository": {"name": REPO, "owner": {"login": OWNER}}}
    ).encode()
    headers = {"X-GitHub-Event": "push"}
    result = await handler.parse_payload(payload, headers)
    assert result.affected_refs == []


@pytest.mark.asyncio
async def test_parse_invalid_json_raises() -> None:
    handler = GithubPrWebhookHandler()
    with pytest.raises(ValueError, match="not valid JSON"):
        await handler.parse_payload(b"not-json", {"X-GitHub-Event": "pull_request"})


@pytest.mark.asyncio
async def test_parse_unknown_event_returns_no_refs() -> None:
    """Unsubscribed events are acknowledged but produce no refs."""
    handler = GithubPrWebhookHandler()
    headers = {"X-GitHub-Event": "issue_comment"}
    payload = json.dumps({"repository": {"name": REPO, "owner": {"login": OWNER}}}).encode()
    result = await handler.parse_payload(payload, headers)
    assert result.affected_refs == []


# ---------------------------------------------------------------------------
# ACL invariant — workspace identity is NEVER inferred from payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acl_invariant_handler_does_not_expose_installation_id() -> None:
    """The handler MUST NOT surface installation.id or repository.owner as
    workspace identity. The webhook payload's source_name and affected_refs
    are inputs to the FastAPI receiver; tenant resolution happens earlier
    via the URL path-encoded source_name → Source.tenant_id lookup.
    """
    handler = GithubPrWebhookHandler()
    payload = _pr_event()  # carries `installation.id = 12345`
    headers = {"X-GitHub-Event": "pull_request"}

    # Verify with the correct secret first.
    assert await handler.verify_signature(payload, {"X-Hub-Signature-256": _sign(payload)}, SECRET)

    result = await handler.parse_payload(payload, headers)
    serialised = result.model_dump_json()
    assert "12345" not in serialised, (
        "Installation ID leaked from payload — workspace identity must "
        "come from Source.tenant_id, never from webhook payload fields."
    )
    # The source_name is repository-scoped (used as a label), not for ACL.
    assert "installation" not in result.model_dump()


@pytest.mark.asyncio
async def test_cross_workspace_isolation_same_repo_different_secrets() -> None:
    """Two workspaces (A and B) configure the same upstream repo with
    different webhook secrets.  A request signed for A must NOT verify for
    B's handler instance, and vice versa.

    This is the ACL-isolation contract: even if the GitHub payload itself
    is identical (same upstream PR), the per-source secret prevents
    cross-tenant replay.
    """
    handler = GithubPrWebhookHandler()
    payload = _pr_event()
    sig_a = _sign(payload, "secret-a")
    sig_b = _sign(payload, "secret-b")

    # Workspace A's secret verifies workspace A's signature only.
    assert await handler.verify_signature(payload, {"X-Hub-Signature-256": sig_a}, "secret-a")
    assert not await handler.verify_signature(payload, {"X-Hub-Signature-256": sig_a}, "secret-b")
    # And vice versa.
    assert await handler.verify_signature(payload, {"X-Hub-Signature-256": sig_b}, "secret-b")
    assert not await handler.verify_signature(payload, {"X-Hub-Signature-256": sig_b}, "secret-a")
