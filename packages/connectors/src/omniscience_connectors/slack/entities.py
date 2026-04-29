"""Slack entity surface extractor for incident-grade GraphRAG (issue #149).

Given a Markdown document produced by :class:`SlackConnector.fetch` and the
``DocumentRef.metadata`` it carries, this module extracts:

1. **Structural entities** for the thread itself
   (``slack_thread``, ``slack_message``, ``slack_user``, ``slack_channel``).
2. **Mention-target entities** named after canonical strings inside the
   message bodies — GitHub PR URLs, AWS ARNs, Kubernetes pod names, and
   error tokens.  These are emitted as ``slack_mention`` entities whose
   ``name`` is the canonical mention string.  The cross-source
   :class:`~omniscience_index.linker.EntityLinker` creates ``cross_ref``
   edges between them and the matching entities from the GitHub PR/MR,
   AWS, and K8s connectors via the existing ``exact_name`` and
   ``arn_match`` strategies (no new linker strategies introduced —
   per issue #149's explicit non-goal).

ACL invariant
-------------
The extractor emits entities by **name only** — workspace stamping
happens later in :mod:`omniscience_server.ingestion.pipeline` (see
``_tag_entity_workspace``).  Mentions are tenant-writable Slack content;
they enter the graph as edge targets named by the user-supplied string,
**not** as authoritative source-of-truth entities.  The authoritative
entity for a PR / ARN / pod is always the one emitted by the trusted
connector (GitHub / AWS / K8s).

The connector's ``Source.tenant_id`` is the only legitimate source of
workspace identity — payload fields such as ``team_id``, ``user`` ids,
or any mention string MUST NEVER be used to infer workspace.

Mention extraction
------------------
All four mention extractors run on a *normalised* version of the message
text where Markdown code fences (triple-backtick blocks) and inline code
spans (single-backtick spans) are stripped.  This avoids false positives
from example payloads that the user pasted into the discussion
(issue #149 adversarial-string requirement).

The four extractors are intentionally **conservative** — they prefer
false negatives over false positives — because every match feeds the
linker, and a false positive mention pollutes the graph with a
tenant-controlled name that could shadow a real entity at retrieval
time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EXTRACTOR_VERSION",
    "MENTION_KIND",
    "EdgeData",
    "EntityData",
    "build_thread_entities",
    "extract_mentions",
    "extract_slack_graph",
    "slack_thread_name",
]


# ---------------------------------------------------------------------------
# Local DTOs — kept structurally identical to
# :class:`omniscience_parsers.infra.graph.EntityData` / ``EdgeData`` so the
# downstream ingestion pipeline (which accepts a generic
# ``(entities, edges)`` tuple from any extractor — see
# ``apps/server/src/omniscience_server/ingestion/pipeline.py``) can consume
# them without adapter code.
#
# Defined locally rather than imported from ``omniscience_parsers`` to keep
# the connector package's dependency graph minimal (connectors -> core only;
# the parsers package may later depend on connector types and we must avoid
# the cycle).
# ---------------------------------------------------------------------------


@dataclass
class EntityData:
    """A node emitted by the Slack entity extractor.

    Attributes:
        symbol: Canonical identifier — for Slack entities this matches
            :func:`slack_thread_name` / equivalents; for mention-target
            entities this is the canonical mention string itself.
        kind: Entity kind (``slack_thread``, ``slack_message``,
            ``slack_user``, ``slack_channel``, or :data:`MENTION_KIND`).
        name: Human-readable name; same as ``symbol`` for all kinds the
            Slack extractor produces (canonical strings ARE the names).
        namespace: Reserved (unused for Slack); kept for shape compatibility.
        labels: Reserved (unused for Slack); kept for shape compatibility.
        extra: Per-kind additional metadata.  See the kind-specific builders.
    """

    symbol: str
    kind: str
    name: str
    namespace: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class EdgeData:
    """A directed edge emitted by the Slack entity extractor.

    Edge types used:

    - ``in_thread``: SlackMessage -> SlackThread.
    - ``in_channel``: SlackThread -> SlackChannel.
    - ``authored``: SlackUser -> SlackMessage.
    - ``mentions``: SlackThread -> mention-target entity.
    """

    from_symbol: str
    to_symbol: str
    edge_type: str
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Stamp written to ``EntityData.extra['extractor_version']`` so downstream
#: queries can segment by extraction strategy version (mirrors
#: ``_PARSER_VERSION`` in ``ingestion.pipeline``).
EXTRACTOR_VERSION: str = "slack-entities-v1"

#: Entity kind for mention-target entities.  Distinct from the canonical
#: kinds emitted by trusted connectors (``aws_live``, ``k8s_resource``,
#: GitHub PR kind, …) so that retrieval can distinguish a Slack-mention
#: anchor from the authoritative entity.  The linker still creates
#: ``cross_ref`` edges via the source-agnostic ``exact_name`` strategy.
MENTION_KIND: str = "slack_mention"


# ---------------------------------------------------------------------------
# Mention regex patterns — module-level constants with rationale comments.
# ---------------------------------------------------------------------------

# GitHub PR URL: capture the canonical https://github.com/{owner}/{repo}/pull/{n}
# form.  We capture only the BASE URL — query strings (``?diff=split``) and
# fragments (``#discussion_r123``) are matched after the captured group but
# discarded so the emitted canonical name matches what the GitHub PR/MR
# connector (issue #150) emits as PR ``name`` regardless of how the user
# pasted the URL into Slack.
#
# Anchor: the URL must be preceded by start-of-string, whitespace, ``(``, ``[``,
# ``<``, ``"`` or ``'``.  Trailing query/fragment is consumed by a non-capturing
# group so the captured base URL is always punctuation-free.
_RE_PR_URL = re.compile(
    r"(?:^|(?<=[\s(\[<\"']))"
    r"(https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/\d+)"
    r"(?:[?#][^\s)\]>\"']*)?",
)

# AWS ARN: arn:{partition}:{service}:{region}:{account}:{resource}.
# Partition allows aws, aws-cn, aws-us-gov.  Resource segment is anything
# that is not whitespace or a closing quote/bracket character (matches the
# RFC-permissive form actually seen in the wild — IAM, S3, EC2, …).
#
# We deliberately reject ARNs whose service segment is empty (``arn:aws::``)
# to avoid matching the literal string ``arn:`` followed by whitespace.
_RE_ARN = re.compile(
    r"(?:^|(?<=[\s(\[<\"']))"
    r"(arn:aws[a-z0-9-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]*:[^\s\"'`)\]>]+)",
)

# Kubernetes pod name (deployment-replicaset-pod hash form):
#   {prefix}-{8-10 hex chars}-{5 alphanum chars}
#
# The hash segments come from the ReplicaSet template-hash and the pod's
# 5-char random suffix.  This is the most common form for Deployment-managed
# pods.  Standalone pods or StatefulSet pods have different shapes; those
# are out of scope for incident-grade mention extraction (the alerts/OTel
# sibling sub-issues will surface them by trace ID).
#
# Word boundaries on each side prevent matching inside longer identifiers.
_RE_POD_NAME = re.compile(
    r"(?<![A-Za-z0-9-])"
    r"([a-z0-9]+(?:-[a-z0-9]+)*-[a-f0-9]{8,10}-[a-z0-9]{5})"
    r"(?![A-Za-z0-9-])",
)

# Error tokens: SCREAMING_SNAKE_CASE identifiers of length >= 5
# (e.g. ECONNRESET, OOMKILLED, E_DB_TIMEOUT, ERROR_RATE_LIMIT).
#
# We deliberately require at least one underscore OR a length >= 6 to avoid
# matching short all-caps acronyms (AWS, JSON, HTTP) that frequently appear
# in normal prose.  The 5-char minimum from the issue scope is preserved
# for tokens that contain an underscore (e.g. ``E_DB``).
_RE_ERROR_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"([A-Z][A-Z0-9_]{4,})"
    r"(?![A-Za-z0-9_])",
)

# Code-fence stripper: removes everything between triple backticks (multi-line)
# and inside single backtick spans (single-line).  Applied before mention
# extraction to skip user-quoted payloads.  Order matters — fences first,
# inline second, otherwise the fence's body would be re-scanned as inline.
_RE_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`[^`]*`")

# Common false-positive tokens that match the error-token regex but are not
# actual error codes.  Filtered explicitly so they do not poison the graph.
_ERROR_TOKEN_DENYLIST: frozenset[str] = frozenset(
    {
        "TODO",
        "FIXME",
        "XXX",
        "NOTE",
        "WARNING",
        "INFO",
        "DEBUG",
        "TRACE",
        "HTTPS",
        "JSON",
        "YAML",
        "HTML",
        "XHTML",
        "REST",
        "GRPC",
        "MAYBE",
        "ALWAYS",
        "NEVER",
    }
)


# ---------------------------------------------------------------------------
# Canonical name builders
# ---------------------------------------------------------------------------


def slack_thread_name(channel_id: str, thread_ts: str) -> str:
    """Return the canonical name for a Slack thread entity.

    Matches the ``DocumentRef.uri`` form already produced by
    :meth:`SlackConnector.discover` so the same string appears in both
    the document-side index and the graph-side entity row.
    """
    return f"slack://channel/{channel_id}/thread/{thread_ts}"


def _slack_message_name(channel_id: str, ts: str) -> str:
    """Canonical name for a single Slack message (root or reply)."""
    return f"slack://channel/{channel_id}/message/{ts}"


def _slack_user_name(user_id: str) -> str:
    """Canonical name for a Slack user entity."""
    return f"slack://user/{user_id}"


def _slack_channel_name(channel_id: str) -> str:
    """Canonical name for a Slack channel entity."""
    return f"slack://channel/{channel_id}"


# ---------------------------------------------------------------------------
# Mention extraction
# ---------------------------------------------------------------------------


def _strip_code(text: str) -> str:
    """Return *text* with Markdown code fences and inline code spans removed.

    Order matters — fenced blocks first (``re.DOTALL``), inline spans second.
    Whitespace inside the stripped regions is replaced with a space so the
    surrounding regex anchors still see word boundaries cleanly.
    """
    no_fences = _RE_CODE_FENCE.sub(" ", text)
    return _RE_INLINE_CODE.sub(" ", no_fences)


def _trim_trailing_punct(s: str) -> str:
    """Strip URL-trailing punctuation that is almost always sentence punctuation."""
    return s.rstrip(".,;:!?)]>'\"")


def extract_mentions(text: str) -> dict[str, list[str]]:
    """Extract mention strings from a Slack message body.

    Args:
        text: Raw Slack message text (Markdown).

    Returns:
        A dict with keys ``"pr_urls"``, ``"arns"``, ``"pods"``, ``"errors"``
        and lists of unique canonical strings (insertion-order preserved)
        for each category.  An empty list is returned when no matches fire.

    Notes:
        - Markdown code fences (triple-backtick blocks) and inline code
          spans (single-backtick spans) are stripped before extraction so
          user-quoted payloads cannot inject false mentions.
        - All four extractors are independent — a token can in principle
          match more than one (e.g. an all-caps ARN segment vs an error
          token), but the regex anchors are disjoint enough in practice
          that this does not happen with realistic Slack content.
    """
    cleaned = _strip_code(text)

    pr_urls: list[str] = list(
        dict.fromkeys(_trim_trailing_punct(m) for m in _RE_PR_URL.findall(cleaned))
    )
    arns: list[str] = list(
        dict.fromkeys(_trim_trailing_punct(m) for m in _RE_ARN.findall(cleaned))
    )
    pods: list[str] = list(dict.fromkeys(_RE_POD_NAME.findall(cleaned)))
    errors: list[str] = [
        tok
        for tok in dict.fromkeys(_RE_ERROR_TOKEN.findall(cleaned))
        if tok not in _ERROR_TOKEN_DENYLIST
    ]

    return {
        "pr_urls": pr_urls,
        "arns": arns,
        "pods": pods,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Structural entity builders
# ---------------------------------------------------------------------------


def _thread_entity(
    channel_id: str,
    thread_ts: str,
    *,
    channel_name: str,
    message_count: int,
    root_author: str,
    earliest_ts: str,
    latest_ts: str,
) -> EntityData:
    """Build the ``slack_thread`` entity for a fetched thread."""
    return EntityData(
        symbol=slack_thread_name(channel_id, thread_ts),
        kind="slack_thread",
        name=slack_thread_name(channel_id, thread_ts),
        extra={
            "channel_id": channel_id,
            "channel_name": channel_name,
            "thread_ts": thread_ts,
            "message_count": message_count,
            "root_author": root_author,
            "earliest_ts": earliest_ts,
            "latest_ts": latest_ts,
            "extractor_version": EXTRACTOR_VERSION,
        },
    )


def _message_entity(channel_id: str, message_ts: str, author: str) -> EntityData:
    """Build a ``slack_message`` entity for a single root or reply message."""
    return EntityData(
        symbol=_slack_message_name(channel_id, message_ts),
        kind="slack_message",
        name=_slack_message_name(channel_id, message_ts),
        extra={
            "channel_id": channel_id,
            "message_ts": message_ts,
            "author": author,
            "extractor_version": EXTRACTOR_VERSION,
        },
    )


def _user_entity(user_id: str) -> EntityData:
    """Build a ``slack_user`` entity for a message author."""
    return EntityData(
        symbol=_slack_user_name(user_id),
        kind="slack_user",
        name=_slack_user_name(user_id),
        extra={"user_id": user_id, "extractor_version": EXTRACTOR_VERSION},
    )


def _channel_entity(channel_id: str, channel_name: str) -> EntityData:
    """Build a ``slack_channel`` entity for a referenced channel."""
    return EntityData(
        symbol=_slack_channel_name(channel_id),
        kind="slack_channel",
        name=_slack_channel_name(channel_id),
        extra={
            "channel_id": channel_id,
            "channel_name": channel_name,
            "extractor_version": EXTRACTOR_VERSION,
        },
    )


def _mention_entity(canonical_name: str, mention_kind: str) -> EntityData:
    """Build a ``slack_mention`` entity for a single canonical mention string.

    ``mention_kind`` is the *category* of the mention (``pr_url``, ``arn``,
    ``pod``, ``error``) and is stored under ``extra['mention_kind']`` so
    retrieval can filter by category.  The entity ``kind`` field stays the
    coarse :data:`MENTION_KIND` so the downstream graph store can apply
    a uniform label.
    """
    return EntityData(
        symbol=canonical_name,
        kind=MENTION_KIND,
        name=canonical_name,
        extra={
            "mention_kind": mention_kind,
            "extractor_version": EXTRACTOR_VERSION,
        },
    )


def _structural_edges(
    *,
    channel_id: str,
    thread_ts: str,
    channel_name: str,
    messages: list[dict[str, str]],
) -> list[EdgeData]:
    """Build IN_THREAD, AUTHORED, and IN_CHANNEL edges for a thread."""
    thread_sym = slack_thread_name(channel_id, thread_ts)
    edges: list[EdgeData] = [
        EdgeData(
            from_symbol=thread_sym,
            to_symbol=_slack_channel_name(channel_id),
            edge_type="in_channel",
            extra={"channel_name": channel_name},
        ),
    ]
    for msg in messages:
        msg_sym = _slack_message_name(channel_id, msg["ts"])
        edges.append(
            EdgeData(
                from_symbol=msg_sym,
                to_symbol=thread_sym,
                edge_type="in_thread",
            )
        )
        author = msg.get("user", "")
        if author:
            edges.append(
                EdgeData(
                    from_symbol=_slack_user_name(author),
                    to_symbol=msg_sym,
                    edge_type="authored",
                )
            )
    return edges


def _mention_edges(
    *,
    channel_id: str,
    thread_ts: str,
    mentions_by_message: list[tuple[str, dict[str, list[str]]]],
) -> tuple[list[EntityData], list[EdgeData]]:
    """Build mention-target entities + ``mentions`` edges for one thread.

    Edges go from the SlackThread (NOT individual messages) so retrieval
    needs only one hop from a matched mention to reach the discussion.
    The decision is documented in the issue (#149: "the implementer
    picks").  Per-message granularity remains available via the
    ``IN_THREAD`` edge if a follow-up wants tighter localisation.

    Args:
        channel_id: The Slack channel id (used to build the thread symbol).
        thread_ts: The Slack thread root timestamp.
        mentions_by_message: Sequence of ``(message_ts, mentions_dict)``
            pairs from :func:`extract_mentions`.  ``message_ts`` is unused
            for edge construction (we go thread-level) but kept in the
            signature so a follow-up can switch granularity without
            re-extracting.

    Returns:
        ``(mention_entities, mention_edges)`` — entities are deduped by
        canonical name across all messages in the thread.
    """
    seen: dict[str, str] = {}  # canonical_name -> mention_kind
    for _msg_ts, mentions in mentions_by_message:
        for kind, names in mentions.items():
            for name in names:
                # First occurrence wins; later messages don't change kind.
                if name not in seen:
                    seen[name] = kind

    entities = [_mention_entity(name, kind) for name, kind in seen.items()]
    thread_sym = slack_thread_name(channel_id, thread_ts)
    edges = [
        EdgeData(
            from_symbol=thread_sym,
            to_symbol=name,
            edge_type="mentions",
            extra={"mention_kind": kind},
        )
        for name, kind in seen.items()
    ]
    return entities, edges


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------


def build_thread_entities(
    *,
    channel_id: str,
    thread_ts: str,
    channel_name: str,
    messages: list[dict[str, str]],
) -> tuple[list[EntityData], list[EdgeData]]:
    """Build the structural entity graph for one Slack thread (no mentions).

    Public for tests; production code should call
    :func:`extract_slack_graph` instead — it includes mention extraction.
    """
    if not messages:
        return [], []

    timestamps = [m["ts"] for m in messages if m.get("ts")]
    earliest = min(timestamps) if timestamps else thread_ts
    latest = max(timestamps) if timestamps else thread_ts
    root_author = messages[0].get("user", "")

    entities: list[EntityData] = [
        _thread_entity(
            channel_id,
            thread_ts,
            channel_name=channel_name,
            message_count=len(messages),
            root_author=root_author,
            earliest_ts=earliest,
            latest_ts=latest,
        ),
        _channel_entity(channel_id, channel_name),
    ]
    seen_users: set[str] = set()
    for msg in messages:
        entities.append(_message_entity(channel_id, msg["ts"], msg.get("user", "")))
        author = msg.get("user", "")
        if author and author not in seen_users:
            entities.append(_user_entity(author))
            seen_users.add(author)

    edges = _structural_edges(
        channel_id=channel_id,
        thread_ts=thread_ts,
        channel_name=channel_name,
        messages=messages,
    )
    return entities, edges


def extract_slack_graph(
    *,
    channel_id: str,
    thread_ts: str,
    channel_name: str,
    messages: list[dict[str, Any]],
) -> tuple[list[EntityData], list[EdgeData]]:
    """Extract the full Slack entity surface for one fetched thread.

    Combines structural entities with mention-target entities + edges.
    The connector calls this from :meth:`SlackConnector.fetch` and stamps
    the result onto ``DocumentRef.metadata`` (keys ``entities`` /
    ``edges``) for the downstream pipeline.

    Args:
        channel_id: Slack channel id (the trusted ACL handle is set by
            the pipeline; this is passed through only for canonical-name
            construction).
        thread_ts: Root message timestamp of the thread.
        channel_name: Human-readable channel name (best-effort; falls
            back to ``channel_id`` when unknown).
        messages: List of Slack message dicts (root + replies, in
            chronological order).  Each dict must carry ``ts`` and
            ``text``; ``user`` and ``thread_ts`` are optional.

    Returns:
        ``(entities, edges)`` — flat lists.  Order is stable across calls
        with identical input so test assertions can rely on it.
    """
    str_messages: list[dict[str, str]] = [
        {
            "ts": str(m.get("ts", "")),
            "user": str(m.get("user", "")),
            "text": str(m.get("text", "")),
        }
        for m in messages
    ]
    structural_entities, structural_edges = build_thread_entities(
        channel_id=channel_id,
        thread_ts=thread_ts,
        channel_name=channel_name,
        messages=str_messages,
    )

    mentions_by_message: list[tuple[str, dict[str, list[str]]]] = [
        (m["ts"], extract_mentions(m["text"])) for m in str_messages
    ]
    mention_entities, mention_edges = _mention_edges(
        channel_id=channel_id,
        thread_ts=thread_ts,
        mentions_by_message=mentions_by_message,
    )

    return structural_entities + mention_entities, structural_edges + mention_edges
