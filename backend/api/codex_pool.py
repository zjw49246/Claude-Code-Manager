"""API endpoints for Codex account pool management."""

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.api.deps import require_admin

router = APIRouter(prefix="/api/codex-pool", tags=["codex-pool"])
logger = logging.getLogger(__name__)

# Background task state
_relogin_state: dict[str, dict] = {}
_add_state: dict[str, dict] = {}
_login_lock = asyncio.Lock()


def _get_pool():
    from backend.main import codex_pool
    if not codex_pool:
        raise HTTPException(status_code=404, detail="Codex pool not enabled. Set CODEX_POOL_ENABLED=true in .env")
    return codex_pool


@router.get("/status")
async def codex_pool_status():
    pool = _get_pool()
    return pool.status()


@router.get("/usage")
async def codex_pool_usage(force: bool = False):
    """Pool status merged with per-account quota from rollout files."""
    pool = _get_pool()
    status = pool.status()
    quota_list = await pool.fetch_quota(force=force)
    quota_by_id = {q["id"]: q for q in quota_list}
    for account in status["accounts"]:
        q = quota_by_id.get(account["id"], {})
        account["plan_type"] = q.get("plan_type")
        account["quota"] = q.get("quota")
        account["quota_error"] = q.get("error")
    return status


@router.post("/reload")
async def codex_pool_reload(request: Request):
    require_admin(request)
    pool = _get_pool()
    pool.reload()
    return pool.status()


@router.post("/accounts/{account_id}/clear-cooldown")
async def codex_clear_cooldown(request: Request, account_id: str):
    require_admin(request)
    pool = _get_pool()
    pool.clear_cooldown(account_id)
    return {"ok": True, "account_id": account_id}


@router.get("/accounts/{account_id}/verify")
async def codex_verify_account(account_id: str):
    """Check login status of an account by reading its auth.json."""
    pool = _get_pool()
    acc = pool.account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail=f"Unknown account: {account_id}")
    from backend.services.codex_pool import verify_login
    return verify_login(acc.codex_home)


# ---------------------------------------------------------------------------
# Relogin (automated)
# ---------------------------------------------------------------------------

async def _watch_relogin(account_id: str, proc: asyncio.subprocess.Process):
    try:
        out, _ = await proc.communicate()
        tail = (out or b"").decode("utf-8", errors="replace")[-5000:]
        _relogin_state[account_id] = {
            "status": "success" if proc.returncode == 0 else "failed",
            "detail": tail,
            "finished_at": time.time(),
        }
        if proc.returncode == 0:
            try:
                _get_pool().reload()
                _get_pool()._quota_cache = None
            except Exception:
                pass
    finally:
        _login_lock.release()


@router.post("/accounts/{account_id}/relogin")
async def codex_relogin(request: Request, account_id: str):
    require_admin(request)
    pool = _get_pool()
    acc = pool.account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail=f"Unknown account: {account_id}")

    state = _relogin_state.get(account_id)
    if state and state.get("status") == "running":
        return {"ok": True, "status": "running"}

    if _login_lock.locked():
        running = [k for k, v in _relogin_state.items() if v.get("status") == "running"]
        raise HTTPException(status_code=409, detail=f"另一个账号正在登录中（{', '.join(running)}）")

    # Look up 171mail token
    tokens_path = Path.home() / ".codex-pool" / "email_tokens.json"
    token_171 = ""
    if tokens_path.exists():
        try:
            tokens = json.loads(tokens_path.read_text())
            token_171 = tokens.get(acc.email, "")
        except Exception:
            pass

    if not token_171:
        raise HTTPException(status_code=400, detail=f"No 171mail token for {acc.email}. Add via /api/codex-pool/add first.")

    root = Path(__file__).resolve().parents[2]
    login_py = root / ".venv" / "bin" / "python3"
    if not login_py.exists():
        raise HTTPException(status_code=501, detail="Python venv not found")

    # Ensure Xvfb
    await _ensure_xvfb()

    await _login_lock.acquire()
    script = root / "scripts" / "codex_login.py"
    proc = await asyncio.create_subprocess_exec(
        str(login_py), str(script),
        "--email", acc.email,
        "--token", token_171,
        "--codex-home", acc.codex_home,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "DISPLAY": ":99", "PYTHONUNBUFFERED": "1"},
    )
    _relogin_state[account_id] = {"status": "running", "started_at": time.time()}
    asyncio.get_running_loop().create_task(_watch_relogin(account_id, proc))
    return {"ok": True, "status": "running"}


