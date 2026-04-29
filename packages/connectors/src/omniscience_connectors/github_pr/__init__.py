"""GitHub PR/MR source connector for Omniscience.

Discovers and indexes pull requests, commits, reviews, and review comments as
first-class entities, distinct from the file-tree indexing handled by
:class:`omniscience_connectors.git.connector.GitConnector`.

Both connectors live side-by-side and target the same upstream (a GitHub repo)
but cover different aspects:

* :class:`GitConnector` — indexes file blobs (source code).
* :class:`GithubPrConnector` — indexes review history (PRs, commits, reviews,
  comments) so that ``mcp_get_related_entities`` can surface "responsible PR"
  links from runtime resources.

The connector emits PR entities by their canonical GitHub URL form
(``https://github.com/{owner}/{repo}/pull/{n}``) so that
``EntityLinker.exact_name`` produces ``cross_ref`` edges to the same URL
emitted by the Slack mention extractor.
"""

from omniscience_connectors.github_pr.connector import (
    CANONICAL_PR_URL_REGEX,
    GithubPrConfig,
    GithubPrConnector,
)
from omniscience_connectors.github_pr.webhook import GithubPrWebhookHandler

__all__ = [
    "CANONICAL_PR_URL_REGEX",
    "GithubPrConfig",
    "GithubPrConnector",
    "GithubPrWebhookHandler",
]
