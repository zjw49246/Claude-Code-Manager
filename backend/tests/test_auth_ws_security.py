"""Security regressions for HTTP authentication and WebSocket channel ACLs."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.auth import create_jwt, decode_jwt
from backend.config import settings
from backend.models.discussion import Discussion
from backend.models.task import Task
from backend.models.team_share import TeamTaskShare  # noqa: F401
from backend.models.user import User
from backend.models.user_group import UserGroupMember  # noqa: F401
from backend.models.worker import Worker


@pytest_asyncio.fixture
async def secured_client(db_engine, monkeypatch):
    """Run the real app with auth enabled and one shared in-memory database."""

    session_factory = async_sessionmaker(
        db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    from backend import database
    from backend.api import ask_user as ask_user_api
    from backend.database import get_db
    from backend.main import app
    from backend.middleware.auth import TokenAuthMiddleware

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(database, "async_session", session_factory)
    monkeypatch.setattr(ask_user_api, "async_session", session_factory)

    original_token = settings.auth_token
    settings.auth_token = "security-service-token"
    TokenAuthMiddleware._admin_user_id = None
    TokenAuthMiddleware._admin_resolved = False
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            yield client, session_factory
    finally:
        settings.auth_token = original_token
        TokenAuthMiddleware._admin_user_id = None
        TokenAuthMiddleware._admin_resolved = False
        app.dependency_overrides.clear()


async def _create_user(
    session_factory,
    *,
    email: str,
    role: str,
    active: bool = True,
) -> tuple[int, str]:
    async with session_factory() as db:
        user = User(
            email=email,
            name=email.split("@", 1)[0],
            password_hash="not-used",
            role=role,
            is_active=active,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id, create_jwt(user)


@pytest.mark.asyncio
async def test_http_uses_current_database_role_for_admin_routes(secured_client):
    client, session_factory = secured_client
    user_id, stale_admin_token = await _create_user(
        session_factory,
        email="demoted@example.com",
        role="admin",
    )

    response = await client.get(
        "/api/instances",
        headers={"Authorization": f"Bearer {stale_admin_token}"},
    )
    assert response.status_code == 200

    async with session_factory() as db:
        user = await db.get(User, user_id)
        user.role = "member"
        await db.commit()

    response = await client.get(
        "/api/instances",
        headers={"Authorization": f"Bearer {stale_admin_token}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_user", [False, True])
async def test_http_rejects_disabled_or_deleted_jwt_user(
    secured_client,
    remove_user,
):
    client, session_factory = secured_client
    user_id, token = await _create_user(
        session_factory,
        email=f"revoked-{remove_user}@example.com",
        role="admin",
    )

    async with session_factory() as db:
        user = await db.get(User, user_id)
        if remove_user:
            await db.delete(user)
        else:
            user.is_active = False
        await db.commit()

    response = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_member_cannot_control_process_wide_system_operations(
    secured_client,
):
    client, session_factory = secured_client
    _, token = await _create_user(
        session_factory,
        email="system-member@example.com",
        role="member",
    )
    headers = {"Authorization": f"Bearer {token}"}

    responses = [
        await client.post(
            "/api/system/update",
            headers=headers,
            json={"dry_run": True},
        ),
        await client.get("/api/system/update/status", headers=headers),
        await client.post("/api/system/update/rollback", headers=headers),
        await client.post("/api/system/skills/curator", headers=headers),
        await client.post("/api/system/skills/distill", headers=headers),
    ]

    assert [response.status_code for response in responses] == [
        403,
        403,
        403,
        403,
        403,
    ]


@pytest.mark.asyncio
async def test_internal_ask_user_wait_rejects_member_jwt(secured_client):
    client, session_factory = secured_client
    _, token = await _create_user(
        session_factory,
        email="member@example.com",
        role="member",
    )
    payload = {
        "session_id": "visible-session-id",
        "questions": [],
    }

    denied = await client.post(
        "/api/ask-user/wait",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 403

    service = await client.post(
        "/api/ask-user/wait",
        json=payload,
        headers={"Authorization": "Bearer security-service-token"},
    )
    assert service.status_code == 200
    assert service.json() == {"answered": False, "reason": "no questions"}


@pytest.mark.asyncio
async def test_ask_user_pending_submit_follow_task_acl_and_validate_answers(
    secured_client,
):
    from backend.services.ask_user import ask_user_registry

    client, session_factory = secured_client
    owner_id, owner_token = await _create_user(
        session_factory,
        email="ask-owner@example.com",
        role="member",
    )
    other_id, _ = await _create_user(
        session_factory,
        email="ask-other@example.com",
        role="member",
    )
    async with session_factory() as db:
        owned = Task(
            title="owned",
            description="d",
            created_by=owner_id,
            session_id="owned-session",
        )
        other = Task(
            title="other",
            description="d",
            created_by=other_id,
            session_id="other-session",
        )
        db.add_all([owned, other])
        await db.commit()
        await db.refresh(owned)
        await db.refresh(other)
        owned_id = owned.id
        other_id = other.id

    question = [{
        "header": "Choice",
        "question": "Continue?",
        "options": [{"label": "Yes"}],
    }]
    owned_pending = ask_user_registry.create(
        task_id=owned_id,
        session_id="owned-session",
        questions=question,
    )
    other_pending = ask_user_registry.create(
        task_id=other_id,
        session_id="other-session",
        questions=question,
    )
    headers = {"Authorization": f"Bearer {owner_token}"}
    try:
        own = await client.get(
            f"/api/tasks/{owned_id}/ask-user/pending",
            headers=headers,
        )
        assert own.status_code == 200
        assert own.json()["pending"][0]["request_id"] == owned_pending.request_id

        denied = await client.get(
            f"/api/tasks/{other_id}/ask-user/pending",
            headers=headers,
        )
        assert denied.status_code == 403

        global_pending = await client.get(
            "/api/ask-user/pending",
            headers=headers,
        )
        assert global_pending.status_code == 200
        assert {
            item["request_id"]
            for item in global_pending.json()["pending"]
        } == {owned_pending.request_id}

        malformed = await client.post(
            f"/api/tasks/{owned_id}/ask-user/{owned_pending.request_id}",
            headers=headers,
            json={"answers": [{"labels": [123]}]},
        )
        assert malformed.status_code == 422
        assert ask_user_registry.get(owned_pending.request_id) is not None

        answered = await client.post(
            f"/api/tasks/{owned_id}/ask-user/{owned_pending.request_id}",
            headers=headers,
            json={"answers": [{"labels": ["Yes"]}]},
        )
        assert answered.status_code == 200
    finally:
        ask_user_registry.discard(owned_pending.request_id)
        ask_user_registry.discard(other_pending.request_id)


class _IdentityWebSocket:
    def __init__(self, token: str):
        self.headers = {}
        self.query_params = {"token": token}


@pytest.mark.asyncio
async def test_ws_identity_uses_current_role_and_active_state(db_factory):
    from backend.api.ws import _current_ws_identity, _revalidate_ws_identity

    async with db_factory() as db:
        user = User(
            email="ws-admin@example.com",
            name="ws-admin",
            password_hash="not-used",
            role="admin",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        token = create_jwt(user)
        identity = {**decode_jwt(token), "auth_type": "jwt"}
        user.role = "member"
        await db.commit()

    ws = _IdentityWebSocket(token)
    async with db_factory() as db:
        # A stale admin claim must reconnect as the current member rather than
        # being locked out until the JWT expires.
        current = await _current_ws_identity(ws, db)
        assert current is not None
        assert current["role"] == "member"

        refreshed = await _revalidate_ws_identity(ws, identity, db)
        assert refreshed is not None
        assert refreshed["role"] == "member"
        user = await db.get(User, identity["user_id"])
        user.is_active = False
        await db.commit()

    async with db_factory() as db:
        assert await _revalidate_ws_identity(ws, identity, db) is None


@pytest.mark.asyncio
async def test_ws_channels_apply_resource_acl_and_default_deny(db_factory):
    from backend.api.ws import _ws_channel_allowed

    async with db_factory() as db:
        owner = User(
            email="owner@example.com",
            name="owner",
            password_hash="not-used",
            role="member",
        )
        other = User(
            email="other@example.com",
            name="other",
            password_hash="not-used",
            role="member",
        )
        db.add_all([owner, other])
        await db.flush()
        task = Task(title="owned", description="d", created_by=owner.id)
        worker = Worker(name="owned-worker", owner_user_id=owner.id)
        discussion = Discussion(title="owned", creator_user_id=owner.id)
        db.add_all([task, worker, discussion])
        await db.commit()
        await db.refresh(task)
        await db.refresh(worker)
        await db.refresh(discussion)

        owner_identity = {
            "user_id": owner.id,
            "role": "member",
            "auth_type": "jwt",
        }
        other_identity = {
            "user_id": other.id,
            "role": "member",
            "auth_type": "jwt",
        }

        assert await _ws_channel_allowed(
            f"task:{task.id}",
            owner_identity,
            db,
        )
        assert await _ws_channel_allowed(
            f"worker:{worker.id}",
            owner_identity,
            db,
        )
        assert await _ws_channel_allowed(
            f"discussion:{discussion.id}:agent:9",
            owner_identity,
            db,
        )
        assert not await _ws_channel_allowed(
            f"task:{task.id}",
            other_identity,
            db,
        )
        assert not await _ws_channel_allowed(
            f"worker:{worker.id}",
            other_identity,
            db,
        )
        assert not await _ws_channel_allowed(
            "instance:1",
            owner_identity,
            db,
        )
        assert not await _ws_channel_allowed("workers", owner_identity, db)
        assert not await _ws_channel_allowed(
            "task:1:spoofed",
            owner_identity,
            db,
        )

        admin_identity = {
            "user_id": owner.id,
            "role": "admin",
            "auth_type": "jwt",
        }
        assert await _ws_channel_allowed("instance:1", admin_identity, db)
        assert await _ws_channel_allowed("workers", admin_identity, db)
