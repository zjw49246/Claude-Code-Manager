"""Tests for PR Monitor API endpoints (CRUD + GitHub webhook)."""
import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, get_db
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
    head_sha="head-sha-1",
):
    payload = {
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
    if head_sha is not None:
        payload["pull_request"]["head"] = {"sha": head_sha}
    return payload


async def _post_webhook(
    client,
    secret,
    payload,
    event="pull_request",
    signature=None,
    delivery_id=None,
):
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": signature if signature is not None else _sign(secret, body),
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }
    if delivery_id:
        headers["X-GitHub-Delivery"] = delivery_id
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
        assert review.head_sha == "head-sha-1"
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
async def test_webhook_duplicate_opened_same_head_ignored(client):
    repo = await _create_repo(client, "owner/repo")
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload())
    assert resp.json()["status"] == "accepted"
    resp = await _post_webhook(client, repo["webhook_secret"], _pr_payload())
    data = resp.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "PR commit already reviewed"


@pytest.mark.asyncio
async def test_webhook_synchronize_supersedes_old_review(client, session_factory):
    repo = await _create_repo(client, "owner/repo")
    resp = await _post_webhook(
        client,
        repo["webhook_secret"],
        _pr_payload(action="opened", head_sha="head-sha-1"),
    )
    first_review_id = resp.json()["review_id"]

    resp = await _post_webhook(
        client,
        repo["webhook_secret"],
        _pr_payload(action="synchronize", head_sha="head-sha-2"),
    )
    assert resp.json()["status"] == "accepted"
    second_review_id = resp.json()["review_id"]
    assert second_review_id != first_review_id

    async with session_factory() as db:
        old = await db.get(PRReview, first_review_id)
        new = await db.get(PRReview, second_review_id)
        assert old.status == "superseded"
        assert old.head_sha == "head-sha-1"
        assert new.status == "reviewing"
        assert new.head_sha == "head-sha-2"


@pytest.mark.asyncio
async def test_webhook_duplicate_synchronize_same_head_ignored(
    client, session_factory
):
    """A redelivery with a new delivery ID must not review the same commit twice."""
    repo = await _create_repo(client, "owner/repo")
    payload = _pr_payload(action="synchronize", head_sha="same-head-sha")

    first = await _post_webhook(
        client,
        repo["webhook_secret"],
        payload,
        delivery_id="delivery-1",
    )
    second = await _post_webhook(
        client,
        repo["webhook_secret"],
        payload,
        delivery_id="delivery-2",
    )

    assert first.json()["status"] == "accepted"
    assert second.json() == {
        "status": "ignored",
        "reason": "PR commit already reviewed",
        "review_id": first.json()["review_id"],
    }

    async with session_factory() as db:
        reviews = (await db.execute(select(PRReview))).scalars().all()
        tasks = (await db.execute(
            select(Task).where(Task.title == "PR Review: owner/repo#42")
        )).scalars().all()
        assert len(reviews) == 1
        assert len(tasks) == 1
        assert reviews[0].delivery_id == "delivery-1"


@pytest.mark.asyncio
async def test_webhook_duplicate_delivery_id_ignored(client):
    repo = await _create_repo(client, "owner/repo")
    payload = _pr_payload(action="opened", head_sha="same-head-sha")

    first = await _post_webhook(
        client,
        repo["webhook_secret"],
        payload,
        delivery_id="same-delivery",
    )
    second = await _post_webhook(
        client,
        repo["webhook_secret"],
        payload,
        delivery_id="same-delivery",
    )

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "ignored"
    assert second.json()["reason"] == "webhook delivery already processed"


@pytest.mark.asyncio
async def test_webhook_missing_head_sha_ignored(client, session_factory):
    repo = await _create_repo(client, "owner/repo")
    resp = await _post_webhook(
        client,
        repo["webhook_secret"],
        _pr_payload(head_sha=None),
    )

    assert resp.json() == {"status": "ignored", "reason": "missing PR head SHA"}
    async with session_factory() as db:
        assert (await db.execute(select(PRReview))).scalars().all() == []


