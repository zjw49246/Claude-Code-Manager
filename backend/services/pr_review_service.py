import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.pr_monitor import MonitoredRepo, PRReview
from backend.models.task import Task

logger = logging.getLogger(__name__)


def build_review_prompt(repo: MonitoredRepo, pr_data: dict) -> str:
    pr_number = pr_data["number"]
    repo_name = repo.repo_full_name

    if repo.auto_merge:
        action_instructions = """
- If the code looks good (no bugs, no security issues, no major problems):
  1. Run: gh pr review {pr_number} --repo {repo_name} --approve --body "LGTM - automated review passed"
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


async def create_pr_review_task(
    db: AsyncSession, repo: MonitoredRepo, pr_data: dict
) -> PRReview:
    review = PRReview(
        repo_id=repo.id,
        pr_number=pr_data["number"],
        pr_title=pr_data["title"],
        pr_author=pr_data["author"],
        pr_url=pr_data["url"],
        status="pending",
    )
    db.add(review)
    await db.flush()

    prompt = build_review_prompt(repo, pr_data)

    task = Task(
        title=f"PR Review: {repo.repo_full_name}#{pr_data['number']}",
        description=prompt,
        mode="auto",
        tags=["pr-review"],
        metadata_={"pr_review_id": review.id},
        model=repo.review_model,
    )
    db.add(task)
    await db.flush()

    review.task_id = task.id
    review.status = "reviewing"

    await db.commit()
    await db.refresh(review)

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
    except Exception:
        logger.debug("WebSocket broadcast failed (non-critical)")

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

    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "view", str(review.pr_number),
            "--repo", repo_full_name,
            "--json", "state,mergedAt,reviews",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        import json
        pr_info = json.loads(stdout.decode())
    except Exception as e:
        logger.error("Failed to check PR status: %s", e)
        review.status = "error"
        review.review_summary = f"Failed to check PR status: {e}"
        review.completed_at = datetime.utcnow()
        await db.commit()
        return

    state = pr_info.get("state", "").upper()
    merged_at = pr_info.get("mergedAt")

    if merged_at:
        review.status = "merged"
        review.action_taken = "approved_merged"
    elif state == "CLOSED":
        review.status = "error"
        review.action_taken = "error"
    else:
        reviews = pr_info.get("reviews", [])
        if reviews:
            latest = reviews[-1]
            review_state = latest.get("state", "")
            if review_state == "APPROVED":
                review.status = "approved"
                review.action_taken = "lgtm_comment"
            elif review_state == "CHANGES_REQUESTED":
                review.status = "commented"
                review.action_taken = "review_comments"
            else:
                review.status = "approved"
                review.action_taken = "lgtm_comment"
        else:
            review.status = "approved"
            review.action_taken = "lgtm_comment"

    review.completed_at = datetime.utcnow()
    review.review_summary = f"PR state: {state}, merged: {bool(merged_at)}"
    await db.commit()

    try:
        from backend.main import broadcaster
        await broadcaster.broadcast("pr-monitor", {
            "type": "review_updated",
            "review_id": review.id,
            "status": review.status,
            "action_taken": review.action_taken,
        })
    except Exception:
        logger.debug("WebSocket broadcast failed (non-critical)")
