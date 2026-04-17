# Claude Code Manager - 项目指南

> **重要：Claude 必须自主维护本文件。** 当项目架构、约定、关键路径发生变化时，只做必要的修改，保持简洁。不要大段重写，只更新变化的部分。

## 概述

Web 端调度管理多个 Claude Code 实例并行工作。Backend (FastAPI) + Frontend (React/Vite) + SQLite/PostgreSQL/MySQL。

GitHub: https://github.com/zjw49246/Claude-Code-Manager.git

## 技术栈

- **后端**: Python 3.11+, FastAPI, SQLAlchemy async, SQLite/PostgreSQL/MySQL
- **前端**: React 19, Vite, Tailwind CSS v4, TypeScript, Lucide icons
- **实时通信**: WebSocket (原生, channel-based pub/sub)
- **语音**: OpenAI Whisper API
- **隧道**: Cloudflare Tunnel / ngrok

## 项目结构

```
claude-manager/
├── backend/
│   ├── main.py                  # FastAPI 入口, 全局单例, 静态文件服务
│   ├── config.py                # Pydantic BaseSettings (.env)
│   ├── database.py              # SQLAlchemy async engine + session
│   ├── api/                     # 路由
│   │   ├── tasks.py             # 任务 CRUD + plan 审批 + conflict 解决
│   │   ├── chat.py              # 多轮对话 (基于 task, --resume)
│   │   ├── instances.py         # 实例 CRUD + Ralph Loop 控制 + Dispatcher 端点
│   │   ├── projects.py          # Project CRUD + git clone
│   │   ├── ws.py                # WebSocket 端点
│   │   ├── voice.py             # Whisper 语音转文字
│   │   ├── auth.py              # Token 登录
│   │   └── system.py            # 健康检查 + 统计
│   ├── middleware/auth.py       # Bearer token 认证中间件
│   ├── models/                  # SQLAlchemy ORM 模型
│   │   ├── task.py              # Task (含 session_id, last_cwd, project_id)
│   │   ├── instance.py          # Claude Code 实例
│   │   ├── project.py           # Project (name, git_url, local_path)
│   │   ├── log_entry.py         # 执行日志
│   │   └── worktree.py          # Git worktree 跟踪
│   ├── schemas/                 # Pydantic 请求/响应模型
│   └── services/                # 核心业务逻辑
│       ├── instance_manager.py  # 子进程生命周期 (launch/stop/consume output)
│       ├── dispatcher.py        # 全局调度器 (9 步任务生命周期)
│       ├── ralph_loop.py        # 自动取活循环 (legacy, 保留兼容)
│       ├── stream_parser.py     # NDJSON stream-json 解析器
│       ├── task_queue.py        # 优先级队列 (asc = 优先级越高)
│       ├── worktree_manager.py  # Git worktree 创建/合并/删除 + rebase+push
│       ├── ws_broadcaster.py    # WebSocket channel 广播
│       ├── whisper_client.py    # OpenAI Whisper 客户端
│       └── backup_service.py    # 数据库备份 (auto-backup SDK 封装, 可选)
├── frontend/
│   └── src/
│       ├── api/client.ts        # API 客户端 + 类型 (401 自动登出, 动态 base URL)
│       ├── api/ws.ts            # WebSocket 客户端 (指数退避重连)
│       ├── config/server.ts     # 远程服务器 URL 配置 (Capacitor/Android 支持)
│       ├── config/theme.ts      # 浅色/深色主题切换 (localStorage 持久化)
│       ├── pages/               # Dashboard, TasksPage, LoginPage, ServerConfigPage
│       ├── components/
│       │   ├── Chat/ChatView.tsx       # 多轮对话 UI (基于 task)
│       │   ├── Instances/              # InstanceGrid, InstanceLog
│       │   ├── Tasks/                  # TaskForm, TaskList
│       │   ├── PlanReview/PlanPanel.tsx # Plan 审批
│       │   └── Voice/VoiceButton.tsx   # MediaRecorder → Whisper
│       └── hooks/useWebSocket.ts
├── scripts/
│   ├── dev.sh                   # 一键启动开发环境
│   └── tunnel.sh                # ngrok 隧道
├── .env                         # AUTH_TOKEN, OPENAI_API_KEY, DATABASE_URL
└── pyproject.toml
```

## 关键约定

