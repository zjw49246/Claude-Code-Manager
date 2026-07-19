"""Tests for PR Monitor API endpoints (CRUD + GitHub webhook)."""
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from backend.models.pr_monitor import MonitoredRepo, PRReview
from backend.models.task import Task


# === Helpers ===


async def _create_repo(client, repo_full_name="owner/repo", **overrides):
    payload = {
        "repo_full_name": repo_full_name,
        "auto_merge": False,
        "default_branch": "main",
        "allowed_authors": [],
    }
    payload.update(overrides)
    resp = await client.post("/api/pr-monitor/repos", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pr_payload(
    repo_full_name="owner/repo",
    action="opened",
    number=42,
    title="Add feature",
    author="alice",
    base="main",
    draft=False,
):
    return {
        "action": action,
        "repository": {"full_name": repo_full_name},
        "pull_request": {
            "number": number,
            "title": title,
            "html_url": f"https://github.com/{repo_full_name}/pull/{number}",
            "draft": draft,
            "base": {"ref": base},
            "user": {"login": author},
        },
    }


async def _post_webhook(client, secret, payload, event="pull_request", signature=None):
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": signature if signature is not None else _sign(secret, body),
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }
    return await client.post("/api/github/webhook", content=body, headers=headers)


# === CRUD tests ===


@pytest.mark.asyncio
async def test_create_repo_success(client):
    data = await _create_repo(client, "owner/repo", auto_merge=True, allowed_authors=["alice"])
    assert data["repo_full_name"] == "owner/repo"
    assert data["auto_merge"] is True
    assert data["enabled"] is True
    assert data["allowed_authors"] == ["alice"]
    # Detail response: full (unmasked) webhook secret
    assert len(data["webhook_secret"]) == 64


