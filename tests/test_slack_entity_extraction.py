"""Slack entity / mention extraction tests (issue #149).

Covers the four mention extractors (PR URL, ARN, pod name, error token),
the structural entity surface (slack_thread / slack_message / slack_user
/ slack_channel), the canonical-name shape, and the false-positive
handling for code fences and inline code spans.

The cross-workspace isolation property is asserted in
:mod:`tests.test_slack_connector_entities` (integration scope) — this
file keeps to pure-function unit coverage.
"""

from __future__ import annotations

from omniscience_connectors.slack.entities import (
    EXTRACTOR_VERSION,
    MENTION_KIND,
    EdgeData,
    EntityData,
    build_thread_entities,
    extract_mentions,
    extract_slack_graph,
    slack_thread_name,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ents_by_kind(entities: list[EntityData], kind: str) -> list[EntityData]:
    return [e for e in entities if e.kind == kind]


def _names(entities: list[EntityData]) -> list[str]:
    return [e.name for e in entities]


def _edge_pairs(edges: list[EdgeData], edge_type: str) -> list[tuple[str, str]]:
    return [(e.from_symbol, e.to_symbol) for e in edges if e.edge_type == edge_type]


# ===========================================================================
# Mention extraction — PR URLs
# ===========================================================================


class TestPrUrlExtraction:
    def test_clean_url_in_prose(self) -> None:
        text = "See https://github.com/foo/bar/pull/42 for the fix."
        out = extract_mentions(text)
        assert out["pr_urls"] == ["https://github.com/foo/bar/pull/42"]

    def test_url_with_query_string_keeps_base(self) -> None:
        # The canonical name emitted MUST be the bare URL form so it
        # matches what the GitHub PR/MR connector emits as PR.name.
        text = "Diff at https://github.com/foo/bar/pull/42?diff=split now."
        out = extract_mentions(text)
        assert out["pr_urls"] == ["https://github.com/foo/bar/pull/42"]

    def test_url_inside_markdown_link(self) -> None:
        text = "[the fix](https://github.com/foo/bar/pull/42)"
        out = extract_mentions(text)
        assert out["pr_urls"] == ["https://github.com/foo/bar/pull/42"]

    def test_repeated_url_is_deduped(self) -> None:
        text = "See https://github.com/foo/bar/pull/42 . Again: https://github.com/foo/bar/pull/42"
        out = extract_mentions(text)
        assert out["pr_urls"] == ["https://github.com/foo/bar/pull/42"]

    def test_url_inside_code_fence_is_skipped(self) -> None:
        # Code fences carry user-pasted content that must not pollute the
        # graph (issue #149 false-positive requirement).
        text = "```\nhttps://github.com/foo/bar/pull/9999\n```"
        out = extract_mentions(text)
        assert out["pr_urls"] == []

    def test_inline_code_span_is_skipped(self) -> None:
        text = "Don't reference `https://github.com/foo/bar/pull/9999` directly."
        out = extract_mentions(text)
        assert out["pr_urls"] == []

    def test_non_pull_path_is_ignored(self) -> None:
        # Issues, commits, blob URLs are explicitly NOT in scope for #149.
        text = "https://github.com/foo/bar/issues/42"
        out = extract_mentions(text)
        assert out["pr_urls"] == []


# ===========================================================================
# Mention extraction — ARNs
# ===========================================================================


class TestArnExtraction:
    def test_s3_bucket_arn(self) -> None:
        text = "Bucket arn:aws:s3:::pilot-logs is empty."
        out = extract_mentions(text)
        assert out["arns"] == ["arn:aws:s3:::pilot-logs"]

    def test_iam_role_arn(self) -> None:
        text = "Assume arn:aws:iam::123456789012:role/EKSClusterRole then."
        out = extract_mentions(text)
        assert out["arns"] == ["arn:aws:iam::123456789012:role/EKSClusterRole"]

    def test_ec2_instance_arn(self) -> None:
        text = "Instance arn:aws:ec2:us-east-1:123456789012:instance/i-0abc123."
        out = extract_mentions(text)
        assert out["arns"] == ["arn:aws:ec2:us-east-1:123456789012:instance/i-0abc123"]

    def test_china_partition_arn(self) -> None:
        text = "China region: arn:aws-cn:s3:::cn-bucket"
        out = extract_mentions(text)
        assert out["arns"] == ["arn:aws-cn:s3:::cn-bucket"]

    def test_arn_inside_code_fence_skipped(self) -> None:
        # The classic adversarial case: a user pastes an example ARN block.
        text = "Example payload:\n```\narn:aws:iam::000000000000:user/example\n```"
        out = extract_mentions(text)
        assert out["arns"] == []

    def test_arn_inside_inline_code_skipped(self) -> None:
        text = "The format is `arn:aws:s3:::example-bucket` — never paste real ones."
        out = extract_mentions(text)
        assert out["arns"] == []

    def test_bare_arn_colon_does_not_match(self) -> None:
        # ``arn:`` followed by whitespace is sentence prose, not an ARN.
        text = "The arn: format is documented at AWS."
        out = extract_mentions(text)
        assert out["arns"] == []


# ===========================================================================
# Mention extraction — pod names
# ===========================================================================


class TestPodNameExtraction:
    def test_classic_replicaset_pod_name(self) -> None:
        text = "Pod web-frontend-7d9f8b4c8-xk5pq is in CrashLoopBackOff."
        out = extract_mentions(text)
        assert out["pods"] == ["web-frontend-7d9f8b4c8-xk5pq"]

    def test_multi_segment_deployment_name(self) -> None:
        text = "checking api-backend-v2-66dccfb9b8-z2w8m logs"
        out = extract_mentions(text)
        assert out["pods"] == ["api-backend-v2-66dccfb9b8-z2w8m"]

    def test_pod_inside_code_fence_skipped(self) -> None:
        text = "```\nweb-frontend-7d9f8b4c8-xk5pq\n```"
        out = extract_mentions(text)
        assert out["pods"] == []

    def test_no_match_for_short_hash(self) -> None:
        # Hash segment must be >= 8 hex chars; deployment-only names are
        # not reliable pod names so we reject them.
        text = "web-frontend-abc-xk5pq"
        out = extract_mentions(text)
        assert out["pods"] == []

    def test_no_match_for_uppercase(self) -> None:
        # K8s pod names are always lowercase per RFC 1123.
        text = "Web-Frontend-7d9f8b4c8-XK5PQ"
        out = extract_mentions(text)
        assert out["pods"] == []


# ===========================================================================
# Mention extraction — error tokens
# ===========================================================================


class TestErrorTokenExtraction:
    def test_classic_econnreset(self) -> None:
        text = "Got ECONNRESET from upstream at 14:23 UTC."
        out = extract_mentions(text)
        assert "ECONNRESET" in out["errors"]

    def test_oomkilled(self) -> None:
        text = "Pod was OOMKILLED last night."
        out = extract_mentions(text)
        assert "OOMKILLED" in out["errors"]

    def test_underscore_token(self) -> None:
        text = "Saw E_DB_TIMEOUT three times in a row."
        out = extract_mentions(text)
        assert "E_DB_TIMEOUT" in out["errors"]

    def test_denylist_filters_common_words(self) -> None:
        # Placeholder words and common acronyms (HTTPS/JSON) match the regex but are not errors
        # codes — they would otherwise pollute every Slack thread.
        text = "TODO check this. Use HTTPS. Parse JSON output."
        out = extract_mentions(text)
        assert out["errors"] == []

    def test_short_acronym_rejected(self) -> None:
        # Length < 5 — single-token acronyms like AWS, FOO are noise.
        text = "AWS is fine. FOO is a placeholder."
        out = extract_mentions(text)
        assert out["errors"] == []

    def test_error_inside_code_fence_skipped(self) -> None:
        # Counter-example: an error inside a fence is treated as quoted
        # diagnostic output and skipped (issue #149: code-fence false-
        # positive rejection requirement).  Decision documented in the
        # extractor module.
        text = "```\npanic: ECONNRESET\n```"
        out = extract_mentions(text)
        assert out["errors"] == []


# ===========================================================================
# Structural entities + canonical names
# ===========================================================================


class TestStructuralEntities:
    _CHANNEL = "C123"
    _THREAD_TS = "1700000000.000100"
    _CHANNEL_NAME = "incidents"

    def _messages(self) -> list[dict[str, str]]:
        return [
            {"ts": self._THREAD_TS, "user": "U_ALICE", "text": "Issue spotted."},
            {"ts": "1700000060.000200", "user": "U_BOB", "text": "Looking now."},
            {"ts": "1700000120.000300", "user": "U_ALICE", "text": "Fixed in PR."},
        ]

    def test_slack_thread_canonical_name(self) -> None:
        # The canonical name MUST equal the URI form already used by
        # SlackConnector.discover so the document-side index and graph
        # entity row carry the same string (linker exact_name relies on it).
        name = slack_thread_name(self._CHANNEL, self._THREAD_TS)
        assert name == "slack://channel/C123/thread/1700000000.000100"

    def test_thread_entity_emitted_with_canonical_name(self) -> None:
        entities, _edges = build_thread_entities(
            channel_id=self._CHANNEL,
            thread_ts=self._THREAD_TS,
            channel_name=self._CHANNEL_NAME,
            messages=self._messages(),
        )
        threads = _ents_by_kind(entities, "slack_thread")
        assert len(threads) == 1
        assert threads[0].name == slack_thread_name(self._CHANNEL, self._THREAD_TS)
        assert threads[0].extra["message_count"] == 3
        assert threads[0].extra["root_author"] == "U_ALICE"
        assert threads[0].extra["earliest_ts"] == self._THREAD_TS
        assert threads[0].extra["latest_ts"] == "1700000120.000300"
        assert threads[0].extra["extractor_version"] == EXTRACTOR_VERSION

    def test_messages_users_channel_emitted(self) -> None:
        entities, _ = build_thread_entities(
            channel_id=self._CHANNEL,
            thread_ts=self._THREAD_TS,
            channel_name=self._CHANNEL_NAME,
            messages=self._messages(),
        )
        assert len(_ents_by_kind(entities, "slack_message")) == 3
        # Two distinct users: U_ALICE (twice) and U_BOB — dedup by user_id.
        users = _ents_by_kind(entities, "slack_user")
        assert sorted(_names(users)) == ["slack://user/U_ALICE", "slack://user/U_BOB"]
        channels = _ents_by_kind(entities, "slack_channel")
        assert _names(channels) == ["slack://channel/C123"]

    def test_structural_edges(self) -> None:
        _ents, edges = build_thread_entities(
            channel_id=self._CHANNEL,
            thread_ts=self._THREAD_TS,
            channel_name=self._CHANNEL_NAME,
            messages=self._messages(),
        )
        thread_sym = slack_thread_name(self._CHANNEL, self._THREAD_TS)
        # Three IN_THREAD edges (one per message).
        in_thread = _edge_pairs(edges, "in_thread")
        assert len(in_thread) == 3
        assert all(to == thread_sym for _from, to in in_thread)

        # One IN_CHANNEL edge thread -> channel.
        in_channel = _edge_pairs(edges, "in_channel")
        assert in_channel == [(thread_sym, "slack://channel/C123")]

        # AUTHORED edges: 3 messages, each one authored by its user.
        authored = _edge_pairs(edges, "authored")
        assert len(authored) == 3


# ===========================================================================
# extract_slack_graph (top-level): mentions become entities + cross-ref edges
# ===========================================================================


class TestSlackGraphMentions:
    _CHANNEL = "C_INC"
    _THREAD_TS = "1700001000.000100"
    _CHANNEL_NAME = "incidents"

    def test_pr_mention_creates_named_entity_and_edge(self) -> None:
        messages = [
            {
                "ts": self._THREAD_TS,
                "user": "U_SRE",
                "text": "Probably https://github.com/acme/api/pull/42",
            }
        ]
        entities, edges = extract_slack_graph(
            channel_id=self._CHANNEL,
            thread_ts=self._THREAD_TS,
            channel_name=self._CHANNEL_NAME,
            messages=messages,
        )
        mentions = _ents_by_kind(entities, MENTION_KIND)
        # The mention entity carries the canonical PR URL as its name.
        # The linker's exact_name strategy will match this against the
        # GitHub PR/MR connector's PR entity (issue #150) which uses the
        # same string as ``name``.
        assert _names(mentions) == ["https://github.com/acme/api/pull/42"]
        assert mentions[0].extra["mention_kind"] == "pr_urls"

        thread_sym = slack_thread_name(self._CHANNEL, self._THREAD_TS)
        mention_edges = _edge_pairs(edges, "mentions")
        assert mention_edges == [(thread_sym, "https://github.com/acme/api/pull/42")]

    def test_arn_pod_error_in_one_thread(self) -> None:
        # Mixed-mention thread.  Three categories must produce three
        # mention-target entities + three thread->mention edges.
        messages = [
            {
                "ts": self._THREAD_TS,
                "user": "U_SRE",
                "text": (
                    "Pod web-frontend-7d9f8b4c8-xk5pq is OOMKILLED. "
                    "Resource: arn:aws:ec2:us-east-1:111122223333:instance/i-0abc."
                ),
            }
        ]
        entities, edges = extract_slack_graph(
            channel_id=self._CHANNEL,
            thread_ts=self._THREAD_TS,
            channel_name=self._CHANNEL_NAME,
            messages=messages,
        )
        mention_names = sorted(_names(_ents_by_kind(entities, MENTION_KIND)))
        assert mention_names == sorted(
            [
                "arn:aws:ec2:us-east-1:111122223333:instance/i-0abc",
                "OOMKILLED",
                "web-frontend-7d9f8b4c8-xk5pq",
            ]
        )
        assert len(_edge_pairs(edges, "mentions")) == 3

    def test_no_mentions_yields_only_structural(self) -> None:
        # Sanity: a thread with no canonical strings still emits structural
        # entities (thread/message/user/channel) but no mention entities.
        messages = [{"ts": self._THREAD_TS, "user": "U_SRE", "text": "Looking into it."}]
        entities, edges = extract_slack_graph(
            channel_id=self._CHANNEL,
            thread_ts=self._THREAD_TS,
            channel_name=self._CHANNEL_NAME,
            messages=messages,
        )
        assert _ents_by_kind(entities, MENTION_KIND) == []
        assert _edge_pairs(edges, "mentions") == []
        # Structural sanity: thread + channel + 1 message + 1 user = 4.
        assert len(entities) == 4

    def test_mention_dedup_across_messages(self) -> None:
        # The same PR URL mentioned in two messages produces ONE mention
        # entity and ONE edge (the linker would create one cross_ref edge
        # anyway, but we keep the Slack-side graph clean too).
        messages = [
            {
                "ts": self._THREAD_TS,
                "user": "U1",
                "text": "Fix at https://github.com/acme/api/pull/42",
            },
            {
                "ts": "1700001060.000200",
                "user": "U2",
                "text": "Confirmed: https://github.com/acme/api/pull/42 worked",
            },
        ]
        entities, edges = extract_slack_graph(
            channel_id=self._CHANNEL,
            thread_ts=self._THREAD_TS,
            channel_name=self._CHANNEL_NAME,
            messages=messages,
        )
        mentions = _ents_by_kind(entities, MENTION_KIND)
        assert len(mentions) == 1
        assert len(_edge_pairs(edges, "mentions")) == 1