- **优先级**: 数字越小优先级越高 (P0 > P1 > P2)，排序用 `.asc()`
- **Session 绑定**: `session_id` 和 `last_cwd` 在 **Task** 上（不是 Instance），因为 instance 是轮换执行不同 task 的 worker
- **Claude Code 调用**: `claude -p [prompt] --dangerously-skip-permissions --output-format stream-json --verbose`
- **Resume**: `claude -p [follow-up] --resume [session_id]` — 必须使用和原始 session 相同的 cwd
- **Model 配置**: 默认 `claude-opus-4-6`，支持全称模型 ID（`claude-opus-4-6`, `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`）。`[1m]` 后缀开启 1M context（计费翻倍）
- **Effort Level**: 默认 `medium`，支持 `low/medium/high/xhigh/max`。优先级链：Task.effort_level → Instance.effort_level → settings.default_effort。通过 CLI `--effort` 参数传递
- **Extended Thinking 预算**: Instance 上的 `thinking_budget` 字段 → 子进程 `MAX_THINKING_TOKENS` env var；NULL = 用 CLI 默认
- **Thinking 解析**: stream_parser 兼容多种字段名（`thinking` / `text` / 嵌套 content blocks）；加密 thinking 显示为 `[encrypted thinking ...]` 标记
- **环境变量清理**: 生成子进程前必须 unset `CLAUDECODE` / `CLAUDE_CODE`，避免嵌套检测
- **停止顺序**: SIGTERM → 等 10s → SIGKILL
- **备份服务**: `BackupService`（`backend/services/backup_service.py`）封装 auto-backup SDK，在 lifespan 中以后台线程（APScheduler）运行，支持 local / s3 / oss；`BACKUP_ENABLED=false` 时完全不加载
- **WebSocket channels**: `instance:{id}`, `task:{id}`, `tasks`, `system`
- **认证**: 除 `/api/system/health` 和 `/api/auth/login` 外，所有 API 需要 `Authorization: Bearer <token>`
- **前端 type 导入**: 用 `import type { X }` 导入类型，`import { api }` 导入值（Vite 会去除 type-only exports）
- **Tailwind v4**: 用 `@import "tailwindcss"` + `@tailwindcss/vite` 插件，无 tailwind.config
- **主题**: 深色/浅色切换通过覆盖 `--color-gray-*` CSS 变量实现灰度反转，内容文字用 `text-foreground`（随主题变化），按钮文字保持 `text-white`
- **Android App**: Capacitor 打包，API/WS 地址通过 `config/server.ts` 动态获取，LoginPage 可展开配置 Server URL
- **调度器**: `GlobalDispatcher` 只负责分配任务、启动 Claude Code、判断成败。所有 git 操作（worktree、commit、merge、push）全由 Claude Code 自主完成
- **任务生命周期**: pending → in_progress → executing → completed（失败回 pending 重试）
- **项目**: `Project` 模型管理 git repo，支持 clone 已有仓库（has_remote=True）和本地 git init（has_remote=False）
- **Task.project_id**: 可选关联 Project，dispatcher 自动解析为 target_repo

## 任务生命周期（9 步）

你收到任务后，按以下流程自主完成：

1. **领取任务** — 你已被分配任务，阅读 CLAUDE.md 和相关代码
2. **创建工作区**:
   - `git fetch origin`（如有 remote）
   - `git worktree add -b task-<简短描述> .claude-manager/worktrees/task-<简短描述> origin/main`
   - 进入 worktree 目录工作（后续所有操作在 worktree 中）
   - 如果 worktree 创建失败，直接在当前分支工作
3. **实现功能** — 编写代码，确保可运行
4. **提交代码** — `git add` + `git commit`
5. **Merge + 测试**:
   - `git fetch origin && git merge origin/main`
   - `uv run python -m pytest backend/tests/ -v`（后端测试）
   - `cd frontend && npx tsc --noEmit`（前端类型检查）
6. **自动合并到 main**:
   - `git fetch origin main`
   - `git rebase origin/main`，冲突则自行 resolve
   - 成功后: `git checkout main && git merge <task-branch> && git push origin main`
   - 失败则退回步骤 5 重试
7. **标记完成** — 更新文档（在清理之前）
8. **清理** — 回到项目根目录:
   - `git worktree remove .claude-manager/worktrees/<worktree名>`
   - `git branch -D <task-branch>`
   - 如有 remote: `git push origin --delete <task-branch>`
9. **经验沉淀** — 在 PROGRESS.md 记录经验教训（可选）

