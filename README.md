# Claude Code Manager

Web 端调度和管理多个 Claude Code 实例并行工作。灵感来自胡渊鸣的文章「我给 10 个 Claude Code 打工」。

> **⚠️ 重要安全提示：** 本项目会以 `--dangerously-skip-permissions` 模式运行 Claude Code，这意味着 Claude Code 将拥有**不受限制的文件读写、命令执行和网络访问权限**，并且会自动执行 `git push` 等操作。**强烈建议在一台单独的、没有重要文件的电脑或虚拟机上部署**，避免对你的个人数据或工作环境造成意外影响。

## 功能

### 核心调度
- **全局调度器** — 启动时自动创建 worker、自动分配任务，无需手动操作
- **Claude Code 完全自主** — Claude Code 自主完成 worktree 创建、commit、fetch、merge、push、冲突解决和清理，Dispatcher 只负责分配任务和判断成败
- **9 步任务生命周期** — 领取 → 创建工作区 → 实现 → 提交 → merge + 测试 → 合并到 main → 标记完成 → 清理 → 经验沉淀
- **任务队列** — 按优先级自动调度（数字越小优先级越高）
- **多实例并行** — 同时运行多个 Claude Code 实例，各自处理不同任务
- **Git Worktree** — 每个实例在独立的 worktree 中工作，互不干扰

### 执行模式
- **PTY 持久会话模式** — 默认模式，Claude Code 以常驻交互会话运行，多轮免冷启动（热 session 复用），首次启动有 Cold Start 指示器
- **Goal 模式** — `mode="goal"` 使用自然语言完成条件（`goal_condition`），每 turn 后由轻量评估器（默认 Haiku）自动判断是否达成目标
- **Plan Mode** — 敏感任务先生成只读计划，人工审批后再执行
- **Effort Level** — 支持 `low` / `medium` / `high` / `xhigh` / `max` 五档，优先级链：Task → Instance → 全局默认
- **Model 配置** — 支持全称模型 ID（`claude-opus-4-6`、`claude-sonnet-4-6`、`claude-haiku-4-5` 等），`[1m]` 后缀开启 1M context
- **Thinking Budget** — Instance 级别设置 `thinking_budget`，通过 `MAX_THINKING_TOKENS` 传递给 CLI
- **Workflows 开关** — Task 级别控制是否启用 Workflow 工具，关闭时节省 token

### 智能能力
- **Skills 系统** — MCP-based 技能注入，创建 Task 时勾选需要的 Skills（如 Monitor），Dispatcher 动态生成 MCP config 注入 Claude CLI
- **Monitor Sub-Agent** — Agent 可自主创建持久监控子 Agent，子 Agent 拥有独立 MCP 工具（report_status / mark_complete / get_context），自主决定检查频率并向系统汇报
- **原生子 Agent 镜像（PTY 模式）** — 模型用内置 Agent/Task/Monitor 工具开的子 agent 会被 PTY 层观测并自动注册进子 agent 体系（类别 native-agent / native-monitor），统一展示和管理

### 交互与对话
- **多轮对话** — 任务完成后可通过 Chat 界面继续追问，自动 `--resume` 同一 session
- **交互式提问（ask_user）** — 模型调用内置 `AskUserQuestion` 时，聊天里弹出可选卡片（单选/多选/自定义文本），用户选完即把答案喂回模型继续。超时默认 1800s，支持跨页面全局通知（右下角弹窗 + 未读标记），可用 `ASK_USER_ENABLED=false` 关闭
- **权限透传（PTY 模式）** — CC 请求工具权限时聊天里出现卡片（工具名/描述/输入预览），点允许/拒绝实时回包；120s 超时默认拒绝
- **语音输入** — 通过 OpenAI Whisper API 语音转文字创建任务

### 可靠性
- **Claude Pool** — 多账号池自动切换：撞限/认证失败时自动换号并硬链接 session 实现无缝 `--resume`；PTY 模式下支持主动限速轮换（仅在 5h 窗口利用率 >=90% 时触发）
- **瞬时 429/过载自动重试** — Anthropic 基础设施侧的临时限流/过载（非账号额度用尽），指数退避+jitter 用同一账号自动 `--resume` 重试，最多 5 次
- **进程超时保护** — 单任务最长执行时间可配置，超时后自动 kill