@pytest.mark.asyncio
async def test_webhook_concurrent_unique_conflict_returns_winner(client):
    """The database constraint winner is returned instead of an HTTP 500."""
    import backend.api.pr_monitor as prm

    repo = await _create_repo(client, "owner/repo")
    winner = MagicMock(id=77, delivery_id="delivery-1")
    duplicate_lookup = AsyncMock(side_effect=[None, winner])
    create_review = AsyncMock(
        side_effect=IntegrityError("INSERT", {}, Exception("unique constraint"))
    )

    with patch.object(prm, "_find_processed_review", duplicate_lookup), patch(
        "backend.services.pr_review_service.create_pr_review_task",
        create_review,
    ):
        resp = await _post_webhook(
            client,
            repo["webhook_secret"],
            _pr_payload(head_sha="same-head-sha"),
            delivery_id="delivery-1",
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ignored",
        "reason": "webhook delivery already processed",
        "review_id": 77,
    }
    assert duplicate_lookup.await_count == 2


@pytest.mark.asyncio
async def test_webhook_concurrent_same_head_creates_one_task(
    app, tmp_path
):
    from backend.models.project import Project
    from backend.services.pr_review_service import PR_MONITOR_PROJECT_NAME

    db_path = tmp_path / "concurrent-webhooks.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    file_session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    real_app, _ = app

    async def override_get_db():
        async with file_session_factory() as db:
            yield db

    real_app.dependency_overrides[get_db] = override_get_db
    try:
        async with file_session_factory() as db:
            db.add(Project(name=PR_MONITOR_PROJECT_NAME))
            await db.commit()

        async with AsyncClient(
            transport=ASGITransport(app=real_app),
            base_url="http://test",
        ) as client:
            repo = await _create_repo(client, "owner/repo")
            payload = _pr_payload(action="synchronize", head_sha="same-head-sha")
            responses = await asyncio.gather(
                _post_webhook(
                    client,
                    repo["webhook_secret"],
                    payload,
                    delivery_id="concurrent-delivery-1",
                ),
                _post_webhook(
                    client,
                    repo["webhook_secret"],
                    payload,
                    delivery_id="concurrent-delivery-2",
                ),
            )

        assert sorted(resp.json()["status"] for resp in responses) == [
            "accepted",
            "ignored",
        ]
        async with file_session_factory() as db:
            reviews = (await db.execute(select(PRReview))).scalars().all()
            tasks = (await db.execute(
                select(Task).where(Task.title == "PR Review: owner/repo#42")
            )).scalars().all()
            assert len(reviews) == 1
            assert len(tasks) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pr_review_head_sha_unique_constraint(db_session):
    repo = MonitoredRepo(repo_full_name="owner/repo", webhook_secret="secret")
    db_session.add(repo)
    await db_session.commit()

    common = {
        "repo_id": repo.id,
        "pr_number": 42,
        "head_sha": "same-head-sha",
        "pr_title": "Title",
        "pr_author": "alice",
        "pr_url": "https://github.com/owner/repo/pull/42",
        "status": "reviewing",
    }
    db_session.add(PRReview(**common, delivery_id="delivery-1"))
    await db_session.commit()

    db_session.add(PRReview(**common, delivery_id="delivery-2"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_webhook_synchronize_stops_exact_running_review_generation(
    client,
    session_factory,
):
    """A replacement review is created only after the old owner is reaped."""

    import backend.main
    from backend.models.instance import Instance

    repo = await _create_repo(client, "owner/running-review")
    opened = await _post_webhook(
        client,
        repo["webhook_secret"],
        _pr_payload("owner/running-review", action="opened"),
    )
    old_review_id = opened.json()["review_id"]
    old_started_at = datetime.utcnow() - timedelta(minutes=1)
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_review.task_id)
        instance = Instance(
            name="pr-review-running",
            status="running",
            pid=51001,
            current_task_id=old_task.id,
            started_at=old_started_at,
        )
        db.add(instance)
        await db.flush()
        old_task.status = "executing"
        old_task.instance_id = instance.id
        old_task.started_at = old_started_at
        await db.commit()
        old_task_id = old_task.id
        instance_id = instance.id

    lifecycle_order: list[str] = []

    async def stop_exact(stopped_instance_id, **kwargs):
        assert stopped_instance_id == instance_id
        assert kwargs == {
            "expected_task_id": old_task_id,
            "expected_pid": 51001,
            "expected_started_at": old_started_at,
            "task_status": "completed",
            "terminal_consumer_timeout": 30.0,
            "consumer_cancel_timeout": 10.0,
        }
        async with session_factory() as db:
            owner = await db.get(Instance, instance_id)
            assert owner.current_task_id == old_task_id
            assert owner.pid == 51001
            assert owner.started_at == old_started_at
            owner.status = "idle"
            owner.current_task_id = None
            owner.pid = None
            await db.commit()
        lifecycle_order.append("stopped")
        return True

    async def publish_after_cleanup(task_id, status):
        assert task_id == old_task_id
        assert status == "completed"
        assert lifecycle_order == ["stopped"]
        lifecycle_order.append("published")

    with (
        patch.object(
            backend.main.dispatcher,
            "abort_task_queue",
            new_callable=AsyncMock,
            return_value=0,
        ) as abort_queue,
        patch.object(
            backend.main.instance_manager,
            "wait_for_task_launch_barrier",
            new_callable=AsyncMock,
            return_value=True,
        ) as launch_barrier,
        patch.object(
            backend.main.instance_manager,
            "stop",
            new_callable=AsyncMock,
            side_effect=stop_exact,
        ) as stop,
        patch(
            "backend.services.task_events.broadcast_status_change",
            new_callable=AsyncMock,
            side_effect=publish_after_cleanup,
        ),
    ):
        synchronized = await _post_webhook(
            client,
            repo["webhook_secret"],
            _pr_payload(
                "owner/running-review",
                action="synchronize",
                head_sha="head-sha-2",
            ),
        )

    assert synchronized.status_code == 200, synchronized.text
    assert synchronized.json()["status"] == "accepted"
    abort_queue.assert_awaited_once_with(old_task_id)
    launch_barrier.assert_awaited_once_with(instance_id, old_task_id)
    stop.assert_awaited_once()
    assert lifecycle_order == ["stopped", "published"]

    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_task_id)
        instance = await db.get(Instance, instance_id)
        assert old_review.status == "superseded"
        assert old_task.status == "completed"
        assert old_task.error_message == "Superseded by new push"
        assert instance.status == "idle"
        assert instance.current_task_id is None
        assert instance.pid is None


