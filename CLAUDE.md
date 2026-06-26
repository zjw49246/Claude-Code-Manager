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
│   │   ├── monitor.py           # Monitor Session CRUD + 子 agent checks/complete endpoints
│   │   ├── pool.py              # Claude 账号池 status/usage/reload/clear-cooldown
│   │   ├── pr_monitor.py       # PR Monitor CRUD + GitHub webhook endpoint
│   │   ├── workers.py           # 分布式 Worker CRUD + stop/start/destroy/retry
│   │   ├── sub_agents.py        # 通用子 Agent summary API (GET /tasks/{id}/sub-agents/summary)
│   │   ├── ws.py                # WebSocket 端点
│   │   ├── voice.py             # Whisper 语音转文字
│   │   ├── auth.py              # Token 登录
│   │   └── system.py            # 健康检查 + 统计
│   ├── middleware/auth.py       # Bearer token 认证中间件
│   ├── models/                  # SQLAlchemy ORM 模型
│   │   ├── task.py              # Task (含 session_id, last_cwd, project_id, enabled_skills)
│   │   ├── instance.py          # Claude Code 实例
│   │   ├── project.py           # Project (name, git_url, local_path)
│   │   ├── sub_agent.py         # SubAgentSession + SubAgentReport (通用子 agent 表, agent_type 分类)
│   │   ├── monitor_session.py   # 兼容 shim: MonitorSession/MonitorCheck = sub_agent 别名
│   │   ├── pr_monitor.py       # MonitoredRepo + PRReview (PR 自动审核)
│   │   ├── worker.py            # 分布式 Worker（EC2 实例 + bootstrap 状态机）
│   │   ├── log_entry.py         # 执行日志
│   │   └── worktree.py          # Git worktree 跟踪
│   ├── schemas/                 # Pydantic 请求/响应模型
│   ├── mcp/                     # MCP Server (给 Claude 注入工具能力)
│   │   ├── __init__.py
│   │   ├── ccm_skills_server.py # FastMCP server: create_monitor / check_monitors / stop_monitor
│   │   └── ccm_monitor_agent_server.py # 子 Agent MCP server: report_status / mark_complete / get_context
│   └── services/                # 核心业务逻辑
│       ├── instance_manager.py  # 子进程生命周期 (launch/stop/consume output, MCP config 注入)
│       ├── dispatcher.py        # 全局调度器 (9 步任务生命周期, 含 goal 模式 + monitor 子 agent)
│       ├── goal_evaluator.py    # Goal 条件评估器 (claude -p 子进程)
│       ├── mcp_config.py        # MCP config 动态生成 (主 agent + 子 agent)
│       ├── claude_pool.py       # Claude 账号池 (限速检测/自动切换/session 迁移/额度查询)
│       ├── ralph_loop.py        # 自动取活循环 (legacy, 保留兼容)
│       ├── stream_parser.py     # NDJSON stream-json 解析器
│       ├── task_queue.py        # 优先级队列 (asc = 优先级越高)
│       ├── worktree_manager.py  # Git worktree 创建/合并/删除 + rebase+push
│       ├── pr_review_service.py  # PR 审核 prompt 构建 + task 创建 + 状态回查
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
│       │   ├── Chat/ChatView.tsx              # 多轮对话 UI (基于 task, 含 monitor 消息渲染)
│       │   ├── Chat/SubSessionIndicator.tsx   # 子 session 计数指示器
│       │   ├── Chat/MonitorPanel.tsx          # Monitor 面板 (活跃 monitor 列表 + 历史 checks)
│       │   ├── Instances/              # InstanceGrid, InstanceLog
│       │   ├── Tasks/                  # TaskForm (含 Monitor skill 勾选), TaskList
│       │   ├── Layout/PoolDrawer.tsx   # Pool 额度抽屉 (Header "Pro" 徽标 + 账号额度进度条)
│       │   ├── PlanReview/PlanPanel.tsx # Plan 审批
│       │   └── Voice/VoiceButton.tsx   # MediaRecorder → Whisper
│       └── hooks/useWebSocket.ts
├── scripts/
│   ├── dev.sh                   # 一键启动开发环境
│   └── tunnel.sh                # ngrok 隧道
├── .env                         # AUTH_TOKEN, OPENAI_API_KEY, DATABASE_URL
└── pyproject.toml
```

## 依赖链（重要）

- 本仓库依赖 **claude-pty**（Claude-Code-PTY 仓库），git rev **pin 在 uv.lock**，不会自动浮动
- PTY 框架更新后必须显式级联：`uv lock --upgrade-package claude-pty && uv sync`，提交 uv.lock
- 生产（8002, ccm-b.service）要使依赖生效：`systemctl --user restart ccm-b`（重启时机需用户确认；启动属主与错库教训见 PROGRESS）
- 领取任务时若涉及 PTY 接口/行为变化，先对比 uv.lock 中 pin 的 rev 与 Claude-Code-PTY main HEAD，落后则先 bump

## 关键约定

- **优先级**: 数字越小优先级越高 (P0 > P1 > P2)，排序用 `.asc()`
- **Session 绑定**: `session_id` 和 `last_cwd` 在 **Task** 上（不是 Instance），因为 instance 是轮换执行不同 task 的 worker
- **Claude Code 调用**: `claude -p [prompt] --dangerously-skip-permissions --output-format stream-json --verbose`
- **Resume**: `claude -p [follow-up] --resume [session_id]` — 必须使用和原始 session 相同的 cwd
- **Model 配置**: 默认 `claude-opus-4-6`，支持全称模型 ID（`claude-fable-5`, `claude-opus-4-6`, `claude-opus-4-7`, `claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5`）。`[1m]` 后缀开启 1M context（计费翻倍）
- **Effort Level**: 默认 `medium`，支持 `low/medium/high/xhigh/max`。优先级链：Task.effort_level → Instance.effort_level → settings.default_effort。通过 CLI `--effort` 参数传递
- **Extended Thinking 预算**: Instance 上的 `thinking_budget` 字段 → 子进程 `MAX_THINKING_TOKENS` env var；NULL = 用 CLI 默认
- **Thinking 解析**: stream_parser 兼容多种字段名（`thinking` / `text` / 嵌套 content blocks）；加密 thinking 显示为 `[encrypted thinking ...]` 标记
- **Workflows 开关**: Task.enable_workflows（默认 False）→ CLI `--disallowedTools Workflow`；关闭时 Workflow 工具不可用，节省 token
- **Skills 系统**: Task.enabled_skills（JSON dict，如 `{"monitor": true}`）控制注入哪些 MCP 工具。创建 task 时勾选 Skills，dispatcher 根据 enabled_skills 动态生成 MCP config 并通过 `--mcp-config` 注入 Claude CLI
- **Monitor Skill**: 后台监控子 session，主 Agent 通过 MCP 工具（create_monitor / check_monitors / stop_monitor）创建和管理。子 session 是**持久 Claude 子 Agent 进程**，拥有自己的 MCP server（`ccm_monitor_agent_server.py`），通过 report_status / mark_complete / get_context 工具自主与系统通信。每 task 最多 5 个并发 monitor
- **子 Agent 架构**: 子 agent 是分类别的一等概念，统一存 `sub_agent_sessions`（`agent_type` 区分类别，`source` 区分启动方）。Monitor（agent_type=monitor, source=ccm）是第一个类别；PTY 模式下模型原生子 agent 自动镜像进来：`native-agent`（Agent/Task 工具）、`native-monitor`（内置 Monitor 工具），由 claude_pty 从 JSONL + subagents/ 目录观测，经 `_upsert_native_sub_agent` 入库并广播 `sub_agent_*` 事件。CCM 自有子 agent 生命周期：注册 → 启动持久进程 → 自主运行（MCP tools → HTTP API → DB + WebSocket）→ 完成/停止 → 清理，进程最长 4 小时超时兜底
- **PTY 权限透传**: BridgeHub 的 permission handler 由 instance_manager 注册（`_on_pty_permission_request`，bridge HTTP 线程 → `_loop` 调度）；卡片事件 `permission_request`/`permission_resolved` 走 task WS 频道，回包端点 `POST /api/tasks/{id}/permissions/{request_id}`；CC 侧 channel server 最多阻塞 120s，超时默认 deny
- **PTY turn 对齐**: claude_pty 的 send_prompt 以"本次 prompt 的 user 回显"为 turn 起点，之前的积压事件标 `orphan` 上报；turn 间由 Session 空闲 watcher 持续消费 harness 自主 turn（带 `autonomous` 标记）。修复 task 87 的回复错位事故（详见 PROGRESS.md）
- **ask_user（拦截内置 AskUserQuestion）**: 内置 `AskUserQuestion` 在 headless/PTY 下无人应答会卡住。CCM 在 `instance_manager.launch()`（`-p` 与 PTY 的**统一入口**，分流之前）把一个 PreToolUse hook（matcher=`AskUserQuestion`）幂等合并进**本次使用的 `{config_dir}/settings.json`**（`ask_user_settings.ensure_ask_user_hook`，`config_dir` 空则落 `~/.claude`）——Claude 在 `--dangerously-skip-permissions` 下自动加载该文件、无审批弹窗。**为何走 settings 文件而非 CLI flag**：claude_pty 命令构建是固定字段不接受 `--settings`，且本仓库对 PTY 仓库只有 READ 权限无法 bump 依赖；两条链路都用 `CLAUDE_CONFIG_DIR`，故 settings 文件是唯一两路统吃的注入后门。hook 脚本 `backend/hooks/ask_user_hook.py`（纯 stdlib urllib、**fail-open**）阻塞式 `POST /api/ask-user/wait`：后端按 `session_id` 找 task → 登记 `asyncio.Future`（`ask_user_registry`）→ 广播 `ask_user_question` 卡片 → `await` 直到前端 `POST /api/tasks/{id}/ask-user/{request_id}` resolve 或 `ask_user_timeout`(默认 1800s) 超时。**答案回流机制**：hook 拿到答案后以 `permissionDecision=deny` + `permissionDecisionReason=<格式化答案>` 输出——deny 的 reason 会作为 `tool_result`(is_error) 原样喂回模型，模型据此当作"用户的回答"继续（已实测）。任何 CCM 不可达 / 非托管 session / 超时一律 fail-open 放行原生工具，绝不打断会话。整套照搬 PTY 权限透传范式（卡片 live-only + `system_event` 落库 + `GET /api/tasks/{id}/ask-user/pending` 重连回填）。**跨页面全局通知**：内联卡片只走 `task:{id}` 频道，用户不在对应 task 页面时提问会「消失」。故 `/ask-user/wait` 同时 ① 把 task 标 `has_unread=True`（任务列表亮未读点）② 向全局 `tasks` 频道广播 `ask_user_pending`/`ask_user_resolved`（带 `task_id`/`request_id`/`summary`）。前端 `AskUserNotifications`（App 顶层常驻）订阅 `tasks` 频道 + 刷新/重连时拉**全局** `GET /api/ask-user/pending`（`ask_user_registry.list_all()`），右下角弹可点击通知，点击跳 `#/tasks/chat/{id}`，答完/超时由 `ask_user_resolved` 自动消除。开关 `ask_user_enabled`（默认 True，关闭时 `ensure_*` 自动移除已注入的 hook 项）
- **MCP 架构**: 主 Agent 用 `ccm_skills_server.py`，子 Agent 用 `ccm_monitor_agent_server.py`，均为 FastMCP server，通过 stdio 与 Claude CLI 通信，通过 HTTP 调用 CCM 后端 API。配置文件动态生成到 `/tmp/ccm_mcp_{task_id}.json`（主 Agent）和 `/tmp/ccm_monitor_agent_{session_id}.json`（子 Agent），结束后自动清理
- **环境变量清理**: 生成子进程前必须 unset `CLAUDECODE` / `CLAUDE_CODE`，避免嵌套检测
- **停止顺序**: SIGTERM → 等 10s → SIGKILL
- **Per-task 消息队列**: chat/monitor 的后续消息走 dispatcher 的 per-task 队列（`_task_queues`），由**单个** consumer（`_task_queue_consumer`）串行 `--resume`，保证同一 session 不被并发 resume。`_ensure_queue_worker` 的 ">stuck 阈值 cancel+respawn" 看门狗（`QUEUE_STUCK_THRESHOLD`）只兜底真正卡死的 consumer：consumer 全程跑一个 `_queue_heartbeat`，长 turn（`_wait_process` 等十几分钟）和 idle 等待都持续刷新 activity，故不会被误判。consumer 退出时**只在 `_task_queue_workers[task_id]` 仍指向自己时才 pop**，否则会抹掉 respawn 出来的新 consumer 登记、让下次 enqueue 再起一个 → 双 consumer 并发 resume（task #728 事故，详见 PROGRESS.md）
- **Claude Pool**: 多账号自动切换（`backend/services/claude_pool.py`，`POOL_ENABLED=true` + `~/.claude-pool/accounts.json` 启用）。进程失败后用窄正则检测限速/认证失败 → 标记冷却（限速 5min，认证失败永久直到手动清除）→ `select` 换号（`validate=True` 会用 `claude -p` 探测，必须经 `select_async` 走线程避免阻塞事件循环）→ `migrate_session` 硬链接 session JSONL 到新账号 config_dir 实现 `--resume` 续上下文。**注意 `migrate_session` 参数是 keyword-only，必须用关键字调用**；session 实际所在目录用 `locate_session_config_dir` 查找，不要假设在 env `CLAUDE_CONFIG_DIR` 下。**找 session JSONL 一律用 `projects/*/{sid}.jsonl` 通配，绝不按 DB 里的 `last_cwd` 字面编码拼路径**——符号链接会让落盘编码（CLI 取 `os.getcwd()` realpath）与存库路径不一致（`_find_session_jsonl`/`_clone_session`，task #725）。chat resume 前 dispatcher 先探测 session 在不在磁盘，不在就走恢复（clone→摘要），让第一条消息即可自救而非被牺牲。**resume 选号统一走 `GlobalDispatcher._resolve_resume_config_dir(sid)`**：有健康号就迁移 session 进去；**号池耗尽（select 返回 None）时回退到 `locate_session_config_dir(sid)`——session 真正所在的号，而不是放任 `config_dir=None` 让子进程继承 systemd env 里写死的 `CLAUDE_CONFIG_DIR`**（那个号没存过该 session → `claude --resume` 秒挂 `No conversation found`、丢 session；task #734/#740 事故）。限速是可恢复态，绝不能升级成丢 session 的硬失败。**主动换号（`_try_proactive_pool_switch`）只在 `rate_limit_event` 真·临界时才触发**：CLI 几乎每个 turn 都吐一条 `rate_limit_event` 状态 ping，`status="allowed"` 是健康、`allowed_warning` 才是接近阈值。早期代码对**任意** `rate_limit_event` 都 `mark_rate_limited`（5min 冷却）+ 换号——连「7 天额度用了 37%」这种 ping 都把健康号冷却 5 分钟，3 个号几轮就全冷却→`select` 返回 None→号池假性耗尽（task #734/#740 真凶）。现由 `rate_limit_event_is_actionable(rate_limit_info)` 把关：`allowed` 永不触发；`allowed_warning` 仅当 `rateLimitType=five_hour` 且利用率 ≥0.9 才触发（`seven_day` 警告永不主动换——5min 冷却改变不了 7 天窗口，只会空转）；其余非 allowed 状态（rejected/blocked）才触发。**实际失败时的反应式轮换（`is_rate_limited` 命中真·限速横幅）是另一条路、不受影响**。额度查询走 OAuth usage API（`fetch_usage`，缓存 60s），前端 Header 的 "Pro" 徽标 → PoolDrawer 抽屉展示 5h/7d 利用率
- **瞬时 429/过载自动等待重试**: Anthropic **基础设施侧**的临时限流/过载（CLI 文案 `Server is temporarily limiting requests (not your usage limit)` / overloaded，是 Anthropic 官方报错而非 CCM 内部），换号无用 → 退避后用**同一账号** `--resume` 重试。检测器 `is_transient_overload`（`claude_pool.py`，先排除 `is_rate_limited`/`is_auth_failure` 保证与「额度用尽要换号」互斥），退避 `transient_retry_delay`（指数+jitter）。开关/参数：`transient_retry_enabled`(默认 True)、`transient_retry_max`(5)、`transient_retry_base_delay`(10s)、`transient_retry_max_delay`(120s)。**关键陷阱**：PTY 模式下 api_error 中止 turn 但**持久 session 仍存活 → exit_code 报 0**，单看退出码会误判成功；故 instance_manager 在 `_process_event` 里对带 `is_error` 且命中检测器的事件打 **turn-scoped 标记** `_transient_seen`（`launch()` 重置、`transient_error_seen()` 读取），dispatcher 据此即便 exit_code=0 也触发重试。**标记必须只认当前前台 turn 的活事件**：`_process_event` 打标前要排除 `orphan`（resume 时 PTY 重读 JSONL 回放的上一 turn 旧 api_error）和 `autonomous`（后台子 agent turn 的报错）事件——否则成功 resume 的 turn 会被旧错误重新置标，`still_transient` 永真→烧光重试预算→任务被误判 failed（recover-then-failed bug，task #729）。Autonomous 任务走 dispatcher `_run_transient_retry`（递归自驱）；chat 子进程模式走 instance_manager `_try_chat_transient_retry`（`_consume_output.finally` 自驱循环），PTY 模式由 `_process_queued_message` 在 `_wait_process` 后用 while 循环驱动（heartbeat 覆盖、不会被看门狗误杀）。重试与号池轮换单向衔接（transient 用尽→轮换；轮换不回切 transient），无 ping-pong
- **备份服务**: `BackupService`（`backend/services/backup_service.py`）封装 auto-backup SDK，在 lifespan 中以后台线程（APScheduler）运行，支持 local / s3 / oss；`BACKUP_ENABLED=false` 时完全不加载
- **PR Monitor**: GitHub PR 自动审核功能。GitHub Webhook 推送 PR 事件 → 创建 CCM task 让 Claude 审核 → 审核通过可自动 merge。数据模型：`MonitoredRepo`（监控仓库配置）+ `PRReview`（审核记录）。Webhook 端点 `/api/github/webhook`（公开，HMAC-SHA256 验签）。前端独立页面 `PRMonitorPage`。Webhook URL: `https://youchengsong.claude-code-manager.com/api/github/webhook`
- **WebSocket channels**: `instance:{id}`, `task:{id}`, `tasks`, `system`, `pr-monitor`
- **认证**: 除 `/api/system/health`、`/api/auth/login`、`/api/github/webhook` 外，所有 API 需要 `Authorization: Bearer <token>`
- **前端 type 导入**: 用 `import type { X }` 导入类型，`import { api }` 导入值（Vite 会去除 type-only exports）
- **Tailwind v4**: 用 `@import "tailwindcss"` + `@tailwindcss/vite` 插件，无 tailwind.config
- **主题**: 深色/浅色切换通过覆盖 `--color-gray-*` CSS 变量实现灰度反转，内容文字用 `text-foreground`（随主题变化），按钮文字保持 `text-white`
- **Android App**: Capacitor 打包，API/WS 地址通过 `config/server.ts` 动态获取，LoginPage 可展开配置 Server URL
- **Goal 模式**: `mode="goal"` 任务使用自然语言完成条件（`goal_condition`），每 turn 后由独立评估器（默认 Haiku）判断是否达成。使用 `--resume` 保持同一 session 的连续上下文。评估器通过 `claude -p` 子进程调用，不需要额外 API key
- **Goal 评估器**: `GoalEvaluator`（`backend/services/goal_evaluator.py`）读取对话日志摘要，发给轻量模型判断条件是否满足。默认模型 `claude-haiku-4-5`，可通过 `goal_evaluator_model` 覆盖
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

