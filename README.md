# Claude Code Manager

Web 端调度和管理多个 Claude Code 实例并行工作。灵感来自胡渊鸣的文章「我给 10 个 Claude Code 打工」。

> **⚠️ 重要安全提示：** 本项目会以 `--dangerously-skip-permissions` 模式运行 Claude Code，这意味着 Claude Code 将拥有**不受限制的文件读写、命令执行和网络访问权限**，并且会自动执行 `git push` 等操作。**强烈建议在一台单独的、没有重要文件的电脑或虚拟机上部署**，避免对你的个人数据或工作环境造成意外影响。

## 功能

- **全局调度器** — 启动时自动创建 worker、自动分配任务，无需手动操作
- **Claude Code 完全自主** — Claude Code 自主完成 worktree 创建、commit、fetch、merge、push、冲突解决和清理，Dispatcher 只负责分配任务和判断成败
- **9 步任务生命周期** — 领取 → 创建工作区 → 实现 → 提交 → merge + 测试 → 合并到 main → 标记完成 → 清理 → 经验沉淀
- **项目管理** — 支持 clone 已有仓库（有 remote）和本地 git init（无 remote），创建任务时可直接新建项目
- **任务队列** — 创建任务，按优先级自动调度（数字越小优先级越高）
- **多实例并行** — 同时运行多个 Claude Code 实例，各自处理不同任务
- **Git Worktree** — 每个实例在独立的 worktree 中工作，互不干扰
- **多轮对话** — 任务完成后可通过 Chat 界面继续追问，自动 `--resume` 同一 session
- **实时日志** — WebSocket 推送，实时查看每个实例的执行过程
- **Plan Mode** — 敏感任务先生成计划，人工审批后再执行
- **语音输入** — 通过 OpenAI Whisper API 语音转文字创建任务
- **PWA** — 手机浏览器 Add to Home Screen，原生 App 体验
- **Android App** — 通过 Capacitor 打包原生 APK，App 内可配置远程服务器地址
- **PR Monitor** — GitHub PR 自动审核，Webhook 接收 PR 事件后创建审核 Task，Claude 审核代码后自动 approve/merge 或 request-changes。支持白名单作者、auto-merge 开关、自定义审核模型
- **Monitor Sub-Agent** — Agent 可自主创建持久监控子 Agent，子 Agent 拥有独立 MCP 工具（report_status / mark_complete / get_context），自主决定检查频率并通过 API 向系统汇报
- **权限透传（PTY 模式）** — CC 请求工具权限时，聊天里出现 🔐 卡片（工具名/描述/输入预览），点 允许/拒绝 实时回包给 CC；120 秒内未回应则 CC 侧默认拒绝，过期卡片标记为只读
- **原生子 Agent 镜像（PTY 模式）** — 模型用内置 Agent/Task/Monitor 工具开的子 agent 会被 PTY 层观测并自动注册进子 agent 体系（类别 native-agent / native-monitor），任务卡徽章、Sub-Agents 面板、WebSocket 实时事件与 $monitor 同一套展示；后台子 agent 唤醒模型产生的自主回复实时进入聊天流，不再错位到下一条消息
- **Claude Pool** — 多账号池自动切换：撞限/认证失败时自动换号并硬链接 session 实现无缝 `--resume`；Header 的 "Pro" 徽标可打开额度抽屉，查看每个账号 5h/7d 窗口的利用率（绿/黄/红进度条）、冷却状态，并可手动解除冷却
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
| Backend | Python 3.11+, FastAPI, SQLAlchemy (async), SQLite |
| Frontend | React, Vite, Tailwind CSS v4, TypeScript |
| 实时通信 | WebSocket |
| 语音 | OpenAI Whisper API |
| 远程 | Cloudflare Tunnel |

## 项目结构