@pytest.mark.asyncio
async def test_webhook_synchronize_same_task_slot_aba_does_not_stop_new_generation(
    client,
    session_factory,
):
    """A same-task retry cannot satisfy the old PID/start/generation fences."""

    import backend.main
    from backend.models.instance import Instance

    repo = await _create_repo(client, "owner/review-aba")
    opened = await _post_webhook(
        client,
        repo["webhook_secret"],
        _pr_payload("owner/review-aba", action="opened"),
    )
    old_review_id = opened.json()["review_id"]
    old_started_at = datetime.utcnow() - timedelta(minutes=2)
    replacement_started_at = datetime.utcnow()
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_review.task_id)
        instance = Instance(
            name="pr-review-aba-slot",
            status="running",
            pid=52001,
            current_task_id=old_task.id,
            started_at=old_started_at,
        )
        db.add(instance)
        await db.flush()
        old_task.status = "executing"
        old_task.instance_id = instance.id
        old_task.started_at = old_started_at
        await db.commit()
        old_task_id = old_task.id
        instance_id = instance.id

    async def slot_reused_before_exact_stop(stopped_instance_id, **kwargs):
        assert stopped_instance_id == instance_id
        assert kwargs["expected_task_id"] == old_task_id
        assert kwargs["expected_pid"] == 52001
        assert kwargs["expected_started_at"] == old_started_at
        async with session_factory() as db:
            instance = await db.get(Instance, instance_id)
            retried_task = await db.get(Task, old_task_id)
            instance.current_task_id = old_task_id
            instance.pid = 52002
            instance.started_at = replacement_started_at
            retried_task.status = "executing"
            retried_task.retry_count += 1
            retried_task.instance_id = instance_id
            retried_task.started_at = replacement_started_at
            retried_task.completed_at = None
            retried_task.error_message = None
            await db.commit()
        # Real InstanceManager.stop returns False when its exact owner fence no
        # longer matches. It must not signal or clear the new generation.
        return False

    with (
        patch.object(
            backend.main.dispatcher,
            "abort_task_queue",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch.object(
            backend.main.instance_manager,
            "wait_for_task_launch_barrier",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            backend.main.instance_manager,
            "stop",
            new_callable=AsyncMock,
            side_effect=slot_reused_before_exact_stop,
        ) as stop,
    ):
        synchronized = await _post_webhook(
            client,
            repo["webhook_secret"],
            _pr_payload(
                "owner/review-aba",
                action="synchronize",
                head_sha="head-sha-2",
            ),
        )

    assert synchronized.status_code == 409, synchronized.text
    assert "no new review was created" in synchronized.json()["detail"]
    stop.assert_awaited_once()
    async with session_factory() as db:
        instance = await db.get(Instance, instance_id)
        retried_task = await db.get(Task, old_task_id)
        old_review = await db.get(PRReview, old_review_id)
        reviews = (
            await db.execute(
                select(PRReview).where(
                    PRReview.repo_id == repo["id"],
                    PRReview.pr_number == 42,
                )
            )
        ).scalars().all()
        assert len(reviews) == 1
        assert old_review.status == "reviewing"
        assert instance.current_task_id == old_task_id
        assert instance.pid == 52002
        assert instance.started_at == replacement_started_at
        assert retried_task.status == "executing"
        assert retried_task.retry_count == 1
        assert retried_task.instance_id == instance_id
        assert retried_task.started_at == replacement_started_at


@pytest.mark.asyncio
async def test_webhook_synchronize_refuses_new_review_when_cleanup_unconfirmed(
    client,
    session_factory,
):
    """An exact owner left behind keeps the old review active and returns 409."""

    import backend.main
    from backend.models.instance import Instance

    repo = await _create_repo(client, "owner/review-unreaped")
    opened = await _post_webhook(
        client,
        repo["webhook_secret"],
        _pr_payload("owner/review-unreaped", action="opened"),
    )
    old_review_id = opened.json()["review_id"]
    old_started_at = datetime.utcnow() - timedelta(minutes=1)
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_review.task_id)
        instance = Instance(
            name="pr-review-unreaped",
            status="error",
            pid=53001,
            current_task_id=old_task.id,
            started_at=old_started_at,
        )
        db.add(instance)
        await db.flush()
        old_task.status = "executing"
        old_task.instance_id = instance.id
        old_task.started_at = old_started_at
        await db.commit()
        old_task_id = old_task.id
        instance_id = instance.id

    with (
        patch.object(
            backend.main.dispatcher,
            "abort_task_queue",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch.object(
            backend.main.instance_manager,
            "wait_for_task_launch_barrier",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            backend.main.instance_manager,
            "stop",
            new_callable=AsyncMock,
            return_value=False,
        ) as stop,
        patch(
            "backend.services.task_events.broadcast_status_change",
            new_callable=AsyncMock,
        ) as publish,
    ):
        synchronized = await _post_webhook(
            client,
            repo["webhook_secret"],
            _pr_payload(
                "owner/review-unreaped",
                action="synchronize",
                head_sha="head-sha-2",
            ),
        )

    assert synchronized.status_code == 409, synchronized.text
    assert "no new review was created" in synchronized.json()["detail"]
    stop.assert_awaited_once()
    publish.assert_not_awaited()
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_task_id)
        instance = await db.get(Instance, instance_id)
        reviews = (
            await db.execute(
                select(PRReview).where(
                    PRReview.repo_id == repo["id"],
                    PRReview.pr_number == 42,
                )
            )
        ).scalars().all()
        assert len(reviews) == 1
        assert old_review.status == "reviewing"
        assert old_task.status == "completed"
        assert old_task.error_message == "Superseded by new push"
        assert instance.current_task_id == old_task_id
        assert instance.pid == 53001


