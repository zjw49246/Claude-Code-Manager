import hashlib
import hmac
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import get_db
from backend.models.pr_monitor import MonitoredRepo, PRReview
from backend.models.task import Task
from backend.api.deps import get_current_user_id, get_current_user_role
from backend.schemas.pr_monitor import (
    MonitoredRepoCreate,
    MonitoredRepoUpdate,
    MonitoredRepoResponse,
    MonitoredRepoDetailResponse,
    PRReviewResponse,
)

logger = logging.getLogger(__name__)

_GH_LOGIN_CACHE: str | None = None


def _gh_login() -> str:
    """本机 gh CLI 登录的用户名（缓存；未登录返回空串）。"""
    global _GH_LOGIN_CACHE
    if _GH_LOGIN_CACHE is None:
        import subprocess
        try:
            r = subprocess.run(
                ["gh", "api", "user", "-q", ".login"],
                capture_output=True, text=True, timeout=10,
            )
            _GH_LOGIN_CACHE = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            _GH_LOGIN_CACHE = ""
    return _GH_LOGIN_CACHE


router = APIRouter(prefix="/api/pr-monitor", tags=["pr-monitor"])
webhook_router = APIRouter(prefix="/api/github", tags=["pr-monitor"])


@router.get("/webhook-info")
async def webhook_info():
    """Return the public webhook URL (from PUBLIC_BASE_URL), or null if unset."""
    base = settings.public_base_url.strip().rstrip("/")
    return {"webhook_url": f"{base}/api/github/webhook" if base else None}


@router.get("/repos", response_model=list[MonitoredRepoResponse])
async def list_repos(request: Request, db: AsyncSession = Depends(get_db)):
    user_role = get_current_user_role(request)
    user_id = get_current_user_id(request)
    stmt = select(MonitoredRepo).order_by(desc(MonitoredRepo.created_at))
    if user_role not in ("admin", "super_admin"):
        from backend.models.worker import Worker
        owned_worker_ids = select(Worker.id).where(Worker.owner_user_id == user_id)
        stmt = stmt.where(MonitoredRepo.worker_id.in_(owned_worker_ids))
    result = await db.execute(stmt)
    return result.scalars().all()


async def _require_pr_monitor_write(request: Request, db: AsyncSession):
    """Admin or Worker owner can manage PR monitors."""
    role = get_current_user_role(request)
    if role in ("admin", "super_admin"):
        return
    user_id = get_current_user_id(request)
    if user_id:
        from backend.models.worker import Worker
        has_worker = (await db.execute(
            select(Worker.id).where(Worker.owner_user_id == user_id).limit(1)
        )).scalar_one_or_none()
        if has_worker:
            return
    raise HTTPException(403, "You need a Worker or admin role to manage PR monitors")


