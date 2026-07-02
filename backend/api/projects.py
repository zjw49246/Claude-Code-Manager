import asyncio
import fnmatch
import os
import pathlib

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import get_db, async_session
from backend.models.project import Project
from backend.api.deps import get_current_user_id, get_current_user_role
from backend.models.tag import Tag
from backend.models.global_settings import GlobalSettings
from backend.schemas.project import ProjectCreate, ProjectUpdate, ProjectResponse, ProjectReorderItem
from backend.services.git_config import merge_git_config, settings_to_dict
from backend.services.dispatcher import _build_git_env

router = APIRouter(prefix="/api/projects", tags=["projects"])

async def _require_project_access(request: Request, project_id: int, db: AsyncSession):
    """Check if user has access to this project."""
    from backend.api.deps import is_admin as _is_admin, get_current_user_id as _get_uid
    if _is_admin(request):
        return
    uid = _get_uid(request)
    if not uid:
        raise HTTPException(403, "Not authenticated")
    from backend.models.team_share import TeamProjectShare
    from backend.models.worker import Worker
    from backend.models.task import Task
    from backend.models.user_group import UserGroupMember
    user_group_ids = select(UserGroupMember.group_id).where(UserGroupMember.user_id == uid)
    shared = (await db.execute(
        select(TeamProjectShare.id).where(
            TeamProjectShare.project_id == project_id,
            ((TeamProjectShare.target_type == "user") & (TeamProjectShare.target_id == uid))
            | ((TeamProjectShare.target_type == "group") & TeamProjectShare.target_id.in_(user_group_ids))
        ).limit(1)
    )).scalar_one_or_none()
    if shared:
        return
    owned_worker_ids = select(Worker.id).where(Worker.owner_user_id == uid)
    worker_proj = (await db.execute(
        select(Task.id).where(Task.worker_id.in_(owned_worker_ids), Task.project_id == project_id).limit(1)
    )).scalar_one_or_none()
    if worker_proj:
        return
    raise HTTPException(403, "No access to this project")



@router.get("")
async def list_projects(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    stmt = select(Project).order_by(Project.sort_order.asc(), Project.name.asc())
    if user_role not in ("admin", "super_admin") and user_id:
        from backend.models.team_share import TeamProjectShare
        from backend.models.worker import Worker
        from backend.models.task import Task
        from backend.models.user_group import UserGroupMember
        user_group_ids = select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)
        shared_project_ids = select(TeamProjectShare.project_id).where(
            ((TeamProjectShare.target_type == "user") & (TeamProjectShare.target_id == user_id))
            | ((TeamProjectShare.target_type == "group") & TeamProjectShare.target_id.in_(user_group_ids))
        )
        owned_worker_ids = select(Worker.id).where(Worker.owner_user_id == user_id)
        worker_project_ids = select(Task.project_id).where(
            Task.worker_id.in_(owned_worker_ids), Task.project_id.is_not(None)
        ).distinct()
        stmt = stmt.where(
            Project.id.in_(shared_project_ids)
            | Project.id.in_(worker_project_ids)
        )
    result = await db.execute(stmt)
    projects = list(result.scalars().all())

    # Annotate each project with its location (local or worker name)
    from backend.models.task import Task
    from backend.models.worker import Worker as WorkerModel
    project_worker_map: dict[int, str] = {}
    if projects:
        pids = [p.id for p in projects]
        tw_result = await db.execute(
            select(Task.project_id, WorkerModel.name)
            .join(WorkerModel, Task.worker_id == WorkerModel.id)
            .where(Task.project_id.in_(pids), Task.worker_id.is_not(None))
            .distinct()
        )
        for pid, wname in tw_result:
            project_worker_map[pid] = wname

    out = []
    for p in projects:
        d = ProjectResponse.model_validate(p).model_dump()
        d["location"] = project_worker_map.get(p.id, "local")
        out.append(d)
    return out


