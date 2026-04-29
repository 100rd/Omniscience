"""Slack connector entity-emission integration tests (issue #149).

Asserts that :meth:`SlackConnector.fetch` stamps the extracted entity
surface onto ``DocumentRef.metadata`` for the downstream ingestion
pipeline AND that the ACL invariant holds — workspace identity is
NEVER inferred from the Slack payload.

Existing fetch / discover behaviour is covered in
``tests/test_connectors_v2.py``; this file is additive.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from omniscience_connectors import DocumentRef, SlackConnector
from omniscience_connectors.slack.connector import SlackConfig
from omniscience_connectors.slack.entities import (
    MENTION_KIND,
    EdgeData,
    EntityData,
    slack_thread_name,
)

_SLACK_TOKEN = "xoxb-fake-token"


def _ents(metadata: dict[str, Any]) -> list[EntityData]:
    raw = metadata.get("entities", [])
    assert isinstance(raw, list)
    return raw  # type: ignore[no-any-return]


def _edges(metadata: dict[str, Any]) -> list[EdgeData]:
    raw = metadata.get("edges", [])
    assert isinstance(raw, list)
    return raw  # type: ignore[no-any-return]


def _names_of_kind(entities: list[EntityData], kind: str) -> list[str]:
    return [e.name for e in entities if e.kind == kind]


def _mock_thread(
    *,
    channel_id: str,
    thread_ts: str,
    root_text: str,
    reply_text: str,
    reply_ts: str = "1700000060.000200",
) -> None:
    """Install respx mocks for a single Slack thread fetch."""
    respx.get("https://slack.com/api/conversations.history").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"user": "U_SRE", "ts": thread_ts, "text": root_text},
                ],
            },
        )
    )
    respx.get("https://slack.com/api/conversations.replies").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"user": "U_SRE", "ts": thread_ts, "text": root_text},
                    {"user": "U_DEV", "ts": reply_ts, "text": reply_text},
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            },
        )
    )


# ===========================================================================
# Connector emits entities + edges via ref.metadata
# ===========================================================================


class TestConnectorEntityEmission:
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_stamps_thread_and_mention_entities(self) -> None:
        channel_id = "C_INC"
        thread_ts = "1700000000.000100"
        root_text = "Investigating https://github.com/acme/api/pull/42"
        reply_text = "Pod web-frontend-7d9f8b4c8-xk5pq OOMKILLED. Bucket arn:aws:s3:::acme-logs."
        _mock_thread(
            channel_id=channel_id,
            thread_ts=thread_ts,
            root_text=root_text,
            reply_text=reply_text,
        )

        connector = SlackConnector()
        config = SlackConfig(include_threads=True)
        ref = DocumentRef(
            external_id="abc",
            uri=f"slack://channel/{channel_id}/thread/{thread_ts}",
            metadata={
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "channel_name": "incidents",
            },
        )
        result = await connector.fetch(config, {"bot_token": _SLACK_TOKEN}, ref)

        # Backwards compat: existing Markdown content shape preserved.
        body = result.content_bytes.decode()
        assert "OOMKILLED" in body
        assert "https://github.com/acme/api/pull/42" in body

        ents = _ents(ref.metadata)
        edges = _edges(ref.metadata)

        # SlackThread carries the canonical name shape.
        threads = [e for e in ents if e.kind == "slack_thread"]
        assert len(threads) == 1
        assert threads[0].name == slack_thread_name(channel_id, thread_ts)

        # Four mention-target entities: PR + ARN + pod + OOMKILLED.
        mentions = sorted(_names_of_kind(ents, MENTION_KIND))
        assert mentions == sorted(
            [
                "arn:aws:s3:::acme-logs",
                "https://github.com/acme/api/pull/42",
                "OOMKILLED",
                "web-frontend-7d9f8b4c8-xk5pq",
            ]
        )

        # Each mention has a thread -> mention edge (linker picks them up).
        thread_sym = slack_thread_name(channel_id, thread_ts)
        mention_edges = [(e.from_symbol, e.to_symbol) for e in edges if e.edge_type == "mentions"]
        assert {to for _from, to in mention_edges} == set(mentions)
        assert all(frm == thread_sym for frm, _to in mention_edges)

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_with_no_mentions_emits_only_structural(self) -> None:
        # Backwards compat sanity: a no-mention thread still produces the
        # structural entity surface but no mention-target entities.
        channel_id = "C_OPS"
        thread_ts = "1700000000.000100"
        _mock_thread(
            channel_id=channel_id,
            thread_ts=thread_ts,
            root_text="Looking into it.",
            reply_text="Got it, working now.",
        )

        connector = SlackConnector()
        config = SlackConfig(include_threads=True)
        ref = DocumentRef(
            external_id="abc",
            uri=f"slack://channel/{channel_id}/thread/{thread_ts}",
            metadata={
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "channel_name": "ops",
            },
        )
        await connector.fetch(config, {"bot_token": _SLACK_TOKEN}, ref)

        ents = _ents(ref.metadata)
        assert _names_of_kind(ents, MENTION_KIND) == []
        # 1 thread + 1 channel + 2 messages + 2 users = 6 structural.
        assert len(ents) == 6


# ===========================================================================
# ACL invariant — workspace_id NEVER inferred from Slack payload
# ===========================================================================


class TestWorkspaceAclInvariant:
    """Cross-workspace isolation contract for the Slack entity surface.

    The Slack connector emits entities by NAME only — workspace
    stamping happens later in
    :func:`omniscience_server.ingestion.pipeline._tag_entity_workspace`
    using the ``Source.tenant_id`` resolved by the worker.  This test
    asserts that the connector's emission is workspace-agnostic: two
    different ``Source`` rows pointing at the same Slack thread (same
    payload, same mention strings) emit IDENTICAL entity/edge data
    irrespective of any payload field.

    The actual cross-workspace edge isolation is enforced in the graph
    layer (``test_graphrag_workspace_isolation``); the connector-level
    invariant we assert here is that the connector NEVER reads
    ``team_id``, ``user.team`` or any other payload field as a
    workspace hint.
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_payload_team_id_does_not_leak_into_entities(self) -> None:
        # The Slack message payload includes a ``team`` field; a buggy
        # implementation might use it as a workspace hint.  This test
        # asserts that even when a hostile payload sets ``team`` to a
        # different value than the connector's source-config workspace,
        # the emitted entity names contain ZERO references to team or
        # any payload-derived workspace identity.
        channel_id = "C_HOSTILE"
        thread_ts = "1700000000.000100"

        # Hostile payload: pretends to be from "evil-team" and tries to
        # use a workspace-shaped string in the message body.  None of
        # this should appear in emitted entity names.
        respx.get("https://slack.com/api/conversations.history").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [
                        {
                            "user": "U_HOSTILE",
                            "ts": thread_ts,
                            "team": "T_EVIL",
                            "text": "workspace://evil-team — payload PR ref: "
                            "https://github.com/acme/api/pull/42",
                        }
                    ],
                },
            )
        )
        respx.get("https://slack.com/api/conversations.replies").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [],
                    "has_more": False,
                    "response_metadata": {"next_cursor": ""},
                },
            )
        )

        connector = SlackConnector()
        config = SlackConfig(include_threads=True)
        ref = DocumentRef(
            external_id="x",
            uri=f"slack://channel/{channel_id}/thread/{thread_ts}",
            # The trusted handle here is ``channel_id``; ``team`` from
            # the payload MUST NOT be used.  We deliberately omit any
            # team_id from the metadata to make the contract explicit.
            metadata={
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "channel_name": "hostile",
            },
        )
        await connector.fetch(config, {"bot_token": _SLACK_TOKEN}, ref)

        ents = _ents(ref.metadata)
        # The emitted thread name uses ``channel_id`` (trusted source-
        # config handle), NEVER the payload's ``team`` field.
        thread_names = _names_of_kind(ents, "slack_thread")
        assert thread_names == [slack_thread_name(channel_id, thread_ts)]
        # No entity carries the payload's ``T_EVIL`` team id.
        all_names = " ".join(e.name for e in ents)
        assert "T_EVIL" not in all_names
        # No entity carries any ``workspace_id`` either — that stamp is
        # the ingestion pipeline's responsibility, not the connector's.
        for e in ents:
            assert "workspace_id" not in e.extra

    @respx.mock
    @pytest.mark.asyncio
    async def test_two_workspaces_same_thread_emit_identical_entities(self) -> None:
        # Same Slack thread (channel + ts + content) produces byte-identical
        # entity NAMES regardless of which Source.tenant_id eventually
        # owns them.  The pipeline tags the workspace; the connector does
        # not.  This is the connector-level half of the cross-workspace
        # isolation property; the graph-level half lives in
        # ``test_graphrag_workspace_isolation``.
        channel_id = "C_SHARED"
        thread_ts = "1700000000.000100"
        _mock_thread(
            channel_id=channel_id,
            thread_ts=thread_ts,
            root_text="Same content https://github.com/acme/api/pull/42",
            reply_text="ack",
        )

        connector = SlackConnector()
        config = SlackConfig(include_threads=True)
        # Two refs differing only in external_id (representing two Source
        # rows that both happen to subscribe to the same channel).
        ref_a = DocumentRef(
            external_id="src-A",
            uri=f"slack://channel/{channel_id}/thread/{thread_ts}",
            metadata={
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "channel_name": "shared",
            },
        )
        ref_b = DocumentRef(
            external_id="src-B",
            uri=f"slack://channel/{channel_id}/thread/{thread_ts}",
            metadata={
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "channel_name": "shared",
            },
        )
        await connector.fetch(config, {"bot_token": _SLACK_TOKEN}, ref_a)
        await connector.fetch(config, {"bot_token": _SLACK_TOKEN}, ref_b)

        # Names must be identical between A and B because the connector
        # never reads workspace from the payload.  The ingestion pipeline
        # is the place where the same name in two workspaces becomes two
        # distinct rows scoped by workspace_id.
        names_a = sorted(e.name for e in _ents(ref_a.metadata))
        names_b = sorted(e.name for e in _ents(ref_b.metadata))
        assert names_a == names_b