# 刷新 claude-pty 到 PTY 仓库 main 最新（git 依赖是安装时快照，
# git pull 不会更新它——部署同步时必须跑；editable 安装自动跳过）
./scripts/refresh_pty.sh

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

# 生产后台部署 (systemd, SSH 断开后持续运行)
# 两个 systemd 服务:
#   ccm-backend  — uvicorn 后端
#   ccm-tunnel   — cloudflare tunnel
# 服务文件位于 /etc/systemd/system/ccm-backend.service 和 ccm-tunnel.service
# 常用命令:
sudo systemctl restart ccm-backend   # 重启后端
sudo systemctl restart ccm-tunnel    # 重启 tunnel
sudo systemctl stop ccm-backend      # 停止后端
sudo journalctl -u ccm-backend -f    # 查看后端日志
sudo journalctl -u ccm-tunnel -f     # 查看 tunnel 日志
# 开机自启已通过 systemctl enable 配置
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

## 分布式 Worker（Phase 1，设计见 docs/plans/elastic-worker-design.md）

- **形态**：Worker = 一台跑完整 CCM 的 EC2，Manager 全生命周期管理（创建/收养/关机/开机/销毁），前端 Workers 一级页面操作
- **配置自举**：新 EC2 的机型/AMI/子网/密钥从 Manager 自身实例元数据继承（IMDSv2 + boto3，凭证走 IAM instance profile）；通信全走 VPC 内网 private IP
- **部署 = rsync**（不走 git clone）：Manager 本地仓库 → worker `/home/ubuntu/ccm`，`--filter ':- .gitignore'` + 排除 `.git`（worktree 的 .git 是悬空指针）；版本锁定靠 `.deploy_commit` 文件（`git_info.git_head_commit` 的回退路径），health 端点带 commit 供校验
- **auth 探针**：`/api/system/health` 在 PUBLIC_PATHS 不校验 token，bootstrap 健康检查必须再打需认证端点（`/api/system/stats`）验证 worker 的 AUTH_TOKEN 真可用
- **error 语义**：`bootstrap_step` 非 None 的 error 是 bootstrap 失败（不自动恢复，UI 给 retry）；为 None 的是健康降级（健康检查恢复后自动回 ready）
- **开关**：`WORKER_ENABLED=true` + `WORKER_SSH_KEY_PATH`（默认关，不装 boto3 也能跑）
- Phase 2（任务转发 + WorkerRelay）、Phase 3（TaskMigrator 实时切换执行位置）见设计文档 §20