@router.get("/tags", response_model=list[str])
async def list_project_tags(request: Request, db: AsyncSession = Depends(get_db)):
    """Return unique tags from projects the user can see."""
    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    stmt = select(Project.tags)
    if user_role not in ("admin", "super_admin") and user_id:
        from backend.models.team_share import TeamProjectShare
        from backend.models.worker import Worker
        from backend.models.task import Task
        from backend.models.user_group import UserGroupMember
        user_group_ids = select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)
        shared_project_ids = select(TeamProjectShare.project_id).where(
            ((TeamProjectShare.target_type == "user") & (TeamProjectShare.target_id == user_id))
            | ((TeamProjectShare.target_type == "group") & TeamProjectShare.target_id.in_(user_group_ids))
        )
        owned_worker_ids = select(Worker.id).where(Worker.owner_user_id == user_id)
        worker_project_ids = select(Task.project_id).where(
            Task.worker_id.in_(owned_worker_ids), Task.project_id.is_not(None)
        ).distinct()
        stmt = stmt.where(
            Project.id.in_(shared_project_ids) | Project.id.in_(worker_project_ids)
        )
    result = await db.execute(stmt)
    all_tags: set[str] = set()
    for (tags,) in result:
        if tags:
            all_tags.update(tags)
    return sorted(all_tags)