### 分布式
- **分布式 Worker** — 将任务分发到远程 EC2 实例执行，突破单机并发瓶颈。Phase 1（创建/部署/管理）+ Phase 2（任务转发+事件中继）+ Phase 3（任务实时迁移）全部可用。详见 [Worker 部署指南](docs/worker-deployment-guide.md)
- **一键更新重启** — `POST /api/system/update` 拉取最新代码 + 刷新 PTY 依赖 + 数据库迁移 + 重建前端 + 智能重启（自动检测 systemd 服务名）

### 项目与协作
- **项目管理** — 支持 clone 已有仓库（有 remote）和本地 git init（无 remote），创建任务时可直接新建项目
- **PR Monitor** — GitHub PR 自动审核，Webhook 接收 PR 事件后创建审核 Task，Claude 审核代码后自动 approve/merge 或 request-changes
- **PWA** — 手机浏览器 Add to Home Screen，原生 App 体验
- **Android App** — 通过 Capacitor 打包原生 APK，App 内可配置远程服务器地址
- **主题切换** — 支持浅色/深色主题，偏好持久化
- **Token 认证** — Bearer Token 保护所有 API，安全远程访问
- **远程访问** — 通过 Cloudflare Tunnel 隧道暴露到公网

## 任务生命周期

Dispatcher 只负责分配任务和判断成败，Claude Code 自主完成整个工作流：

1. **领取任务** — Dispatcher dequeue，status=in_progress
2. **创建工作区** — Claude Code 自主创建 git worktree，status=executing
3. **实现功能** — Claude Code 在 worktree 中编写代码
4. **提交代码** — Claude Code 自主 `git add` + `git commit`
5. **Merge + 测试** — Claude Code 自主 `git fetch origin && git merge origin/main` + 运行测试
6. **合并到 main** — Claude Code 自主 rebase + merge + push（有冲突自行解决）
7. **标记完成** — Claude Code 更新文档
8. **清理** — Claude Code 自主清理 worktree 和 task 分支
9. **经验沉淀** — Claude Code 在 PROGRESS.md 记录经验

**状态流转：**
```
pending → in_progress → executing → completed
                           ↓
                        (fail)
                           ↓
                        pending (retry)
```

## 技术栈

| 层 | 技术 |
|---|------|
| Backend | Python 3.11+, FastAPI, SQLAlchemy (async), Alembic |
| Database | SQLite (默认) / PostgreSQL / MySQL |
| Frontend | React 19, Vite, Tailwind CSS v4, TypeScript, Lucide icons |
| PTY | claude-pty（Claude Code 持久会话框架） |
| 实时通信 | WebSocket (原生, channel-based pub/sub) |
| MCP | FastMCP server (Skills / Monitor Agent) |
| 语音 | OpenAI Whisper API |
| 远程 | Cloudflare Tunnel / ngrok |
| Worker | AWS EC2, boto3, rsync, SSH |

## 项目结构

