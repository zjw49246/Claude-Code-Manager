"""Worker 管理 API（elastic-worker 设计 §18）。

长流程（创建/开关机/销毁）全部 fire-and-forget 后台执行，
进度经 "workers" WS channel 实时广播，API 立即返回当前记录。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shlex
import socket
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import get_db
from backend.models.worker import Worker
from backend.schemas.worker import WorkerCreate, WorkerLogsResponse, WorkerResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workers", tags=["workers"])

# 后台任务强引用：event loop 只持弱引用，长耗时 bootstrap 任务可能被 GC
# 掐死在半路（asyncio 文档明确的坑）
_background_tasks: set[asyncio.Task] = set()
# Ready-Worker account logins outlive their initiating HTTP request.  Keep only
# challenge metadata here; passwords, mailbox tokens and OTP codes never enter
# this process-wide status store.
_worker_login_state: dict[str, dict] = {}
_worker_login_admission_lock = asyncio.Lock()
# Background logins for different accounts can finish at the same time.  The
# accounts column is one JSON value, so serialize its read/modify/write cycle
# to prevent one successful login from overwriting another.
_worker_account_store_lock = asyncio.Lock()

_LOGIN_METHODS = frozenset({"", "171mail", "mailcom", "onet", "gazeta"})
_CODEX_LOGIN_METHODS = _LOGIN_METHODS | {"mailcatcher"}
_WORKER_ACCOUNT_PROVIDERS = frozenset({"claude", "codex"})
_WORKER_DESTROYABLE_STATUSES = frozenset({"ready", "stopped", "error"})
_WORKER_AUTH_FAILURE_STATUSES = frozenset({401, 403})
_WORKER_ACTIVE_LOGIN_STATUSES = frozenset({
    "running", "awaiting_otp", "verifying_otp", "finalizing", "cancelling",
})
_NO_WORKER_JSON = object()


def _normalize_login_method(value: str | None) -> str:
    if value is not None and not isinstance(value, str):
        raise HTTPException(400, "login_method 必须是字符串")
    method = (value or "").strip().lower()
    if method not in _LOGIN_METHODS:
        raise HTTPException(400, f"不支持的登录方式: {method}")
    return method


def _normalize_worker_account_provider(value: str | None) -> str:
    if not isinstance(value, str):
        raise HTTPException(400, "provider 必须是字符串")
    provider = value.strip().lower()
    if provider not in _WORKER_ACCOUNT_PROVIDERS:
        raise HTTPException(400, f"不支持的 Worker 账号 provider: {provider}")
    return provider


def _normalize_worker_login_method(value: str | None, provider: str) -> str:
    if value is not None and not isinstance(value, str):
        raise HTTPException(400, "login_method 必须是字符串")
    method = (value or "").strip().lower()
    allowed = _CODEX_LOGIN_METHODS if provider == "codex" else _LOGIN_METHODS
    if method not in allowed:
        raise HTTPException(400, f"不支持的 {provider} 登录方式: {method}")
    return method


def _normalize_worker_account(
    *,
    email: str,
    provider: str,
    token: str | None,
    password: str | None,
    login_method: str | None,
    require_unattended: bool = False,
) -> dict:
    normalized_email = email.strip()
    if not normalized_email:
        raise HTTPException(400, "账号 email 必填")

    normalized_provider = _normalize_worker_account_provider(provider)
    normalized_token = (token or "").strip()
    # OpenAI passwords are opaque. In particular, never trim leading/trailing
    # characters while moving them through Manager storage into the Worker.
    normalized_password = password or ""
    if normalized_provider == "claude":
        if not normalized_token:
            raise HTTPException(400, f"Claude 账号 {normalized_email} 缺少 token")
    elif not normalized_token and not normalized_password:
        raise HTTPException(
            400,
            f"Codex 账号 {normalized_email} 的 token 和 password 至少填写一项",
        )
    elif normalized_provider == "codex" and require_unattended and not normalized_token:
        raise HTTPException(
            400,
            f"Codex 账号 {normalized_email} 的 Worker 自动 bootstrap 必须提供邮箱 token",
        )

    return {
        "email": normalized_email,
        "provider": normalized_provider,
        "token": normalized_token,
        "password": normalized_password,
        "login_method": _normalize_worker_login_method(
            login_method, normalized_provider
        ),
    }


def _reject_duplicate_worker_accounts(accounts: list[dict]) -> None:
    """Reject identities that would resolve to the same remote pool slot."""
    seen: set[tuple[str, str]] = set()
    seen_slots: set[tuple[str, str]] = set()
    for account in accounts:
        provider = str(account.get("provider") or "claude").lower()
        identity = (
            provider,
            str(account.get("email") or "").strip().casefold(),
        )
        if identity in seen:
            raise HTTPException(
                400,
                f"重复的 Worker 账号: {account.get('email')} ({identity[0]})",
            )
        seen.add(identity)
        account_id = str(account.get("account_id") or "").strip()
        if account_id:
            slot = (provider, account_id)
            if slot in seen_slots:
                raise HTTPException(
                    400,
                    f"重复的 Worker 账号槽位: {account_id} ({provider})",
                )
            seen_slots.add(slot)


def _build_add_account_command(
    remote_dir: str,
    *,
    email: str,
    token: str,
    slot: str,
    login_method: str,
) -> str:
    """Build the remote login command with every dynamic argv shell-quoted."""
    argv = [
        "xvfb-run",
        "--auto-servernum",
        "--server-args=-screen 0 1920x1080x24",
        "uv",
        "run",
        "python",
        "scripts/auto_login.py",
        "--email",
        email,
        "--token",
        token,
        "--add-to-pool",
        slot,
        "--save-token",
    ]
    if login_method:
        argv.extend(["--login-method", login_method])
    return (
        f"cd {shlex.quote(remote_dir)} && "
        'export PATH="$HOME/.local/bin:$PATH" && '
        f"{shlex.join(argv)}"
    )


def _remove_persisted_worker_account(
    accounts: list | None,
    *,
    provider: str,
    account_id: str,
) -> tuple[list, bool]:
    """Remove a remotely deleted account from bootstrap retry credentials.

    New records persist ``account_id``.  Historical Claude-only records did
    not, so reconstruct their deterministic legacy slots as a compatibility
    fallback.  Codex never had historical provider-less Worker records.
    """
    kept: list = []
    removed = False
    provider_index = 0
    for account in accounts or []:
        if not isinstance(account, dict):
            kept.append(account)
            continue
        account_provider = str(account.get("provider") or "claude").lower()
        inferred_id = None
        if account_provider == provider:
            provider_index += 1
            if provider == "claude":
                inferred_id = (
                    "default" if provider_index == 1
                    else f"account-{provider_index}"
                )
        persisted_id = account.get("account_id") or inferred_id
        if (
            not removed
            and account_provider == provider
            and persisted_id == account_id
        ):
            removed = True
            continue
        if inferred_id and not account.get("account_id"):
            # Freeze legacy positional slots while the full original ordering
            # is still available.  Otherwise deleting ``default`` makes the
            # old ``account-2`` look like default on the next request.
            kept.append({**account, "account_id": inferred_id})
        else:
            kept.append(account)
    return kept, removed


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _worker_http_request(
    worker: Worker,
    method: str,
    path: str,
    *,
    timeout: float,
    payload: object = _NO_WORKER_JSON,
    allow_statuses: frozenset[int] = frozenset(),
    client: httpx.AsyncClient | None = None,
):
    """Call a Worker without leaking its auth/upstream errors to the client.

    A Worker bearer token is an internal Manager-to-Worker credential.  In
    particular, forwarding an upstream 401 would make the frontend treat the
    *Manager* session as expired and clear the user's Manager token.
    """
    if not worker.private_ip:
        raise HTTPException(502, "Worker 网关缺少目标地址")
    url = f"http://{worker.private_ip}:{worker.ccm_port}{path}"
    kwargs: dict = {
        "headers": {"Authorization": f"Bearer {worker.auth_token}"},
    }
    if payload is not _NO_WORKER_JSON:
        kwargs["json"] = payload

    async def _send(active_client):
        sender = getattr(active_client, method.lower())
        return await sender(url, **kwargs)

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as active_client:
                response = await _send(active_client)
        else:
            response = await _send(client)
    except (httpx.RequestError, OSError, TimeoutError) as exc:
        raise HTTPException(
            502,
            f"Worker 网关连接失败: {type(exc).__name__}: {str(exc)[:200]}",
        ) from exc

    status_code = response.status_code
    if status_code in _WORKER_AUTH_FAILURE_STATUSES:
        raise HTTPException(
            502,
            f"Worker 认证失败（远端 HTTP {status_code}），请重试 Worker 引导以同步认证凭据",
        )
    if not 200 <= status_code < 300 and status_code not in allow_statuses:
        raise HTTPException(
            502,
            f"Worker 上游请求失败（远端 HTTP {status_code}）",
        )
    return response


def _worker_response_json(response) -> object:
    """Decode a Worker response or surface malformed upstream data as 502."""
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise HTTPException(502, "Worker 上游返回了无效 JSON") from exc


async def _persist_worker_account_state(
    provisioner,
    worker_id: int,
    account: dict,
    *,
    status: str,
    account_id: str | None = None,
) -> None:
    """Upsert login intent/result so process restarts cannot lose credentials."""
    async with _worker_account_store_lock:
        async with provisioner.db_factory() as db:
            worker = await db.get(Worker, worker_id)
            if worker is None:
                raise RuntimeError("Worker record disappeared after account login")
            if worker.status in {"destroying", "terminated"}:
                # A late browser callback must never repopulate credentials
                # after destroy has scrubbed them.  This also closes the race
                # where /pool/add read ready immediately before destroy CAS.
                raise RuntimeError(
                    f"Worker account persistence rejected while {worker.status}"
                )
            provider = account["provider"]
            updated_accounts = [
                item for item in (worker.accounts or [])
                if not (
                    isinstance(item, dict)
                    and str(item.get("provider") or "claude").lower() == provider
                    and (
                        (account_id and item.get("account_id") == account_id)
                        or (
                            str(item.get("email") or "").strip().casefold()
                            == account["email"].casefold()
                        )
                    )
                )
            ]
            updated_accounts.append({
                **account,
                **({"account_id": account_id} if account_id else {}),
                "status": status,
            })
            # End the snapshot read transaction, then make status gating and
            # the JSON write one SQL statement.  A destroy CAS/credential
            # scrub that wins between the read and write must make rowcount 0;
            # a stale login callback can never update a terminated row.
            await db.rollback()
            persisted = await db.execute(
                update(Worker)
                .where(
                    Worker.id == worker_id,
                    Worker.status.not_in(("destroying", "terminated")),
                )
                .values(accounts=updated_accounts)
            )
            if persisted.rowcount != 1:
                await db.rollback()
                current_status = await db.scalar(
                    select(Worker.status).where(Worker.id == worker_id)
                )
                raise RuntimeError(
                    "Worker account persistence rejected while "
                    f"{current_status or 'missing'}"
                )
            await db.commit()


def _provisioner():
    from backend.main import worker_provisioner

    if worker_provisioner is None:
        raise HTTPException(503, "Worker 功能未启用（WORKER_ENABLED=false 或缺少 boto3）")
    return worker_provisioner


@router.get("", response_model=list[WorkerResponse])
async def list_workers(request: Request, db: AsyncSession = Depends(get_db)):
    from backend.api.deps import get_current_user_id, get_current_user_role
    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    stmt = select(Worker).where(Worker.status != "terminated").order_by(desc(Worker.created_at))
    if user_role not in ("admin", "super_admin"):
        stmt = stmt.where(Worker.owner_user_id == user_id)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=WorkerResponse)
async def create_worker(body: WorkerCreate, request: Request, db: AsyncSession = Depends(get_db)):
    from backend.api.deps import require_admin
    require_admin(request)
    prov = _provisioner()
    if not body.name or not body.name.strip():
        raise HTTPException(400, "请填写 Worker 名称")
    # Fail before creating a DB job or a billable EC2 instance.  The same
    # preflight is repeated inside the background provisioner to close races
    # where a key is replaced between request validation and instance launch.
    from backend.services.ssh_executor import SSHKeyPreflightError
    try:
        prov.preflight_ssh_key()
    except SSHKeyPreflightError as exc:
        raise HTTPException(
            503,
            f"Worker SSH 密钥配置无效（{exc.code}）：{exc.detail}",
        ) from exc
    accounts = []
    for account in body.accounts:
        accounts.append(_normalize_worker_account(
            email=account.email,
            provider=account.provider,
            token=account.token,
            password=account.password,
            login_method=account.login_method,
            require_unattended=True,
        ))
    _reject_duplicate_worker_accounts(accounts)
    worker = Worker(
        name=body.name.strip(),
        status="creating",
        auth_token=secrets.token_hex(24),
        ssh_user=settings.worker_ssh_user,
        ssh_key_path=settings.worker_ssh_key_path,
        accounts=[{**account, "status": "pending"} for account in accounts],
    )
    db.add(worker)
    await db.commit()
    await db.refresh(worker)

    _spawn(
        prov.create_worker(worker.id, accounts=accounts)
    )
    return worker


@router.get("/{worker_id}", response_model=WorkerResponse)
async def get_worker(worker_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from backend.api.deps import require_worker_access
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    await require_worker_access(request, worker)
    return worker


@router.get("/{worker_id}/logs", response_model=WorkerLogsResponse)
async def get_worker_logs(worker_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from backend.api.deps import require_worker_access
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    await require_worker_access(request, worker)
    return WorkerLogsResponse(id=worker.id, bootstrap_log=worker.bootstrap_log)


async def _transition_worker_status(
    db: AsyncSession,
    worker_id: int,
    *,
    allowed_statuses: tuple[str, ...] | frozenset[str],
    target_status: str,
) -> Worker:
    """Atomically claim a Worker lifecycle transition.

    Routes perform authorization from a read first.  End that read transaction
    before the compare-and-set so concurrent SQLite requests do not both try to
    upgrade a shared read lock.  Only the UPDATE winner may spawn background
    lifecycle work.
    """
    await db.rollback()
    result = await db.execute(
        update(Worker)
        .where(
            Worker.id == worker_id,
            Worker.status.in_(tuple(allowed_statuses)),
        )
        .values(status=target_status)
    )
    if result.rowcount != 1:
        await db.rollback()
        current_status = await db.scalar(
            select(Worker.status).where(Worker.id == worker_id)
        )
        if current_status is None:
            raise HTTPException(404, "Worker not found")
        raise HTTPException(
            409,
            f"Worker 当前状态 {current_status}，不允许该操作",
        )
    await db.commit()
    worker = await db.get(Worker, worker_id)
    if worker is None:  # Defensive: the row cannot normally disappear here.
        raise HTTPException(404, "Worker not found")
    await db.refresh(worker)
    return worker


@router.post("/{worker_id}/stop", response_model=WorkerResponse)
async def stop_worker(worker_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from backend.api.deps import require_worker_access
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    await require_worker_access(request, worker)
    if worker.bootstrap_step == "destroy":
        raise HTTPException(409, "Worker 有未完成的销毁操作，只能重试销毁")
    prov = _provisioner()
    worker = await _transition_worker_status(
        db,
        worker_id,
        allowed_statuses=("ready", "error"),
        target_status="stopping",
    )
    _spawn(prov.stop_worker(worker.id))
    return worker


@router.post("/{worker_id}/start", response_model=WorkerResponse)
async def start_worker(worker_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from backend.api.deps import require_worker_access
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    await require_worker_access(request, worker)
    if worker.bootstrap_step == "destroy":
        raise HTTPException(409, "Worker 有未完成的销毁操作，只能重试销毁")
    prov = _provisioner()
    worker = await _transition_worker_status(
        db,
        worker_id,
        allowed_statuses=("stopped", "error"),
        target_status="starting",
    )
    _spawn(prov.start_worker(worker.id))
    return worker


@router.post("/{worker_id}/destroy", response_model=WorkerResponse)
async def destroy_worker(worker_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from backend.api.deps import require_admin
    require_admin(request)
    prov = _provisioner()
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    worker = await _transition_worker_status(
        db,
        worker_id,
        allowed_statuses=_WORKER_DESTROYABLE_STATUSES,
        target_status="destroying",
    )
    # 先把该 worker 的 task 全部迁回本机（执行态无损），再销毁实例
    _spawn(_migrate_back_then_destroy(prov, worker.id))
    return worker


async def _migrate_back_then_destroy(prov, worker_id: int, db_factory=None):
    """销毁 = 批量 migrate(task, 本机) + terminate（设计 §10.3）。

    单个 task 迁移失败不阻塞销毁（日志/状态在 Manager 本就完整，丢的只是
    session 续聊能力），但要记到 task.error_message 让用户知情。"""
    from backend.main import task_migrator, worker_relay
    from backend.models.task import Task
    from sqlalchemy import select

    if db_factory is None:
        from backend.database import async_session as db_factory

    # TaskMigrator 已接受 destroying 状态作为迁移源，无需临时改 ready
    async with db_factory() as db:
        result = await db.execute(select(Task).where(Task.worker_id == worker_id))
        tasks = result.scalars().all()
    # Stop executing tasks before migrating — running sessions can't be migrated
    for task in tasks:
        if task.status in ("executing", "in_progress"):
            try:
                from backend.services.worker_proxy import WorkerProxy
                proxy = WorkerProxy(db_factory, worker_relay)
                await proxy.proxy_to_worker(task, "POST", f"/api/tasks/{task.id}/stop-session")
                logger.info("destroy: stopped executing task %s before migration", task.id)
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning("destroy: failed to stop task %s: %s", task.id, e)
    # Refresh task statuses after stopping
    async with db_factory() as db:
        result = await db.execute(select(Task).where(Task.worker_id == worker_id))
        tasks = result.scalars().all()
    for task in tasks:
        try:
            if task_migrator is not None:
                await task_migrator.migrate(task.id, None)
        except Exception as e:
            logger.warning("destroy: migrate task %s back failed: %s", task.id, e)
            async with db_factory() as db:
                t = await db.get(Task, task.id)
                if t:
                    t.worker_id = None  # 指针总要切回，否则 task 永远指向死 worker
                    t.error_message = (t.error_message or "") + f"\n[销毁迁移失败: {e}]"
                    await db.commit()
    if worker_relay is not None:
        try:
            await worker_relay.stop_worker(worker_id)
        except Exception as e:
            # Relay is Manager-local cleanup.  A stale relay must not prevent
            # the cloud termination attempt or strand the row in destroying.
            logger.warning("destroy: stop worker relay %s failed: %s", worker_id, e)
    await prov.destroy_worker(worker_id)


@router.post("/{worker_id}/retry", response_model=WorkerResponse)
async def retry_bootstrap(worker_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """error 状态下重跑创建/bootstrap 流程。"""
    from backend.api.deps import require_worker_access
    worker = await db.get(Worker, worker_id)
    if worker:
        await require_worker_access(request, worker)
    prov = _provisioner()
    if worker is None:
        raise HTTPException(404, "Worker not found")
    if worker.status != "error":
        raise HTTPException(
            409,
            f"Worker 当前状态 {worker.status}，不允许该操作",
        )
    if worker.bootstrap_step == "destroy":
        raise HTTPException(409, "Worker 有未完成的销毁操作，只能重试销毁")
    # 从 DB 读已有账号信息，retry 时重新登录。历史记录没有
    # provider，它们均由旧 Claude-only Worker 链路创建。
    saved_accounts = worker.accounts or []
    accounts = []
    for account in saved_accounts:
        email = str(account.get("email", "")).strip()
        if not email:
            raise HTTPException(409, "Worker 保存的账号缺少 email，无法重试")
        try:
            provider = _normalize_worker_account_provider(
                account.get("provider") or "claude"
            )
            token = account.get("token") or ""
            password = account.get("password") or ""
            if not isinstance(token, str) or not isinstance(password, str):
                raise HTTPException(400, "保存的账号凭据格式无效")
            normalized = _normalize_worker_account(
                email=email,
                provider=provider,
                token=token,
                password=password,
                login_method=account.get("login_method"),
                require_unattended=True,
            )
            account_id = account.get("account_id") or ""
            if not isinstance(account_id, str):
                raise HTTPException(400, "保存的账号 account_id 格式无效")
            if account_id.strip():
                normalized["account_id"] = account_id.strip()
        except HTTPException as exc:
            raise HTTPException(
                409,
                f"账号 {email} 的保存登录信息无效，无法重试：{exc.detail}",
            ) from exc
        accounts.append(normalized)
    try:
        _reject_duplicate_worker_accounts(accounts)
    except HTTPException as exc:
        raise HTTPException(409, f"Worker 保存了重复账号，无法重试：{exc.detail}") from exc
    worker = await _transition_worker_status(
        db,
        worker_id,
        allowed_statuses=("error",),
        target_status="creating",
    )
    _spawn(
        prov.create_worker(worker.id, accounts=accounts)
    )
    return worker


@router.get("/{worker_id}/pool")
async def get_worker_pool(
    worker_id: int,
    request: Request,
    provider: str = "codex",
    db: AsyncSession = Depends(get_db),
):
    """实时拉取 Worker 上指定 provider 的账号池状态。"""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    from backend.api.deps import require_worker_access as _rwa
    await _rwa(request, worker)
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")
    provider = _normalize_worker_account_provider(provider)
    status_path = (
        "/api/codex-pool/status" if provider == "codex" else "/api/pool/status"
    )
    r = await _worker_http_request(
        worker,
        "GET",
        status_path,
        timeout=10,
        allow_statuses=frozenset({404}) if provider == "claude" else frozenset(),
    )
    if provider == "claude" and r.status_code == 404:
        # worker 端 POOL_ENABLED=false：单账号模式。
        # 老版 worker 没有账号查询端点，经 SSH 读 ~/.claude.json
        # 的 oauthAccount.emailAddress 兜底，让用户知道用的是哪个号
        email = None
        try:
            from backend.services.ssh_executor import (
                SSHExecutor,
                worker_known_hosts_path,
            )
            ssh = SSHExecutor(
                host=worker.private_ip,
                user=worker.ssh_user,
                key_path=(worker.ssh_key_path or settings.worker_ssh_key_path),
                known_hosts_path=(
                    worker_known_hosts_path(worker.cloud_instance_id)
                    if worker.cloud_instance_id else None
                ),
            )
            code, out = await ssh.run(
                "python3 -c \"import json;"
                "print(json.load(open('/home/'+__import__('getpass').getuser()+'/.claude.json'))"
                ".get('oauthAccount',{}).get('emailAddress',''))\"",
                timeout=15,
            )
            if code == 0 and out.strip():
                email = out.strip().splitlines()[-1]
        except Exception:
            email = None
        accounts = (
            [{"id": "default", "email": email, "enabled": True,
              "available": True, "cooldown_remaining": 0}]
            if email else []
        )
        return {"enabled": True, "total": len(accounts),
                "available": len(accounts), "accounts": accounts}
    return _worker_response_json(r)


@router.post("/{worker_id}/pool/add")
async def add_worker_account(worker_id: int, request: Request, body: dict, db: AsyncSession = Depends(get_db)):
    """在 Worker 上添加 Codex（默认）或兼容 Claude 账号。"""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    from backend.api.deps import require_worker_access as _rwa
    await _rwa(request, worker)
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")

    raw_email = body.get("email", "")
    raw_token = body.get("token", "")
    raw_password = body.get("password", "")
    raw_provider = body.get("provider", "codex")
    if not all(
        isinstance(value, str)
        for value in (raw_email, raw_token, raw_password, raw_provider)
    ):
        raise HTTPException(400, "email/provider/token/password 必须是字符串")
    account = _normalize_worker_account(
        email=raw_email,
        provider=raw_provider,
        token=raw_token,
        password=raw_password,
        login_method=body.get("login_method"),
        require_unattended=True,
    )
    email = account["email"]
    provider = account["provider"]

    # Email identity is case-insensitive.  Normalize the in-memory admission
    # key as well as the persisted lookup so differently-cased concurrent
    # requests cannot start two browser logins for the same account.
    state_key = f"{worker_id}:{provider}:{email.casefold()}"

    if provider == "codex":
        prov = _provisioner()
        async with _worker_login_admission_lock:
            existing_state = _worker_login_state.get(state_key, {})
            if existing_state.get("status") in _WORKER_ACTIVE_LOGIN_STATUSES:
                return {
                    "ok": True,
                    "provider": provider,
                    **{
                        key: existing_state[key]
                        for key in (
                            "status", "attempt_id", "challenge_id",
                            "expires_at", "account_id",
                        )
                        if existing_state.get(key) is not None
                    },
                }
            async with prov.db_factory() as account_db:
                current_worker = await account_db.get(Worker, worker_id)
            if current_worker is None:
                raise HTTPException(404, "Worker not found")
            persisted_matches = [
                item for item in (current_worker.accounts or [])
                if isinstance(item, dict)
                and str(item.get("provider") or "claude").lower() == provider
                and str(item.get("email") or "").strip().casefold()
                == email.casefold()
            ]
            if len(persisted_matches) > 1:
                raise HTTPException(409, "Manager 中存在重复的 Worker 账号记录，请先清理")
            if persisted_matches:
                persisted = persisted_matches[0]
                persisted_status = str(persisted.get("status") or "")
                if persisted_status == "logged_in":
                    raise HTTPException(409, "该 Codex 邮箱已在 Worker 号池中")
                if persisted_status == "pending":
                    # Resume an intent that survived Manager restart without
                    # replacing its known-good credentials from an add form.
                    account = dict(persisted)
                elif persisted.get("account_id"):
                    # A failed slot is an explicit retry: retain its identity
                    # while allowing corrected credentials from this request.
                    account["account_id"] = persisted["account_id"]
            _worker_login_state[state_key] = {
                "status": "running",
                "provider": provider,
                "started_at": time.time(),
            }
            # Persist the intent before starting the long remote browser flow.
            # A Manager restart can then reclaim the active/committed slot.
            try:
                await _persist_worker_account_state(
                    prov,
                    worker_id,
                    account,
                    status="pending",
                )
            except Exception:
                _worker_login_state.pop(state_key, None)
                raise

        async def _publish_codex_status(remote_state: dict) -> None:
            current = _worker_login_state.get(state_key, {})
            safe = {
                key: remote_state[key]
                for key in (
                    "status", "detail", "attempt_id", "challenge_id",
                    "expires_at", "account_id",
                )
                if remote_state.get(key) is not None
            }
            # No remote terminal status is the Manager transaction boundary:
            # credentials/account_id or retryable failure still need to commit
            # to Worker.accounts.  Keep DELETE/retry blocked until _run_codex
            # performs the final DB write and publishes the sole terminal
            # state.  This includes unexpected/idle remote states because
            # ensure_codex_account raises only after this callback returns.
            remote_status = safe.get("status")
            if (
                remote_status is not None
                and remote_status not in _WORKER_ACTIVE_LOGIN_STATUSES
            ):
                safe["status"] = (
                    "cancelling" if remote_status == "cancelled" else "finalizing"
                )
            _worker_login_state[state_key] = {
                **current,
                **safe,
                "provider": provider,
            }
            remote_account_id = str(remote_state.get("account_id") or "").strip()
            if remote_account_id and account.get("account_id") != remote_account_id:
                account["account_id"] = remote_account_id
                await _persist_worker_account_state(
                    prov,
                    worker_id,
                    account,
                    status="pending",
                    account_id=remote_account_id,
                )

        async def _run_codex():
            try:
                account_id = await prov.ensure_codex_account(
                    worker,
                    account,
                    allow_manual_otp=True,
                    on_status=_publish_codex_status,
                )
                if not account_id:
                    raise RuntimeError("Worker Codex login returned no account id")
                await _persist_worker_account_state(
                    prov,
                    worker_id,
                    account,
                    status="logged_in",
                    account_id=account_id,
                )
                _worker_login_state[state_key] = {
                    "status": "success",
                    "provider": provider,
                    "account_id": account_id,
                }
            except Exception as exc:
                logger.warning(
                    "Worker %s Codex account login failed for %s: %s",
                    worker_id,
                    email,
                    exc,
                )
                failed_state = {
                    **_worker_login_state.get(state_key, {}),
                    "status": "failed",
                    "provider": provider,
                    "detail": str(exc)[-1000:],
                }
                try:
                    await _persist_worker_account_state(
                        prov,
                        worker_id,
                        account,
                        status="failed",
                        account_id=(
                            str(account.get("account_id") or "").strip() or None
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist Worker %s Codex login failure for %s",
                        worker_id,
                        email,
                    )
                finally:
                    # A terminal state is also the promise that no later DB
                    # write from this login remains.  DELETE relies on that
                    # ordering to prevent removed credentials being revived.
                    _worker_login_state[state_key] = failed_state

        _spawn(_run_codex())
        return {"ok": True, "status": "running", "provider": provider}

    _worker_login_state[state_key] = {
        "status": "running",
        "provider": provider,
        "started_at": time.time(),
    }

    from backend.config import settings
    from backend.services.ssh_executor import SSHExecutor, worker_known_hosts_path
    ssh = SSHExecutor(host=worker.private_ip, user=worker.ssh_user,
                      key_path=worker.ssh_key_path or settings.worker_ssh_key_path,
                      known_hosts_path=(
                          worker_known_hosts_path(worker.cloud_instance_id)
                          if worker.cloud_instance_id else None
                      ))

    # 算 slot 名：查 worker 现有账号数
    try:
        r = await _worker_http_request(
            worker,
            "GET",
            "/api/pool/status",
            timeout=10,
            allow_statuses=frozenset({404}),
        )
    except HTTPException as exc:
        _worker_login_state[state_key] = {
            "status": "failed",
            "provider": provider,
            "detail": str(exc.detail),
        }
        raise
    if r.status_code == 404:
        # Explicit legacy POOL_ENABLED=false is the only safe empty-pool
        # fallback.  Auth/5xx/connectivity failures must stop before choosing
        # ``default`` and potentially colliding with an existing account.
        existing = 0
    else:
        pool_status = _worker_response_json(r)
        if not isinstance(pool_status, dict) or not isinstance(
            pool_status.get("accounts"), list
        ):
            raise HTTPException(502, "Worker Claude 号池返回了无效状态")
        existing = len(pool_status["accounts"])

    slot = f"account-{existing + 1}" if existing > 0 else "default"
    remote_dir = settings.worker_remote_dir

    # 后台跑 auto_login（xvfb-run 包装）
    cmd = _build_add_account_command(
        remote_dir,
        email=email,
        token=account["token"],
        slot=slot,
        login_method=account["login_method"],
    )

    # 这个任务可能跑 1-2 分钟，用 fire-and-forget
    async def _run():
        code, out = await ssh.run(cmd, timeout=600, sensitive=True)
        _worker_login_state[state_key] = {
            "status": "success" if code == 0 else "failed",
            "provider": provider,
            "detail": out[-1000:],
        }

    _spawn(_run())
    return {"ok": True, "status": "running", "provider": provider, "slot": slot}


@router.get("/{worker_id}/pool/add/{email}")
async def worker_add_status(
    worker_id: int,
    email: str,
    request: Request,
    provider: str = "codex",
    db: AsyncSession = Depends(get_db),
):
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    from backend.api.deps import require_worker_access as _rwa
    await _rwa(request, worker)
    provider = _normalize_worker_account_provider(provider)
    return _worker_login_state.get(
        f"{worker_id}:{provider}:{email.casefold()}"
    ) or {"status": "idle", "provider": provider}


def _worker_login_attempt_state(worker_id: int, attempt_id: str) -> dict | None:
    prefix = f"{worker_id}:codex:"
    matches = [
        state for key, state in _worker_login_state.items()
        if key.startswith(prefix) and state.get("attempt_id") == attempt_id
    ]
    return matches[0] if len(matches) == 1 else None


@router.post("/{worker_id}/pool/login-attempts/{attempt_id}/otp")
async def submit_worker_login_otp(
    worker_id: int,
    attempt_id: str,
    request: Request,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Relay a one-time code over the Worker's SSH loopback API channel."""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    from backend.api.deps import require_worker_access as _rwa
    await _rwa(request, worker)
    state = _worker_login_attempt_state(worker_id, attempt_id)
    if not state:
        raise HTTPException(404, "Worker 登录流程已结束或不存在")
    challenge_id = body.get("challenge_id")
    code = body.get("code")
    if not isinstance(challenge_id, str) or challenge_id != state.get("challenge_id"):
        raise HTTPException(409, "验证码挑战已更新")
    if not isinstance(code, str) or not code.strip().isdigit() or len(code.strip()) != 6:
        raise HTTPException(422, "请输入 6 位数字验证码")
    response = await _provisioner().worker_local_api(
        worker,
        "POST",
        f"/api/codex-pool/login-attempts/{quote(attempt_id, safe='')}/otp",
        payload={"challenge_id": challenge_id, "code": code.strip()},
        timeout=30,
    )
    state.update({"status": "verifying_otp"})
    return response


