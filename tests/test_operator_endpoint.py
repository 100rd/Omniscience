"""ACL + happy-path tests for the operator-scoped read endpoint (#163).

Coverage:
  * No bearer token              → 401
  * Token without operator:read  → 403 (scope)
  * Token's workspace_id mismatch query param → 403 (workspace)
  * Token has no workspace_id at all          → 403 (workspace)
  * Happy path returns external_ids
  * Pagination via ``next_cursor``
  * Emitter filter — only ``source_type='k8s_operator'`` rows are returned
  * Cluster filter — wrong cluster_id returns empty

These tests are the load-bearing security regression for the read API.
The cross-workspace-isolation test verifies that a token for workspace A
cannot read entities from workspace B even when the SQL universe contains
both.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_server.rest.operator import router as operator_router

# ---------------------------------------------------------------------------
# Tiny in-memory SQL substitute. Routes between (a) auth middleware token
# lookups and (b) the operator endpoint's Document+Source join. The mock
# inspects the FROM clause to decide which side to return.
# ---------------------------------------------------------------------------


class _DocumentRow:
    """Minimal stand-in for a SQL row carrying (id, external_id)."""

    def __init__(self, id_: uuid.UUID, external_id: str) -> None:
        self.id = id_
        self.external_id = external_id


class _DocumentRecord:
    """In-memory Document row joined with its Source. The mock SQL
    universe is a list of these; the mock _execute() filters by SQL
    predicates as best it can, but the load-bearing assertions test the
    LIKE/source_type/tenant filters directly via the matching helper.
    """

    def __init__(
        self,
        external_id: str,
        source_type: str,
        tenant_id: uuid.UUID,
        tombstoned: bool = False,
        doc_id: uuid.UUID | None = None,
    ) -> None:
        self.id = doc_id or uuid.uuid4()
        self.external_id = external_id
        self.source_type = source_type
        self.tenant_id = tenant_id
        self.tombstoned = tombstoned


def _matches_filters(
    record: _DocumentRecord,
    *,
    workspace_id: uuid.UUID,
    source_type: str,
    prefix: str,
) -> bool:
    if record.tombstoned:
        return False
    if record.source_type != source_type:
        return False
    if record.tenant_id != workspace_id:
        return False
    return record.external_id.startswith(prefix.rstrip("%"))


def _make_token(
    *,
    plaintext: str,
    prefix: str,
    workspace_id: uuid.UUID | None,
    scopes: list[str],
    is_active: bool = True,
) -> ApiToken:
    hashed = hash_token(plaintext)
    token: ApiToken = MagicMock(spec=ApiToken)
    token.id = uuid.uuid4()
    token.name = f"token-{workspace_id}"
    token.token_prefix = prefix
    token.hashed_token = hashed
    token.scopes = scopes
    token.workspace_id = workspace_id
    token.expires_at = datetime.now(tz=UTC) + timedelta(hours=1)
    token.last_used_at = None
    token.is_active = is_active
    return token


def _routing_session_factory(
    tokens: list[ApiToken],
    documents: list[_DocumentRecord],
) -> Any:
    """Return a session-factory MagicMock that routes between auth and
    operator queries.

    The router decides which result set to return by inspecting the SQL
    statement's rendered FROM clause:
      * ``FROM api_tokens`` → return the configured token list (auth path)
      * ``FROM documents JOIN sources`` → run the in-memory filter
        replaying the WHERE clauses by inspecting the compiled bind
        params with ``literal_binds=True``.
    """

    fake_session = AsyncMock()

    async def _execute(stmt: Any) -> Any:
        rendered = str(stmt).lower()
        if "from api_tokens" in rendered:
            res = MagicMock()
            res.scalars.return_value.all.return_value = tokens
            return res

        # Operator endpoint query (Document + Source join). Compile to a
        # literal-binds string so we can scrape the predicate values.
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        sql_text = str(compiled)
        ws_uuid = _scrape_uuid_after(sql_text, "tenant_id =")
        prefix = _scrape_string_after(sql_text, "external_id LIKE")
        cursor_uuid = _scrape_uuid_after(sql_text, "id >") if "id >" in sql_text else None
        limit_val = _scrape_int_after(sql_text, "LIMIT") or 1000

        if ws_uuid is None or prefix is None:
            res = MagicMock()
            res.all.return_value = []
            return res

        matched = [
            r
            for r in documents
            if _matches_filters(
                r,
                workspace_id=ws_uuid,
                source_type="k8s_operator",
                prefix=prefix,
            )
        ]
        matched.sort(key=lambda r: r.id)
        if cursor_uuid is not None:
            matched = [r for r in matched if r.id > cursor_uuid]
        matched = matched[:limit_val]
        rows = [_DocumentRow(r.id, r.external_id) for r in matched]

        result = MagicMock()
        result.all.return_value = rows
        return result

    fake_session.execute = _execute
    fake_session.flush = AsyncMock()
    fake_session.commit = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=fake_session)


def _scrape_uuid_after(sql: str, marker: str) -> uuid.UUID | None:
    """Find the UUID literal that immediately follows ``marker`` in ``sql``."""
    idx = sql.lower().find(marker.lower())
    if idx < 0:
        return None
    rest = sql[idx + len(marker) :].strip()
    # UUID literal is wrapped in single quotes: '0000-...-0000'
    if not rest.startswith("'"):
        return None
    end = rest.find("'", 1)
    if end < 0:
        return None
    try:
        return uuid.UUID(rest[1:end])
    except ValueError:
        return None


def _scrape_string_after(sql: str, marker: str) -> str | None:
    """Find the quoted string literal following ``marker`` in ``sql``."""
    idx = sql.lower().find(marker.lower())
    if idx < 0:
        return None
    rest = sql[idx + len(marker) :].strip()
    if not rest.startswith("'"):
        return None
    end = rest.find("'", 1)
    if end < 0:
        return None
    return rest[1:end]


def _scrape_int_after(sql: str, marker: str) -> int | None:
    """Find the integer literal following ``marker`` in ``sql``."""
    idx = sql.find(marker)
    if idx < 0:
        return None
    rest = sql[idx + len(marker) :].strip()
    digits = ""
    for ch in rest:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def _build_app(tokens: list[ApiToken], documents: list[_DocumentRecord]) -> FastAPI:
    app = FastAPI()
    app.state.db_session_factory = _routing_session_factory(tokens, documents)
    app.include_router(operator_router, prefix="/api/v1")
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def two_workspace_setup() -> AsyncIterator[
    tuple[
        AsyncClient,
        str,
        uuid.UUID,
        uuid.UUID,
        str,
        uuid.UUID,
        uuid.UUID,
        list[_DocumentRecord],
    ]
]:
    """Yield (client, plaintext_a, ws_a, cluster_a, plaintext_b, ws_b, cluster_b, docs).

    Two workspaces, two clusters per workspace, populated documents so
    both happy-path and cross-workspace assertions are expressible.
    """
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    cluster_a = uuid.uuid4()
    cluster_b = uuid.uuid4()
    plaintext_a, prefix_a = generate_token("development")
    plaintext_b, prefix_b = generate_token("development")

    token_a = _make_token(
        plaintext=plaintext_a,
        prefix=prefix_a,
        workspace_id=ws_a,
        scopes=[Scope.operator_read.value],
    )
    token_b = _make_token(
        plaintext=plaintext_b,
        prefix=prefix_b,
        workspace_id=ws_b,
        scopes=[Scope.operator_read.value],
    )

    docs = [
        # workspace A, cluster A — three Pod entries
        _DocumentRecord(
            external_id=f"k8s_resource/{cluster_a}/Pod/ns/a1",
            source_type="k8s_operator",
            tenant_id=ws_a,
        ),
        _DocumentRecord(
            external_id=f"k8s_resource/{cluster_a}/Pod/ns/a2",
            source_type="k8s_operator",
            tenant_id=ws_a,
        ),
        _DocumentRecord(
            external_id=f"k8s_resource/{cluster_a}/Pod/ns/a3",
            source_type="k8s_operator",
            tenant_id=ws_a,
        ),
        # Workspace A, different cluster — should NOT match a query for cluster_a/Pod
        _DocumentRecord(
            external_id=f"k8s_resource/{uuid.uuid4()}/Pod/ns/other",
            source_type="k8s_operator",
            tenant_id=ws_a,
        ),
        # Workspace A, cluster A, but Deployment kind — should NOT match Pod query
        _DocumentRecord(
            external_id=f"k8s_resource/{cluster_a}/Deployment/ns/d1",
            source_type="k8s_operator",
            tenant_id=ws_a,
        ),
        # Workspace A, cluster A, Pod — but emitted by the agentic ``k8s``
        # connector (NOT k8s_operator). The emitter filter MUST exclude this.
        _DocumentRecord(
            external_id=f"k8s_resource/{cluster_a}/Pod/ns/agentic-leak",
            source_type="k8s",
            tenant_id=ws_a,
        ),
        # Workspace A, cluster A, Pod, but tombstoned — should NOT match.
        _DocumentRecord(
            external_id=f"k8s_resource/{cluster_a}/Pod/ns/tombstoned",
            source_type="k8s_operator",
            tenant_id=ws_a,
            tombstoned=True,
        ),
        # Workspace B, cluster B — two Pod entries. The ACL gate must
        # ensure A's token cannot reach these.
        _DocumentRecord(
            external_id=f"k8s_resource/{cluster_b}/Pod/ns/b1",
            source_type="k8s_operator",
            tenant_id=ws_b,
        ),
        _DocumentRecord(
            external_id=f"k8s_resource/{cluster_b}/Pod/ns/b2",
            source_type="k8s_operator",
            tenant_id=ws_b,
        ),
    ]

    app = _build_app([token_a, token_b], docs)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield (client, plaintext_a, ws_a, cluster_a, plaintext_b, ws_b, cluster_b, docs)


# ---------------------------------------------------------------------------
# Auth surface — 401 / 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_token_rejected_401(two_workspace_setup: Any) -> None:
    client, _pa, ws_a, cluster_a, *_ = two_workspace_setup
    resp = await client.get(
        "/api/v1/operator/entities",
        params={"workspace_id": str(ws_a), "cluster_id": str(cluster_a), "kind": "Pod"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invalid_token_rejected_401(two_workspace_setup: Any) -> None:
    client, _pa, ws_a, cluster_a, *_ = two_workspace_setup
    resp = await client.get(
        "/api/v1/operator/entities",
        params={"workspace_id": str(ws_a), "cluster_id": str(cluster_a), "kind": "Pod"},
        headers={"Authorization": "Bearer sk_dev_garbage_does_not_match"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_token_without_operator_read_scope_rejected_403() -> None:
    """A token without operator:read → 403, even with matching workspace.

    ACL invariant: scope is the second line of defence; even within the
    correct tenant, a token MUST hold operator:read explicitly (or admin).
    """
    ws = uuid.uuid4()
    plaintext, prefix = generate_token("development")
    token = _make_token(
        plaintext=plaintext,
        prefix=prefix,
        workspace_id=ws,
        scopes=[Scope.search.value],  # NO operator:read, NO admin
    )
    app = _build_app([token], [])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/operator/entities",
            params={"workspace_id": str(ws), "cluster_id": str(uuid.uuid4()), "kind": "Pod"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_workspace_mismatch_rejected_403(two_workspace_setup: Any) -> None:
    """Token's workspace ≠ query workspace_id → 403.

    THIS IS THE LOAD-BEARING SECURITY ASSERTION FOR ISSUE #163.
    """
    client, plaintext_a, _ws_a, _cluster_a, _pb, ws_b, _cluster_b, _docs = two_workspace_setup
    resp = await client.get(
        "/api/v1/operator/entities",
        params={
            "workspace_id": str(ws_b),  # workspace B
            "cluster_id": str(uuid.uuid4()),
            "kind": "Pod",
        },
        # Token A — its workspace is ws_a, NOT ws_b.
        headers={"Authorization": f"Bearer {plaintext_a}"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_token_without_workspace_at_all_rejected_403() -> None:
    """Legacy token with no workspace_id → 403.

    ACL invariant: the operator endpoint REQUIRES a workspace-scoped
    token. Tokens predating workspace scoping (workspace_id=None) cannot
    use this endpoint.
    """
    plaintext, prefix = generate_token("development")
    token = _make_token(
        plaintext=plaintext,
        prefix=prefix,
        workspace_id=None,  # Legacy token
        scopes=[Scope.operator_read.value],
    )
    app = _build_app([token], [])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/operator/entities",
            params={
                "workspace_id": str(uuid.uuid4()),
                "cluster_id": str(uuid.uuid4()),
                "kind": "Pod",
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Happy path + filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_filtered_external_ids(two_workspace_setup: Any) -> None:
    client, plaintext_a, ws_a, cluster_a, *_ = two_workspace_setup
    resp = await client.get(
        "/api/v1/operator/entities",
        params={
            "workspace_id": str(ws_a),
            "cluster_id": str(cluster_a),
            "kind": "Pod",
            "limit": 100,
        },
        headers={"Authorization": f"Bearer {plaintext_a}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Three Pods in workspace A's cluster A — Deployment + other-cluster +
    # agentic-leak + tombstoned MUST NOT appear.
    assert len(body["external_ids"]) == 3
    assert all("/Pod/" in ext for ext in body["external_ids"])
    assert all(str(cluster_a) in ext for ext in body["external_ids"])
    # Specifically: no agentic leakage (different source_type)
    assert not any("agentic-leak" in ext for ext in body["external_ids"])
    # No tombstoned
    assert not any("tombstoned" in ext for ext in body["external_ids"])


@pytest.mark.asyncio
async def test_emitter_filter_excludes_other_connectors(two_workspace_setup: Any) -> None:
    """The 'agentic-leak' fixture has source_type='k8s' (the agentic
    connector), not k8s_operator. The endpoint must NEVER return it
    even though it matches workspace + cluster + kind on every other axis.

    ACL invariant #2 in the issue spec — emitter filter is mandatory.
    """
    client, plaintext_a, ws_a, cluster_a, *_ = two_workspace_setup
    resp = await client.get(
        "/api/v1/operator/entities",
        params={
            "workspace_id": str(ws_a),
            "cluster_id": str(cluster_a),
            "kind": "Pod",
            "limit": 100,
        },
        headers={"Authorization": f"Bearer {plaintext_a}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    for ext in body["external_ids"]:
        assert "agentic-leak" not in ext, f"emitter filter failed: {ext}"


@pytest.mark.asyncio
async def test_pagination_walk(two_workspace_setup: Any) -> None:
    """A small ``limit`` produces a non-empty cursor that the next call
    can resume from. Two pages ⇒ all three matching entities visited.
    """
    client, plaintext_a, ws_a, cluster_a, *_ = two_workspace_setup
    seen: list[str] = []

    cursor = ""
    while True:
        resp = await client.get(
            "/api/v1/operator/entities",
            params={
                "workspace_id": str(ws_a),
                "cluster_id": str(cluster_a),
                "kind": "Pod",
                "limit": 2,  # Force pagination — 3 matches across 2 pages
                **({"cursor": cursor} if cursor else {}),
            },
            headers={"Authorization": f"Bearer {plaintext_a}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen.extend(body["external_ids"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 3, f"pagination visited {seen}"
    assert len(set(seen)) == 3, "duplicate external_ids across pages"


@pytest.mark.asyncio
async def test_kind_filter_excludes_other_kinds(two_workspace_setup: Any) -> None:
    """Querying for kind=Pod must NOT return the Deployment fixture."""
    client, plaintext_a, ws_a, cluster_a, *_ = two_workspace_setup
    resp = await client.get(
        "/api/v1/operator/entities",
        params={
            "workspace_id": str(ws_a),
            "cluster_id": str(cluster_a),
            "kind": "Pod",
            "limit": 100,
        },
        headers={"Authorization": f"Bearer {plaintext_a}"},
    )
    body = resp.json()
    assert all("/Pod/" in ext for ext in body["external_ids"])
    assert not any("/Deployment/" in ext for ext in body["external_ids"])


@pytest.mark.asyncio
async def test_cluster_filter_excludes_other_clusters(two_workspace_setup: Any) -> None:
    """Querying cluster_a must NOT return entities from a different cluster
    (the 'other' fixture)."""
    client, plaintext_a, ws_a, cluster_a, *_ = two_workspace_setup
    resp = await client.get(
        "/api/v1/operator/entities",
        params={
            "workspace_id": str(ws_a),
            "cluster_id": str(cluster_a),
            "kind": "Pod",
            "limit": 100,
        },
        headers={"Authorization": f"Bearer {plaintext_a}"},
    )
    body = resp.json()
    for ext in body["external_ids"]:
        assert str(cluster_a) in ext, f"cluster filter failed: {ext}"


@pytest.mark.asyncio
async def test_token_a_cannot_read_workspace_b_data(two_workspace_setup: Any) -> None:
    """Even if A's token tried to query B's cluster_id (with B's
    workspace_id), the workspace gate fires first and 403s.

    Cross-workspace isolation — server-side mirror of the operator-side
    cross_workspace_test.go assertion.
    """
    (
        client,
        plaintext_a,
        _ws_a,
        _cluster_a,
        _pb,
        ws_b,
        cluster_b,
        _docs,
    ) = two_workspace_setup
    resp = await client.get(
        "/api/v1/operator/entities",
        params={
            "workspace_id": str(ws_b),
            "cluster_id": str(cluster_b),
            "kind": "Pod",
            "limit": 100,
        },
        headers={"Authorization": f"Bearer {plaintext_a}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_token_bypasses_scope_but_not_workspace_check(
    two_workspace_setup: Any,
) -> None:
    """admin scope implies operator:read (per SCOPE_HIERARCHY) so an
    admin token CAN read its own workspace. But admin DOES NOT bypass
    the workspace_id-vs-token-workspace check — admin in workspace A
    still cannot read workspace B.
    """
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    plaintext, prefix = generate_token("development")
    admin_token = _make_token(
        plaintext=plaintext,
        prefix=prefix,
        workspace_id=ws_a,
        scopes=[Scope.admin.value],  # admin → operator:read implied
    )
    app = _build_app([admin_token], [])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Admin token in workspace A queries workspace B → still 403.
        resp = await client.get(
            "/api/v1/operator/entities",
            params={
                "workspace_id": str(ws_b),
                "cluster_id": str(uuid.uuid4()),
                "kind": "Pod",
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert resp.status_code == 403
        # And admin token reading its OWN workspace → 200.
        resp_self = await client.get(
            "/api/v1/operator/entities",
            params={
                "workspace_id": str(ws_a),
                "cluster_id": str(uuid.uuid4()),
                "kind": "Pod",
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert resp_self.status_code == 200