@router.put("/reorder", response_model=list[ProjectResponse])
async def reorder_projects(
    body: list[ProjectReorderItem], db: AsyncSession = Depends(get_db)
):
    """Bulk-update sort_order for a list of projects."""
    for item in body:
        await db.execute(
            update(Project).where(Project.id == item.id).values(sort_order=item.sort_order)
        )
    await db.commit()
    result = await db.execute(
        select(Project).order_by(Project.sort_order.asc(), Project.name.asc())
    )
    return list(result.scalars().all())


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(request: Request, body: ProjectCreate, db: AsyncSession = Depends(get_db)):
    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    if user_role not in ("admin", "super_admin") and user_id:
        from backend.models.worker import Worker
        owned = await db.execute(select(Worker.id).where(Worker.owner_user_id == user_id).limit(1))
        if not owned.scalar_one_or_none():
            raise HTTPException(403, "You need a Worker to create Projects")
    # Check duplicate name
    existing = await db.execute(select(Project).where(Project.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Project '{body.name}' already exists")

    workspace = os.path.expanduser(settings.workspace_dir)
    local_path = os.path.join(workspace, body.name)
    has_remote = body.git_url is not None and body.git_url.strip() != ""

    project = Project(
        name=body.name,
        worker_id=body.worker_id,
        git_url=body.git_url if has_remote else None,
        has_remote=has_remote,
        default_branch=body.default_branch,
        local_path=local_path,
        status="pending",
        sort_order=body.sort_order,
        tags=body.tags,
        env_files=body.env_files,
        git_author_name=body.git_author_name,
        git_author_email=body.git_author_email,
        git_credential_type=body.git_credential_type,
        git_ssh_key_path=body.git_ssh_key_path,
        git_https_username=body.git_https_username,
        git_https_token=body.git_https_token,
    )
    db.add(project)

    # Auto-create Tag records for any new tag names
    if body.tags:
        existing = await db.execute(select(Tag.name))
        existing_names = {row[0] for row in existing}
        for tag_name in body.tags:
            if tag_name not in existing_names:
                db.add(Tag(name=tag_name))
                existing_names.add(tag_name)

    await db.commit()
    await db.refresh(project)

    global_cfg = await db.get(GlobalSettings, 1)
    git_config = merge_git_config(_extract_git_config(project), settings_to_dict(global_cfg))
    if has_remote:
        asyncio.create_task(_clone_repo(project.id, body.git_url, local_path, body.name, body.default_branch, git_config))
    else:
        asyncio.create_task(_init_local_repo(project.id, local_path, body.name, body.default_branch, git_config))

    return project


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    await _require_project_access(request, project_id, db)
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int, body: ProjectUpdate, request: Request, db: AsyncSession = Depends(get_db)
):
    await _require_project_access(request, project_id, db)
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    updates = body.model_dump(exclude_unset=True)
    # Auto-sync has_remote when git_url is set
    if "git_url" in updates and updates["git_url"] and "has_remote" not in updates:
        updates["has_remote"] = True
    for key, value in updates.items():
        setattr(project, key, value)

    # Auto-create Tag records for any new tag names
    if "tags" in updates and updates["tags"]:
        existing = await db.execute(select(Tag.name))
        existing_names = {row[0] for row in existing}
        for tag_name in updates["tags"]:
            if tag_name not in existing_names:
                db.add(Tag(name=tag_name))
                existing_names.add(tag_name)

    await db.commit()
    await db.refresh(project)

    # Apply git config to local repo immediately if any git fields changed
    git_fields = {"git_author_name", "git_author_email", "git_credential_type",
                  "git_ssh_key_path", "git_https_username", "git_https_token"}
    if git_fields & updates.keys() and project.local_path and os.path.isdir(project.local_path):
        await _apply_git_config(project.local_path, _extract_git_config(project))

    return project


@router.delete("/{project_id}")
async def delete_project(project_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from backend.api.deps import require_admin
    require_admin(request)
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    await db.delete(project)
    await db.commit()
    return {"ok": True}


@router.post("/{project_id}/reclone")
async def reclone_project(project_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    await _require_project_access(request, project_id, db)
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.has_remote:
        raise HTTPException(400, "Cannot reclone a local project")
    project.status = "pending"
    project.error_message = None
    await db.commit()
    global_cfg = await db.get(GlobalSettings, 1)
    git_config = merge_git_config(_extract_git_config(project), settings_to_dict(global_cfg))
    asyncio.create_task(_clone_repo(project_id, project.git_url, project.local_path, project.name, project.default_branch, git_config))
    return {"ok": True}


def _extract_git_config(project) -> dict:
    """Extract git config fields from a Project instance into a plain dict."""
    return {
        "git_author_name": project.git_author_name,
        "git_author_email": project.git_author_email,
        "git_credential_type": project.git_credential_type,
        "git_ssh_key_path": project.git_ssh_key_path,
        "git_https_username": project.git_https_username,
        "git_https_token": project.git_https_token,
    }


async def _apply_git_config(local_path: str, git_config: dict):
    """Write per-repo git config after clone/init so commits use the correct identity."""
    async def _git_config(key: str, value: str):
        proc = await asyncio.create_subprocess_exec(
            "git", "config", key, value,
            cwd=local_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    if git_config.get("git_author_name"):
        await _git_config("user.name", git_config["git_author_name"])
    if git_config.get("git_author_email"):
        await _git_config("user.email", git_config["git_author_email"])

    ctype = git_config.get("git_credential_type")
    if ctype == "ssh" and git_config.get("git_ssh_key_path"):
        key_path = git_config["git_ssh_key_path"]
        ssh_cmd = f"ssh -i {key_path} -o StrictHostKeyChecking=no"
        await _git_config("core.sshCommand", ssh_cmd)
    elif ctype == "https" and git_config.get("git_https_token"):
        # Store credentials in the repo's local credential store so git push/pull can auth.
        # We write a plaintext .git/credentials file and point credential.helper at it.
        import pathlib
        from urllib.parse import urlparse
        creds_path = pathlib.Path(local_path) / ".git" / "credentials"
        username = git_config.get("git_https_username") or "oauth2"
        token = git_config["git_https_token"]
        # Extract host from remote URL; fall back to wildcard if not available
        host = ""
        try:
            remote_proc = await asyncio.create_subprocess_exec(
                "git", "remote", "get-url", "origin",
                cwd=local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_data, _ = await remote_proc.communicate()
            remote_url = stdout_data.decode().strip()
            if remote_url.startswith("http"):
                parsed = urlparse(remote_url)
                host = parsed.hostname or ""
            elif ":" in remote_url and "@" in remote_url:
                # git@github.com:user/repo.git format
                host = remote_url.split("@")[1].split(":")[0]
        except Exception:
            pass
        if not host:
            host = "github.com"
        # Build credential lines for both https and http schemes
        creds_content = f"https://{username}:{token}@{host}\nhttp://{username}:{token}@{host}\n"
        creds_path.write_text(creds_content)
        # Reset credential helper chain first — an empty string clears all inherited
        # helpers (e.g. macOS osxkeychain) so they don't take priority over our store.
        await _git_config("credential.helper", "")
        await _git_config("credential.helper", f"store --file {creds_path}")


def _generate_claude_md(project_name: str, git_url: str | None, default_branch: str) -> str:
    """Generate a CLAUDE.md template for a new project."""
    remote_info = git_url if git_url else "无（纯本地项目）"
    return f"""# {project_name} — 项目指南

> **重要：Claude 必须自主维护本文件。** 架构或约定变化时更新，保持简洁。

## Git 信息

- Remote: {remote_info}
- 默认分支: {default_branch}

## 任务生命周期

你收到任务后，按以下 9 步流程自主完成：

1. **领取任务** — 你已被分配任务，阅读本文件和项目代码理解上下文
2. **创建工作区**:
   - `git fetch origin`（如有 remote）
   - `git worktree add -b task-<简短描述> .claude-manager/worktrees/task-<简短描述> origin/{default_branch}`
   - 进入 worktree 目录工作（后续所有操作在 worktree 中）
   - 如果 worktree 创建失败，直接在当前分支工作
3. **实现功能** — 编写代码，确保可运行
4. **提交代码** — `git add` + `git commit`，commit message 简洁描述改动
5. **Merge + 测试**:
   - `git fetch origin && git merge origin/{default_branch}`（集成最新代码，如有 remote）
   - 运行测试（如有测试命令）
6. **自动合并到 {default_branch}**（如有 remote）:
   - `git fetch origin {default_branch}`
   - `git rebase origin/{default_branch}`，如果冲突则自行 resolve
   - 如果成功：`git checkout {default_branch} && git merge <task-branch> && git push origin {default_branch}`
   - 如果这一步有任何失败，退回到步骤 5 重试
   - （纯本地项目跳过本步）
7. **标记完成** — 更新文档（必须在清理之前，防止进程被杀时状态丢失）
8. **清理** — 回到项目根目录:
   - `git worktree remove .claude-manager/worktrees/<worktree名>`
   - `git branch -D <task-branch>`
   - 如有 remote: `git push origin --delete <task-branch>`
9. **经验沉淀** — 在 PROGRESS.md 记录经验教训（可选）

### 冲突处理

rebase 发生冲突时：
1. 查看冲突文件: `git diff --name-only --diff-filter=U`
2. 逐个解决冲突
3. `git add <resolved-files> && git rebase --continue`
4. 如果无法解决: `git rebase --abort`，退回步骤 5

### 状态判断

- 通过 `git remote -v` 判断是否有 remote
- 有 remote → 必须完成步骤 6（merge + push）
- 无 remote → 跳过步骤 5 的 fetch、步骤 6 和步骤 8 的远程分支删除

## 文件维护规则

> **以下文件都由 Claude Code 自主维护，每次功能变更后必须同步更新。**

- **CLAUDE.md**（本文件）：架构、约定、关键路径变化时更新，只改变化的部分，保持简洁
- **README.md**：面向用户的文档，功能、使用流程变化时同步更新，保持与实际代码一致
- **TEST.md**：测试指南，新增功能时同步添加测试用例和文档
- **PROGRESS.md**：见下方「经验教训沉淀」

## 测试规范

**开发时必须主动使用测试，不是事后补充！**

- **改代码前**：先跑测试，确认基线全绿
- **改代码后**：再跑一遍确认无回归
- **新增功能**：同步新增测试用例，更新 TEST.md
- **修 bug**：先写复现 bug 的测试（红），修复后确认变绿

## 经验教训沉淀

每次遇到问题或完成重要改动后，要在 PROGRESS.md 中记录：
- 遇到了什么问题
- 如何解决的
- 以后如何避免
- **必须附上 git commit ID**

**同样的问题不要犯两次！**

## 注意事项

- 在 worktree 中工作时，不要切换到其他分支
- 完成任务后确保代码可运行、测试通过
"""


async def _clone_repo(project_id: int, git_url: str, local_path: str, project_name: str, default_branch: str, git_config: dict | None = None):
    """Clone a git repo in the background."""
    async with async_session() as db:
        await db.execute(
            update(Project).where(Project.id == project_id).values(status="cloning")
        )
        await db.commit()

    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        # Build env with git credentials so clone/fetch can authenticate
        git_env = _build_git_env(git_config or {})
        env = {**os.environ, **git_env} if git_env else None

        if os.path.isdir(local_path):
            # Already exists, just fetch
            proc = await asyncio.create_subprocess_exec(
                "git", "fetch", "--all",
                cwd=local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"git fetch failed: {stderr.decode()}")
        else:
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", git_url, local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"git clone failed: {stderr.decode()}")

        # Apply per-repo git config (author identity + credentials)
        if git_config:
            await _apply_git_config(local_path, git_config)

        # Generate CLAUDE.md if not exists
        claude_md_path = os.path.join(local_path, "CLAUDE.md")
        if not os.path.exists(claude_md_path):
            with open(claude_md_path, "w") as f:
                f.write(_generate_claude_md(project_name, git_url, default_branch))

            # Stage and commit CLAUDE.md so it's not left untracked
            proc = await asyncio.create_subprocess_exec(
                "git", "add", "CLAUDE.md",
                cwd=local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            proc = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", "Add CLAUDE.md for Claude Code Manager",
                cwd=local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            # Don't fail on commit error — repo may have no user config yet

        # Auto-scan for .env files after clone
        env_files = _scan_env_files(local_path)
        async with async_session() as db:
            await db.execute(
                update(Project).where(Project.id == project_id).values(
                    status="ready", env_files=env_files
                )
            )
            await db.commit()

    except Exception as e:
        async with async_session() as db:
            await db.execute(
                update(Project)
                .where(Project.id == project_id)
                .values(status="error", error_message=str(e)[:1000])
            )
            await db.commit()


async def _init_local_repo(project_id: int, local_path: str, project_name: str, default_branch: str, git_config: dict | None = None):
    """Initialize a local git repo (no remote)."""
    async with async_session() as db:
        await db.execute(
            update(Project).where(Project.id == project_id).values(status="initializing")
        )
        await db.commit()

    try:
        os.makedirs(local_path, exist_ok=True)

        if not os.path.isdir(os.path.join(local_path, ".git")):
            # git init
            proc = await asyncio.create_subprocess_exec(
                "git", "init", "-b", default_branch,
                cwd=local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"git init failed: {stderr.decode()}")

            # Apply per-repo git config before first commit so author is correct
            if git_config:
                await _apply_git_config(local_path, git_config)

            # Generate CLAUDE.md
            claude_md_path = os.path.join(local_path, "CLAUDE.md")
            with open(claude_md_path, "w") as f:
                f.write(_generate_claude_md(project_name, None, default_branch))

            # Initial commit
            proc = await asyncio.create_subprocess_exec(
                "git", "add", "CLAUDE.md",
                cwd=local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            proc = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", "Initial commit with CLAUDE.md",
                cwd=local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"git commit failed: {stderr.decode()}")

        # Auto-scan for .env files after init
        env_files = _scan_env_files(local_path)
        async with async_session() as db:
            await db.execute(
                update(Project).where(Project.id == project_id).values(
                    status="ready", env_files=env_files
                )
            )
            await db.commit()

    except Exception as e:
        async with async_session() as db:
            await db.execute(
                update(Project)
                .where(Project.id == project_id)
                .values(status="error", error_message=str(e)[:1000])
            )
            await db.commit()


# ── Env files helpers ─────────────────────────────────────────────────────────

# Patterns to match when auto-scanning for .env files
_ENV_FILE_PATTERNS = [".env", ".env.*", "*.env"]
# Directories to skip during scan
_SCAN_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", "vendor",
    ".claude-manager",
}


def _scan_env_files(local_path: str) -> list[str]:
    """Walk project tree and return relative paths of .env-style files."""
    found: list[str] = []
    root = pathlib.Path(local_path)
    for dirpath, dirnames, filenames in os.walk(local_path):
        dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP_DIRS]
        for fname in filenames:
            if any(fnmatch.fnmatch(fname, pat) for pat in _ENV_FILE_PATTERNS):
                rel = str(pathlib.Path(dirpath, fname).relative_to(root))
                found.append(rel)
    return sorted(found)


def _safe_resolve(local_path: str, rel_path: str) -> pathlib.Path:
    """Resolve rel_path under local_path, raising 400 on path traversal."""
    root = pathlib.Path(local_path).resolve()
    target = (root / rel_path).resolve()
    if not str(target).startswith(str(root) + os.sep) and target != root:
        raise HTTPException(400, "Invalid path")
    return target


# ── Env files endpoints ───────────────────────────────────────────────────────

class EnvFileInfo(BaseModel):
    path: str
    exists: bool


class EnvFilesListResponse(BaseModel):
    files: list[EnvFileInfo]


class EnvFileContent(BaseModel):
    content: str


class ScanEnvFilesResponse(BaseModel):
    tracked: list[str]    # already in env_files
    discovered: list[str] # found in repo but not yet tracked


@router.get("/{project_id}/env-files", response_model=EnvFilesListResponse)
async def list_env_files(project_id: int, db: AsyncSession = Depends(get_db)):
    """List all configured env file paths and whether each exists on disk."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.local_path:
        raise HTTPException(400, "Project has no local path")
    files = []
    for rel in (project.env_files or []):
        target = _safe_resolve(project.local_path, rel)
        files.append(EnvFileInfo(path=rel, exists=target.exists()))
    return EnvFilesListResponse(files=files)


@router.get("/{project_id}/env-files/{filepath:path}", response_model=EnvFileContent)
async def get_env_file(
    project_id: int, filepath: str, db: AsyncSession = Depends(get_db)
):
    """Read content of a configured env file. Returns empty string if not yet created."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.local_path:
        raise HTTPException(400, "Project has no local path")
    if filepath not in (project.env_files or []):
        raise HTTPException(403, "Path not in project env_files list")
    target = _safe_resolve(project.local_path, filepath)
    if not target.exists():
        return EnvFileContent(content="")
    return EnvFileContent(content=target.read_text(encoding="utf-8"))


@router.put("/{project_id}/env-files/{filepath:path}", response_model=EnvFileContent)
async def update_env_file(
    project_id: int, filepath: str, body: EnvFileContent, db: AsyncSession = Depends(get_db)
):
    """Write content to a configured env file. Creates the file (and dirs) if needed."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.local_path:
        raise HTTPException(400, "Project has no local path")
    if filepath not in (project.env_files or []):
        raise HTTPException(403, "Path not in project env_files list")
    target = _safe_resolve(project.local_path, filepath)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    return EnvFileContent(content=body.content)


@router.post("/{project_id}/scan-env-files", response_model=ScanEnvFilesResponse)
async def scan_env_files(project_id: int, db: AsyncSession = Depends(get_db)):
    """Scan the project repo for .env-style files and return discovered paths."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.local_path or not os.path.isdir(project.local_path):
        raise HTTPException(400, "Project has no local path or directory does not exist")
    tracked = list(project.env_files or [])
    tracked_set = set(tracked)
    all_found = _scan_env_files(project.local_path)
    discovered = [p for p in all_found if p not in tracked_set]
    return ScanEnvFilesResponse(tracked=tracked, discovered=discovered)
