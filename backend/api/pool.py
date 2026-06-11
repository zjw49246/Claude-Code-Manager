"""API endpoints for Claude account pool management."""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/pool", tags=["pool"])


def _get_pool():
    from backend.main import dispatcher
    if not dispatcher.pool:
        raise HTTPException(status_code=404, detail="Pool is not enabled. Set POOL_ENABLED=true in .env")
    return dispatcher.pool


@router.get("/status")
async def pool_status():
    pool = _get_pool()
    return pool.status()


@router.get("/usage")
async def pool_usage():
    """Pool status merged with per-account quota utilization (OAuth usage API)."""
    pool = _get_pool()
    status = pool.status()
    usage_by_id = {u["id"]: u for u in await pool.fetch_usage()}
    for account in status["accounts"]:
        u = usage_by_id.get(account["id"], {})
        account["subscription_type"] = u.get("subscription_type")
        account["usage"] = u.get("usage")
        account["usage_error"] = u.get("error")
    return status


@router.post("/reload")
async def pool_reload():
    pool = _get_pool()
    pool.reload()
    return pool.status()


@router.post("/accounts/{account_id}/clear-cooldown")
async def clear_cooldown(account_id: str):
    pool = _get_pool()
    pool.clear_cooldown(account_id)
    return {"ok": True, "account_id": account_id}


@router.post("/preferred")
async def set_preferred(body: dict):
    """Pin an account for subsequent launches (manual switch).

    Body: {"account_id": "account-1"} or {"account_id": null} to clear.
    Session continuity is handled by the existing launch path: every launch
    re-selects an account and hardlink-migrates the session JSONL, so the
    next turn resumes seamlessly on the pinned account. If the pinned
    account is rate-limited, auto rotation falls back to the others.
    """
    pool = _get_pool()
    account_id = body.get("account_id")
    if not pool.set_preferred(account_id):
        raise HTTPException(status_code=404, detail=f"Unknown account: {account_id}")
    return {"ok": True, "preferred": pool.preferred_account_id}