@router.post("/repos", response_model=MonitoredRepoDetailResponse)
async def create_repo(request: Request, body: MonitoredRepoCreate, db: AsyncSession = Depends(get_db)):
    await _require_pr_monitor_write(request, db)
    existing = await db.execute(
        select(MonitoredRepo).where(MonitoredRepo.repo_full_name == body.repo_full_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Repository '{body.repo_full_name}' already monitored")

    # Validate worker_id: admin can use NULL (local) or any worker; member only own workers
    worker_id = getattr(body, 'worker_id', None)
    user_role = get_current_user_role(request)
    user_id = get_current_user_id(request)
    if worker_id is None and user_role not in ("admin", "super_admin"):
        raise HTTPException(403, "Only admin can create PR monitors on local machine")
    if worker_id is not None and user_role not in ("admin", "super_admin"):
        from backend.models.worker import Worker
        w = await db.get(Worker, worker_id)
        if not w or w.owner_user_id != user_id:
            raise HTTPException(403, "You can only create PR monitors on your own Worker")

    repo = MonitoredRepo(
        repo_full_name=body.repo_full_name,
        project_id=body.project_id,
        worker_id=worker_id,
        auto_merge=body.auto_merge,
        provider=body.provider,
        review_model=body.review_model,
        default_branch=body.default_branch,
        allowed_authors=body.allowed_authors,
        webhook_secret=secrets.token_hex(32),
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    return repo


@router.get("/repos/{repo_id}", response_model=MonitoredRepoDetailResponse)
async def get_repo(repo_id: int, db: AsyncSession = Depends(get_db)):
    repo = await db.get(MonitoredRepo, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")
    return repo


@router.put("/repos/{repo_id}", response_model=MonitoredRepoDetailResponse)
async def update_repo(
    repo_id: int,
    body: MonitoredRepoUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _require_pr_monitor_write(request, db)
    repo = await db.get(MonitoredRepo, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")

    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(repo, key, value)

    await db.commit()
    await db.refresh(repo)
    return repo


@router.delete("/repos/{repo_id}")
async def delete_repo(repo_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    await _require_pr_monitor_write(request, db)
    repo = await db.get(MonitoredRepo, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")

    await db.execute(
        select(PRReview).where(PRReview.repo_id == repo_id)
    )
    reviews = (await db.execute(
        select(PRReview).where(PRReview.repo_id == repo_id)
    )).scalars().all()
    for review in reviews:
        await db.delete(review)

    await db.delete(repo)
    await db.commit()
    return {"ok": True}


@router.post("/repos/{repo_id}/toggle", response_model=MonitoredRepoResponse)
async def toggle_repo(repo_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    await _require_pr_monitor_write(request, db)
    repo = await db.get(MonitoredRepo, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")
    repo.enabled = not repo.enabled
    await db.commit()
    await db.refresh(repo)
    return repo


@router.post("/repos/{repo_id}/regenerate-secret", response_model=MonitoredRepoDetailResponse)
async def regenerate_secret(repo_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    await _require_pr_monitor_write(request, db)
    repo = await db.get(MonitoredRepo, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")
    repo.webhook_secret = secrets.token_hex(32)
    await db.commit()
    await db.refresh(repo)
    return repo


@router.get("/repos/{repo_id}/reviews", response_model=list[PRReviewResponse])
async def list_reviews(
    repo_id: int,
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
):
    repo = await db.get(MonitoredRepo, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")

    offset = (page - 1) * size
    result = await db.execute(
        select(PRReview)
        .where(PRReview.repo_id == repo_id)
        .order_by(desc(PRReview.created_at))
        .offset(offset)
        .limit(size)
    )
    return result.scalars().all()


@router.get("/reviews/{review_id}", response_model=PRReviewResponse)
async def get_review(review_id: int, db: AsyncSession = Depends(get_db)):
    review = await db.get(PRReview, review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    return review


# --- Webhook endpoint ---

@webhook_router.post("/webhook")
async def github_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.body()

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    repo_full_name = payload.get("repository", {}).get("full_name")
    if not repo_full_name:
        return {"status": "ignored", "reason": "no repository info"}

    result = await db.execute(
        select(MonitoredRepo).where(MonitoredRepo.repo_full_name == repo_full_name)
    )
    repo = result.scalar_one_or_none()
    if not repo or not repo.enabled:
        return {"status": "ignored", "reason": "repository not monitored or disabled"}

    # HMAC-SHA256 signature verification
    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if not signature_header.startswith("sha256="):
        raise HTTPException(403, "Missing or invalid signature")

    expected_sig = "sha256=" + hmac.new(
        repo.webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature_header, expected_sig):
        raise HTTPException(403, "Invalid signature")

    # Only handle pull_request events
    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type != "pull_request":
        return {"status": "ignored", "reason": f"event type: {event_type}"}

    action = payload.get("action", "")
    if action not in ("opened", "synchronize"):
        return {"status": "ignored", "reason": f"action: {action}"}

    pr = payload.get("pull_request", {})

    # Skip draft PRs
    if pr.get("draft", False):
        return {"status": "ignored", "reason": "draft PR"}

    # Check target branch
    base_branch = pr.get("base", {}).get("ref", "")
    if base_branch != repo.default_branch:
        return {"status": "ignored", "reason": f"target branch: {base_branch}"}

    # Check allowed authors
    pr_author = pr.get("user", {}).get("login", "")
    allowed = repo.allowed_authors or []
    if allowed and pr_author not in allowed:
        return {"status": "ignored", "reason": f"author not allowed: {pr_author}"}

    # 自动屏蔽本机 gh 登录账号的 PR：审核者与作者同账号时 GitHub 禁止
    # self-approval，审了也无法 approve；除非白名单显式包含该账号
    own_login = _gh_login()
    if own_login and pr_author == own_login and pr_author not in allowed:
        return {"status": "ignored", "reason": f"self PR (gh login: {own_login})"}

    pr_number = pr.get("number")
    pr_title = pr.get("title", "")
    pr_url = pr.get("html_url", "")

    # Dedup: check for existing reviews (any non-terminal status)
    active_result = await db.execute(
        select(PRReview).where(
            PRReview.repo_id == repo.id,
            PRReview.pr_number == pr_number,
            PRReview.status.in_(["pending", "reviewing"]),
        )
    )
    active_reviews = active_result.scalars().all()

    superseded_task_ids = []
    if action == "synchronize":
        # Mark old reviews as superseded and cancel their tasks
        for old in active_reviews:
            old.status = "superseded"
            if old.task_id:
                old_task = await db.get(Task, old.task_id)
                if old_task and old_task.status not in ("completed", "failed"):
                    old_task.status = "completed"
                    old_task.error_message = "Superseded by new push"
                    superseded_task_ids.append(old.task_id)
                    logger.info("Cancelled task %d (superseded PR review)", old.task_id)
    elif active_reviews:
        return {"status": "ignored", "reason": "review already in progress"}
    elif action == "opened":
        # Also skip if a completed review already exists for this PR
        completed_result = await db.execute(
            select(func.count()).select_from(PRReview).where(
                PRReview.repo_id == repo.id,
                PRReview.pr_number == pr_number,
                PRReview.status.in_(["approved", "merged", "commented"]),
            )
        )
        if completed_result.scalar():
            return {"status": "ignored", "reason": "PR already reviewed"}

    # Import and call service
    from backend.services.pr_review_service import create_pr_review_task

    review = await create_pr_review_task(db, repo, {
        "number": pr_number,
        "title": pr_title,
        "author": pr_author,
        "url": pr_url,
    })

    if superseded_task_ids:
        # 显式 commit：不依赖 create_pr_review_task 内部恰好提交了 superseded
        # 改动（那个耦合将来加早退路径就断了）；已提交时这里是幂等空操作
        await db.commit()
        from backend.services.task_events import broadcast_status_change
        for tid in superseded_task_ids:
            await broadcast_status_change(tid, "completed")

    return {"status": "accepted", "review_id": review.id}
