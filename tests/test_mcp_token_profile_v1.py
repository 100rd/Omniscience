"""Security contract for the workspace-bound MCP v1 bootstrap token."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.db.models import ApiToken
from omniscience_core.db.schemas import ApiTokenRead

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORKSPACE_ID = uuid.UUID("aaaaaaaa-1111-2222-3333-444400000001")
ADMIN_ID = uuid.UUID("aaaaaaaa-1111-2222-3333-444400000099")


def _token(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "profile_id": "omniscience-mcp-read-v1",
        "scopes": ["search"],
        "workspace_id": WORKSPACE_ID,
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=30),
        "is_active": True,
        "token_prefix": "sk_t",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_valid_profile_is_exactly_workspace_bound_search_and_bounded_expiry() -> None:
    from omniscience_core.auth.tokens import validate_mcp_read_token

    assert validate_mcp_read_token(_token(), now=NOW) == WORKSPACE_ID


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"profile_id": None}, "legacy_token"),
        ({"scopes": ["search", "sources:read"]}, "exact_search_scope_required"),
        ({"workspace_id": None}, "workspace_required"),
        ({"expires_at": None}, "expiry_required"),
        ({"expires_at": NOW + timedelta(days=30, seconds=1)}, "expiry_exceeds_30_days"),
        ({"is_active": False}, "token_revoked"),
    ],
)
def test_profile_rejects_legacy_extra_scope_unbound_overlong_and_revoked_tokens(
    overrides: dict[str, object],
    reason: str,
) -> None:
    from omniscience_core.auth.tokens import McpReadTokenValidationError, validate_mcp_read_token

    with pytest.raises(McpReadTokenValidationError, match=reason):
        validate_mcp_read_token(_token(**overrides), now=NOW)


def test_rotation_overlap_is_capped_at_24_hours() -> None:
    from omniscience_core.auth.tokens import rotation_overlap_expiry

    assert rotation_overlap_expiry(
        NOW,
        NOW + timedelta(days=10),
    ) == NOW + timedelta(hours=24)
    assert rotation_overlap_expiry(
        NOW,
        NOW + timedelta(hours=3),
    ) == NOW + timedelta(hours=3)


def test_profile_validation_rejects_expired_tokens() -> None:
    from omniscience_core.auth.tokens import McpReadTokenValidationError, validate_mcp_read_token

    with pytest.raises(McpReadTokenValidationError, match="token_expired"):
        validate_mcp_read_token(_token(expires_at=NOW - timedelta(seconds=1)), now=NOW)


def _admin_token() -> MagicMock:
    token = MagicMock(spec=ApiToken)
    token.id = ADMIN_ID
    token.token_prefix = "sk_admin"
    token.scopes = ["admin"]
    return token


def _read_model(token_id: uuid.UUID | None = None) -> ApiTokenRead:
    return ApiTokenRead(
        id=token_id or uuid.uuid4(),
        name="omnius",
        token_prefix="sk_read_",
        scopes=["search"],
        profile_id="omniscience-mcp-read-v1",
        rotated_from_id=None,
        workspace_id=WORKSPACE_ID,
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
        last_used_at=None,
        is_active=True,
    )


def _profile_app() -> tuple[FastAPI, AsyncMock]:
    from omniscience_server.routes import tokens as routes

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    app = FastAPI()
    app.state.db_session_factory = MagicMock(return_value=session)
    app.include_router(routes.router)
    app.dependency_overrides[routes._admin_dep.dependency] = _admin_token
    return app, session


@pytest.mark.asyncio
async def test_profile_lifecycle_endpoints_require_admin_auth() -> None:
    from omniscience_server.routes import tokens as routes

    app = FastAPI()
    app.include_router(routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/tokens/profiles/omniscience-mcp-read-v1",
            json={"name": "omnius", "workspace_id": str(WORKSPACE_ID)},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_profile_create_is_exact_and_audited() -> None:
    read_model = _read_model()
    app, session = _profile_app()
    with (
        patch(
            "omniscience_server.routes.tokens.create_mcp_read_token",
            new_callable=AsyncMock,
            return_value=(read_model, "secret-once"),
        ) as create,
        patch("omniscience_server.routes.tokens.audit_mcp_token_created") as audit,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/tokens/profiles/omniscience-mcp-read-v1",
                json={"name": "omnius", "workspace_id": str(WORKSPACE_ID)},
            )

    assert response.status_code == 201
    assert response.json()["secret"] == "secret-once"
    assert response.json()["token"]["scopes"] == ["search"]
    create.assert_awaited_once_with(
        session,
        name="omnius",
        workspace_id=WORKSPACE_ID,
        env="development",
        expires_at=None,
    )
    audit.assert_called_once_with(
        str(read_model.id),
        "sk_read_",
        str(WORKSPACE_ID),
        expires_at=(NOW + timedelta(days=30)).isoformat(),
        actor_id=str(ADMIN_ID),
        actor_prefix="sk_admin",
    )


@pytest.mark.asyncio
async def test_profile_rotation_and_revocation_are_audited() -> None:
    old_id = uuid.uuid4()
    read_model = _read_model()
    app, session = _profile_app()
    with (
        patch(
            "omniscience_server.routes.tokens.rotate_mcp_read_token",
            new_callable=AsyncMock,
            return_value=(
                read_model,
                "rotated-secret",
                "sk_old__",
                NOW + timedelta(hours=24),
                86400,
            ),
        ) as rotate,
        patch(
            "omniscience_server.routes.tokens.revoke_mcp_read_token",
            new_callable=AsyncMock,
            return_value=("sk_read_", WORKSPACE_ID),
        ) as revoke,
        patch("omniscience_server.routes.tokens.audit_mcp_token_rotated") as rotate_audit,
        patch("omniscience_server.routes.tokens.audit_mcp_token_revoked") as revoke_audit,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            rotated = await client.post(
                f"/api/v1/tokens/profiles/omniscience-mcp-read-v1/{old_id}/rotate"
            )
            revoked = await client.delete(
                f"/api/v1/tokens/profiles/omniscience-mcp-read-v1/{read_model.id}"
            )

    assert rotated.status_code == 201
    assert revoked.status_code == 204
    rotate.assert_awaited_once_with(session, token_id=old_id, env="development")
    revoke.assert_awaited_once_with(session, read_model.id)
    rotate_audit.assert_called_once_with(
        str(old_id),
        "sk_old__",
        str(read_model.id),
        "sk_read_",
        workspace_id=str(WORKSPACE_ID),
        old_expires_at=(NOW + timedelta(hours=24)).isoformat(),
        new_expires_at=(NOW + timedelta(days=30)).isoformat(),
        overlap_seconds=86400,
        actor_id=str(ADMIN_ID),
        actor_prefix="sk_admin",
    )
    revoke_audit.assert_called_once_with(
        str(read_model.id),
        "sk_read_",
        workspace_id=str(WORKSPACE_ID),
        actor_id=str(ADMIN_ID),
        actor_prefix="sk_admin",
    )


def test_profile_create_emits_structured_audit_record() -> None:
    from omniscience_core.auth.audit import audit_mcp_token_created

    token_id = uuid.uuid4()
    with patch("omniscience_core.auth.audit.log") as audit_log:
        audit_mcp_token_created(
            str(token_id),
            "sk_read_",
            str(WORKSPACE_ID),
            expires_at=(NOW + timedelta(days=30)).isoformat(),
            actor_id=str(ADMIN_ID),
            actor_prefix="sk_admin",
        )

    audit_log.info.assert_called_once_with(
        "audit.token.mcp_read.created",
        event_type="mcp_read_token_created",
        token_id=str(token_id),
        token_prefix="sk_read_",
        profile_id="omniscience-mcp-read-v1",
        scopes=["search"],
        workspace_id=str(WORKSPACE_ID),
        expires_at=(NOW + timedelta(days=30)).isoformat(),
        actor_id=str(ADMIN_ID),
        actor_prefix="sk_admin",
    )


def test_profile_rotation_emits_structured_audit_record() -> None:
    from omniscience_core.auth.audit import audit_mcp_token_rotated

    old_id = uuid.uuid4()
    new_id = uuid.uuid4()
    with patch("omniscience_core.auth.audit.log") as audit_log:
        audit_mcp_token_rotated(
            str(old_id),
            "sk_old__",
            str(new_id),
            "sk_new__",
            workspace_id=str(WORKSPACE_ID),
            old_expires_at=(NOW + timedelta(hours=24)).isoformat(),
            new_expires_at=(NOW + timedelta(days=30)).isoformat(),
            overlap_seconds=86400,
            actor_id=str(ADMIN_ID),
            actor_prefix="sk_admin",
        )

    audit_log.info.assert_called_once_with(
        "audit.token.mcp_read.rotated",
        event_type="mcp_read_token_rotated",
        old_token_id=str(old_id),
        old_token_prefix="sk_old__",
        new_token_id=str(new_id),
        new_token_prefix="sk_new__",
        profile_id="omniscience-mcp-read-v1",
        workspace_id=str(WORKSPACE_ID),
        old_expires_at=(NOW + timedelta(hours=24)).isoformat(),
        new_expires_at=(NOW + timedelta(days=30)).isoformat(),
        overlap_seconds=86400,
        overlap_cap_hours=24,
        actor_id=str(ADMIN_ID),
        actor_prefix="sk_admin",
    )


def test_profile_revoke_emits_structured_audit_record() -> None:
    from omniscience_core.auth.audit import audit_mcp_token_revoked

    token_id = uuid.uuid4()
    with patch("omniscience_core.auth.audit.log") as audit_log:
        audit_mcp_token_revoked(
            str(token_id),
            "sk_read_",
            workspace_id=str(WORKSPACE_ID),
            actor_id=str(ADMIN_ID),
            actor_prefix="sk_admin",
        )

    audit_log.info.assert_called_once_with(
        "audit.token.mcp_read.revoked",
        event_type="mcp_read_token_revoked",
        token_id=str(token_id),
        token_prefix="sk_read_",
        profile_id="omniscience-mcp-read-v1",
        workspace_id=str(WORKSPACE_ID),
        actor_id=str(ADMIN_ID),
        actor_prefix="sk_admin",
    )


@pytest.mark.asyncio
async def test_profile_creation_persists_exact_bounded_fields() -> None:
    from omniscience_core.auth.tokens import create_mcp_read_token

    read_model = _read_model()
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    with (
        patch(
            "omniscience_core.auth.tokens.generate_token",
            return_value=("secret-once", "sk_read_"),
        ),
        patch("omniscience_core.auth.tokens.hash_token", return_value="argon-hash"),
        patch(
            "omniscience_core.auth.tokens.ApiTokenRead.model_validate",
            return_value=read_model,
        ),
    ):
        result, secret = await create_mcp_read_token(
            session,
            name="omnius",
            workspace_id=WORKSPACE_ID,
            env="production",
            now=NOW,
        )

    persisted = session.add.call_args.args[0]
    assert result == read_model
    assert secret == "secret-once"
    assert persisted.hashed_token == "argon-hash"
    assert persisted.token_prefix == "sk_read_"
    assert persisted.profile_id == "omniscience-mcp-read-v1"
    assert persisted.scopes == ["search"]
    assert persisted.workspace_id == WORKSPACE_ID
    assert persisted.created_at == NOW
    assert persisted.expires_at == NOW + timedelta(days=30)


@pytest.mark.asyncio
async def test_profile_rotation_caps_overlap_and_links_successor() -> None:
    from omniscience_core.auth.tokens import rotate_mcp_read_token

    old_id = uuid.uuid4()
    old = _token(
        id=old_id,
        name="omnius",
        token_prefix="sk_old__",
        expires_at=NOW + timedelta(days=10),
    )
    successor = _read_model()
    session = AsyncMock()
    session.get = AsyncMock(return_value=old)
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    with patch(
        "omniscience_core.auth.tokens.create_mcp_read_token",
        new_callable=AsyncMock,
        return_value=(successor, "successor-secret"),
    ) as create:
        result = await rotate_mcp_read_token(
            session,
            token_id=old_id,
            env="production",
            now=NOW,
        )

    assert old.expires_at == NOW + timedelta(hours=24)
    assert result == (
        successor,
        "successor-secret",
        "sk_old__",
        NOW + timedelta(hours=24),
        86400,
    )
    session.get.assert_awaited_once_with(ApiToken, old_id, with_for_update=True)
    create.assert_awaited_once_with(
        session,
        name="omnius (rotated)",
        workspace_id=WORKSPACE_ID,
        env="production",
        rotated_from_id=old_id,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_profile_rotation_rejects_a_predecessor_with_a_successor() -> None:
    from omniscience_core.auth.tokens import (
        McpReadTokenValidationError,
        rotate_mcp_read_token,
    )

    old_id = uuid.uuid4()
    old = _token(
        id=old_id,
        name="omnius",
        token_prefix="sk_old__",
        expires_at=NOW + timedelta(days=10),
    )
    session = AsyncMock()
    session.get = AsyncMock(return_value=old)
    session.scalar = AsyncMock(return_value=uuid.uuid4())

    with (
        patch(
            "omniscience_core.auth.tokens.create_mcp_read_token",
            new_callable=AsyncMock,
        ) as create,
        pytest.raises(McpReadTokenValidationError, match="token_already_rotated"),
    ):
        await rotate_mcp_read_token(
            session,
            token_id=old_id,
            env="production",
            now=NOW,
        )

    create.assert_not_awaited()


def test_profile_rotation_lineage_allows_only_one_successor() -> None:
    rotation_index = next(
        index
        for index in ApiToken.__table__.indexes
        if index.name == "ix_api_tokens_rotated_from_id"
    )

    assert rotation_index.unique is True


@pytest.mark.asyncio
async def test_profile_revoke_is_immediate_and_validator_rejects_token() -> None:
    from omniscience_core.auth.tokens import (
        McpReadTokenValidationError,
        revoke_mcp_read_token,
        validate_mcp_read_token,
    )

    token = _token(token_prefix="sk_read_")
    session = AsyncMock()
    session.get = AsyncMock(return_value=token)
    session.flush = AsyncMock()

    prefix, workspace_id = await revoke_mcp_read_token(session, token.id)

    assert (prefix, workspace_id) == ("sk_read_", WORKSPACE_ID)
    assert token.is_active is False
    with pytest.raises(McpReadTokenValidationError, match="token_revoked"):
        validate_mcp_read_token(token, now=NOW)
