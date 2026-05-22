"""Tests for the GitHub PR/MR connector (Issue #150).

Coverage:
- GithubPrConfig validation
- Canonical-name helpers (PR URL, commit, review, comment, user)
- _validate_repos / _build_auth_headers
- GithubPrConnector.validate — success, 401, 404, missing repos, missing token
- GithubPrConnector.discover — pagination, max_age cutoff, state filter, all-closed
- GithubPrConnector.fetch — Markdown rendering, no diff markers, edges,
  metadata enrichment (commit_shas, file_paths, review_ids, comment_ids)
- Rate limit handling — sleeps and retries when X-RateLimit-Remaining=0
- Registry integration — github_pr is registered
- Slack-mention canonicalisation regression — PR URL matches the regex used
  by mention extractors verbatim
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from omniscience_connectors import get_connector
from omniscience_connectors.github_pr.connector import (
    CANONICAL_PR_URL_REGEX,
    GithubPrConfig,
    GithubPrConnector,
    _build_auth_headers,
    _validate_repos,
    canonical_commit_name,
    canonical_pr_url,
    canonical_review_comment_name,
    canonical_review_name,
    canonical_user_name,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

API_BASE = "https://api.github.com"
OWNER = "100rd"
REPO = "Omniscience"
TOKEN = "ghp_fakeFAKEfake"


def _recent_ts(*, days_ago: int = 1) -> str:
    """Return an ISO-8601 Z timestamp `days_ago` days before now (UTC).

    Default fixtures must stay within `max_age_days` cutoffs (90 by default,
    30 in the cutoff test) regardless of when the suite runs.
    """
    return (datetime.now(tz=UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr_payload(number: int, *, updated_at: str | None = None) -> dict[str, Any]:
    if updated_at is None:
        updated_at = _recent_ts(days_ago=1)
    return {
        "number": number,
        "state": "open",
        "title": f"Add feature {number}",
        "body": f"Body of PR {number}",
        "user": {"login": "alice"},
        "html_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}",
        "merged_at": None,
        "closed_at": None,
        "created_at": _recent_ts(days_ago=2),
        "updated_at": updated_at,
    }


def _commit_payload(sha: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {
            "message": f"chore: tweak {sha[:7]}",
            "author": {"name": "Alice"},
        },
    }


def _review_payload(review_id: int, login: str = "bob") -> dict[str, Any]:
    return {
        "id": review_id,
        "user": {"login": login},
        "state": "APPROVED",
        "body": f"LGTM ({review_id})",
    }


def _comment_payload(comment_id: int, login: str = "carol") -> dict[str, Any]:
    return {
        "id": comment_id,
        "user": {"login": login},
        "path": "src/foo.py",
        "body": f"nit: tweak {comment_id}",
    }


def _file_payload(path: str) -> dict[str, Any]:
    return {"filename": path, "additions": 1, "deletions": 0}


# ---------------------------------------------------------------------------
# Config + helpers
# ---------------------------------------------------------------------------


def test_config_defaults() -> None:
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"])
    assert cfg.include_closed is True
    assert cfg.max_age_days == 90
    assert cfg.include_review_comments is True
    assert cfg.api_base_url == API_BASE


def test_config_connector_type() -> None:
    assert GithubPrConnector.connector_type == "github_pr"
    assert GithubPrConnector.config_schema is GithubPrConfig


def test_validate_repos_accepts_owner_repo() -> None:
    parsed = _validate_repos(["acme/widget", "100rd/Omniscience"])
    assert parsed == [("acme", "widget"), ("100rd", "Omniscience")]


@pytest.mark.parametrize(
    "spec",
    ["bad", "no/slash/here", "", "owner/", "/repo", "white space/repo"],
)
def test_validate_repos_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError, match="Invalid repository spec"):
        _validate_repos([spec])


def test_build_auth_headers_uses_bearer() -> None:
    headers = _build_auth_headers({"github_token": TOKEN})
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == "application/vnd.github+json"
    assert "X-GitHub-Api-Version" in headers


def test_build_auth_headers_missing_token_raises() -> None:
    with pytest.raises(ValueError, match="github_token"):
        _build_auth_headers({})


def test_canonical_pr_url_lowercases_owner() -> None:
    assert canonical_pr_url("ACME", "Repo", 42) == "https://github.com/acme/Repo/pull/42"


def test_canonical_helpers() -> None:
    assert canonical_commit_name("ABCDEF1234") == "abcdef1234"
    assert canonical_review_name("Acme", "R", 7, 99) == "github://review/acme/R/7/99"
    assert canonical_review_comment_name("Acme", "R", 7, 12) == "github://comment/acme/R/7/12"
    assert canonical_user_name("alice") == "github://user/alice"


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_validate_success() -> None:
    respx.get(f"{API_BASE}/repos/{OWNER}/{REPO}").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"])
    await connector.validate(cfg, {"github_token": TOKEN})


@respx.mock
@pytest.mark.asyncio
async def test_validate_unauthorized_raises_permission_error() -> None:
    respx.get(f"{API_BASE}/repos/{OWNER}/{REPO}").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"])
    with pytest.raises(PermissionError):
        await connector.validate(cfg, {"github_token": TOKEN})


@respx.mock
@pytest.mark.asyncio
async def test_validate_repo_missing_raises_runtime_error() -> None:
    respx.get(f"{API_BASE}/repos/{OWNER}/{REPO}").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"])
    with pytest.raises(RuntimeError, match="not found"):
        await connector.validate(cfg, {"github_token": TOKEN})


@pytest.mark.asyncio
async def test_validate_no_repos_raises() -> None:
    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[])
    with pytest.raises(ValueError, match="at least one repo"):
        await connector.validate(cfg, {"github_token": TOKEN})


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_discover_yields_canonical_refs() -> None:
    respx.get(f"{API_BASE}/repos/{OWNER}/{REPO}/pulls").mock(
        return_value=httpx.Response(200, json=[_pr_payload(1), _pr_payload(2)])
    )
    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"])
    refs = [r async for r in connector.discover(cfg, {"github_token": TOKEN})]

    assert len(refs) == 2
    assert refs[0].uri == f"https://github.com/{OWNER.lower()}/{REPO}/pull/1"
    assert CANONICAL_PR_URL_REGEX.match(refs[0].uri)
    assert refs[0].metadata["number"] == 1
    assert refs[0].metadata["author"] == "alice"
    assert refs[0].external_id != refs[1].external_id


@respx.mock
@pytest.mark.asyncio
async def test_discover_paginates_until_short_page() -> None:
    full_page = [_pr_payload(i) for i in range(1, 101)]  # exactly _PAGE_SIZE
    short_page = [_pr_payload(101)]
    route = respx.get(f"{API_BASE}/repos/{OWNER}/{REPO}/pulls").mock(
        side_effect=[
            httpx.Response(200, json=full_page),
            httpx.Response(200, json=short_page),
        ]
    )
    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"])
    refs = [r async for r in connector.discover(cfg, {"github_token": TOKEN})]
    assert len(refs) == 101
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_discover_respects_max_age_cutoff() -> None:
    """Older PRs are skipped (sorted desc by updated_at — first old = stop)."""
    respx.get(f"{API_BASE}/repos/{OWNER}/{REPO}/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                _pr_payload(1, updated_at=_recent_ts(days_ago=5)),  # within 30-day cutoff
                _pr_payload(2, updated_at="2020-01-01T00:00:00Z"),  # too old
            ],
        )
    )
    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"], max_age_days=30)
    refs = [r async for r in connector.discover(cfg, {"github_token": TOKEN})]
    assert len(refs) == 1
    assert refs[0].metadata["number"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_discover_state_filter_open_only() -> None:
    route = respx.get(f"{API_BASE}/repos/{OWNER}/{REPO}/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"], include_closed=False)
    _ = [r async for r in connector.discover(cfg, {"github_token": TOKEN})]

    request = route.calls[0].request
    assert "state=open" in str(request.url)


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _stub_fetch_routes(pr_number: int) -> None:
    """Wire up respx routes for a complete fetch round-trip."""
    base = f"{API_BASE}/repos/{OWNER}/{REPO}/pulls/{pr_number}"
    respx.get(base).mock(return_value=httpx.Response(200, json=_pr_payload(pr_number)))
    respx.get(f"{base}/commits").mock(
        return_value=httpx.Response(
            200, json=[_commit_payload("a" * 40), _commit_payload("b" * 40)]
        )
    )
    respx.get(f"{base}/reviews").mock(
        return_value=httpx.Response(200, json=[_review_payload(101), _review_payload(102)])
    )
    respx.get(f"{base}/comments").mock(
        return_value=httpx.Response(200, json=[_comment_payload(201)])
    )
    respx.get(f"{base}/files").mock(
        return_value=httpx.Response(
            200, json=[_file_payload("src/foo.py"), _file_payload("docs/bar.md")]
        )
    )


@respx.mock
@pytest.mark.asyncio
async def test_fetch_returns_markdown_with_canonical_metadata() -> None:
    _stub_fetch_routes(42)
    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"])
    # Build the ref directly to keep the test focused.
    from omniscience_connectors.base import DocumentRef

    ref = DocumentRef(
        external_id="x",
        uri=canonical_pr_url(OWNER, REPO, 42),
        metadata={"owner": OWNER, "repo": REPO, "number": 42},
    )
    doc = await connector.fetch(cfg, {"github_token": TOKEN}, ref)

    assert doc.content_type == "text/markdown"
    md = doc.content_bytes.decode()
    assert "# PR #42:" in md
    assert "Add feature 42" in md
    assert "src/foo.py" in md
    assert "docs/bar.md" in md
    # No diff markers
    assert "--- a/" not in md
    assert "+++ b/" not in md

    # Enriched metadata
    assert doc.ref.metadata["pr_url"] == ref.uri
    assert doc.ref.metadata["commit_shas"] == ["a" * 40, "b" * 40]
    assert doc.ref.metadata["file_paths"] == ["src/foo.py", "docs/bar.md"]
    assert sorted(doc.ref.metadata["review_ids"]) == [101, 102]
    assert doc.ref.metadata["review_comment_ids"] == [201]


@respx.mock
@pytest.mark.asyncio
async def test_fetch_emits_named_edges() -> None:
    _stub_fetch_routes(42)
    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"])
    from omniscience_connectors.base import DocumentRef

    ref = DocumentRef(
        external_id="x",
        uri=canonical_pr_url(OWNER, REPO, 42),
        metadata={"owner": OWNER, "repo": REPO, "number": 42},
    )
    doc = await connector.fetch(cfg, {"github_token": TOKEN}, ref)

    edges: list[dict[str, str]] = doc.ref.metadata["edges"]
    pr_name = canonical_pr_url(OWNER, REPO, 42)
    types = {e["type"] for e in edges}
    assert {"HAS_COMMIT", "TOUCHES", "REVIEWS", "COMMENT_ON", "AUTHORED"} <= types
    has_commit = [e for e in edges if e["type"] == "HAS_COMMIT"]
    assert any(e["to"] == "a" * 40 and e["from"] == pr_name for e in has_commit)
    touches = [e for e in edges if e["type"] == "TOUCHES"]
    assert any(e["to"] == "src/foo.py" and e["from"] == pr_name for e in touches)
    reviews = [e for e in edges if e["type"] == "REVIEWS"]
    assert any(
        e["to"] == pr_name and e["from"] == canonical_review_name(OWNER, REPO, 42, 101)
        for e in reviews
    )
    authored = [e for e in edges if e["type"] == "AUTHORED"]
    assert any(e["to"] == pr_name and e["from"] == canonical_user_name("alice") for e in authored)


@respx.mock
@pytest.mark.asyncio
async def test_fetch_strips_review_body_diff_markers_defensively() -> None:
    """Even if a review body contains diff hunks, fetched Markdown rejects them."""
    base = f"{API_BASE}/repos/{OWNER}/{REPO}/pulls/7"
    respx.get(base).mock(return_value=httpx.Response(200, json=_pr_payload(7)))
    respx.get(f"{base}/commits").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{base}/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "user": {"login": "x"},
                    "state": "COMMENTED",
                    "body": "look at this:\n--- a/old\n+++ b/new\n@@ -1 +1 @@\n-foo\n+bar",
                }
            ],
        )
    )
    respx.get(f"{base}/comments").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{base}/files").mock(return_value=httpx.Response(200, json=[]))

    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"])
    from omniscience_connectors.base import DocumentRef

    ref = DocumentRef(
        external_id="x",
        uri=canonical_pr_url(OWNER, REPO, 7),
        metadata={"owner": OWNER, "repo": REPO, "number": 7},
    )
    doc = await connector.fetch(cfg, {"github_token": TOKEN}, ref)
    md = doc.content_bytes.decode()
    assert "--- a/" not in md
    assert "+++ b/" not in md


# ---------------------------------------------------------------------------
# Rate-limit handling
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_rate_limit_sleeps_and_retries() -> None:
    """403 + X-RateLimit-Remaining=0 triggers a sleep and a retry."""
    rate_limit_response = httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1",  # epoch in the past — sleeps near 0s
        },
    )
    success = httpx.Response(200, json={"id": 1})
    route = respx.get(f"{API_BASE}/repos/{OWNER}/{REPO}").mock(
        side_effect=[rate_limit_response, success]
    )

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    connector = GithubPrConnector()
    cfg = GithubPrConfig(repos=[f"{OWNER}/{REPO}"])
    with patch("omniscience_connectors.github_pr.connector.asyncio.sleep", _fake_sleep):
        await connector.validate(cfg, {"github_token": TOKEN})

    assert route.call_count == 2
    assert len(sleep_calls) == 1


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_github_pr_connector_is_registered() -> None:
    connector = get_connector("github_pr")
    assert isinstance(connector, GithubPrConnector)


# ---------------------------------------------------------------------------
# PR-URL canonicalisation regression (#150 acceptance criterion 2)
# ---------------------------------------------------------------------------

# This regex is what a Slack mention extractor would use to find PR URLs
# in message text.  It MUST match the connector's emitted URLs verbatim.
_SLACK_PR_URL_REGEX = re.compile(r"https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/\d+")


def test_slack_mention_regex_round_trips_through_connector() -> None:
    fixture_message = (
        "FYI the deploy was https://github.com/100rd/Omniscience/pull/42 "
        "and a follow-up at https://github.com/acme/widget/pull/7"
    )
    extracted = _SLACK_PR_URL_REGEX.findall(fixture_message)
    assert extracted == [
        "https://github.com/100rd/Omniscience/pull/42",
        "https://github.com/acme/widget/pull/7",
    ]
    # The connector must produce strings that match the same regex AND
    # equal the extracted strings byte-for-byte.
    rebuilt = [
        canonical_pr_url("100rd", "Omniscience", 42),
        canonical_pr_url("acme", "widget", 7),
    ]
    assert rebuilt == extracted
    for url in rebuilt:
        assert CANONICAL_PR_URL_REGEX.fullmatch(url)


def test_canonical_pr_url_has_no_trailing_slash_or_anchor() -> None:
    url = canonical_pr_url(OWNER, REPO, 99)
    assert not url.endswith("/")
    assert "#" not in url
    assert "?" not in url
