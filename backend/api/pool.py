"""API endpoints for Claude account pool management."""

import asyncio
import glob
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
        env={k: v for k, v in os.environ.items() if k != "DISPLAY"} | {"PYTHONUNBUFFERED": "1"},
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


# ---------------------------------------------------------------------------
# Add account (三参数自动登录)
# ---------------------------------------------------------------------------

class AddAccountRequest(BaseModel):
    email: str
    token: str  # 171mail 的接码 token 或 mail.com 的邮箱密码（按邮箱后缀自动判断）


# 后台 add 状态：key = email -> {"status": running|success|failed, ...}
_add_state: dict[str, dict] = {}


async def _watch_add(email: str, proc: asyncio.subprocess.Process):
    out, _ = await proc.communicate()
    tail = (out or b"").decode("utf-8", errors="replace")[-2000:]
    _add_state[email] = {
        "status": "success" if proc.returncode == 0 else "failed",
        "detail": tail,
        "finished_at": time.time(),
    }
    if proc.returncode == 0:
        try:
            _get_pool().reload()
            _get_pool()._usage_cache = None
        except Exception:
            pass


@router.post("/add")
async def add_account(body: AddAccountRequest):
    """自动登录新账号并加入号池。三参数：email、接码 token、接码渠道。

    后台跑 auto_login.py，前端轮询 GET /api/pool/add/{email} 看进度。"""
    email = body.email.strip()
    if not email or not body.token.strip():
        raise HTTPException(400, "email 和 token 必填")

    state = _add_state.get(email)
    if state and state.get("status") == "running":
        return {"ok": True, "status": "running"}

    root = Path(__file__).resolve().parents[2]
    login_py = root / ".login-venv" / "bin" / "python3"
    if not login_py.exists():
        login_py = Path(shutil.which("python3") or "python3")

    script = root / "scripts" / "auto_login.py"
    # 用号池已有账号数 +1 作为 slot 名
    pool = _get_pool()
    existing = len(pool._accounts) if pool else 0
    account_id = f"account-{existing + 1}" if existing > 0 else "default"
    # 第一个账号用 ~/.claude，后续用 ~/.claude-account-N
    config_dir = str(Path.home() / ".claude") if account_id == "default" else str(
        Path.home() / f".claude-{account_id}"
    )

    # xvfb-run 包装：Chrome CDP 需要 display，Popen 起的 Xvfb 在 systemd 子进程下会 zombie
    proc = await asyncio.create_subprocess_exec(
        "xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1080x24",
        str(login_py), str(script),
        "--email", email,
        "--token", body.token.strip(),
        "--config-dir", config_dir,
        "--add-to-pool", account_id,
        "--save-token",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={k: v for k, v in os.environ.items() if k != "DISPLAY"} | {"PYTHONUNBUFFERED": "1"},
    )
    _add_state[email] = {"status": "running", "started_at": time.time(), "account_id": account_id}
    asyncio.get_running_loop().create_task(_watch_add(email, proc))
    return {"ok": True, "status": "running", "account_id": account_id}


@router.get("/add/{email}")
async def add_status(email: str):
    return _add_state.get(email) or {"status": "idle"}
