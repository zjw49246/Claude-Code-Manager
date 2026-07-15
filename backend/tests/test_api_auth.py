"""Tests for Auth API endpoints."""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from backend.config import settings


@pytest_asyncio.fixture
async def auth_app(db_engine):
    """App fixture that does NOT disable auth (unlike the shared one)."""
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    from backend.main import app as real_app
    from backend.database import get_db

    async def override_get_db():
        async with session_factory() as session:
            yield session

    real_app.dependency_overrides[get_db] = override_get_db
    yield real_app
    real_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_client(auth_app):
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_login_no_auth_configured(auth_client):
    """When auth_token is empty, login always succeeds."""
    original = settings.auth_token
    settings.auth_token = ""
    try:
        resp = await auth_client.post("/api/auth/login", json={"token": "anything"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "No auth configured" in resp.json().get("message", "")
    finally:
        settings.auth_token = original


@pytest.mark.asyncio
async def test_login_valid_token(auth_client):
    """Valid token returns ok."""
    original = settings.auth_token
    settings.auth_token = "test-secret-123"
    try:
        resp = await auth_client.post("/api/auth/login", json={"token": "test-secret-123"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
    finally:
        settings.auth_token = original


@pytest.mark.asyncio
async def test_login_invalid_token(auth_client):
    """Invalid token returns 401."""
    original = settings.auth_token
    settings.auth_token = "test-secret-123"
    try:
        resp = await auth_client.post("/api/auth/login", json={"token": "wrong"})
        assert resp.status_code == 401
    finally:
        settings.auth_token = original


@pytest.mark.asyncio
async def test_login_missing_token_field(auth_client):
    """token 字段已可选（支持 email+password 登录），空 body 走业务校验返回 400。"""
    resp = await auth_client.post("/api/auth/login", json={})
    assert resp.status_code == 400
    assert "Email and password required" in resp.text


@pytest.mark.asyncio
async def test_no_auth_mode_grants_full_access(client):
    """无鉴权模式（AUTH_TOKEN 为空）回归测试。

    RBAC 上线后中间件在无 token 分支曾直接放行而不设置身份，导致
    require_task_access / require_admin 全线 403、无鉴权部署不可用。
    修复后该模式所有请求视为 super_admin（等价于历史「无鉴权 = 全开放」语义）。
    """
    # require_task_access 路径：创建后读取（修复前 GET 返回 403）
    created = await client.post("/api/tasks", json={"title": "t", "description": "d"})
    assert created.status_code == 201
    task_id = created.json()["id"]
    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200

    # require_admin 路径：admin-only 端点（修复前 403 "Admin only"）
    resp = await client.post("/api/instances", json={"name": "no-auth-inst"})
    assert resp.status_code == 201
