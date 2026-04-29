"""GitHub Pull Request source connector.

Discovers and fetches GitHub pull requests as first-class artifacts (distinct
from file-blob indexing in :class:`GitConnector`).  PRs, commits, reviews, and
review comments are emitted as named entities by their canonical GitHub URL
form so the entity linker can produce ``cross_ref`` edges to the same URLs
emitted by the Slack mention extractor.

Authentication
--------------
Resolved at call time from the ``secrets`` dict.  Both Personal Access Tokens
(PAT) and GitHub App installation tokens are accepted; format is identical
from this connector's point of view (``Authorization: Bearer <token>``).  The
GitHub App private-key → JWT → installation-token flow is operator concern
and lives outside this module.

* ``github_token`` — PAT or GitHub App installation token.
* (optional) ``github_app_id`` / ``github_installation_id`` — recorded as
  metadata only; this connector does not mint installation tokens itself.

Tokens are never logged.

Canonical PR URL
----------------
The ``DocumentRef.uri`` and the entity ``name`` for a PR are guaranteed to
match the regex :data:`CANONICAL_PR_URL_REGEX` exactly:

    https://github.com/{owner}/{repo}/pull/{n}

Lower-case host, no trailing slash, no query, no anchor.  This is a hard
contract — ``cross_ref`` matching depends on byte-for-byte equality with the
URL Slack mentions emit.

Rate limiting
-------------
The connector respects ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset`` and
sleeps until reset when the quota is exhausted.  No retries on 4xx errors
other than 429/403-with-rate-limit.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from omniscience_connectors.base import (
    Connector,
    DocumentRef,
    FetchedDocument,
    WebhookHandler,
)
from omniscience_connectors.github_pr.webhook import GithubPrWebhookHandler

__all__ = [
    "CANONICAL_PR_URL_REGEX",
    "GithubPrConfig",
    "GithubPrConnector",
    "canonical_commit_name",
    "canonical_pr_url",
    "canonical_review_comment_name",
    "canonical_review_name",
    "canonical_user_name",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (named with rationale)
# ---------------------------------------------------------------------------

# GitHub REST API root.  Override is intentionally not exposed in config —
# GitHub Enterprise support is a follow-up; the issue scopes to github.com.
_GITHUB_API_BASE = "https://api.github.com"

# REST API page size.  GitHub caps list endpoints at 100; we use the max to
# minimise round-trips for large repos.
_PAGE_SIZE = 100

# HTTP client timeout (seconds).  Generous to absorb GitHub's occasional
# slow paths on review comment listing for large PRs.
_HTTP_TIMEOUT = 30.0

# Maximum sleep when rate-limited.  Defends against bogus reset headers.
_MAX_RATE_LIMIT_SLEEP_SECONDS = 60 * 60

# Maximum backoff retries on rate-limit.  After this many waits we surrender
# and re-raise to the caller.
_MAX_RATE_LIMIT_RETRIES = 3

# Repository spec must look like ``owner/repo`` (one slash, no whitespace).
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# Canonical PR URL pattern — matches the exact string emitted by the Slack
# mention extractor.  Used both for self-validation (regression test) and for
# the webhook handler's URL synthesis.  Anchored at both ends.
CANONICAL_PR_URL_REGEX: re.Pattern[str] = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/(\d+)$"
)

# Diff markers — a fetched-document regression test asserts these never appear
# in the connector's Markdown output (diffs are GitConnector's domain).
_DIFF_HUNK_MARKERS = ("--- a/", "+++ b/")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class GithubPrConfig(BaseModel):
    """Public configuration for the GitHub PR connector (no secrets)."""

    repos: list[str] = Field(default_factory=list)
    """Repositories to discover, each in ``owner/repo`` form."""

    include_closed: bool = True
    """Include closed and merged PRs as well as open ones."""

    max_age_days: int = 90
    """Skip PRs whose ``updated_at`` is older than this cut-off (days)."""

    include_review_comments: bool = True
    """Include inline review comments in the fetched Markdown body."""

    api_base_url: str = _GITHUB_API_BASE
    """API root.  Override only for testing; production stays on github.com."""


# ---------------------------------------------------------------------------
# Canonical-name helpers (entity name conventions per the architect memo)
# ---------------------------------------------------------------------------


def canonical_pr_url(owner: str, repo: str, number: int) -> str:
    """Return the canonical PR URL form used as the entity ``name``."""
    return f"https://github.com/{owner.lower()}/{repo}/pull/{number}"


def canonical_commit_name(sha: str) -> str:
    """Return the canonical commit-entity name (full 40-char SHA)."""
    return sha.lower()


def canonical_review_name(owner: str, repo: str, pr_number: int, review_id: int) -> str:
    """Return the canonical review-entity name."""
    return f"github://review/{owner.lower()}/{repo}/{pr_number}/{review_id}"


def canonical_review_comment_name(owner: str, repo: str, pr_number: int, comment_id: int) -> str:
    """Return the canonical review-comment-entity name."""
    return f"github://comment/{owner.lower()}/{repo}/{pr_number}/{comment_id}"


def canonical_user_name(login: str) -> str:
    """Return the canonical GitHub-user-entity name."""
    return f"github://user/{login}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_repos(repos: list[str]) -> list[tuple[str, str]]:
    """Parse and validate ``owner/repo`` specs; returns ``[(owner, repo), ...]``."""
    parsed: list[tuple[str, str]] = []
    for spec in repos:
        if not _REPO_PATTERN.match(spec):
            raise ValueError(f"Invalid repository spec {spec!r}; expected 'owner/repo' format")
        owner, repo = spec.split("/", 1)
        parsed.append((owner, repo))
    return parsed


def _build_auth_headers(secrets: dict[str, str]) -> dict[str, str]:
    """Build authorization headers from runtime secrets.

    Accepts either a PAT or a GitHub App installation token under the same
    key (``github_token``) — both are sent as bearer credentials.
    """
    token = secrets.get("github_token", "").strip()
    if not token:
        raise ValueError(
            "GitHub PR connector requires 'github_token' in secrets "
            "(PAT or GitHub App installation token)"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "omniscience-github-pr-connector/0.2.0",
    }


def _parse_iso8601(value: str | None) -> datetime | None:
    """Parse a GitHub timestamp; returns ``None`` on missing/invalid input."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _pr_external_id(owner: str, repo: str, number: int) -> str:
    """Stable ID for de-duplication: SHA-1 of ``owner/repo:number``.

    SHA-1 is used as a *fingerprint*, not for security; ``noqa: S324`` would
    apply at the call site if we used hashlib.sha1 directly.
    """
    fingerprint = f"{owner.lower()}/{repo}:{number}"
    return hashlib.sha1(fingerprint.encode()).hexdigest()  # noqa: S324


