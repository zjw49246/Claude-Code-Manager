"""API endpoints for Claude account pool management."""

import asyncio
import glob
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/pool", tags=["pool"])

# 重新登录后台任务状态：account_id -> {"status": running|success|failed, ...}
_relogin_state: dict[str, dict] = {}


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


async def _watch_relogin(account_id: str, proc: asyncio.subprocess.Process):
    out, _ = await proc.communicate()
    tail = (out or b"").decode("utf-8", errors="replace")[-2000:]
    _relogin_state[account_id] = {
        "status": "success" if proc.returncode == 0 else "failed",
        "detail": tail,
        "finished_at": time.time(),
    }
    if proc.returncode == 0:
        try:
            _get_pool()._usage_cache = None  # 立即反映新凭证
        except HTTPException:
            pass


@router.post("/accounts/{account_id}/relogin")
async def relogin_account(account_id: str):
    """重新登录账号。先试 OAuth refresh（token 过期 ≠ 要重新登录，CLI 平时
    会自动刷，闲置账号刷一下就恢复）；refresh 真失败才跑 auto_login.py。"""
    pool = _get_pool()
    acc = pool.account(account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"Unknown account: {account_id}")

    # 1) OAuth refresh——绝大多数"过期"到这一步就解决了
    if await pool.refresh_oauth_token(account_id):
        _relogin_state.pop(account_id, None)
        return {"ok": True, "method": "refresh", "status": "success"}

    # 2) refresh 失败（refreshToken 失效/吊销）→ 真正重新登录
    state = _relogin_state.get(account_id)
    if state and state.get("status") == "running":
        return {"ok": True, "method": "auto_login", "status": "running"}
    root = Path(__file__).resolve().parents[2]
    # auto_login 的依赖（playwright/mitmproxy）装在仓库自带的 .login-venv，
    # 不在 CCM 主 venv 里——6/7 三个号就是用它登录的
    login_py = root / ".login-venv" / "bin" / "python3"
    if not login_py.exists():
        raise HTTPException(status_code=501, detail=(
            "Token 刷新失败，且 .login-venv 不存在（auto_login 依赖装在那里）。"
            f"请手动登录：python3 scripts/auto_login.py --email {acc.email} "
            f"--config-dir {acc.config_dir}"
        ))
    # auto_login 用 channel="chrome"（系统 Google Chrome，headed 过 Cloudflare）；
    # playwright 自带 chromium 仅作兜底
    has_browser = (
        Path("/opt/google/chrome/chrome").exists()
        or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        or glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    )
    if not has_browser:
        raise HTTPException(status_code=501, detail=(
            "Token 刷新失败，且找不到浏览器（auto_login 需要系统 Google Chrome）。"
            "安装 google-chrome-stable 或 .login-venv/bin/python3 -m playwright install chromium"
        ))
    script = root / "scripts" / "auto_login.py"
    proc = await asyncio.create_subprocess_exec(
        str(login_py), str(script), "--email", acc.email, "--config-dir", acc.config_dir,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    _relogin_state[account_id] = {"status": "running", "started_at": time.time()}
    asyncio.get_running_loop().create_task(_watch_relogin(account_id, proc))
    return {"ok": True, "method": "auto_login", "status": "running"}


@router.get("/accounts/{account_id}/relogin")
async def relogin_status(account_id: str):
    return _relogin_state.get(account_id) or {"status": "idle"}


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
