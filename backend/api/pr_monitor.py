import hashlib
import hmac
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.pr_monitor import MonitoredRepo, PRReview
from backend.schemas.pr_monitor import (
    MonitoredRepoCreate,
    MonitoredRepoUpdate,
    MonitoredRepoResponse,
    MonitoredRepoDetailResponse,
    PRReviewResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pr-monitor", tags=["pr-monitor"])
webhook_router = APIRouter(prefix="/api/github", tags=["pr-monitor"])


@router.get("/repos", response_model=list[MonitoredRepoResponse])
async def list_repos(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(MonitoredRepo).order_by(desc(MonitoredRepo.created_at))
    )
    return result.scalars().all()


@router.post("/repos", response_model=MonitoredRepoDetailResponse)
async def create_repo(body: MonitoredRepoCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(
        select(MonitoredRepo).where(MonitoredRepo.repo_full_name == body.repo_full_name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Repository '{body.repo_full_name}' already monitored")

    repo = MonitoredRepo(
        repo_full_name=body.repo_full_name,
        project_id=body.project_id,
        auto_merge=body.auto_merge,
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
    db: AsyncSession = Depends(get_db),
):
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
async def delete_repo(repo_id: int, db: AsyncSession = Depends(get_db)):
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
async def toggle_repo(repo_id: int, db: AsyncSession = Depends(get_db)):
    repo = await db.get(MonitoredRepo, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")
    repo.enabled = not repo.enabled
    await db.commit()
    await db.refresh(repo)
    return repo


@router.post("/repos/{repo_id}/regenerate-secret", response_model=MonitoredRepoDetailResponse)
async def regenerate_secret(repo_id: int, db: AsyncSession = Depends(get_db)):
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

    pr_number = pr.get("number")
    pr_title = pr.get("title", "")
    pr_url = pr.get("html_url", "")

    # Dedup: check for in-progress review
    existing_review = await db.execute(
        select(PRReview).where(
            PRReview.repo_id == repo.id,
            PRReview.pr_number == pr_number,
            PRReview.status.in_(["pending", "reviewing"]),
        )
    )
    if action == "synchronize":
        # Mark old reviews as superseded
        old_reviews = existing_review.scalars().all()
        for old in old_reviews:
            old.status = "superseded"
    elif existing_review.scalar_one_or_none():
        return {"status": "ignored", "reason": "review already in progress"}

    # Import and call service
    from backend.services.pr_review_service import create_pr_review_task

    review = await create_pr_review_task(db, repo, {
        "number": pr_number,
        "title": pr_title,
        "author": pr_author,
        "url": pr_url,
    })

    return {"status": "accepted", "review_id": review.id}