async def _sleep_until_rate_limit_reset(response: httpx.Response) -> bool:
    """If the response indicates exhaustion, sleep until reset.

    Returns ``True`` when a sleep happened and the caller should retry,
    ``False`` when no rate-limit handling is needed.
    """
    remaining = response.headers.get("X-RateLimit-Remaining", "")
    if remaining != "0":
        return False
    reset_str = response.headers.get("X-RateLimit-Reset", "")
    try:
        reset_epoch = int(reset_str)
    except (TypeError, ValueError):
        return False
    sleep_for = max(0.0, float(reset_epoch) - time.time())
    sleep_for = min(sleep_for, float(_MAX_RATE_LIMIT_SLEEP_SECONDS))
    logger.warning(
        "github_pr.rate_limited",
        extra={"sleep_seconds": int(sleep_for), "reset_epoch": reset_epoch},
    )
    await asyncio.sleep(sleep_for)
    return True


async def _request_with_rate_limit(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Issue an HTTP request, transparently waiting on GitHub rate limits."""
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        response = await client.request(method, url, params=params)
        if response.status_code in (403, 429):
            slept = await _sleep_until_rate_limit_reset(response)
            if slept and attempt < _MAX_RATE_LIMIT_RETRIES:
                continue
        return response
    return response


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_pr_markdown(
    pr: dict[str, Any],
    commits: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    review_comments: list[dict[str, Any]],
    files: list[dict[str, Any]],
    *,
    include_review_comments: bool,
) -> str:
    """Render the PR + reviews + comments + file list as a Markdown document.

    Diffs are NOT included — diff hunks are GitConnector's domain.  The
    ``files`` list is rendered as filenames only.
    """
    lines: list[str] = []
    title = str(pr.get("title", "")).strip()
    number = pr.get("number", "")
    state = pr.get("state", "")
    user = (pr.get("user") or {}).get("login", "")

    lines.append(f"# PR #{number}: {title}")
    lines.append("")
    lines.append(f"- State: {state}")
    if user:
        lines.append(f"- Author: {user}")
    if pr.get("merged_at"):
        lines.append(f"- Merged at: {pr['merged_at']}")
    if pr.get("closed_at"):
        lines.append(f"- Closed at: {pr['closed_at']}")
    if pr.get("created_at"):
        lines.append(f"- Created at: {pr['created_at']}")
    if pr.get("updated_at"):
        lines.append(f"- Updated at: {pr['updated_at']}")
    lines.append("")

    body = (pr.get("body") or "").strip()
    if body:
        lines.append("## Description")
        lines.append("")
        lines.append(body)
        lines.append("")

    if commits:
        lines.append("## Commits")
        lines.append("")
        for commit in commits:
            sha = str(commit.get("sha", ""))
            commit_obj = commit.get("commit") or {}
            message = str(commit_obj.get("message", "")).splitlines()[0]
            author = (commit_obj.get("author") or {}).get("name", "")
            lines.append(f"- `{sha}` {message} ({author})")
        lines.append("")

    if reviews:
        lines.append("## Reviews")
        lines.append("")
        for review in reviews:
            reviewer = (review.get("user") or {}).get("login", "")
            r_state = review.get("state", "")
            review_body = (review.get("body") or "").strip()
            lines.append(f"- {reviewer}: {r_state}")
            if review_body:
                for body_line in review_body.splitlines():
                    lines.append(f"  > {body_line}")
        lines.append("")

    if include_review_comments and review_comments:
        lines.append("## Review comments")
        lines.append("")
        for comment in review_comments:
            commenter = (comment.get("user") or {}).get("login", "")
            path = comment.get("path", "")
            comment_body = (comment.get("body") or "").strip()
            lines.append(f"- {commenter} on `{path}`:")
            for body_line in comment_body.splitlines():
                lines.append(f"  > {body_line}")
        lines.append("")

    if files:
        lines.append("## Files touched")
        lines.append("")
        for f in files:
            filename = f.get("filename", "")
            if filename:
                lines.append(f"- `{filename}`")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class GithubPrConnector(Connector):
    """Connector for GitHub pull requests.

    Stateless: all state is derived from config + secrets at call time, so a
    single instance can serve many ``Source`` rows.

    See module docstring for the canonical-URL contract.
    """

    connector_type: ClassVar[str] = "github_pr"
    config_schema: ClassVar[type[BaseModel]] = GithubPrConfig

    async def validate(self, config: BaseModel, secrets: dict[str, str]) -> None:
        """Verify the token works and every configured repo is reachable.

        Raises ``PermissionError`` on 401, ``RuntimeError`` on missing repos
        or other API errors.
        """
        cfg: GithubPrConfig = config  # type: ignore[assignment]
        repos = _validate_repos(cfg.repos)
        if not repos:
            raise ValueError("GitHub PR connector requires at least one repo in config.repos")
        headers = _build_auth_headers(secrets)
        async with httpx.AsyncClient(headers=headers, timeout=_HTTP_TIMEOUT) as client:
            for owner, repo in repos:
                url = f"{cfg.api_base_url}/repos/{owner}/{repo}"
                resp = await _request_with_rate_limit(client, "GET", url)
                if resp.status_code == 401:
                    raise PermissionError("GitHub authentication failed — check github_token.")
                if resp.status_code == 404:
                    raise RuntimeError(f"Repository {owner}/{repo!r} not found or inaccessible")
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"GitHub API error for {owner}/{repo}: HTTP {resp.status_code}"
                    )

    async def discover(
        self,
        config: BaseModel,
        secrets: dict[str, str],
    ) -> AsyncIterator[DocumentRef]:
        """Yield a :class:`DocumentRef` for every PR matching the filters."""
        cfg: GithubPrConfig = config  # type: ignore[assignment]
        repos = _validate_repos(cfg.repos)
        headers = _build_auth_headers(secrets)
        cutoff = datetime.now(tz=UTC) - timedelta(days=cfg.max_age_days)
        state_filter = "all" if cfg.include_closed else "open"

        async with httpx.AsyncClient(headers=headers, timeout=_HTTP_TIMEOUT) as client:
            for owner, repo in repos:
                async for ref in self._discover_repo(
                    client, cfg, owner, repo, state_filter, cutoff
                ):
                    yield ref

    async def _discover_repo(
        self,
        client: httpx.AsyncClient,
        cfg: GithubPrConfig,
        owner: str,
        repo: str,
        state_filter: str,
        cutoff: datetime,
    ) -> AsyncIterator[DocumentRef]:
        """Paginate the PR list endpoint for one repo."""
        url = f"{cfg.api_base_url}/repos/{owner}/{repo}/pulls"
        page = 1
        while True:
            params: dict[str, Any] = {
                "state": state_filter,
                "per_page": _PAGE_SIZE,
                "page": page,
                "sort": "updated",
                "direction": "desc",
            }
            resp = await _request_with_rate_limit(client, "GET", url, params=params)
            if resp.status_code >= 400:
                logger.warning(
                    "github_pr.discover.api_error",
                    extra={"owner": owner, "repo": repo, "status": resp.status_code},
                )
                return
            items = resp.json()
            if not isinstance(items, list) or not items:
                return
            for item in items:
                ref = self._build_pr_ref(owner, repo, item, cutoff)
                if ref is None:
                    return  # sorted by updated desc — older items follow
                yield ref
            if len(items) < _PAGE_SIZE:
                return
            page += 1

    def _build_pr_ref(
        self,
        owner: str,
        repo: str,
        pr: dict[str, Any],
        cutoff: datetime,
    ) -> DocumentRef | None:
        """Translate a PR JSON object into a :class:`DocumentRef`.

        Returns ``None`` if the PR is older than ``cutoff`` (signals end of
        the desc-sorted page stream).
        """
        number = pr.get("number")
        if not isinstance(number, int):
            return None
        updated_at = _parse_iso8601(pr.get("updated_at"))
        if updated_at is not None and updated_at < cutoff:
            return None
        uri = canonical_pr_url(owner, repo, number)
        return DocumentRef(
            external_id=_pr_external_id(owner, repo, number),
            uri=uri,
            updated_at=updated_at,
            metadata={
                "owner": owner,
                "repo": repo,
                "number": number,
                "state": pr.get("state", ""),
                "title": pr.get("title", ""),
                "author": (pr.get("user") or {}).get("login", ""),
                "html_url": pr.get("html_url", uri),
                "merged_at": pr.get("merged_at"),
                "closed_at": pr.get("closed_at"),
                "created_at": pr.get("created_at"),
            },
        )

    async def fetch(
        self,
        config: BaseModel,
        secrets: dict[str, str],
        ref: DocumentRef,
    ) -> FetchedDocument:
        """Fetch the PR, its commits/reviews/comments/files; render Markdown."""
        cfg: GithubPrConfig = config  # type: ignore[assignment]
        owner = str(ref.metadata.get("owner", ""))
        repo = str(ref.metadata.get("repo", ""))
        number = ref.metadata.get("number")
        if not owner or not repo or not isinstance(number, int):
            raise ValueError(f"DocumentRef missing owner/repo/number metadata: {ref.uri}")

        headers = _build_auth_headers(secrets)
        async with httpx.AsyncClient(headers=headers, timeout=_HTTP_TIMEOUT) as client:
            pr = await self._fetch_json(client, cfg, owner, repo, f"pulls/{number}")
            commits = await self._paginate(client, cfg, owner, repo, f"pulls/{number}/commits")
            reviews = await self._paginate(client, cfg, owner, repo, f"pulls/{number}/reviews")
            review_comments: list[dict[str, Any]] = []
            if cfg.include_review_comments:
                review_comments = await self._paginate(
                    client, cfg, owner, repo, f"pulls/{number}/comments"
                )
            files = await self._paginate(client, cfg, owner, repo, f"pulls/{number}/files")

        # Strip diff fields from file objects to honour the no-diffs invariant.
        files_no_diff = [{k: v for k, v in f.items() if k != "patch"} for f in files]
        markdown = _render_pr_markdown(
            pr if isinstance(pr, dict) else {},
            commits,
            reviews,
            review_comments,
            files_no_diff,
            include_review_comments=cfg.include_review_comments,
        )
        # Defensive: assert no diff markers leaked through (e.g. via review body).
        if any(marker in markdown for marker in _DIFF_HUNK_MARKERS):
            markdown = _strip_diff_markers(markdown)

        commit_shas = [str(c.get("sha", "")) for c in commits if c.get("sha")]
        file_paths = [str(f.get("filename", "")) for f in files if f.get("filename")]
        review_ids = [int(r["id"]) for r in reviews if isinstance(r.get("id"), int)]
        comment_ids = [int(c["id"]) for c in review_comments if isinstance(c.get("id"), int)]

        enriched_metadata: dict[str, Any] = {
            **ref.metadata,
            "pr_url": ref.uri,
            "commit_shas": commit_shas,
            "file_paths": file_paths,
            "review_ids": review_ids,
            "review_comment_ids": comment_ids,
            "edges": _build_edges(
                owner, repo, number, commit_shas, review_ids, comment_ids, file_paths, pr
            ),
        }
        enriched_ref = DocumentRef(
            external_id=ref.external_id,
            uri=ref.uri,
            updated_at=ref.updated_at,
            metadata=enriched_metadata,
        )
        return FetchedDocument(
            ref=enriched_ref,
            content_bytes=markdown.encode("utf-8"),
            content_type="text/markdown",
        )

    async def _fetch_json(
        self,
        client: httpx.AsyncClient,
        cfg: GithubPrConfig,
        owner: str,
        repo: str,
        path: str,
    ) -> dict[str, Any]:
        """Fetch and JSON-decode a single endpoint; returns ``{}`` on error."""
        url = f"{cfg.api_base_url}/repos/{owner}/{repo}/{path}"
        resp = await _request_with_rate_limit(client, "GET", url)
        if resp.status_code >= 400:
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def _paginate(
        self,
        client: httpx.AsyncClient,
        cfg: GithubPrConfig,
        owner: str,
        repo: str,
        path: str,
    ) -> list[dict[str, Any]]:
        """Paginate a list endpoint; returns flattened list of dicts."""
        url = f"{cfg.api_base_url}/repos/{owner}/{repo}/{path}"
        page = 1
        out: list[dict[str, Any]] = []
        while True:
            params = {"per_page": _PAGE_SIZE, "page": page}
            resp = await _request_with_rate_limit(client, "GET", url, params=params)
            if resp.status_code >= 400:
                break
            items = resp.json()
            if not isinstance(items, list) or not items:
                break
            out.extend(item for item in items if isinstance(item, dict))
            if len(items) < _PAGE_SIZE:
                break
            page += 1
        return out

    def webhook_handler(self) -> WebhookHandler | None:
        return GithubPrWebhookHandler()


# ---------------------------------------------------------------------------
# Edge construction (named-entity contract)
# ---------------------------------------------------------------------------


def _build_edges(
    owner: str,
    repo: str,
    pr_number: int,
    commit_shas: list[str],
    review_ids: list[int],
    comment_ids: list[int],
    file_paths: list[str],
    pr: dict[str, Any],
) -> list[dict[str, str]]:
    """Build the edge-list for the PR graph fragment.

    Edge shape: ``{"from": <name>, "to": <name>, "type": <relation>}``.
    Names are canonical strings only — this connector never emits database
    IDs into the graph.
    """
    pr_name = canonical_pr_url(owner, repo, pr_number)
    edges: list[dict[str, str]] = []

    # PR -[HAS_COMMIT]-> commit
    for sha in commit_shas:
        edges.append({"from": pr_name, "to": canonical_commit_name(sha), "type": "HAS_COMMIT"})
    # PR -[TOUCHES]-> file (file entity emitted by GitConnector)
    for path in file_paths:
        edges.append({"from": pr_name, "to": path, "type": "TOUCHES"})
    # review -[REVIEWS]-> PR
    for rid in review_ids:
        edges.append(
            {
                "from": canonical_review_name(owner, repo, pr_number, rid),
                "to": pr_name,
                "type": "REVIEWS",
            }
        )
    # comment -[COMMENT_ON]-> PR
    for cid in comment_ids:
        edges.append(
            {
                "from": canonical_review_comment_name(owner, repo, pr_number, cid),
                "to": pr_name,
                "type": "COMMENT_ON",
            }
        )
    # author -[AUTHORED]-> PR
    author_login = (pr.get("user") or {}).get("login", "")
    if author_login:
        edges.append(
            {
                "from": canonical_user_name(str(author_login)),
                "to": pr_name,
                "type": "AUTHORED",
            }
        )
    return edges


def _strip_diff_markers(text: str) -> str:
    """Remove diff-hunk lines defensively (re-runs the no-diffs invariant)."""
    out_lines = [
        line for line in text.splitlines() if not any(m in line for m in _DIFF_HUNK_MARKERS)
    ]
    return "\n".join(out_lines)
