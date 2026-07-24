import asyncio
import json
import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.pr_monitor import MonitoredRepo, PRReview
from backend.models.task import Task

logger = logging.getLogger(__name__)

# Markers in gh output that indicate an authentication problem (not transient).
GH_AUTH_ERROR_MARKERS = ("gh auth login", "http 401", "http 403", "bad credentials")

# Delay before the single retry of a transient gh failure (tests override this).
GH_RETRY_DELAY_SECONDS = 2.0


class GhError(Exception):
    """A `gh` CLI invocation failed. `is_auth` distinguishes auth errors."""

    def __init__(self, message: str):
        super().__init__(message)
        low = message.lower()
        self.is_auth = any(marker in low for marker in GH_AUTH_ERROR_MARKERS)


async def _gh_pr_view(pr_number: int, repo_full_name: str) -> dict:
    """Run `gh pr view --json ...` and return parsed JSON. Raises GhError."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "view", str(pr_number),
            "--repo", repo_full_name,
            "--json", "state,mergedAt,reviews",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except GhError:
        raise
    except Exception as e:
        raise GhError(str(e)) from e

    if proc.returncode != 0:
        output = ((stderr or b"") + b"\n" + (stdout or b"")).decode(errors="replace").strip()
        raise GhError(output or f"gh exited with code {proc.returncode}")

    try:
        return json.loads(stdout.decode())
    except Exception as e:
        raise GhError(f"invalid gh output: {e}") from e


def build_review_prompt(repo: MonitoredRepo, pr_data: dict) -> str:
    pr_number = pr_data["number"]
    repo_name = repo.repo_full_name

    if repo.auto_merge:
        action_instructions = """
- If the code looks good (no bugs, no security issues, no major problems):
  1. Run: gh pr review {pr_number} --repo {repo_name} --approve --body "LGTM - automated review passed"
     (If approve fails because the PR author is the same account — GitHub forbids
     self-approval — fall back to: gh pr comment {pr_number} --repo {repo_name} --body "LGTM - automated review passed (self-PR)")
  2. Run: gh pr merge {pr_number} --repo {repo_name} --merge
  3. Output: PR_REVIEW_RESULT: approved_merged
- If there are issues:
  1. Run: gh pr review {pr_number} --repo {repo_name} --request-changes --body "<your detailed review comments>"
  2. Output: PR_REVIEW_RESULT: review_comments
""".format(pr_number=pr_number, repo_name=repo_name)
    else:
        action_instructions = """
- If the code looks good (no bugs, no security issues, no major problems):
  1. Run: gh pr review {pr_number} --repo {repo_name} --approve --body "LGTM - automated review passed"
     (If approve fails because the PR author is the same account — GitHub forbids
     self-approval — fall back to: gh pr comment {pr_number} --repo {repo_name} --body "LGTM - automated review passed (self-PR, approval not permitted)")
  2. Output: PR_REVIEW_RESULT: lgtm_comment
- If there are issues:
  1. Run: gh pr review {pr_number} --repo {repo_name} --request-changes --body "<your detailed review comments>"
  2. Output: PR_REVIEW_RESULT: review_comments
""".format(pr_number=pr_number, repo_name=repo_name)

    return """You are reviewing a GitHub Pull Request.

## Step 1: Read the PR
Run these commands to understand the PR:
```
gh pr view {pr_number} --repo {repo_name}
gh pr diff {pr_number} --repo {repo_name}
```

## Step 2: Review the code
Evaluate the changes for:
- **Correctness**: Logic errors, edge cases, potential bugs
- **Security**: Injection vulnerabilities, auth issues, data exposure
- **Performance**: N+1 queries, unnecessary allocations, blocking calls
- **Code quality**: Naming, structure, duplication
- **Tests**: Are changes covered by tests?

## Step 3: Take action
{action_instructions}

