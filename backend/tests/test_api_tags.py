"""Tests for Tag API endpoints and tag–project integration."""
import pytest
from unittest.mock import patch, AsyncMock


@pytest.fixture
def mock_bg_tasks():
    """Patch background git tasks to prevent real git operations."""
    with patch("backend.api.projects._clone_repo", new_callable=AsyncMock), \
         patch("backend.api.projects._init_local_repo", new_callable=AsyncMock):
        yield


# ── Tag CRUD ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tags_empty(client):
    resp = await client.get("/api/tags")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_tag(client):
    resp = await client.post("/api/tags", json={"name": "backend", "color": "sky"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "backend"
    assert data["color"] == "sky"
    assert "id" in data


@pytest.mark.asyncio
async def test_create_tag_default_color(client):
    resp = await client.post("/api/tags", json={"name": "infra"})
    assert resp.status_code == 201
    assert resp.json()["color"] == "indigo"


@pytest.mark.asyncio
async def test_create_tag_duplicate(client):
    await client.post("/api/tags", json={"name": "dup-tag"})
    resp = await client.post("/api/tags", json={"name": "dup-tag"})
    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_tags_ordered(client):
    await client.post("/api/tags", json={"name": "zebra"})
    await client.post("/api/tags", json={"name": "alpha"})
    await client.post("/api/tags", json={"name": "middle"})
    resp = await client.get("/api/tags")
    names = [t["name"] for t in resp.json()]
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_update_tag_color(client):
    create = await client.post("/api/tags", json={"name": "recolor", "color": "sky"})
    tag_id = create.json()["id"]
    resp = await client.put(f"/api/tags/{tag_id}", json={"color": "rose"})
    assert resp.status_code == 200
    assert resp.json()["color"] == "rose"
    assert resp.json()["name"] == "recolor"


@pytest.mark.asyncio
async def test_update_tag_rename(client):
    create = await client.post("/api/tags", json={"name": "old-name"})
    tag_id = create.json()["id"]
    resp = await client.put(f"/api/tags/{tag_id}", json={"name": "new-name"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "new-name"


@pytest.mark.asyncio
async def test_update_tag_rename_duplicate(client):
    await client.post("/api/tags", json={"name": "existing-a"})
    create = await client.post("/api/tags", json={"name": "existing-b"})
    tag_id = create.json()["id"]
    resp = await client.put(f"/api/tags/{tag_id}", json={"name": "existing-a"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_tag_not_found(client):
    resp = await client.put("/api/tags/9999", json={"name": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_tag(client):
    create = await client.post("/api/tags", json={"name": "to-delete"})
    tag_id = create.json()["id"]
    resp = await client.delete(f"/api/tags/{tag_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    tags = await client.get("/api/tags")
    assert not any(t["name"] == "to-delete" for t in tags.json())


@pytest.mark.asyncio
async def test_delete_tag_not_found(client):
    resp = await client.delete("/api/tags/9999")
    assert resp.status_code == 404


# ── Tag ↔ Project integration ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_project_auto_creates_tag_records(client, mock_bg_tasks):
    """Creating a project with tags should auto-create Tag records."""
    resp = await client.post("/api/projects", json={
        "name": "proj-with-tags",
        "tags": ["frontend", "urgent"],
    })
    assert resp.status_code == 201
    assert set(resp.json()["tags"]) == {"frontend", "urgent"}

    tags_resp = await client.get("/api/tags")
    tag_names = {t["name"] for t in tags_resp.json()}
    assert "frontend" in tag_names
    assert "urgent" in tag_names


@pytest.mark.asyncio
async def test_update_project_auto_creates_tag_records(client, mock_bg_tasks):
    """Updating a project's tags should auto-create Tag records for new names."""
    create = await client.post("/api/projects", json={"name": "proj-tag-update"})
    pid = create.json()["id"]

    await client.put(f"/api/projects/{pid}", json={"tags": ["newbie"]})

    tags_resp = await client.get("/api/tags")
    tag_names = {t["name"] for t in tags_resp.json()}
    assert "newbie" in tag_names


@pytest.mark.asyncio
async def test_rename_tag_cascades_to_projects(client, mock_bg_tasks):
    """Renaming a tag via /api/tags should update all projects referencing it."""
    await client.post("/api/tags", json={"name": "old-label"})
    create = await client.post("/api/projects", json={
        "name": "proj-cascade-rename",
        "tags": ["old-label", "keep-me"],
    })
    pid = create.json()["id"]

    tag_id = None
    for t in (await client.get("/api/tags")).json():
        if t["name"] == "old-label":
            tag_id = t["id"]
            break

    await client.put(f"/api/tags/{tag_id}", json={"name": "new-label"})

    proj = (await client.get(f"/api/projects/{pid}")).json()
    assert "new-label" in proj["tags"]
    assert "old-label" not in proj["tags"]
    assert "keep-me" in proj["tags"]


@pytest.mark.asyncio
async def test_delete_tag_removes_from_projects(client, mock_bg_tasks):
    """Deleting a tag should remove it from all projects' tag lists."""
    await client.post("/api/tags", json={"name": "doomed"})
    create = await client.post("/api/projects", json={
        "name": "proj-cascade-del",
        "tags": ["doomed", "survivor"],
    })
    pid = create.json()["id"]

    tag_id = None
    for t in (await client.get("/api/tags")).json():
        if t["name"] == "doomed":
            tag_id = t["id"]
            break

    await client.delete(f"/api/tags/{tag_id}")

    proj = (await client.get(f"/api/projects/{pid}")).json()
    assert "doomed" not in proj["tags"]
    assert "survivor" in proj["tags"]


@pytest.mark.asyncio
async def test_list_project_tags_only_returns_assigned(client, mock_bg_tasks):
    """GET /api/projects/tags only returns tags that are assigned to projects."""
    await client.post("/api/tags", json={"name": "unassigned-tag"})
    await client.post("/api/projects", json={
        "name": "proj-assigned",
        "tags": ["assigned-tag"],
    })

    resp = await client.get("/api/projects/tags")
    tag_list = resp.json()
    assert "assigned-tag" in tag_list
    assert "unassigned-tag" not in tag_list


@pytest.mark.asyncio
async def test_tags_table_includes_unassigned(client, mock_bg_tasks):
    """GET /api/tags returns all tags including those not assigned to any project.

    This is the core of the bug fix: the tag registry must include tags
    created via TagManager even if no project uses them yet.
    """
    await client.post("/api/tags", json={"name": "standalone-tag", "color": "rose"})
    await client.post("/api/projects", json={
        "name": "proj-with-one-tag",
        "tags": ["project-tag"],
    })

    project_tags_resp = await client.get("/api/projects/tags")
    project_tags = project_tags_resp.json()

    all_tags_resp = await client.get("/api/tags")
    all_tag_names = {t["name"] for t in all_tags_resp.json()}

    assert "standalone-tag" not in project_tags
    assert "standalone-tag" in all_tag_names
    assert "project-tag" in all_tag_names


@pytest.mark.asyncio
async def test_create_project_does_not_duplicate_existing_tags(client, mock_bg_tasks):
    """Creating a project with tags that already exist in the registry should not duplicate them."""
    await client.post("/api/tags", json={"name": "pre-existing"})
    await client.post("/api/projects", json={
        "name": "proj-no-dup",
        "tags": ["pre-existing"],
    })

    tags_resp = await client.get("/api/tags")
    matches = [t for t in tags_resp.json() if t["name"] == "pre-existing"]
    assert len(matches) == 1