@router.get("/accounts/{account_id}/relogin")
async def codex_relogin_status(account_id: str):
    return _relogin_state.get(account_id) or {"status": "idle"}


# ---------------------------------------------------------------------------
# Add account
# ---------------------------------------------------------------------------

class AddCodexAccountRequest(BaseModel):
    email: str
    token: str  # 171mail token
    password: str = ""


_xvfb_proc = None


async def _ensure_xvfb():
    global _xvfb_proc
    if _xvfb_proc is not None and _xvfb_proc.returncode is None:
        return
    import subprocess as _sp
    _sp.run(["pkill", "-f", "Xvfb :99"], capture_output=True)
    await asyncio.sleep(0.5)
    _xvfb_proc = _sp.Popen(
        ["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-nolisten", "tcp", "-ac"],
        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
    )
    os.environ["DISPLAY"] = ":99"
    await asyncio.sleep(1)


async def _watch_add(email: str, proc: asyncio.subprocess.Process):
    out, _ = await proc.communicate()
    tail = (out or b"").decode("utf-8", errors="replace")[-5000:]
    _add_state[email] = {
        "status": "success" if proc.returncode == 0 else "failed",
        "detail": tail,
        "finished_at": time.time(),
    }
    if proc.returncode == 0:
        try:
            _get_pool().reload()
            _get_pool()._quota_cache = None
        except Exception:
            pass


@router.post("/add")
async def codex_add_account(request: Request, body: AddCodexAccountRequest):
    require_admin(request)
    email = body.email.strip()
    if not email or not body.token.strip():
        raise HTTPException(400, "email 和 token 必填")

    state = _add_state.get(email)
    if state and state.get("status") == "running":
        return {"ok": True, "status": "running"}

    pool = _get_pool()
    existing_ids = {a.id for a in pool._accounts}
    n = 1
    while f"codex-{n}" in existing_ids:
        n += 1
    account_id = f"codex-{n}"

    if account_id == "codex-1":
        codex_home = str(Path.home() / ".codex")
    else:
        codex_home = str(Path.home() / f".codex-{account_id}")

    await _ensure_xvfb()

    root = Path(__file__).resolve().parents[2]
    login_py = root / ".venv" / "bin" / "python3"
    if not login_py.exists():
        raise HTTPException(status_code=501, detail="Python venv not found")

    script = root / "scripts" / "codex_login.py"
    cmd = [
        str(login_py), str(script),
        "--email", email,
        "--token", body.token.strip(),
        "--codex-home", codex_home,
        "--add-to-pool", account_id,
        "--save-token",
    ]
    if body.password:
        cmd.extend(["--password", body.password])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "DISPLAY": ":99", "PYTHONUNBUFFERED": "1"},
    )
    _add_state[email] = {"status": "running", "started_at": time.time(), "account_id": account_id}
    asyncio.get_running_loop().create_task(_watch_add(email, proc))
    return {"ok": True, "status": "running", "account_id": account_id}


@router.get("/add/{email}")
async def codex_add_status(email: str):
    return _add_state.get(email) or {"status": "idle"}


# ---------------------------------------------------------------------------
# Delete account
# ---------------------------------------------------------------------------

@router.delete("/accounts/{account_id}")
async def codex_delete_account(request: Request, account_id: str):
    require_admin(request)
    pool = _get_pool()
    acc = pool.account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail=f"Unknown account: {account_id}")

    pool_path = Path.home() / ".codex-pool" / "accounts.json"
    data = json.loads(pool_path.read_text())
    data["accounts"] = [a for a in data["accounts"] if a["id"] != account_id]
    pool_path.write_text(json.dumps(data, indent=2))
    pool.reload()
    return {"ok": True, "deleted": account_id}


# ---------------------------------------------------------------------------
# Preferred account
# ---------------------------------------------------------------------------

@router.post("/preferred")
async def codex_set_preferred(request: Request, body: dict):
    require_admin(request)
    pool = _get_pool()
    account_id = body.get("account_id")
    if not pool.set_preferred(account_id):
        raise HTTPException(status_code=404, detail=f"Unknown account: {account_id}")
    return {"ok": True, "preferred": pool.preferred_account_id}