@pytest.mark.asyncio
async def test_create_repo_duplicate(client):
    await _create_repo(client, "owner/repo")
    resp = await client.post("/api/pr-monitor/repos", json={"repo_full_name": "owner/repo"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_repo_invalid_format(client):
    resp = await client.post("/api/pr-monitor/repos", json={"repo_full_name": "not-a-repo"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_repos_masks_secret(client):
    created = await _create_repo(client, "owner/repo")
    resp = await client.get("/api/pr-monitor/repos")
    assert resp.status_code == 200
    repos = resp.json()
    assert len(repos) == 1
    # List response masks the secret
    assert repos[0]["webhook_secret"] == created["webhook_secret"][:4] + "***"


@pytest.mark.asyncio
async def test_update_repo_settings(client):
    created = await _create_repo(client, "owner/repo")
    resp = await client.put(f"/api/pr-monitor/repos/{created['id']}", json={
        "auto_merge": True,
        "default_branch": "develop",
        "allowed_authors": ["bob"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_merge"] is True
    assert data["default_branch"] == "develop"
    assert data["allowed_authors"] == ["bob"]


@pytest.mark.asyncio
async def test_update_repo_not_found(client):
    resp = await client.put("/api/pr-monitor/repos/9999", json={"auto_merge": True})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_toggle_repo(client):
    created = await _create_repo(client, "owner/repo")
    resp = await client.post(f"/api/pr-monitor/repos/{created['id']}/toggle")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    resp = await client.post(f"/api/pr-monitor/repos/{created['id']}/toggle")
    assert resp.json()["enabled"] is True


@pytest.mark.asyncio
async def test_regenerate_secret(client):
    created = await _create_repo(client, "owner/repo")
    resp = await client.post(f"/api/pr-monitor/repos/{created['id']}/regenerate-secret")
    assert resp.status_code == 200
    new_secret = resp.json()["webhook_secret"]
    assert len(new_secret) == 64
    assert new_secret != created["webhook_secret"]


@pytest.mark.asyncio
async def test_delete_repo(client, session_factory):
    created = await _create_repo(client, "owner/repo")
    # Attach a review so cascade deletion is exercised
    async with session_factory() as db:
        db.add(PRReview(
            repo_id=created["id"], pr_number=1, pr_title="t",
            pr_author="a", pr_url="http://x", status="pending",
        ))
        await db.commit()

    resp = await client.delete(f"/api/pr-monitor/repos/{created['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    resp = await client.get(f"/api/pr-monitor/repos/{created['id']}")
    assert resp.status_code == 404
    async with session_factory() as db:
        reviews = (await db.execute(
            select(PRReview).where(PRReview.repo_id == created["id"])
        )).scalars().all()
        assert reviews == []


# === webhook-info endpoint ===


@pytest.mark.asyncio
async def test_webhook_info_configured(client):
    from backend.config import settings
    original = settings.public_base_url
    settings.public_base_url = "https://ccm.example.com/"
    try:
        resp = await client.get("/api/pr-monitor/webhook-info")
        assert resp.status_code == 200
        assert resp.json() == {"webhook_url": "https://ccm.example.com/api/github/webhook"}
    finally:
        settings.public_base_url = original


@pytest.mark.asyncio
async def test_webhook_info_unconfigured(client):
    from backend.config import settings
    original = settings.public_base_url
    settings.public_base_url = ""
    try:
        resp = await client.get("/api/pr-monitor/webhook-info")
        assert resp.status_code == 200
        assert resp.json() == {"webhook_url": None}
    finally:
        settings.public_base_url = original


# === Webhook tests ===


@pytest.mark.asyncio
async def test_webhook_valid_signature_creates_review_and_task(client, session_factory):
    repo = await _create_repo(client, "owner/repo")
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "accepted"
    review_id = data["review_id"]

    async with session_factory() as db:
        review = await db.get(PRReview, review_id)
        assert review is not None
        assert review.pr_number == 42
        assert review.status == "reviewing"
        assert review.task_id is not None
        task = await db.get(Task, review.task_id)
        assert task is not None
        assert "PR Review: owner/repo#42" == task.title
        assert "gh pr view 42" in task.description
        assert task.metadata_ == {"pr_review_id": review_id}


@pytest.mark.asyncio
async def test_webhook_invalid_signature_rejected(client):
    repo = await _create_repo(client, "owner/repo")
    resp = await _post_webhook(
        client, repo["webhook_secret"], _pr_payload(),
        signature="sha256=" + "0" * 64,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_webhook_missing_signature_rejected(client):
    await _create_repo(client, "owner/repo")
    body = json.dumps(_pr_payload()).encode()
    resp = await client.post("/api/github/webhook", content=body, headers={
        "X-GitHub-Event": "pull_request",
        "Content-Type": "application/json",
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_webhook_unknown_repo_ignored(client):
    resp = await _post_webhook(client, "irrelevant", _pr_payload("other/repo"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_disabled_repo_ignored(client):
    repo = await _create_repo(client, "owner/repo")
    await client.post(f"/api/pr-monitor/repos/{repo['id']}/toggle")
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload())
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_non_pull_request_event_ignored(client):
    repo = await _create_repo(client, "owner/repo")
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload(), event="push")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ignored"
    assert "push" in data["reason"]


@pytest.mark.asyncio
async def test_webhook_draft_pr_ignored(client):
    repo = await _create_repo(client, "owner/repo")
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload(draft=True))
    data = resp.json()
    assert data["status"] == "ignored"
    assert "draft" in data["reason"]


@pytest.mark.asyncio
async def test_webhook_wrong_base_branch_ignored(client):
    repo = await _create_repo(client, "owner/repo")
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload(base="develop"))
    data = resp.json()
    assert data["status"] == "ignored"
    assert "develop" in data["reason"]


@pytest.mark.asyncio
async def test_webhook_author_not_allowed_ignored(client):
    repo = await _create_repo(client, "owner/repo", allowed_authors=["bob"])
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload(author="mallory"))
    data = resp.json()
    assert data["status"] == "ignored"
    assert "mallory" in data["reason"]


@pytest.mark.asyncio
async def test_webhook_duplicate_opened_ignored_while_in_progress(client):
    repo = await _create_repo(client, "owner/repo")
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload())
    assert resp.json()["status"] == "accepted"
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload())
    data = resp.json()
    assert data["status"] == "ignored"
    assert "in progress" in data["reason"]


@pytest.mark.asyncio
async def test_webhook_synchronize_supersedes_old_review(client, session_factory):
    repo = await _create_repo(client, "owner/repo")
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload(action="opened"))
    first_review_id = resp.json()["review_id"]

    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload(action="synchronize"))
    assert resp.json()["status"] == "accepted"
    second_review_id = resp.json()["review_id"]
    assert second_review_id != first_review_id

    async with session_factory() as db:
        old = await db.get(PRReview, first_review_id)
        new = await db.get(PRReview, second_review_id)
        assert old.status == "superseded"
        assert new.status == "reviewing"


@pytest.mark.asyncio
async def test_webhook_self_pr_ignored(client, session_factory, monkeypatch):
    """本机 gh 登录账号的 PR 自动屏蔽（self-approval 无意义）。"""
    import backend.api.pr_monitor as prm
    monkeypatch.setattr(prm, "_GH_LOGIN_CACHE", "machine-user")

    repo = await _create_repo(client, "owner/self-test")
    payload = _pr_payload("owner/self-test", number=9, author="machine-user")
    resp = await _post_webhook(client, repo["webhook_secret"], payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert "self PR" in resp.json()["reason"]


@pytest.mark.asyncio
async def test_webhook_self_pr_allowed_when_whitelisted(client, session_factory, monkeypatch):
    """白名单显式包含本机账号时不屏蔽（测试后门）。"""
    import backend.api.pr_monitor as prm
    monkeypatch.setattr(prm, "_GH_LOGIN_CACHE", "machine-user")
    from unittest.mock import AsyncMock, MagicMock, patch as _patch

    repo = await _create_repo(client, "owner/self-wl", allowed_authors=["machine-user"])
    payload = _pr_payload("owner/self-wl", number=10, author="machine-user")
    with _patch("backend.services.pr_review_service.create_pr_review_task",
                AsyncMock(return_value=MagicMock(id=1))):
        resp = await _post_webhook(client, repo["webhook_secret"], payload)
    assert resp.status_code == 200
    assert resp.json()["status"] != "ignored"


@pytest.mark.asyncio
async def test_create_repo_with_codex_provider(client):
    data = await _create_repo(client, repo_full_name="owner/codex-repo", provider="codex")
    assert data["provider"] == "codex"


@pytest.mark.asyncio
async def test_create_repo_defaults_to_claude_provider(client):
    data = await _create_repo(client, repo_full_name="owner/default-repo")
    assert data["provider"] == "claude"


@pytest.mark.asyncio
async def test_update_repo_provider(client):
    data = await _create_repo(client, repo_full_name="owner/switch-repo")
    resp = await client.put(
        f"/api/pr-monitor/repos/{data['id']}",
        json={"provider": "codex", "review_model": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "codex"
    assert body["review_model"] is None  # 显式 null 清空旧模型（防跨家族残留）