```
claude-manager/
├── backend/
│   ├── api/            # REST + WebSocket 路由
│   ├── middleware/      # Token 认证中间件
│   ├── models/          # SQLAlchemy ORM (task, instance, project, log_entry, worktree)
│   ├── schemas/         # Pydantic 请求/响应模型
│   ├── mcp/             # MCP Servers (主 agent + 子 agent)
│   ├── services/        # 核心业务逻辑
│   │   ├── dispatcher.py        # 全局调度器 (9 步任务生命周期 + 子 agent 管理)
│   │   ├── instance_manager.py  # Claude Code 子进程管理
│   │   ├── mcp_config.py        # MCP config 动态生成 (主 agent + 子 agent)
│   │   ├── ralph_loop.py        # 自动取活循环 (legacy)
│   │   ├── stream_parser.py     # NDJSON stream-json 解析
│   │   ├── task_queue.py        # 优先级任务队列
│   │   ├── worktree_manager.py  # Git worktree 管理 + rebase + push
│   │   ├── ws_broadcaster.py    # WebSocket 广播
│   │   └── whisper_client.py    # 语音转文字
│   └── main.py          # FastAPI 入口
├── frontend/
│   ├── public/          # PWA manifest, service worker, icons
│   └── src/
│       ├── api/         # HTTP + WebSocket 客户端
│       ├── components/  # Chat, Instances, Tasks, PlanReview, Voice
│       ├── config/      # server (远程服务器配置), theme (主题切换)
│       ├── hooks/       # useWebSocket
│       └── pages/       # Dashboard, TasksPage, LoginPage, ServerConfigPage
├── scripts/
│   ├── dev.sh           # 一键启动开发环境
│   └── tunnel.sh        # 隧道脚本
├── pyproject.toml
└── .env
```

## 快速开始

### 前置条件

