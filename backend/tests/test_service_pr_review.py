"""Tests for backend/services/pr_review_service.py."""
import pytest
import pytest_asyncio
from sqlalchemy import update
from unittest.mock import AsyncMock, MagicMock, patch

from backend.models.pr_monitor import MonitoredRepo, PRReview
from backend.models.task import Task
from backend.services import pr_review_service
from backend.services.pr_review_service import (
    GhError,
    build_review_prompt,
    check_and_update_review,
    create_pr_review_task,
)


PR_DATA = {
    "number": 7,
    "head_sha": "abc123",
    "delivery_id": "delivery-7",
    "title": "Fix bug",
    "author": "alice",
    "url": "https://github.com/owner/repo/pull/7",
}


def _make_repo(**overrides) -> MonitoredRepo:
    defaults = dict(
        repo_full_name="owner/repo",
        webhook_secret="s" * 64,
        auto_merge=False,
        default_branch="main",
        allowed_authors=[],
        review_model="claude-sonnet-4-6",
    )
    defaults.update(overrides)
    return MonitoredRepo(**defaults)


@pytest_asyncio.fixture
async def repo(db_session):
    r = _make_repo()
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)
    return r


# === build_review_prompt ===


def test_build_review_prompt_auto_merge_on():
    prompt = build_review_prompt(_make_repo(auto_merge=True), PR_DATA)
    assert "gh pr view 7 --repo owner/repo" in prompt
    assert "gh pr merge 7 --repo owner/repo --merge" in prompt
    assert "approved_merged" in prompt
    assert "PR_REVIEW_RESULT:" in prompt


def test_build_review_prompt_auto_merge_off():
    prompt = build_review_prompt(_make_repo(auto_merge=False), PR_DATA)
    assert "gh pr merge" not in prompt
    assert "lgtm_comment" in prompt
    assert "gh pr review 7 --repo owner/repo --approve" in prompt


# === create_pr_review_task ===


@pytest.mark.asyncio
async def test_create_pr_review_task_happy_path(db_session, repo):
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    with patch("backend.main.broadcaster", mock_broadcaster):
        review = await create_pr_review_task(db_session, repo, PR_DATA)

    assert review.id is not None
    assert review.status == "reviewing"
    assert review.pr_number == 7
    assert review.head_sha == "abc123"
    assert review.delivery_id == "delivery-7"
    assert review.task_id is not None

    task = await db_session.get(Task, review.task_id)
    assert task.title == "PR Review: owner/repo#7"
    assert task.mode == "auto"
    assert task.model == "claude-sonnet-4-6"
    assert task.metadata_ == {"pr_review_id": review.id}
    assert "gh pr diff 7" in task.description

    mock_broadcaster.broadcast.assert_awaited_once()
    channel, msg = mock_broadcaster.broadcast.await_args.args
    assert channel == "pr-monitor"
    assert msg["type"] == "review_created"
    assert msg["review_id"] == review.id
    assert msg["task_id"] == task.id


@pytest.mark.asyncio
async def test_create_pr_review_task_broadcast_failure_logged_not_raised(db_session, repo):
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock(side_effect=RuntimeError("ws down"))
    with patch("backend.main.broadcaster", mock_broadcaster), \
         patch.object(pr_review_service.logger, "warning") as warn_mock:
        review = await create_pr_review_task(db_session, repo, PR_DATA)

    assert review.status == "reviewing"
    warn_mock.assert_called_once()
    assert "WebSocket broadcast failed" in warn_mock.call_args.args[0]


# === check_and_update_review ===


async def _make_review(db_session, repo, status="reviewing") -> PRReview:
    review = PRReview(
        repo_id=repo.id, pr_number=7, pr_title="Fix bug",
        pr_author="alice", pr_url="http://x", status=status,
    )
    db_session.add(review)
    await db_session.commit()
    await db_session.refresh(review)
    return review


@pytest.fixture
def no_broadcast():
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    with patch("backend.main.broadcaster", mock_broadcaster):
        yield mock_broadcaster