### Phase 2（任务转发 + 中继，已实测）

- **执行链路**：Task.worker_id 非空 → Dispatcher 双路径转发（同 ID 在 worker 创建，worker 自 clone 项目）→ WorkerRelay 每 worker 一条 WS 把 chat/status/plan/loop/goal/monitor 事件双写 Manager DB + 镜像广播 → 前端零改动
- **关键陷阱**（实现处有注释）：worker 广播前 pop session_id（靠 chat 响应同步）；广播无 raw_json；monitor 事件用 "event" 键；worker MonitorSession.id 用 remote_id 列翻译；backfill 用非 user_message 条数对比
- **Phase 2 限制**：纯本地项目不能远程执行（Phase 3 播种）；worker task 不支持 secrets 引用；cost 只有 context_usage（token 级）
- `/ws` 已加 token 认证（header 或 ?token=，前端 WsClient 自动带）

### Phase 3（TaskMigrator，已实测双向闭环）

- **执行位置实时切换**：PUT /api/tasks/{id} 带 worker_id（-1=本机）→ TaskMigrator；前端 TaskConfigBadge 的 Run on 下拉。先复制后切指针，失败状态复原可重试
- **搬运内容**：session JSONL（跨账号 glob 定位 → 目标机 ~/.claude 同编码路径）+ 项目目录全量 rsync（含未提交改动）；worker→worker 经 Manager 两跳
- **cwd 链条两个教训**（task 58 实测）：① worker 转发路径必须像本地一样把 project.local_path 写进 target_repo；② 失败启动会把 os.getcwd() 写进 last_cwd 且其优先级高于 target_repo——迁回本机时无效 last_cwd 必须清掉
- worker 上重建 task 后立即 cancel（否则其 Dispatcher 2 秒内把任务描述重跑一遍）
- Worker 销毁 = 批量迁回 + terminate；纯本地项目 = rsync 播种（_init_local_repo 见 .git 跳过）