@pytest.mark.asyncio
async def test_webhook_synchronize_relocks_terminal_task_before_replacement(
    client,
    session_factory,
):
    """A retry after cleanup but before review replacement forces a 409."""

    import backend.services.task_termination as termination

    repo = await _create_repo(client, "owner/review-post-cleanup-retry")
    opened = await _post_webhook(
        client,
        repo["webhook_secret"],
        _pr_payload("owner/review-post-cleanup-retry", action="opened"),
    )
    old_review_id = opened.json()["review_id"]
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task_id = old_review.task_id

    real_lock_generation = termination.lock_task_generation
    lock_calls = 0

    async def retry_before_pr_relock(*args, **kwargs):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            async with session_factory() as db:
                task = await db.get(Task, old_task_id)
                task.status = "pending"
                task.retry_count += 1
                task.instance_id = None
                task.started_at = None
                task.completed_at = None
                task.error_message = None
                await db.commit()
        return await real_lock_generation(*args, **kwargs)

    with patch.object(
        termination,
        "lock_task_generation",
        new_callable=AsyncMock,
        side_effect=retry_before_pr_relock,
    ):
        synchronized = await _post_webhook(
            client,
            repo["webhook_secret"],
            _pr_payload(
                "owner/review-post-cleanup-retry",
                action="synchronize",
                head_sha="head-sha-2",
            ),
        )

    assert synchronized.status_code == 409, synchronized.text
    assert "started a newer generation" in synchronized.json()["detail"]
    assert lock_calls == 2
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_task_id)
        reviews = (
            await db.execute(
                select(PRReview).where(
                    PRReview.repo_id == repo["id"],
                    PRReview.pr_number == 42,
                )
            )
        ).scalars().all()
        assert len(reviews) == 1
        assert old_review.status == "reviewing"
        assert old_task.status == "pending"
        assert old_task.retry_count == 1