@pytest.mark.asyncio
async def test_check_and_update_review_merged(db_session, repo, no_broadcast):
    review = await _make_review(db_session, repo)
    with patch.object(pr_review_service, "_gh_pr_view", AsyncMock(return_value={
        "state": "MERGED", "mergedAt": "2026-06-11T00:00:00Z", "reviews": [],
    })):
        await check_and_update_review(db_session, review.id, "owner/repo")

    await db_session.refresh(review)
    assert review.status == "merged"
    assert review.action_taken == "approved_merged"
    assert review.completed_at is not None
    no_broadcast.broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_and_update_review_approved(db_session, repo, no_broadcast):
    review = await _make_review(db_session, repo)
    with patch.object(pr_review_service, "_gh_pr_view", AsyncMock(return_value={
        "state": "OPEN", "mergedAt": None, "reviews": [{"state": "APPROVED"}],
    })):
        await check_and_update_review(db_session, review.id, "owner/repo")

    await db_session.refresh(review)
    assert review.status == "approved"
    assert review.action_taken == "lgtm_comment"


@pytest.mark.asyncio
async def test_check_and_update_review_changes_requested(db_session, repo, no_broadcast):
    review = await _make_review(db_session, repo)
    with patch.object(pr_review_service, "_gh_pr_view", AsyncMock(return_value={
        "state": "OPEN", "mergedAt": None, "reviews": [{"state": "CHANGES_REQUESTED"}],
    })):
        await check_and_update_review(db_session, review.id, "owner/repo")

    await db_session.refresh(review)
    assert review.status == "commented"
    assert review.action_taken == "review_comments"


@pytest.mark.asyncio
async def test_check_and_update_review_skips_terminal_status(db_session, repo, no_broadcast):
    review = await _make_review(db_session, repo, status="merged")
    gh_mock = AsyncMock()
    with patch.object(pr_review_service, "_gh_pr_view", gh_mock):
        await check_and_update_review(db_session, review.id, "owner/repo")
    gh_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_and_update_review_auth_error_no_retry(db_session, repo, no_broadcast):
    review = await _make_review(db_session, repo)
    gh_mock = AsyncMock(side_effect=GhError("HTTP 401: Bad credentials. Run `gh auth login`."))
    with patch.object(pr_review_service, "_gh_pr_view", gh_mock):
        await check_and_update_review(db_session, review.id, "owner/repo")

    await db_session.refresh(review)
    assert review.status == "error"
    assert "gh authentication error" in review.review_summary
    assert "gh auth login" in review.review_summary
    # Auth errors must NOT be retried
    assert gh_mock.await_count == 1


@pytest.mark.asyncio
async def test_check_and_update_review_transient_failure_retried_then_error(
    db_session, repo, no_broadcast, monkeypatch
):
    monkeypatch.setattr(pr_review_service, "GH_RETRY_DELAY_SECONDS", 0)
    review = await _make_review(db_session, repo)
    gh_mock = AsyncMock(side_effect=GhError("connect: network is unreachable"))
    with patch.object(pr_review_service, "_gh_pr_view", gh_mock):
        await check_and_update_review(db_session, review.id, "owner/repo")

    await db_session.refresh(review)
    assert review.status == "error"
    assert "network/other" in review.review_summary
    # Exactly one retry => two attempts total
    assert gh_mock.await_count == 2


@pytest.mark.asyncio
async def test_check_and_update_review_transient_failure_retry_succeeds(
    db_session, repo, no_broadcast, monkeypatch
):
    monkeypatch.setattr(pr_review_service, "GH_RETRY_DELAY_SECONDS", 0)
    review = await _make_review(db_session, repo)
    gh_mock = AsyncMock(side_effect=[
        GhError("timeout"),
        {"state": "MERGED", "mergedAt": "2026-06-11T00:00:00Z", "reviews": []},
    ])
    with patch.object(pr_review_service, "_gh_pr_view", gh_mock):
        await check_and_update_review(db_session, review.id, "owner/repo")

    await db_session.refresh(review)
    assert review.status == "merged"
    assert gh_mock.await_count == 2


@pytest.mark.asyncio
async def test_check_and_update_review_cannot_overwrite_superseded_during_gh_wait(
    db_session,
    repo,
    no_broadcast,
):
    """A stale gh result must not move a synchronized review out of superseded."""

    review = await _make_review(db_session, repo)

    async def supersede_before_gh_returns(*_args, **_kwargs):
        await db_session.execute(
            update(PRReview)
            .where(PRReview.id == review.id)
            .values(status="superseded")
        )
        await db_session.commit()
        return {
            "state": "OPEN",
            "mergedAt": None,
            "reviews": [{"state": "APPROVED"}],
        }

    with patch.object(
        pr_review_service,
        "_gh_pr_view",
        side_effect=supersede_before_gh_returns,
    ):
        await check_and_update_review(db_session, review.id, "owner/repo")

    await db_session.refresh(review)
    assert review.status == "superseded"
    assert review.action_taken is None
    assert review.completed_at is None
    no_broadcast.broadcast.assert_not_awaited()