- macOS（Claude Code 部署在本机）
- Python 3.11+
- Node.js 18+
- [uv](https://docs.astral.sh/uv/) — Python 包管理器
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装

### 安装

```bash
git clone https://github.com/zjw49246/Claude-Code-Manager.git && cd Claude-Code-Manager

# 后端依赖（使用 uv）
uv sync

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

## 更新已部署的实例

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
> 自动重装（editable/本地开发安装会自动跳过）。跳过这一步，PTY 模式会一直跑在
> 安装当天的旧框架上。

## 使用流程

### 基本流程

1. **Tasks** — 创建任务，选择已有项目或新建项目（可选填 remote URL），填写 Prompt 和优先级。可勾选 **Monitor** skill 赋予 Agent 后台监控能力
2. **Dispatcher** 自动分配任务到空闲 worker → Claude Code 自主完成所有工作（含 worktree 创建和清理） → 取下一个
3. 点击任务的 **Chat** 按钮，可以对已完成的任务继续追问
4. 启用 Monitor 的任务中，Agent 可自主创建持久监控子 Agent，子 Agent 独立运行并自主汇报。Task 列表显示活跃子 Agent 数量，Chat 界面通过 MonitorPanel 展示监控状态

### Plan Mode

创建任务时选择 Mode = `plan`：
1. Claude Code 先以只读模式分析代码，生成执行计划
2. 任务进入 `plan_review` 状态，在 Tasks 页面显示计划内容
3. 点击 Approve 批准后，任务重新入队执行

### 语音输入

任务创建表单的标题和描述字段旁有 🎙 按钮，点击后录音，松开自动转文字填入。

### PR Monitor 前置条件

PR Monitor 的审核流程会在后端 shell out 调用 `gh pr view` / `gh pr review` / `gh pr merge`，使用前需满足：

1. **gh CLI 已认证**：运行后端的系统用户必须先执行 `gh auth login` 完成 GitHub 认证
2. **账号权限**：该 GitHub 账号需要对被监控仓库有 push / review 权限（auto-merge 还需要 merge 权限）
3. **PUBLIC_BASE_URL**：在 `.env` 中设置 `PUBLIC_BASE_URL`（如 `https://ccm.example.com`），PR Monitor 页面才能显示正确的 Webhook Payload URL；未设置时前端回退为当前页面的 origin

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
| Instances | `GET/POST /api/instances` | 实例列表/创建 |
| | `DELETE /api/instances/{id}` | 删除实例 |
| | `POST /api/instances/{id}/stop` | 停止实例 |
| | `POST /api/instances/{id}/run` | 手动执行 |
| | `GET /api/instances/{id}/logs` | 获取日志 |
| Monitor | `POST /api/tasks/{id}/monitor-sessions` | 创建 monitor 子 session |
| | `GET /api/tasks/{id}/monitor-sessions` | 列出 monitor sessions |
| | `GET /api/tasks/{id}/monitor-sessions/{sid}` | monitor session 详情 |
| | `DELETE /api/tasks/{id}/monitor-sessions/{sid}` | 停止 monitor session |
| | `GET /api/tasks/{id}/monitor-sessions/{sid}/checks` | monitor 检查历史 |
| | `POST /api/tasks/{id}/monitor-sessions/{sid}/checks` | 子 agent 报告状态 |
| | `POST /api/tasks/{id}/monitor-sessions/{sid}/complete` | 子 agent 标记完成 |
| Sub-Agents | `GET /api/tasks/{id}/sub-agents/summary` | 子 agent 按类型汇总 |
| Dispatcher | `GET /api/dispatcher/status` | 调度器状态 |
| | `POST /api/dispatcher/start` | 启动调度器 |
| | `POST /api/dispatcher/stop` | 停止调度器 |
| Pool | `GET /api/pool/status` | 账号池状态（可用/冷却/禁用） |
| | `GET /api/pool/usage` | 账号池状态 + 每账号额度利用率（5h/7d 窗口） |
| | `POST /api/pool/reload` | 重新加载账号配置 |
| | `POST /api/pool/accounts/{id}/clear-cooldown` | 手动清除账号冷却 |
| Voice | `POST /api/voice/transcribe` | 语音转文字 |
| WebSocket | `ws://host/ws` | 实时推送（subscribe channel） |
| Auth | `POST /api/auth/login` | Token 登录 |
| System | `GET /api/system/health` | 健康检查 |
| | `GET /api/system/stats` | 统计信息 |

所有 API（除 health 和 login）需要 `Authorization: Bearer <token>` 头。

## 配置

### 基础配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `AUTH_TOKEN` | (必填) | API 认证 Token |
| `PORT` | `8000` | 主服务监听端口 |
| `PUBLIC_BASE_URL` | （空） | 部署的公网地址（如 `https://ccm.example.com`），用于 PR Monitor 页面展示 Webhook URL |
| `OPENAI_API_KEY` | (可选) | 语音功能所需 |
| `DATABASE_URL` | `sqlite+aiosqlite:///./claude_manager.db` | 数据库连接 |
| `WORKSPACE_DIR` | `~/Projects` | 项目 clone 目标目录 |
| `MAX_CONCURRENT_INSTANCES` | `5` | 最大并发 worker 数 |
| `AUTO_START_DISPATCHER` | `true` | 启动时自动开始调度 |
| `MERGE_PUSH_RETRIES` | `3` | rebase + push 最大重试次数 |
| `AUTO_PUSH_TO_ORIGIN` | `true` | 完成后是否自动 push |
| `TASK_TIMEOUT_SECONDS` | `1800` | 单个任务最长执行时间（秒），超时后 kill 进程 |
| `POOL_ENABLED` | `false` | 启用 Claude 账号池自动切换 |
| `POOL_CONFIG_PATH` | `~/.claude-pool/accounts.json` | 账号池配置文件路径 |
| `POOL_COOLDOWN_SECONDS` | `300` | 撞限账号的冷却时长（秒） |

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
| `BACKUP_OSS_ENDPOINT` | `` | （oss）OSS Endpoint，如 `oss-cn-hangzhou.aliyuncs.com` |
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

- **GlobalDispatcher**：只负责分配任务、启动 Claude Code、判断成败。所有 git 操作（worktree 创建/清理、commit/merge/push/冲突解决）全由 Claude Code 自主完成
- **Claude Code 集成**：通过 `claude -p [prompt] --dangerously-skip-permissions --output-format stream-json --verbose` 非交互模式调用，Claude Code 读项目 CLAUDE.md 决定 git 操作
- **进程超时保护**：任务执行超过 `TASK_TIMEOUT_SECONDS`（默认 30 分钟）后自动 kill，防止进程挂死
- **多轮对话**：session_id 绑定在 Task 上，follow-up 时使用 `--resume <session_id>` 续接会话
- **子 Agent 系统**：Monitor 是第一个子 Agent 类型，持久 Claude 子进程拥有独立 MCP server，通过 HTTP API 与系统通信，架构为后续子 Agent 类型预留
- **进程管理**：`asyncio.create_subprocess_exec` 启动，必须 unset `CLAUDECODE` 环境变量避免嵌套检测
- **停止机制**：SIGTERM → 等待 10s → SIGKILL