@pytest.mark.asyncio
async def test_webhook_synchronize_blocks_retry_that_read_before_replacement(
    client,
    session_factory,
):
    """A retry queued behind supersede revalidates and cannot revive the task."""

    import backend.services.task_termination as termination
    from backend.services.worker_proxy import get_task_operation_lock

    repo = await _create_repo(client, "owner/review-waiting-retry")
    opened = await _post_webhook(
        client,
        repo["webhook_secret"],
        _pr_payload("owner/review-waiting-retry", action="opened"),
    )
    old_review_id = opened.json()["review_id"]
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task_id = old_review.task_id

    supersede_holds_operation_lock = asyncio.Event()
    release_supersede = asyncio.Event()
    real_terminate = termination.terminate_authoritative_task_generation

    async def delayed_supersede(*args, **kwargs):
        assert kwargs["operation_locks_held"] is True
        assert get_task_operation_lock(old_task_id).locked()
        supersede_holds_operation_lock.set()
        await release_supersede.wait()
        return await real_terminate(*args, **kwargs)

    with patch.object(
        termination,
        "terminate_authoritative_task_generation",
        side_effect=delayed_supersede,
    ):
        synchronize_request = asyncio.create_task(
            _post_webhook(
                client,
                repo["webhook_secret"],
                _pr_payload(
                    "owner/review-waiting-retry",
                    action="synchronize",
                    head_sha="head-sha-2",
                ),
            )
        )
        await supersede_holds_operation_lock.wait()
        retry_request = asyncio.create_task(
            client.post(f"/api/tasks/{old_task_id}/retry")
        )
        await asyncio.sleep(0)
        assert not retry_request.done()
        release_supersede.set()
        synchronized = await synchronize_request
        retry_response = await retry_request

    assert synchronized.status_code == 200, synchronized.text
    assert retry_response.status_code == 409, retry_response.text
    chat_response = await client.post(
        f"/api/tasks/{old_task_id}/chat",
        json={"message": "please revive the obsolete review"},
    )
    assert chat_response.status_code == 409, chat_response.text
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_task_id)
        reviews = (
            await db.execute(
                select(PRReview).where(
                    PRReview.repo_id == repo["id"],
                    PRReview.pr_number == 42,
                )
            )
        ).scalars().all()
        assert len(reviews) == 2
        assert old_review.status == "superseded"
        assert old_task.status == "completed"
        assert old_task.retry_count == 0
        assert old_task.metadata_["pr_review_superseded"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_initial_status", ["executing", "completed"])
async def test_webhook_synchronize_worker_review_stops_authoritative_generation(
    client,
    session_factory,
    remote_initial_status,
):
    """Worker reviews use the locked internal full-lifecycle endpoint."""

    import backend.main

    repo = await _create_repo(
        client,
        "owner/worker-review",
        worker_id=77,
    )
    opened = await _post_webhook(
        client,
        repo["webhook_secret"],
        _pr_payload("owner/worker-review", action="opened"),
    )
    old_review_id = opened.json()["review_id"]
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_review.task_id)
        old_task.status = "executing"
        await db.commit()
        old_task_id = old_task.id

    operation_lock = asyncio.Lock()
    migration_lock = asyncio.Lock()
    calls: list[tuple[str, str]] = []

    async def authoritative_worker_call(
        routing_task,
        method,
        path,
        body=None,
        **kwargs,
    ):
        assert routing_task.id == old_task_id
        assert routing_task.worker_id == 77
        assert operation_lock.locked()
        assert migration_lock.locked()
        assert kwargs["operation_lock_held"] is True
        assert kwargs["require_json"] is True
        calls.append((method, path))
        if method == "GET":
            return {
                "id": old_task_id,
                "status": remote_initial_status,
                "retry_count": 0,
            }
        assert method == "POST"
        assert path == f"/api/tasks/{old_task_id}/terminate-generation"
        assert body == {
            "expected_status": remote_initial_status,
            "expected_retry_count": 0,
            "expected_instance_id": None,
            "expected_started_at": None,
            "expected_completed_at": None,
        }
        return {
            "id": old_task_id,
            "status": "completed",
            "retry_count": 0,
            "error_message": "Superseded by new PR push",
            "metadata_": {"pr_review_superseded": True},
        }

    with (
        patch.object(
            backend.main,
            "task_migrator",
            SimpleNamespace(_locks={old_task_id: migration_lock}),
        ),
        patch.object(
            backend.main.worker_proxy,
            "task_operation_lock",
            return_value=operation_lock,
        ),
        patch.object(
            backend.main.worker_proxy,
            "proxy_to_worker",
            new_callable=AsyncMock,
            side_effect=authoritative_worker_call,
        ),
    ):
        synchronized = await _post_webhook(
            client,
            repo["webhook_secret"],
            _pr_payload(
                "owner/worker-review",
                action="synchronize",
                head_sha="head-sha-2",
            ),
        )

    assert synchronized.status_code == 200, synchronized.text
    assert synchronized.json()["status"] == "accepted"
    assert calls == [
        ("GET", f"/api/tasks/{old_task_id}"),
        ("POST", f"/api/tasks/{old_task_id}/terminate-generation"),
    ]
    assert not operation_lock.locked()
    assert not migration_lock.locked()
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_task_id)
        new_review = await db.get(PRReview, synchronized.json()["review_id"])
        new_task = await db.get(Task, new_review.task_id)
        assert old_review.status == "superseded"
        assert old_task.status == "completed"
        assert old_task.worker_id == 77
        assert old_task.metadata_ == {
            "pr_review_id": old_review_id,
            "pr_review_superseded": True,
        }
        assert new_review.status == "reviewing"
        assert new_task.worker_id == 77