```
claude-manager/
├── backend/
│   ├── main.py                  # FastAPI 入口, 全局单例, 静态文件服务
│   ├── config.py                # Pydantic BaseSettings (.env)
│   ├── database.py              # SQLAlchemy async engine + session
│   ├── api/                     # REST + WebSocket 路由
│   │   ├── tasks.py             # 任务 CRUD + plan 审批 + conflict 解决
│   │   ├── chat.py              # 多轮对话 (基于 task, --resume)
│   │   ├── instances.py         # 实例 CRUD + Ralph Loop + Dispatcher 端点
│   │   ├── projects.py          # Project CRUD + git clone
│   │   ├── monitor.py           # Monitor Session CRUD + 子 agent endpoints
│   │   ├── pool.py              # Claude 账号池 status/usage/reload/clear-cooldown
│   │   ├── pr_monitor.py        # PR Monitor CRUD + GitHub webhook
│   │   ├── workers.py           # 分布式 Worker CRUD + stop/start/destroy/retry
│   │   ├── sub_agents.py        # 子 Agent summary API
│   │   ├── ask_user.py          # ask_user 拦截 + 答案回流
│   │   ├── settings.py          # 运行时设置 API
│   │   ├── system.py            # 健康检查 + 统计 + 一键更新
│   │   ├── ws.py                # WebSocket 端点
│   │   ├── voice.py             # Whisper 语音转文字
│   │   └── auth.py              # Token 登录
│   ├── middleware/auth.py       # Bearer token 认证中间件
│   ├── hooks/
│   │   └── ask_user_hook.py     # AskUserQuestion PreToolUse hook 脚本
│   ├── models/                  # SQLAlchemy ORM 模型
│   │   ├── task.py              # Task (session_id, last_cwd, project_id, enabled_skills, effort_level...)
│   │   ├── instance.py          # Claude Code 实例
│   │   ├── project.py           # Project (name, git_url, local_path)
│   │   ├── sub_agent.py         # SubAgentSession + SubAgentReport (通用子 agent)
│   │   ├── pr_monitor.py        # MonitoredRepo + PRReview
│   │   ├── worker.py            # 分布式 Worker (EC2 实例 + bootstrap 状态机)
│   │   ├── log_entry.py         # 执行日志
│   │   └── worktree.py          # Git worktree 跟踪
│   ├── schemas/                 # Pydantic 请求/响应模型
│   ├── mcp/                     # MCP Servers
│   │   ├── ccm_skills_server.py         # 主 Agent MCP: create_monitor / check_monitors / stop_monitor
│   │   └── ccm_monitor_agent_server.py  # 子 Agent MCP: report_status / mark_complete / get_context
│   └── services/                # 核心业务逻辑
│       ├── dispatcher.py        # 全局调度器 (9 步任务生命周期 + goal + monitor)
│       ├── instance_manager.py  # 子进程生命周期 (launch/stop/consume, MCP 注入)
│       ├── claude_pool.py       # 多账号池 (限速检测/自动切换/session 迁移/额度查询)
│       ├── goal_evaluator.py    # Goal 条件评估器 (claude -p 子进程)
│       ├── mcp_config.py        # MCP config 动态生成
│       ├── cloud_provider.py    # AWS EC2 Provider (Worker 实例创建/启停/销毁)
│       ├── worker_provisioner.py # Worker 全生命周期 (创建→bootstrap→ready)
│       ├── worker_proxy.py      # 任务转发到 Worker
│       ├── worker_relay.py      # Manager↔Worker WebSocket 事件中继
│       ├── task_migrator.py     # 任务在本机↔Worker 之间迁移
│       ├── update_service.py    # 一键更新 + 智能重启
│       ├── stream_parser.py     # NDJSON stream-json 解析
│       ├── task_queue.py        # 优先级任务队列
│       ├── worktree_manager.py  # Git worktree 管理 + rebase + push
│       ├── pr_review_service.py # PR 审核 prompt 构建 + 状态回查
│       ├── ask_user.py          # ask_user 注册表 + Future 管理
│       ├── ask_user_settings.py # ask_user hook 注入/移除
│       ├── ws_broadcaster.py    # WebSocket channel 广播
│       ├── whisper_client.py    # 语音转文字
│       └── backup_service.py    # 数据库备份 (可选)
├── frontend/
│   ├── public/                  # PWA manifest, service worker, icons
│   └── src/
│       ├── api/client.ts        # API 客户端 + 类型 (401 自动登出, 动态 base URL)
│       ├── api/ws.ts            # WebSocket 客户端 (指数退避重连)
│       ├── config/server.ts     # 远程服务器 URL 配置 (Capacitor/Android)
│       ├── config/theme.ts      # 浅色/深色主题切换
│       ├── pages/               # Dashboard, TasksPage, WorkersPage, PRMonitorPage, LoginPage...
│       ├── components/
│       │   ├── AskUserNotifications.tsx   # 全局 ask_user 弹窗通知
│       │   ├── Chat/ChatView.tsx          # 多轮对话 UI
│       │   ├── Chat/MonitorPanel.tsx      # Monitor 面板
│       │   ├── Chat/SubSessionIndicator.tsx
│       │   ├── Instances/                 # InstanceGrid, InstanceLog
│       │   ├── Tasks/                     # TaskForm, TaskList, TaskConfigBadge
│       │   ├── Layout/PoolDrawer.tsx      # Pool 额度抽屉
│       │   ├── PlanReview/PlanPanel.tsx    # Plan 审批
│       │   ├── System/                    # UpdatePanel
│       │   └── Voice/VoiceButton.tsx      # 语音录入
│       └── hooks/useWebSocket.ts
├── scripts/
│   ├── dev.sh                   # 一键启动开发环境
│   ├── setup.sh                 # Worker SSH Key + 环境初始化
│   ├── refresh_pty.sh           # 刷新 claude-pty 依赖
│   ├── start_all.sh             # 生产环境启动脚本
│   └── tunnel.sh                # ngrok/cloudflare 隧道
├── docs/
│   └── worker-deployment-guide.md  # Worker 部署指南
├── pyproject.toml
└── .env
```