IMPORTANT: The last line of your output MUST be exactly:
PR_REVIEW_RESULT: <one of: approved_merged, lgtm_comment, review_comments, error>
""".format(
        pr_number=pr_number,
        repo_name=repo_name,
        action_instructions=action_instructions,
    )


PR_MONITOR_PROJECT_NAME = "PR-Monitor"


async def _get_or_create_pr_monitor_project(db: AsyncSession) -> int:
    """审核任务统一归入 PR-Monitor 项目；不存在则创建。"""
    from sqlalchemy import select
    from backend.models.project import Project

    result = await db.execute(
        select(Project).where(Project.name == PR_MONITOR_PROJECT_NAME)
    )
    project = result.scalar_one_or_none()
    if project is None:
        project = Project(name=PR_MONITOR_PROJECT_NAME)
        db.add(project)
        await db.flush()
    return project.id


async def create_pr_review_task(
    db: AsyncSession, repo: MonitoredRepo, pr_data: dict
) -> PRReview:
    review = PRReview(
        repo_id=repo.id,
        pr_number=pr_data["number"],
        head_sha=pr_data.get("head_sha"),
        delivery_id=pr_data.get("delivery_id"),
        pr_title=pr_data["title"],
        pr_author=pr_data["author"],
        pr_url=pr_data["url"],
        status="pending",
    )
    db.add(review)
    await db.flush()

    prompt = build_review_prompt(repo, pr_data)

    provider = (repo.provider or "claude").lower()
    # 直接构造 ORM 会绕过 POST /api/tasks 的 per-provider 默认模型逻辑，
    # codex 且未配 review_model 时补上默认，避免 CLI 侧模型漂移
    model = repo.review_model
    if not model and provider == "codex":
        from backend.config import settings as app_settings
        model = app_settings.default_codex_model
    task = Task(
        title=f"PR Review: {repo.repo_full_name}#{pr_data['number']}",
        description=prompt,
        mode="auto",
        tags=["pr-review"],
        metadata_={"pr_review_id": review.id},
        provider=provider,
        model=model,
        project_id=await _get_or_create_pr_monitor_project(db),
        worker_id=repo.worker_id,
    )
    db.add(task)
    await db.flush()

    review.task_id = task.id
    review.status = "reviewing"

    await db.commit()
    await db.refresh(review)

    try:
        from backend.main import dispatcher
        if dispatcher:
            dispatcher.wake()
    except Exception:
        logger.debug("Could not wake dispatcher for PR review task", exc_info=True)

    logger.info(
        "Created PR review task %d for %s#%d",
        task.id, repo.repo_full_name, pr_data["number"],
    )

    # Broadcast via WebSocket
    try:
        from backend.main import broadcaster
        await broadcaster.broadcast("pr-monitor", {
            "type": "review_created",
            "review_id": review.id,
            "repo_id": repo.id,
            "pr_number": pr_data["number"],
            "task_id": task.id,
        })
    except Exception as e:
        logger.warning(
            "WebSocket broadcast failed for PR review %d (non-critical): %s",
            review.id, e,
        )

    return review


async def check_and_update_review(
    db: AsyncSession, pr_review_id: int, repo_full_name: str
):
    review = await db.get(PRReview, pr_review_id)
    if not review:
        logger.warning("PR review %d not found", pr_review_id)
        return

    if review.status in ("approved", "merged", "commented", "error", "superseded"):
        return
    expected_status = review.status
    expected_task_id = review.task_id
    pr_number = review.pr_number

    async def commit_exact_result(**values) -> bool:
        """Commit only while synchronize has not replaced this review."""

        predicates = [
            PRReview.id == pr_review_id,
            PRReview.status == expected_status,
            (
                PRReview.task_id.is_(None)
                if expected_task_id is None
                else PRReview.task_id == expected_task_id
            ),
        ]
        changed = await db.execute(
            update(PRReview).where(*predicates).values(**values)
        )
        if not changed.rowcount:
            await db.rollback()
            logger.info(
                "Discarding stale PR review result for review %s / task %s",
                pr_review_id,
                expected_task_id,
            )
            return False
        await db.commit()
        return True

    pr_info = None
    for attempt in (1, 2):
        try:
            pr_info = await _gh_pr_view(pr_number, repo_full_name)
            break
        except GhError as e:
            if e.is_auth:
                logger.error("gh authentication error while checking PR status: %s", e)
                await commit_exact_result(
                    status="error",
                    review_summary=(
                        "gh authentication error (run `gh auth login` for "
                        f"the backend user): {e}"
                    ),
                    completed_at=datetime.utcnow(),
                )
                return
            if attempt == 1:
                logger.warning("gh pr view failed (attempt 1/2), retrying: %s", e)
                await asyncio.sleep(GH_RETRY_DELAY_SECONDS)
                continue
            logger.error("Failed to check PR status after retry: %s", e)
            await commit_exact_result(
                status="error",
                review_summary=(
                    "Failed to check PR status (network/other, after 1 "
                    f"retry): {e}"
                ),
                completed_at=datetime.utcnow(),
            )
            return

    state = pr_info.get("state", "").upper()
    merged_at = pr_info.get("mergedAt")

    if merged_at:
        new_status = "merged"
        action_taken = "approved_merged"
    elif state == "CLOSED":
        new_status = "error"
        action_taken = "error"
    else:
        reviews = pr_info.get("reviews", [])
        if reviews:
            latest = reviews[-1]
            review_state = latest.get("state", "")
            if review_state == "APPROVED":
                new_status = "approved"
                action_taken = "lgtm_comment"
            elif review_state == "CHANGES_REQUESTED":
                new_status = "commented"
                action_taken = "review_comments"
            else:
                new_status = "approved"
                action_taken = "lgtm_comment"
        else:
            new_status = "approved"
            action_taken = "lgtm_comment"

    if not await commit_exact_result(
        status=new_status,
        action_taken=action_taken,
        completed_at=datetime.utcnow(),
        review_summary=f"PR state: {state}, merged: {bool(merged_at)}",
    ):
        return

    try:
        from backend.main import broadcaster
        await broadcaster.broadcast("pr-monitor", {
            "type": "review_updated",
            "review_id": pr_review_id,
            "status": new_status,
            "action_taken": action_taken,
        })
    except Exception:
        logger.debug("WebSocket broadcast failed (non-critical)")