# === _gh_pr_view (subprocess mocked) ===


def _mock_proc(stdout=b"", stderr=b"", returncode=0):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


@pytest.mark.asyncio
async def test_gh_pr_view_success():
    proc = _mock_proc(stdout=b'{"state": "OPEN", "mergedAt": null, "reviews": []}')
    with patch.object(
        pr_review_service.asyncio, "create_subprocess_exec",
        AsyncMock(return_value=proc),
    ) as exec_mock:
        info = await pr_review_service._gh_pr_view(7, "owner/repo")
    assert info["state"] == "OPEN"
    args = exec_mock.await_args.args
    assert args[:4] == ("gh", "pr", "view", "7")


@pytest.mark.asyncio
async def test_gh_pr_view_auth_failure_classified():
    proc = _mock_proc(stderr=b"HTTP 401: Bad credentials", returncode=1)
    with patch.object(
        pr_review_service.asyncio, "create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        with pytest.raises(GhError) as exc_info:
            await pr_review_service._gh_pr_view(7, "owner/repo")
    assert exc_info.value.is_auth is True


@pytest.mark.asyncio
async def test_gh_pr_view_network_failure_classified_transient():
    proc = _mock_proc(stderr=b"dial tcp: lookup api.github.com: no such host", returncode=1)
    with patch.object(
        pr_review_service.asyncio, "create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        with pytest.raises(GhError) as exc_info:
            await pr_review_service._gh_pr_view(7, "owner/repo")
    assert exc_info.value.is_auth is False


@pytest.mark.asyncio
async def test_gh_pr_view_spawn_failure_wrapped():
    with patch.object(
        pr_review_service.asyncio, "create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("gh not found")),
    ):
        with pytest.raises(GhError) as exc_info:
            await pr_review_service._gh_pr_view(7, "owner/repo")
    assert exc_info.value.is_auth is False


@pytest.mark.asyncio
async def test_review_task_assigned_to_pr_monitor_project(client, session_factory):
    """审核任务自动归入 PR-Monitor 项目：无则创建，有则复用。"""
    from sqlalchemy import select
    from backend.models.project import Project
    from backend.models.task import Task
    from backend.models.pr_monitor import MonitoredRepo
    from backend.services.pr_review_service import (
        create_pr_review_task, PR_MONITOR_PROJECT_NAME,
    )

    pr_data = {"number": 7, "title": "t", "author": "alice",
               "url": "https://github.com/o/r/pull/7"}

    async with session_factory() as db:
        repo = MonitoredRepo(repo_full_name="o/r", webhook_secret="s")
        db.add(repo)
        await db.flush()
        review1 = await create_pr_review_task(db, repo, pr_data)

    async with session_factory() as db:
        proj = (await db.execute(
            select(Project).where(Project.name == PR_MONITOR_PROJECT_NAME)
        )).scalar_one()
        t1 = await db.get(Task, review1.task_id)
        assert t1.project_id == proj.id

        # 第二次创建复用同一项目（不重复建）
        repo2 = (await db.execute(select(MonitoredRepo))).scalars().first()
        pr_data2 = dict(pr_data, number=8, url="https://github.com/o/r/pull/8")
        review2 = await create_pr_review_task(db, repo2, pr_data2)

    async with session_factory() as db:
        count = (await db.execute(
            select(Project).where(Project.name == PR_MONITOR_PROJECT_NAME)
        )).scalars().all()
        assert len(count) == 1
        t2 = await db.get(Task, review2.task_id)
        assert t2.project_id == proj.id


# === provider 透传（codex 审核）===


@pytest.mark.asyncio
async def test_create_pr_review_task_codex_provider(db_session):
    """repo.provider=codex → 审核 task 用 codex，未配 review_model 时补 codex 默认模型。"""
    r = _make_repo(provider="codex", review_model=None)
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)

    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    with patch("backend.main.broadcaster", mock_broadcaster):
        review = await create_pr_review_task(db_session, r, PR_DATA)

    task = await db_session.get(Task, review.task_id)
    assert task.provider == "codex"
    from backend.config import settings as app_settings
    assert task.model == app_settings.default_codex_model


@pytest.mark.asyncio
async def test_create_pr_review_task_default_provider_claude(db_session, repo):
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    with patch("backend.main.broadcaster", mock_broadcaster):
        review = await create_pr_review_task(db_session, repo, PR_DATA)

    task = await db_session.get(Task, review.task_id)
    assert task.provider == "claude"
    assert task.model == "claude-sonnet-4-6"  # review_model 原样保留
