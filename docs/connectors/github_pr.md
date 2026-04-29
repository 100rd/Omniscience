# GitHub PR/MR connector (`github_pr`)

The `github_pr` connector indexes pull requests, commits, reviews, and review
comments from a GitHub repository as **first-class entities** — distinct from
the file-tree indexing that `GitConnector` already performs.  Both connectors
target the same upstream (a GitHub repo) but cover different aspects:

| Connector | Domain |
|-----------|--------|
| `git` | File blobs (source code, repo tree at a ref) |
| `github_pr` | Review history (PRs, commits, reviews, comments) |

The two connectors run **side-by-side** as separate `Source` rows.

## Why a separate connector?

PRs, commits, reviews, and review comments are not files.  Indexing them via
`GitConnector` would conflate "what is in the repo right now" with "what was
discussed about the changes".  Splitting them lets the PR/MR connector evolve
its own auth model, rate limits (per-token), and webhook handler without
disturbing the file-tree indexer.

## Authentication

Authentication is resolved at call time from the `secrets` dict.  Both
**Personal Access Tokens (PAT)** and **GitHub App installation tokens** are
accepted — they are byte-equivalent from this connector's point of view
(both sent as `Authorization: Bearer <token>`).

| Secret | Required | Notes |
|--------|----------|-------|
| `github_token` | yes | PAT or GitHub App installation token |
| `github_app_id` | no | Recorded as metadata only |
| `github_installation_id` | no | Recorded as metadata only |

The token-issuance flow for GitHub Apps (private key → JWT → installation
token) is **operator concern** and lives outside this connector.  Operators
that mint short-lived installation tokens upstream and rotate them in the
secret store get full GitHub App support without any code change here.

PAT usage is appropriate for personal workspaces and small teams; GitHub
App tokens are recommended for production (higher rate limits, fine-grained
scopes, no per-user coupling).

Tokens are **never logged**.  If you see a token in a log line, file a bug.

## Configuration

```python
from omniscience_connectors import GithubPrConfig

config = GithubPrConfig(
    repos=["owner/repo", "owner/another"],
    include_closed=True,         # default True
    max_age_days=90,             # default 90
    include_review_comments=True,  # default True
)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `repos` | `list[str]` | `[]` | One or more `owner/repo` specs |
| `include_closed` | `bool` | `True` | Discover closed/merged PRs as well as open |
| `max_age_days` | `int` | `90` | Skip PRs older than this (by `updated_at`) |
| `include_review_comments` | `bool` | `True` | Include inline review comments in fetched Markdown |
| `api_base_url` | `str` | `https://api.github.com` | API root; override for testing only |

## Entities and edges emitted

| Entity kind | Canonical `name` |
|-------------|------------------|
| `github_pr` | `https://github.com/{owner}/{repo}/pull/{n}` |
| `git_commit` | `<full-40-char-sha>` |
| `github_review` | `github://review/{owner}/{repo}/{pr}/{review_id}` |
| `github_review_comment` | `github://comment/{owner}/{repo}/{pr}/{comment_id}` |
| `github_user` | `github://user/{login}` |

| Edge | Direction |
|------|-----------|
| `[:HAS_COMMIT]` | PR → commit |
| `[:TOUCHES]` | PR → file (file entity emitted by `GitConnector`) |
| `[:REVIEWS]` | review → PR |
| `[:COMMENT_ON]` | comment → PR |
| `[:AUTHORED]` | user → PR |

### Canonical PR URL — hard contract

The PR `name` is **byte-for-byte equal** to the URL the Slack mention extractor
emits when a Slack message contains a PR link.  This is what enables
`EntityLinker.exact_name` to produce `cross_ref` edges between Slack threads
and PRs without any application-level join.

The format is anchored by `CANONICAL_PR_URL_REGEX`:

```
^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/(\d+)$
```

* lowercase host (`github.com`)
* path `/{owner}/{repo}/pull/{n}`
* no trailing slash, no query string, no anchor

If you change anything about this contract, you also break Slack `cross_ref`
matching — there is a regression test that asserts the round-trip stays
identical.

## Webhook handler

The connector ships a `WebhookHandler` that the FastAPI receiver at
`/api/v1/ingest/webhook/{source_name}` wires up automatically (see
`apps/server/src/omniscience_server/rest/webhooks.py`).

Subscribed events:

* `pull_request` — opened, edited, closed, synchronize, reopened
* `pull_request_review` — submitted, edited, dismissed
* `pull_request_review_comment` — created, edited, deleted
* `push` — acknowledged but produces no PR refs (file content stays on
  `GitConnector`'s webhook)

Signature verification is **HMAC-SHA256 over the raw body**, with the digest
delivered in `X-Hub-Signature-256: sha256=<hex>`.  Constant-time comparison.

### ACL invariant

Workspace identity comes from `Source.tenant_id` resolved by the FastAPI
receiver on the `/webhooks/{source_name}` path — **never** from any field in
the webhook payload.  Adding an endpoint that takes tenant from the GitHub
payload `installation.id` would be a P0 ACL leak and is explicitly rejected
by the design contract.

## Non-goals

* No replacement of `GitConnector`.  File-blob indexing stays where it is.
* No PR diff body indexing — diffs explode embedding budgets and `GitConnector`
  already indexes file contents at HEAD.  `resolve_incident` reaches the file
  via the `[:TOUCHES]` edge.
* No GitLab support in this iteration.  The connector is named `github_pr`;
  a sibling `gitlab_mr` connector is a separate sub-issue.
* No CI status / check-run entities.  Possible follow-up.