@pytest.mark.asyncio
async def test_webhook_synchronize_worker_lost_response_retries_terminal_cleanup(
    client,
    session_factory,
):
    """A lost response is fail-closed, then a terminal retry converges."""

    import backend.main

    repo = await _create_repo(
        client,
        "owner/worker-review-timeout",
        worker_id=78,
    )
    opened = await _post_webhook(
        client,
        repo["webhook_secret"],
        _pr_payload("owner/worker-review-timeout", action="opened"),
    )
    old_review_id = opened.json()["review_id"]
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_review.task_id)
        old_task.status = "executing"
        await db.commit()
        old_task_id = old_task.id

    operation_lock = asyncio.Lock()
    migration_lock = asyncio.Lock()
    post_attempts = 0

    async def lost_worker_response(
        _routing_task,
        method,
        _path,
        body=None,
        **_kwargs,
    ):
        nonlocal post_attempts
        if method == "GET":
            return {
                "id": old_task_id,
                "status": (
                    "executing"
                    if post_attempts == 0
                    else "completed"
                ),
                "retry_count": 0,
                "metadata_": (
                    {"pr_review_superseded": True}
                    if post_attempts
                    else None
                ),
            }
        assert body == {
            "expected_status": (
                "executing" if post_attempts == 0 else "completed"
            ),
            "expected_retry_count": 0,
            "expected_instance_id": None,
            "expected_started_at": None,
            "expected_completed_at": None,
        }
        post_attempts += 1
        if post_attempts == 1:
            raise TimeoutError("response lost after remote commit")
        return {
            "id": old_task_id,
            "status": "completed",
            "retry_count": 0,
            "error_message": "Superseded by new PR push",
            "metadata_": {"pr_review_superseded": True},
        }

    with (
        patch.object(
            backend.main,
            "task_migrator",
            SimpleNamespace(_locks={old_task_id: migration_lock}),
        ),
        patch.object(
            backend.main.worker_proxy,
            "task_operation_lock",
            return_value=operation_lock,
        ),
        patch.object(
            backend.main.worker_proxy,
            "proxy_to_worker",
            new_callable=AsyncMock,
            side_effect=lost_worker_response,
        ),
    ):
        first_attempt = await _post_webhook(
            client,
            repo["webhook_secret"],
            _pr_payload(
                "owner/worker-review-timeout",
                action="synchronize",
                head_sha="head-sha-2",
            ),
        )
        assert first_attempt.status_code == 409, first_attempt.text
        assert "no new review was created" in first_attempt.json()["detail"]
        assert not operation_lock.locked()
        assert not migration_lock.locked()
        async with session_factory() as db:
            old_review = await db.get(PRReview, old_review_id)
            old_task = await db.get(Task, old_task_id)
            reviews = (
                await db.execute(
                    select(PRReview).where(
                        PRReview.repo_id == repo["id"],
                        PRReview.pr_number == 42,
                    )
                )
            ).scalars().all()
            assert len(reviews) == 1
            assert old_review.status == "reviewing"
            # The Manager cannot assume the timed-out remote mutation landed.
            assert old_task.status == "executing"
            assert old_task.worker_id == 78

        second_attempt = await _post_webhook(
            client,
            repo["webhook_secret"],
            _pr_payload(
                "owner/worker-review-timeout",
                action="synchronize",
                head_sha="head-sha-2",
            ),
        )

    assert second_attempt.status_code == 200, second_attempt.text
    assert second_attempt.json()["status"] == "accepted"
    assert post_attempts == 2
    assert not operation_lock.locked()
    assert not migration_lock.locked()
    async with session_factory() as db:
        old_review = await db.get(PRReview, old_review_id)
        old_task = await db.get(Task, old_task_id)
        reviews = (
            await db.execute(
                select(PRReview).where(
                    PRReview.repo_id == repo["id"],
                    PRReview.pr_number == 42,
                )
            )
        ).scalars().all()
        assert len(reviews) == 2
        assert old_review.status == "superseded"
        assert old_task.status == "completed"
        assert old_task.worker_id == 78
        assert old_task.metadata_ == {
            "pr_review_id": old_review_id,
            "pr_review_superseded": True,
        }


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
async def test_create_repo_defaults_to_configured_provider(client):
    with patch("backend.api.pr_monitor.settings.default_provider", "codex"):
        data = await _create_repo(client, repo_full_name="owner/default-repo")
    assert data["provider"] == "codex"


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