@router.delete("/{worker_id}/pool/login-attempts/{attempt_id}")
async def cancel_worker_login(
    worker_id: int,
    attempt_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    from backend.api.deps import require_worker_access as _rwa
    await _rwa(request, worker)
    state = _worker_login_attempt_state(worker_id, attempt_id)
    if not state:
        raise HTTPException(404, "Worker 登录流程已结束或不存在")
    response = await _provisioner().worker_local_api(
        worker,
        "DELETE",
        f"/api/codex-pool/login-attempts/{quote(attempt_id, safe='')}",
        timeout=45,
    )
    # The background poller may have replaced the state dict while the remote
    # cancellation request was in flight.  Re-resolve it before mutating so we
    # never update an orphaned object or overwrite a completed terminal state.
    current_state = _worker_login_attempt_state(worker_id, attempt_id)
    if (
        current_state is not None
        and current_state.get("status") in _WORKER_ACTIVE_LOGIN_STATUSES
    ):
        # The background poller still has to observe cancellation and persist
        # its retryable failure record.  Keep deletion blocked until then.
        current_state.update({"status": "cancelling", "detail": "正在取消登录"})
    return {
        "ok": bool(response.get("ok", True)) if isinstance(response, dict) else True,
        "status": (
            current_state.get("status", "cancelling")
            if current_state is not None else "cancelling"
        ),
    }


@router.delete("/{worker_id}/pool/{account_id}")
async def delete_worker_account(
    worker_id: int,
    request: Request,
    account_id: str,
    provider: str = "codex",
    db: AsyncSession = Depends(get_db),
):
    """从 worker 的号池中删除账号。"""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    from backend.api.deps import require_worker_access as _rwa
    await _rwa(request, worker)
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")
    provider = _normalize_worker_account_provider(provider)
    remote_path = (
        f"/api/codex-pool/accounts/{quote(account_id, safe='')}"
        if provider == "codex"
        else f"/api/pool/accounts/{quote(account_id, safe='')}"
    )

    # Commit deletion intent locally first. If the Manager exits after the
    # remote call, stale bootstrap credentials must never resurrect the slot.
    async with _worker_login_admission_lock:
        prefix = f"{worker_id}:{provider}:"
        if any(
            key.startswith(prefix)
            and state.get("status") in _WORKER_ACTIVE_LOGIN_STATUSES
            for key, state in _worker_login_state.items()
        ):
            raise HTTPException(
                409,
                "Worker 账号登录仍在进行中，请先取消并等待登录结束后再删除",
            )
        async with _worker_account_store_lock:
            # The row was loaded before waiting for the mutation locks. Refresh
            # it so a concurrently completed login is not overwritten.
            await db.refresh(worker)
            remaining_accounts, removed = _remove_persisted_worker_account(
                worker.accounts,
                provider=provider,
                account_id=account_id,
            )
            if removed:
                # Release the snapshot and make lifecycle gating + JSON write
                # atomic.  A concurrent destroy that already scrubbed secrets
                # must make this update fail instead of restoring the stale
                # credentials of accounts that were not deleted.
                await db.rollback()
                deleted = await db.execute(
                    update(Worker)
                    .where(Worker.id == worker_id, Worker.status == "ready")
                    .values(accounts=remaining_accounts)
                )
                if deleted.rowcount != 1:
                    await db.rollback()
                    current_status = await db.scalar(
                        select(Worker.status).where(Worker.id == worker_id)
                    )
                    raise HTTPException(
                        409,
                        f"Worker 状态已变为 {current_status or 'missing'}，账号删除已取消",
                    )
                await db.commit()
                # rollback() expired the route's ORM snapshot. Reload the
                # connection/auth fields before the remote idempotent delete.
                await db.refresh(worker)
        # Keep admission closed until the remote slot is gone.  Otherwise a
        # same-email add can adopt/live-verify the still-present slot after the
        # local delete commits, only for this request to delete it remotely a
        # moment later and strand a false logged_in Manager record.
        r = await _worker_http_request(
            worker,
            "DELETE",
            remote_path,
            timeout=10,
            allow_statuses=frozenset({404}),
        )
        if r.status_code == 404:
            return {"ok": True, "already_absent": True}
        return _worker_response_json(r)


@router.get("/{worker_id}/pool/usage")
async def get_worker_pool_usage(
    worker_id: int,
    request: Request,
    provider: str = "codex",
    db: AsyncSession = Depends(get_db),
):
    """拉取 Worker 指定 provider 的账号额度。"""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    from backend.api.deps import require_worker_access as _rwa
    await _rwa(request, worker)
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")
    provider = _normalize_worker_account_provider(provider)
    usage_path = (
        "/api/codex-pool/usage?force=true"
        if provider == "codex"
        else "/api/pool/usage"
    )
    status_path = (
        "/api/codex-pool/status"
        if provider == "codex"
        else "/api/pool/status"
    )
    timeout = 60 if provider == "codex" else 15
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await _worker_http_request(
            worker,
            "GET",
            usage_path,
            timeout=timeout,
            allow_statuses=frozenset({404}),
            client=client,
        )
        if r.status_code != 404:
            return _worker_response_json(r)

        # Compatibility only: an old Worker can expose pool status but have
        # no usage endpoint, while a disabled legacy pool returns 404 for
        # both.  Auth, quota and 5xx failures never enter this fallback.
        r2 = await _worker_http_request(
            worker,
            "GET",
            status_path,
            timeout=timeout,
            allow_statuses=frozenset({404}),
            client=client,
        )
        if r2.status_code == 404:
            return {"enabled": False, "total": 0, "available": 0, "accounts": []}
        return _worker_response_json(r2)


@router.get("/{worker_id}/settings/runtime")
async def get_worker_runtime_settings(worker_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    from backend.api.deps import require_worker_access as _rwa
    await _rwa(request, worker)
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")
    r = await _worker_http_request(
        worker, "GET", "/api/settings/runtime", timeout=10,
    )
    return _worker_response_json(r)


@router.put("/{worker_id}/settings/runtime")
async def update_worker_runtime_settings(worker_id: int, request: Request, body: dict, db: AsyncSession = Depends(get_db)):
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    from backend.api.deps import require_worker_access as _rwa
    await _rwa(request, worker)
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")
    r = await _worker_http_request(
        worker,
        "PUT",
        "/api/settings/runtime",
        timeout=10,
        payload=body,
    )
    return _worker_response_json(r)


# --- Team CCM: Worker rename ---

from pydantic import BaseModel as _BaseModel


class RenameWorkerBody(_BaseModel):
    name: str


@router.patch("/{worker_id}/rename", response_model=WorkerResponse)
async def rename_worker(worker_id: int, body: RenameWorkerBody, request: Request, db: AsyncSession = Depends(get_db)):
    """Rename a worker (DB + AWS Name tag if cloud_instance_id exists)."""
    from backend.api.deps import require_worker_access
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    await require_worker_access(request, worker)
    new_name = body.name.strip()
    if not new_name:
        raise HTTPException(400, "Worker 名称不能为空")
    # Rename is a lifecycle mutation too.  In particular, changing the Name
    # tag parameter after a lost RunInstances response makes AWS reject the
    # stable ClientToken with IdempotentParameterMismatch.  Requiring a known
    # instance id also closes the rename-vs-retry race via this SQL CAS.
    await db.rollback()
    renamed = await db.execute(
        update(Worker)
        .where(
            Worker.id == worker_id,
            Worker.status.in_(tuple(_WORKER_DESTROYABLE_STATUSES)),
            Worker.cloud_instance_id.is_not(None),
            or_(
                Worker.bootstrap_step.is_(None),
                Worker.bootstrap_step != "destroy",
            ),
        )
        .values(name=new_name)
    )
    if renamed.rowcount != 1:
        await db.rollback()
        raise HTTPException(
            409,
            "Worker 正在执行生命周期操作或 EC2 创建结果尚未认领，暂不能重命名",
        )
    await db.commit()
    worker = await db.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(404, "Worker not found")
    await db.refresh(worker)
    # Update AWS Name tag (best-effort)
    if worker.cloud_instance_id:
        try:
            from backend.services.cloud_provider import AWSProvider
            cloud = AWSProvider()
            await cloud.update_instance_tags(worker.cloud_instance_id, {"Name": new_name})
        except Exception:
            logger.warning("Failed to update AWS Name tag for %s", worker.cloud_instance_id, exc_info=True)
    # Broadcast
    from backend.main import broadcaster
    if broadcaster:
        await broadcaster.broadcast("workers", {
            "event_type": "worker_update",
            "worker_id": worker.id,
            "status": worker.status,
        })
    return worker


# --- Team CCM: Worker assignment ---


class AssignWorkerBody(_BaseModel):
    owner_user_id: int | None = None


@router.put("/{worker_id}/assign", response_model=WorkerResponse)
async def assign_worker(worker_id: int, body: AssignWorkerBody, request: Request, db: AsyncSession = Depends(get_db)):
    """Assign a worker to a user (admin only). Set owner_user_id=null for public pool."""
    from backend.api.deps import require_admin
    require_admin(request)
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    prev_owner = worker.owner_user_id
    worker.owner_user_id = body.owner_user_id
    await db.commit()
    await db.refresh(worker)
    from backend.api.deps import get_current_user_id
    from backend.models.user import User
    admin_id = get_current_user_id(request)
    # Notify new owner
    if body.owner_user_id:
        try:
            from backend.services.feishu_notify import notify_worker_assigned
            admin = await db.get(User, admin_id) if admin_id else None
            import asyncio
            asyncio.create_task(notify_worker_assigned(
                admin.name if admin else "Admin",
                worker.name,
                body.owner_user_id,
            ))
        except Exception:
            pass
    # Notify previous owner (if changed and not self-revoke)
    if prev_owner and prev_owner != body.owner_user_id and prev_owner != admin_id:
        try:
            from backend.services.feishu_notify import notify_worker_unassigned
            admin = await db.get(User, admin_id) if admin_id else None
            import asyncio
            asyncio.create_task(notify_worker_unassigned(
                admin.name if admin else "Admin",
                worker.name,
                prev_owner,
            ))
        except Exception:
            pass
    return worker