通过 `git remote -v` 判断是否有 remote，有则执行步骤 5-6-8 的 remote 操作，无则跳过。

**状态流转：**
```
pending → in_progress → executing → completed
                           ↓
                        (fail)
                           ↓
                        pending
                       (retry)
```

## 开发命令

```bash
# 依赖管理（使用 uv）
uv sync              # 安装生产依赖
uv sync --group dev  # 安装生产 + 开发依赖（pytest 等）

# 一键启动 (后端 + 前端)
./scripts/dev.sh

# 仅后端
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# 仅前端
cd frontend && npx vite --host

# 构建前端
cd frontend && npm run build

# 运行测试
uv run python -m pytest backend/tests/ -v

# 生产模式 (单端口，后端服务前端静态文件)
cd frontend && npm run build && cd ..
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000

# 公网部署 (Cloudflare Tunnel)
# 首次设置: cloudflared tunnel login → create → route dns → 编写 ~/.cloudflared/config.yml
# 每次部署:
cd frontend && npm run build && cd ..
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000  # 终端1
cloudflared tunnel run <tunnel-name>                          # 终端2
```

## 数据库

默认使用 SQLite（`./claude_manager.db`），也支持 PostgreSQL 和 MySQL 作为外部数据库。通过 `.env` 中的 `DATABASE_URL` 切换：

```bash
# SQLite（默认）
DATABASE_URL=sqlite+aiosqlite:///./claude_manager.db

# PostgreSQL（需安装: uv sync --extra postgres）
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/claude_manager

# MySQL（需安装: uv sync --extra mysql）
DATABASE_URL=mysql+aiomysql://user:pass@host:3306/claude_manager
```

**数据迁移脚本**：在数据库之间迁移全部数据（注意使用同步 URL）：
```bash
# 先在目标库初始化 schema
DATABASE_URL=postgresql+asyncpg://... uv run alembic upgrade head

# 再迁移数据（使用同步 URL）
uv run python scripts/transfer_db.py \
    "sqlite:///./claude_manager.db" \
    "postgresql://user:pass@host:5432/claude_manager"
```

使用 **Alembic** 管理 schema 版本。`init_db()` 在启动时自动执行 `alembic upgrade head`，无需手动操作。

> **严禁手动修改数据库 schema**（如直接执行 `ALTER TABLE`、`DROP COLUMN` 等）。所有 schema 变更必须且只能通过 Alembic migration 文件管理，否则会导致 migration 状态不一致、其他环境部署失败。

**Schema 变更流程**（详见 [DATABASE.md](./DATABASE.md)）：
1. 修改 `backend/models/` 中的模型
2. `uv run alembic revision --autogenerate -m "描述"` 生成 migration
3. 测试：upgrade → downgrade → upgrade 全通过后提交
4. migration 文件与模型修改**同一个 commit** 提交

```bash
uv run alembic upgrade head    # 手动升级（通常不需要，启动自动执行）
uv run alembic current         # 查看当前版本
uv run alembic history         # 查看历史
```

## 文件维护规则

> **四个文件都由 Claude Code 自主维护，每次功能变更后必须同步更新。**

- **CLAUDE.md**（本文件）：架构、约定、关键路径变化时更新，只改变化的部分，保持简洁
- **README.md**：面向用户的文档，功能、API、使用流程变化时同步更新，保持与实际代码一致
- **TEST.md**：测试指南，新增功能时同步添加测试用例和文档
- **PROGRESS.md**：见下方「经验教训沉淀」

## 测试规范

**开发时必须主动使用测试，不是事后补充！**

- **改代码前**：先跑 `uv run python -m pytest backend/tests/ -v`，确认基线全绿
- **改代码后**：再跑一遍确认无回归 + `cd frontend && npx tsc --noEmit` 检查类型
- **新增功能**：同步新增测试用例，更新 [TEST.md](./TEST.md)
- **修 bug**：先写复现 bug 的测试（红），修复后确认变绿
- **提交代码**：改完代码 + 更新文档后，`git commit` + `git push origin main`（默认必须 push）
- 详细测试清单和手动测试项见 [TEST.md](./TEST.md)

## 经验教训沉淀

每次遇到问题或完成重要改动后，要在 [PROGRESS.md](./PROGRESS.md) 中记录：
- 遇到了什么问题
- 如何解决的
- 以后如何避免
- **必须附上 git commit ID**

**同样的问题不要犯两次！**