## 快速开始

### 前置条件

- macOS / Linux（推荐 Ubuntu 22.04+，支持 EC2 部署）
- Python 3.11+
- Node.js 18+
- [uv](https://docs.astral.sh/uv/) — Python 包管理器
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装并登录（`claude auth login`）
- Google Chrome + Xvfb（号池自动登录需要，服务器部署时安装）

### 安装

```bash
git clone https://github.com/zjw49246/Claude-Code-Manager.git && cd Claude-Code-Manager

# 后端依赖（使用 uv）
uv sync

# 如需 PostgreSQL 支持
uv sync --extra postgres

# 如需 MySQL 支持
uv sync --extra mysql

# 前端依赖
cd frontend && npm install && cd ..

# 配置
cp .env.example .env
# 编辑 .env，设置：
#   AUTH_TOKEN=你的访问密码
#   OPENAI_API_KEY=sk-...（语音功能需要）
#   WORKSPACE_DIR=~/Projects（项目工作区根目录）
```

### 启动

```bash
# 一键启动
./scripts/dev.sh

# 或分别启动
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload &
cd frontend && npx vite --host &
```

访问 http://localhost:5173，输入 `AUTH_TOKEN` 登录。

启动后 Dispatcher 会自动创建 worker 实例并开始调度。

### Android App 打包

```bash
cd frontend

# 安装 Capacitor（已在 package.json 中）
npm install

# 构建 + 同步 + 打包 APK
npm run build
npx cap sync android
JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" \
  android/gradlew -p android assembleDebug

# APK 位于 android/app/build/outputs/apk/debug/app-debug.apk
```

首次打开 App 在登录页展开 "Server URL" 输入服务器地址（如 Cloudflare Tunnel URL），然后输入 Token 登录。

## 数据库

默认使用 SQLite，也支持 PostgreSQL 和 MySQL。通过 `.env` 中的 `DATABASE_URL` 切换：

```bash
# SQLite（默认）
DATABASE_URL=sqlite+aiosqlite:///./claude_manager.db

# PostgreSQL（需安装: uv sync --extra postgres）
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/claude_manager

# MySQL（需安装: uv sync --extra mysql）
DATABASE_URL=mysql+aiomysql://user:pass@host:3306/claude_manager
```

### Schema 迁移（Alembic）

使用 Alembic 管理 schema 版本。**启动时自动执行 `alembic upgrade head`**，无需手动操作。

```bash
uv run alembic upgrade head    # 手动升级（通常不需要）
uv run alembic current         # 查看当前版本
uv run alembic history         # 查看历史
```

### 数据迁移

在数据库之间迁移全部数据（注意使用同步 URL）：

```bash
# 先在目标库初始化 schema
DATABASE_URL=postgresql+asyncpg://... uv run alembic upgrade head

# 再迁移数据
uv run python scripts/transfer_db.py \
    "sqlite:///./claude_manager.db" \
    "postgresql://user:pass@host:5432/claude_manager"
```

## 更新已部署的实例

### 方式一：一键更新（推荐）

通过 API 或前端 System 面板触发：

```bash
curl -X POST http://localhost:8000/api/system/update \
  -H "Authorization: Bearer $AUTH_TOKEN"
```

自动执行：git pull → 刷新 PTY 依赖 → 数据库迁移 → 重建前端 → 智能重启（自动检测 systemd 服务名 `SERVICE_NAME`）。

### 方式二：手动更新

```bash
git pull                      # 1. 拉取 CCM 最新代码
./scripts/refresh_pty.sh      # 2. 刷新 claude-pty 依赖（见下）
.venv/bin/alembic upgrade head  # 3. 数据库迁移
cd frontend && npm run build && cd ..  # 4. 重建前端
# 5. 重启服务（systemd / 手动）
```

> **为什么需要第 2 步**：`pyproject.toml` 里的 `claude-pty @ git+https://...` 是
> **安装时快照**——`git pull` 只更新 CCM 自己的代码，不会带来新的 PTY 框架代码。
> `scripts/refresh_pty.sh` 会对比已安装的 PTY commit 与远端 main HEAD，不一致时
> 自动重装（editable/本地开发安装会自动跳过）。

## 使用流程

### 基本流程

1. **Tasks** — 创建任务，选择已有项目或新建项目，填写 Prompt、优先级、Effort Level。可勾选 **Monitor** skill 赋予 Agent 后台监控能力
2. **Dispatcher** 自动分配任务到空闲 worker → Claude Code 自主完成所有工作（含 worktree 创建和清理） → 取下一个
3. 点击任务的 **Chat** 按钮，可以对已完成的任务继续追问
4. 启用 Monitor 的任务中，Agent 可自主创建持久监控子 Agent，Task 列表显示活跃子 Agent 数量

### Plan Mode

创建任务时选择 Mode = `plan`：
1. Claude Code 先以只读模式分析代码，生成执行计划
2. 任务进入 `plan_review` 状态，在 Tasks 页面显示计划内容
3. 点击 Approve 批准后，任务重新入队执行

### Goal Mode

创建任务时选择 Mode = `goal`，填写自然语言完成条件：
1. Claude Code 执行任务
2. 每 turn 结束后，轻量评估器（默认 `claude-haiku-4-5`）判断条件是否满足
3. 未满足则自动 `--resume` 继续执行，保持同一 session 的连续上下文
4. 达成目标后自动标记完成

### 语音输入

任务创建表单的标题和描述字段旁有 🎙 按钮，点击后录音，松开自动转文字填入。

### PR Monitor 前置条件

PR Monitor 的审核流程会在后端 shell out 调用 `gh pr view` / `gh pr review` / `gh pr merge`，使用前需满足：

1. **gh CLI 已认证**：运行后端的系统用户必须先执行 `gh auth login` 完成 GitHub 认证
2. **账号权限**：该 GitHub 账号需要对被监控仓库有 push / review 权限（auto-merge 还需要 merge 权限）
3. **PUBLIC_BASE_URL**：在 `.env` 中设置 `PUBLIC_BASE_URL`（如 `https://ccm.example.com`），PR Monitor 页面才能显示正确的 Webhook Payload URL

## 分布式 Worker

Worker 系统支持将任务分发到远程 EC2 实例执行，适合需要更多并行能力的场景。每个 Worker 是一台运行完整 CCM 的 EC2，拥有独立的 Claude 账号池。

**核心能力：**
- 水平扩展并发能力，每个 Worker 可配多个 Claude 账号
- 任务执行位置可实时切换（本机 / 任意 Worker），session 无缝衔接
- Worker 销毁时自动迁回全部任务和数据，不丢失上下文
- 前端零感知差异 — 远程任务与本地任务 UI/操作完全一致

**使用流程：**
1. **创建 Worker**：Workers 页面点 **+**，输入名称，系统自动创建 EC2 → 安装依赖 → 部署代码 → 启动服务
2. **分配账号**：Worker 详情页的号池面板添加 Claude 账号
3. **分配任务**：任务的 Config 面板 → "Run on" 下拉选择 Worker
4. **任务迁移**：运行中的任务可随时在本机和 Worker 之间迁移，session 自动同步
5. **关机/销毁**：Stop 保留实例数据（可重新 Start），Destroy 会先将所有任务迁回本机再终止实例

> 详细部署指南、前置条件、配置参数和故障排除见 **[docs/worker-deployment-guide.md](docs/worker-deployment-guide.md)**

## API

| 模块 | 端点 | 说明 |
|------|------|------|
| Projects | `GET/POST /api/projects` | 项目列表/创建 |
| | `GET/PUT/DELETE /api/projects/{id}` | 项目详情/更新/删除 |
| | `POST /api/projects/{id}/reclone` | 重新 clone |
| Tasks | `GET/POST /api/tasks` | 任务列表/创建 |
| | `GET/PUT/DELETE /api/tasks/{id}` | 任务详情/更新/删除 |
| | `POST /api/tasks/{id}/cancel` | 取消任务 |
| | `POST /api/tasks/{id}/retry` | 重试任务 |
| | `POST /api/tasks/{id}/plan/approve` | 批准计划 |
| | `POST /api/tasks/{id}/chat` | 发送追问消息 |
| | `GET /api/tasks/{id}/chat/history` | 获取对话历史 |
| | `POST /api/tasks/{id}/permissions/{rid}` | 回复权限请求 |
| | `POST /api/tasks/{id}/ask-user/{rid}` | 回复 ask_user 提问 |
| | `GET /api/tasks/{id}/ask-user/pending` | 获取待回复提问 |
| Instances | `GET/POST /api/instances` | 实例列表/创建 |
| | `DELETE /api/instances/{id}` | 删除实例 |
| | `POST /api/instances/{id}/stop` | 停止实例 |
| | `POST /api/instances/{id}/run` | 手动执行 |
| | `GET /api/instances/{id}/logs` | 获取日志 |
| Monitor | `POST /api/tasks/{id}/monitor-sessions` | 创建 monitor 子 session |
| | `GET /api/tasks/{id}/monitor-sessions` | 列出 monitor sessions |
| | `DELETE /api/tasks/{id}/monitor-sessions/{sid}` | 停止 monitor session |
| | `POST /api/tasks/{id}/monitor-sessions/{sid}/checks` | 子 agent 报告状态 |
| | `POST /api/tasks/{id}/monitor-sessions/{sid}/complete` | 子 agent 标记完成 |
| Sub-Agents | `GET /api/tasks/{id}/sub-agents/summary` | 子 agent 按类型汇总 |
| Workers | `GET/POST /api/workers` | Worker 列表/创建 |
| | `GET /api/workers/{id}` | Worker 详情 |
| | `GET /api/workers/{id}/logs` | Bootstrap 日志 |
| | `POST /api/workers/{id}/stop` | 关机 |
| | `POST /api/workers/{id}/start` | 开机 |
| | `POST /api/workers/{id}/destroy` | 销毁（自动迁回任务） |
| | `POST /api/workers/{id}/retry` | 重试 Bootstrap |
| | `GET/POST /api/workers/{id}/pool/*` | Worker 号池管理 |
| Dispatcher | `GET /api/dispatcher/status` | 调度器状态 |
| | `POST /api/dispatcher/start` | 启动调度器 |
| | `POST /api/dispatcher/stop` | 停止调度器 |
| Pool | `GET /api/pool/status` | 账号池状态（可用/冷却/禁用） |
| | `GET /api/pool/usage` | 账号池状态 + 每账号额度利用率（5h/7d） |
| | `POST /api/pool/reload` | 重新加载账号配置 |
| | `POST /api/pool/accounts/{id}/clear-cooldown` | 清除账号冷却 |
| Voice | `POST /api/voice/transcribe` | 语音转文字 |
| System | `GET /api/system/health` | 健康检查 |
| | `GET /api/system/stats` | 统计信息 |
| | `POST /api/system/update` | 一键更新重启 |
| WebSocket | `ws://host/ws` | 实时推送（subscribe channel） |
| Auth | `POST /api/auth/login` | Token 登录 |

所有 API（除 health、login、github webhook）需要 `Authorization: Bearer <token>` 头。

## 配置

### 基础配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `AUTH_TOKEN` | (必填) | API 认证 Token |
| `PORT` | `8000` | 主服务监听端口 |
| `PUBLIC_BASE_URL` | （空） | 部署的公网地址（如 `https://ccm.example.com`） |
| `OPENAI_API_KEY` | (可选) | 语音功能所需 |
| `DATABASE_URL` | `sqlite+aiosqlite:///./claude_manager.db` | 数据库连接（支持 SQLite/PostgreSQL/MySQL） |
| `WORKSPACE_DIR` | `~/Projects` | 项目 clone 目标目录 |
| `MAX_CONCURRENT_INSTANCES` | `5` | 最大并发 worker 数 |
| `AUTO_START_DISPATCHER` | `true` | 启动时自动开始调度 |
| `TASK_TIMEOUT_SECONDS` | `1800` | 单个任务最长执行时间（秒） |
| `SERVICE_NAME` | (自动检测) | systemd 服务名，一键更新重启时使用 |

### PTY 模式

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `USE_PTY_MODE` | `true` | 启用 PTY 持久会话模式（false 则用 `claude -p` 一次性进程） |

### 瞬时过载重试

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `TRANSIENT_RETRY_ENABLED` | `true` | 启用瞬时 429/过载自动重试 |
| `TRANSIENT_RETRY_MAX` | `5` | 最大重试次数 |
| `TRANSIENT_RETRY_BASE_DELAY` | `10` | 基础退避延迟（秒） |
| `TRANSIENT_RETRY_MAX_DELAY` | `120` | 最大退避延迟（秒） |

### ask_user 拦截

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ASK_USER_ENABLED` | `true` | 启用 AskUserQuestion 拦截（false 时自动移除已注入的 hook） |
| `ASK_USER_TIMEOUT` | `1800` | 等待用户回答的超时时间（秒） |

### 号池配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `POOL_ENABLED` | `true` | 启用 Claude 账号池 |
| `POOL_CONFIG_PATH` | `~/.claude-pool/accounts.json` | 账号池配置文件路径 |
| `POOL_COOLDOWN_SECONDS` | `300` | 撞限账号的冷却时长（秒） |

号池默认启用。首次启动时，如果 `accounts.json` 不存在但 `~/.claude/.credentials.json` 有有效凭证，系统会自动将默认账号加入号池。

通过 Header 右侧的 **Pro** 按钮打开号池面板，可以：
- 查看每个账号的 5h/7d 额度利用率
- 点击 **+** 添加新账号（需要邮箱 + 接码 token）
- 刷新 OAuth Token / 重新登录
- 手动切换首选账号

多账号时，撞限或认证失败会自动换号，通过硬链接 session 目录实现无缝 `--resume`。

### Worker（分布式执行节点）

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `WORKER_SSH_KEY_PATH` | (必填) | SSH 私钥 `.pem` 文件路径 |
| `WORKER_SSH_USER` | `ubuntu` | Worker EC2 的 SSH 用户名 |
| `WORKER_ENABLED` | `true` | 是否启用 Worker 功能 |
| `WORKER_INSTANCE_TYPE` | (继承 Manager) | 覆盖 Worker 的 EC2 实例类型 |
| `WORKER_IMAGE_ID` | (继承 Manager) | 覆盖 Worker 的 AMI ID |

> 完整 Worker 配置和前置条件见 [Worker 部署指南](docs/worker-deployment-guide.md)

### Git 相关

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MERGE_PUSH_RETRIES` | `3` | rebase + push 最大重试次数 |
| `AUTO_PUSH_TO_ORIGIN` | `true` | 完成后是否自动 push |

### 数据库自动备份（可选）

集成 [auto-backup](https://github.com/zjw49246/auto-backup)，支持定期备份 SQLite 数据库到本机、AWS S3 或阿里云 OSS。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `BACKUP_ENABLED` | `false` | 设为 `true` 启用备份 |
| `BACKUP_TYPE` | `local` | 目标类型：`local` / `s3` / `oss` |
| `BACKUP_INTERVAL_SECONDS` | `3600` | 备份间隔（秒） |
| `BACKUP_MAX_COPIES` | `10` | 保留的最大备份份数 |
| `BACKUP_DESTINATION_PATH` | `` | （local）备份目标目录 |
| `BACKUP_S3_BUCKET` | `` | （s3）S3 桶名 |
| `BACKUP_S3_REGION` | `` | （s3）AWS 区域 |
| `BACKUP_S3_ACCESS_KEY` | `` | （s3）AWS Access Key ID |
| `BACKUP_S3_SECRET_KEY` | `` | （s3）AWS Secret Access Key |
| `BACKUP_OSS_ENDPOINT` | `` | （oss）OSS Endpoint |
| `BACKUP_OSS_BUCKET` | `` | （oss）OSS 桶名 |
| `BACKUP_OSS_ACCESS_KEY` | `` | （oss）阿里云 Access Key ID |
| `BACKUP_OSS_SECRET_KEY` | `` | （oss）阿里云 Access Key Secret |

**示例（本地备份）：**
```env
BACKUP_ENABLED=true
BACKUP_TYPE=local
BACKUP_DESTINATION_PATH=/mnt/backup/claude-manager
BACKUP_INTERVAL_SECONDS=3600
BACKUP_MAX_COPIES=10
```

## 同一台机器部署多个实例

可以在同一台机器上部署多个 Claude Code Manager 实例，分别服务不同用户/团队，推送到不同 GitHub 账号的仓库。

### 1. 准备独立的 `.env`

每个实例需要独立的端口、Token 和数据库：

```env
# 实例 A（端口 8000）
AUTH_TOKEN=token-for-user-a
PORT=8000
DATABASE_URL=sqlite+aiosqlite:///./claude_manager_a.db

# 实例 B（端口 8002）
AUTH_TOKEN=token-for-user-b
PORT=8002
DATABASE_URL=sqlite+aiosqlite:///./claude_manager_b.db
```

### 2. 配置 Git 凭据（关键）

每个实例可能需要推送到不同 GitHub 账号的仓库。在前端「全局 Git 设置」（Projects 页面齿轮按钮）中配置：

**推荐同时填写 SSH 和 HTTPS 凭据**，系统会根据 remote URL 协议自动选用：

| 字段 | 说明 |
|------|------|
| Author name / email | git commit 的作者信息 |
| SSH private key path | 如 `/Users/you/.ssh/id_ed25519_account_b` |
| HTTPS Username | GitHub 用户名 |
| HTTPS Token | GitHub Personal Access Token (PAT) |

**注意事项**：
- SSH key 在 GitHub 上是全局唯一的，一个公钥只能绑定一个账号
- macOS Keychain 的 `osxkeychain` 会缓存旧账号凭据，系统已自动绕过（`GIT_CONFIG_GLOBAL=/dev/null`）
- HTTPS Token 必须由目标仓库的**所有者账号**生成，而非本机账号

### 3. 为不同 GitHub 账号生成 SSH Key

```bash
# 为账号 A 生成
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_account_a -C "account-a@github" -N ""

# 为账号 B 生成
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_account_b -C "account-b@github" -N ""
```

在 `~/.ssh/config` 中配置 Host 别名：

```
Host github-account-a
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_account_a
  IdentitiesOnly yes

Host github-account-b
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_account_b
  IdentitiesOnly yes
```

将公钥分别添加到对应的 GitHub 账号。

### 4. Cloudflare Tunnel 路由

在 `~/.cloudflared/config.yml` 中为每个实例配置不同的子域名：

```yaml
ingress:
  - hostname: user-a.yourdomain.com
    service: http://localhost:8000
  - hostname: user-b.yourdomain.com
    service: http://localhost:8002
  - service: http_status:404
```

添加 DNS 路由：
```bash
cloudflared tunnel route dns <tunnel-name> user-a.yourdomain.com
cloudflared tunnel route dns <tunnel-name> user-b.yourdomain.com
```

### 5. 启动

```bash
# 构建前端（共用）
cd frontend && npm run build && cd ..

# 启动实例 A
PORT=8000 uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 &

# 启动实例 B（使用实例 B 的 .env）
PORT=8002 uv run uvicorn backend.main:app --host 0.0.0.0 --port 8002 &

# 启动 Cloudflare Tunnel
cloudflared tunnel run <tunnel-name>
```

## 架构要点

- **GlobalDispatcher**：只负责分配任务、启动 Claude Code、判断成败。所有 git 操作全由 Claude Code 自主完成
- **Claude Code 集成**：默认 PTY 模式（常驻交互会话，多轮免冷启动）；可切换为 `claude -p` 一次性进程模式（`USE_PTY_MODE=false`）
- **进程超时保护**：任务执行超过 `TASK_TIMEOUT_SECONDS`（默认 30 分钟）后自动 kill，防止进程挂死
- **多轮对话**：session_id 绑定在 Task 上，follow-up 时使用 `--resume <session_id>` 续接会话
- **子 Agent 系统**：统一存 `sub_agent_sessions` 表，`agent_type` 区分类别（monitor / native-agent / native-monitor）。CCM 自有子 agent 拥有独立 MCP server，通过 HTTP API 与系统通信
- **瞬时过载重试**：Anthropic 基础设施侧 429/overloaded 与账号额度用尽严格区分，前者退避重试同一账号，后者走号池轮换
- **进程管理**：`asyncio.create_subprocess_exec` 启动，必须 unset `CLAUDECODE` 环境变量避免嵌套检测
- **停止机制**：SIGTERM → 等待 10s → SIGKILL
