"""Tests for Project API endpoints."""
import pytest
from unittest.mock import patch, AsyncMock

from backend.models.project import Project


@pytest.fixture
def mock_bg_tasks():
    """Patch background git tasks to prevent real git operations."""
    with patch("backend.api.projects._clone_repo", new_callable=AsyncMock) as mock_clone, \
         patch("backend.api.projects._init_local_repo", new_callable=AsyncMock) as mock_init:
        yield mock_clone, mock_init


@pytest.mark.asyncio
async def test_list_projects_empty(client):
    resp = await client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_project_with_git_url(client, mock_bg_tasks):
    mock_clone, mock_init = mock_bg_tasks
    resp = await client.post("/api/projects", json={
        "name": "my-remote-proj",
        "git_url": "https://github.com/user/repo.git",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "my-remote-proj"
    assert data["has_remote"] is True
    assert data["git_url"] == "https://github.com/user/repo.git"
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_create_project_local_no_git_url(client, mock_bg_tasks):
    mock_clone, mock_init = mock_bg_tasks
    resp = await client.post("/api/projects", json={"name": "local-proj"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "local-proj"
    assert data["has_remote"] is False
    assert data["git_url"] is None


@pytest.mark.asyncio
async def test_create_project_duplicate_name(client, mock_bg_tasks):
    await client.post("/api/projects", json={"name": "dup-proj"})
    resp = await client.post("/api/projects", json={"name": "dup-proj"})
    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_project(client, mock_bg_tasks):
    create_resp = await client.post("/api/projects", json={"name": "proj-get"})
    project_id = create_resp.json()["id"]
    resp = await client.get(f"/api/projects/{project_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "proj-get"


@pytest.mark.asyncio
async def test_get_project_not_found(client):
    resp = await client.get("/api/projects/9999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_project(client, mock_bg_tasks):
    create_resp = await client.post("/api/projects", json={"name": "proj-update"})
    project_id = create_resp.json()["id"]
    resp = await client.put(f"/api/projects/{project_id}", json={"name": "proj-renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "proj-renamed"


@pytest.mark.asyncio
async def test_update_project_git_url_sets_has_remote(client, mock_bg_tasks):
    """Setting git_url via update auto-sets has_remote=True."""
    create_resp = await client.post("/api/projects", json={"name": "local-2-remote"})
    project_id = create_resp.json()["id"]
    assert create_resp.json()["has_remote"] is False

    resp = await client.put(f"/api/projects/{project_id}", json={
        "git_url": "https://github.com/user/repo.git"
    })
    assert resp.status_code == 200
    assert resp.json()["has_remote"] is True


@pytest.mark.asyncio
async def test_update_project_not_found(client):
    resp = await client.put("/api/projects/9999", json={"name": "X"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_project(client, mock_bg_tasks):
    create_resp = await client.post("/api/projects", json={"name": "proj-del"})
    project_id = create_resp.json()["id"]
    resp = await client.delete(f"/api/projects/{project_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = await client.get(f"/api/projects/{project_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_project_not_found(client):
    resp = await client.delete("/api/projects/9999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reclone_success(client, mock_bg_tasks, session_factory):
    """Reclone on a remote project resets status and triggers background clone."""
    mock_clone, mock_init = mock_bg_tasks
    create_resp = await client.post("/api/projects", json={
        "name": "proj-reclone",
        "git_url": "https://github.com/user/repo.git",
    })
    project_id = create_resp.json()["id"]

    resp = await client.post(f"/api/projects/{project_id}/reclone")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_reclone_local_project_rejected(client, mock_bg_tasks):
    """Cannot reclone a local project (has_remote=False)."""
    create_resp = await client.post("/api/projects", json={"name": "proj-local-reclone"})
    project_id = create_resp.json()["id"]
    resp = await client.post(f"/api/projects/{project_id}/reclone")
    assert resp.status_code == 400
    assert "local project" in resp.json()["detail"].lower()


# === AGENTS.md injection (Codex instruction file) ===


def test_inject_agents_md_creates_symlink(tmp_path):
    from backend.api.projects import _inject_agents_md
    (tmp_path / "CLAUDE.md").write_text("# guide\n")
    assert _inject_agents_md(str(tmp_path)) is True
    agents = tmp_path / "AGENTS.md"
    assert agents.exists()
    # Symlink (or fallback pointer file) must surface CLAUDE.md's guidance
    if agents.is_symlink():
        assert agents.read_text() == "# guide\n"
    else:
        assert "CLAUDE.md" in agents.read_text()


def test_inject_agents_md_noop_without_claude_md(tmp_path):
    from backend.api.projects import _inject_agents_md
    assert _inject_agents_md(str(tmp_path)) is False
    assert not (tmp_path / "AGENTS.md").exists()


def test_inject_agents_md_noop_when_exists(tmp_path):
    from backend.api.projects import _inject_agents_md
    (tmp_path / "CLAUDE.md").write_text("# guide\n")
    (tmp_path / "AGENTS.md").write_text("custom\n")
    assert _inject_agents_md(str(tmp_path)) is False
    assert (tmp_path / "AGENTS.md").read_text() == "custom\n"


@pytest.mark.asyncio
async def test_init_local_repo_preserves_existing_claude_md(db_factory, tmp_path, monkeypatch):
    """存量目录（有文件但未 git init）里已有的 CLAUDE.md 不被模板覆盖。"""
    from backend.api import projects as projects_mod
    monkeypatch.setattr(projects_mod, "async_session", db_factory)

    async with db_factory() as db:
        p = Project(name="pre", local_path=str(tmp_path), status="pending")
        db.add(p)
        await db.commit()
        await db.refresh(p)
        pid = p.id

    (tmp_path / "CLAUDE.md").write_text("# my existing guide\n")

    await projects_mod._init_local_repo(
        pid, str(tmp_path), "pre", "main",
        git_config={"git_user_name": "t", "git_user_email": "t@t.co"},
    )

    assert (tmp_path / "CLAUDE.md").read_text() == "# my existing guide\n"
    # AGENTS.md 补上了（指向未被覆盖的原 CLAUDE.md）
    assert (tmp_path / "AGENTS.md").exists()
    async with db_factory() as db:
        p2 = await db.get(Project, pid)
        assert p2.status == "ready"


@pytest.mark.asyncio
async def test_init_local_repo_preserves_both_existing_docs(db_factory, tmp_path, monkeypatch):
    """两个文件都已存在时全部原样保留，且不因无事可提交而报错。"""
    from backend.api import projects as projects_mod
    monkeypatch.setattr(projects_mod, "async_session", db_factory)

    async with db_factory() as db:
        p = Project(name="pre2", local_path=str(tmp_path), status="pending")
        db.add(p)
        await db.commit()
        await db.refresh(p)
        pid = p.id

    (tmp_path / "CLAUDE.md").write_text("# guide\n")
    (tmp_path / "AGENTS.md").write_text("# my own agents doc\n")

    await projects_mod._init_local_repo(
        pid, str(tmp_path), "pre2", "main",
        git_config={"git_user_name": "t", "git_user_email": "t@t.co"},
    )

    assert (tmp_path / "CLAUDE.md").read_text() == "# guide\n"
    assert (tmp_path / "AGENTS.md").read_text() == "# my own agents doc\n"
    async with db_factory() as db:
        p2 = await db.get(Project, pid)
        assert p2.status == "ready"
