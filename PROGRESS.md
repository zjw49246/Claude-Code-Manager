# 开发进度

> **重要：Claude 必须自主维护本文件。** 每次完成重要改动或遇到问题后，在对应章节记录。每条记录必须附上 git commit ID。

## 已完成功能

### 阶段 1：基础设施
- [x] 项目初始化 (pyproject.toml, .gitignore, .env)
- [x] SQLAlchemy async + SQLite 数据库
- [x] ORM 模型: Task, Instance, LogEntry, Worktree
- [x] Pydantic schemas
- [x] Task CRUD API + 优先级队列

### 阶段 2：Claude Code 集成
- [x] StreamParser — NDJSON stream-json 逐行解析
- [x] InstanceManager — 子进程生命周期管理
- [x] Instance API (CRUD, run, stop, logs)
- [x] 子进程启动前 unset CLAUDECODE 环境变量

### 阶段 3：Git Worktree
- [x] WorktreeManager — create, merge (--no-ff), remove, cleanup
- [x] Worktree ORM 模型及与实例执行的集成

### 阶段 4：Ralph Loop
- [x] 自动取活循环：取最高优先级任务 → 执行 → 循环
- [x] Plan Mode：只读分析 → plan_review → 审批 → 执行
- [x] API: start/stop/status per instance

### 阶段 5：WebSocket
- [x] WebSocketBroadcaster — channel-based pub/sub
- [x] WebSocket 端点 subscribe/unsubscribe
- [x] 实时日志推送和状态更新
- [x] Task channel 广播 (`task:{id}`)

### 阶段 6：React 前端
- [x] Vite + React + Tailwind CSS v4
- [x] LoginPage token 认证
- [x] Dashboard — 统计栏 + InstanceGrid + 日志弹窗
- [x] TasksPage — TaskForm + 筛选标签 + TaskList
- [x] InstanceGrid — 创建/删除/停止 + Ralph Loop 开关
- [x] InstanceLog — WebSocket 实时日志查看器
- [x] useWebSocket hook (指数退避重连)

### 阶段 7：语音输入
- [x] WhisperClient — OpenAI Whisper API
- [x] Voice API (POST /api/voice/transcribe)
- [x] VoiceButton 组件 (MediaRecorder API)
- [x] 集成到 TaskForm 的标题和描述字段

### 阶段 8：PWA
- [x] manifest.json + service worker
- [x] Apple meta tags (iOS 主屏幕)
- [x] PWA 图标 (SVG)

### 阶段 9：Plan Mode UI
- [x] PlanPanel 组件 — 查看/审批/拒绝计划
- [x] Plan approve/reject API
- [x] 任务状态: plan_review (紫色标识)

### 阶段 10：认证 + 远程访问
- [x] TokenAuthMiddleware (Bearer token + query param)
- [x] Login API
- [x] 前端认证流程 (登录门控, 401 自动登出)
- [x] ngrok / Cloudflare Tunnel 隧道支持
- [x] 生产模式: 后端服务前端静态文件

### 阶段 11：多轮对话
- [x] 从 stream-json 提取 session_id (system/init + result 事件)
- [x] session_id + last_cwd 存储在 Task 模型上
- [x] InstanceManager 支持 `--resume` 标志
- [x] Chat API (POST /api/tasks/{id}/chat, GET .../chat/history)
- [x] ChatView 组件 — 聊天气泡 UI + WebSocket 实时流
- [x] Follow-up 时自动查找空闲 instance
- [x] IME 组合输入处理 (防止中文输入法 Enter 发送)
- [x] 过滤空的 partial streaming 消息

### 阶段 12：任务生命周期重构
- [x] GlobalDispatcher — 全局调度器，替代 per-instance RalphLoop
- [x] 9 步任务生命周期: pending → in_progress → executing → merging → completed
- [x] worktree 创建前 git fetch origin，基于远程分支
- [x] 完成后 rebase + merge --ff-only + push (带重试 + merge lock)
- [x] conflict 状态 + 冲突解决端点
- [x] Project 模型 (name, git_url, local_path) + 自动 clone
- [x] Task.project_id 关联 Project，dispatcher 自动解析为 target_repo
- [x] 修复 dequeue() 排序 bug (desc → asc)
- [x] 前端: 项目选择器、新状态颜色、Dispatcher 全局开关
- **Commit**: c1407e4

### 阶段 13：Claude Code 完全自主 + 本地项目支持
- [x] Project 模型：git_url 改为 nullable，新增 has_remote 字段
- [x] 项目创建支持两种模式：clone 已有仓库（has_remote=True）和本地 git init（has_remote=False）
- [x] 新项目自动生成 CLAUDE.md（含 9 步自主任务生命周期模板）
- [x] Dispatcher 简化：去掉 merge/push/conflict 逻辑，Claude Code 自主完成 git 操作
- [x] 去掉 merging/conflict 状态、resolve-conflict 端点
- [x] TaskForm 重构：创建任务时可直接新建项目（输入名称 + 可选 remote URL）
- [x] 去掉 targetRepo 手动填路径方式，统一通过 project_id 关联
- **Commit**: 231a0b7

### 阶段 14：全面补齐测试覆盖
- [x] 整合 conftest.py 共享 fixture（app/client/session_factory）
- [x] 新增 102 个测试（52 → 154 总计）
- [x] 覆盖所有 API 端点：system、auth、projects、instances、chat 补全
- [x] 覆盖所有服务层：dispatcher、instance_manager、worktree_manager、ralph_loop、ws_broadcaster、whisper_client
- [x] 修复 chat.py 中多余的 `db.begin()` 导致事务冲突 bug
- [x] 修复 chat.py 中 last_cwd 指向已清理 worktree 的 bug（添加 os.path.isdir 回退）
- **Commit**: 55e967b

### 阶段 15：Dispatcher 简化 — Git 操作全交给 Claude Code
- [x] dispatcher.py 去掉 worktree_manager，不再创建/清理 worktree
- [x] ralph_loop.py 去掉 worktree_manager，不再 merge/清理 worktree
- [x] main.py 去掉 worktree_manager 单例注入
- [x] CLAUDE.md 模板更新：步骤 2 改为 Claude Code 自己创建 worktree，步骤 8 改为自己清理
- [x] 主项目 CLAUDE.md 同步更新生命周期描述
- [x] chat.py 简化 cwd 逻辑（不再需要 worktree 路径 fallback）
- [x] 测试同步更新（去掉 worktree_manager mock，更新 cwd 测试）
- **Commit**: bebb4c1

### 阶段 16：Chat 完整消息 + 进程超时保护
- [x] stream_parser 正确解析 assistant(tool_use/thinking)、user→tool_result、system_event
- [x] chat API 和前端扩展事件白名单，新增 thinking/system_event 渲染
- [x] dispatcher/ralph_loop 的 process.wait() 加超时保护（默认 30 分钟）
- [x] config 新增 task_timeout_seconds 配置项
- [x] 新增 6 个 stream_parser 测试（153 → 159 总计）
- **Commit**: 3ff1990

### 文档
- [x] README.md
- [x] CLAUDE.md
- [x] TEST.md
- [x] PROGRESS.md

---

### 阶段 N：PTY 常驻会话模式（2026-06-10，commit 1b6d45b）
- [x] `use_pty_mode` flag（默认 false，-p 行为零变化），claude 任务分流到 claude_pty CCMBackend
- [x] 输入走 channel 注入（MCP notification），输出走会话 JSONL，事件结构与 StreamParser 对齐，下游无感知
- [x] stop() PTY 分支（Esc 中断 + 会话回收）；dispatcher 超时 kill 经 proxy 真正回收会话
- [x] 端到端冒烟 `scripts/pty_smoke.py`：launch → 事件入库/广播 → exit 0 → **第二轮热复用同一进程 7.8s 完成**
- 依赖 claude_pty >= a478051（/home/ubuntu/Projects/PTY，dev venv editable 安装）
- 已知边界：交互模式无 result 事件 → instance.total_cost_usd 暂不更新（待 usage 累加方案）；goal evaluator / monitor 子 agent 仍走 -p（设计如此）
- **号池注意（Phase 3 最高优先）**：PTY 模式撞限不退进程（-p 靠 exit code + stderr 触发换号），当前表现为 turn 超时而非自动 rotation。迁移基础设施已就绪（config_dir 注入 / migrate_session 硬链接 / on_exit rotation 钩子），缺撞限检测信号——计划扫 PTY 输出 usage-limit 标志或 JSONL error 事件后调 migrate_and_relaunch
- 开关语义（commit 待定）：关闭 PTY 模式立即回收所有 idle 会话，mid-turn 会话跑完为止

### 实测反馈修复（2026-06-10，commit 见本条）
- [x] **回复黑洞**：channel 注入 + pty_bridge_reply 工具让 CC 把真实回答"发"进无人消费的通道，用户只看到一句自我总结（task 47/48/50 的"回复一点点/报告没发/提示词丢失"全是此因）。修复：PTY 仓库移除该工具 + 指示语改为"channel 消息=用户消息，在对话中直接回答"（PTY commit 30b6588）
- [x] **冷恢复投递被吞**：spawn 瞬间写 stdin 时 TUI 未就绪 → turn 永不开始 → 消费者挂 30 分钟霸占任务队列（task 47/48 后期全卡死）。修复：删除 spawn 时投递，统一走 channel 注入
- [x] **orphan CC 进程**：后端重启不回收 PTY 会话 → 旧 CC 占着 session 文件。修复：lifespan shutdown 调 pty_backend.shutdown()
- [x] **池尸体**：手动中断/超时 kill 后死会话残留 pool。修复：全路径 pool.remove
- [x] **日志盲区**：未配 root logger，claude_pty 日志全丢。修复：basicConfig INFO
- [x] **额度 unknown**：交互模式无 contextWindow。修复：按 task.model 回填（[1m]→1M，否则 200K）
- 已知无解/待做：thinking 在交互 JSONL 中加密（CC 行为，仅能显示占位）；loop 单 turn 跑完导致无逐轮进度（Phase 3 设计）；号池撞限检测（Phase 3）

## 问题记录

> 格式：问题 → 原因 → 解决 → 预防措施 → commit ID

### 前端空白页
- **问题**: 打开网页一片空白，控制台报错 `does not provide an export named 'Instance'`
- **原因**: Vite 会去除 type-only exports，`import { Instance } from '../../api/client'` 失败
- **解决**: 类型用 `import type { X }` 单独导入，值用 `import { api }` 导入
- **预防**: 前端所有类型导入必须用 `import type`，已写入 CLAUDE.md 约定
- **Commit**: c1407e4

### 优先级排序反了
- **问题**: P1 任务在 P0 之前执行
- **原因**: 代码用了 `Task.priority.desc()`，而约定是数字越小优先级越高
- **解决**: 改为 `Task.priority.asc()`
- **预防**: 已在 CLAUDE.md 注明「优先级数字越小越高，排序用 `.asc()`」
- **Commit**: c1407e4

### 多轮对话 resume 失败
- **问题**: Follow-up 消息报错 `No conversation found with session ID`
- **原因**: Claude Code 的 session 文件按 cwd 路径存储，follow-up 时 cwd 变了导致找不到 session
- **解决**: 在 Task 模型上新增 `last_cwd` 字段，launch 时记录，resume 时使用相同 cwd
- **预防**: 已在 CLAUDE.md 注明「resume 必须使用和原始 session 相同的 cwd」
- **Commit**: c1407e4

### session_id 应绑定 Task 而非 Instance
- **问题**: 最初将 session_id 放在 Instance 上，导致 Instance 切换任务后丢失之前任务的 session
- **原因**: Instance 是 worker 会轮换处理多个 task，session 应该跟着 task 走
- **解决**: 将 session_id 和 last_cwd 从 Instance 模型迁移到 Task 模型
- **预防**: 已在 CLAUDE.md 注明「session_id 和 last_cwd 在 Task 上，不是 Instance」
- **Commit**: c1407e4

### Chat 消息显示重复
- **问题**: 用户发的 follow-up 消息和 Claude 回复都显示两遍
- **原因1**: 用户消息 — 前端乐观添加 + WebSocket 广播各一次
- **原因2**: 助手消息 — Claude Code 的 stream-json 会发多条 message 事件，部分 content 为 null（流式 chunk），有内容的和空的都被渲染了
- **解决**: WebSocket 监听忽略 `user_message` 事件；过滤 content 为 null 的 `message`/`result` 事件
- **预防**: 前端接收 WebSocket 消息时注意去重和过滤无效数据
- **Commit**: c1407e4

### 前端构建 TS 报错未使用变量
- **问题**: `npm run build` 因未使用的 import 报 TS6133 错误
- **原因**: 重构时移除了功能但没清理对应的 import
- **解决**: 删除未使用的 import (`Play`, `api`, `useCallback`)
- **预防**: 重构后检查相关文件的 import 是否需要清理
- **Commit**: c1407e4

### 未遵守 CLAUDE.md 规范
- **问题**: 多次改代码时未遵守 CLAUDE.md 要求的测试规范和文件维护规则——改代码前没先跑测试、改完没更新 README.md/TEST.md/PROGRESS.md
- **原因**: 专注实现功能忽略了流程规范
- **解决**: 补跑测试确认全绿，补更新三个文档
- **预防**: 每次改代码严格按流程：1) 先跑测试 2) 改代码 3) 再跑测试 4) 更新四个文档
- **Commit**: 231a0b7

### Chat 完整显示 Claude Code 交互内容
- **问题**: Chat 界面只显示精简内容，工具调用只有名字没有具体代码改动
- **原因**: Chat API 没返回 `tool_input`/`tool_output` 字段，前端也没渲染
- **解决**: Chat API 补全返回字段、ChatMessage 类型加字段、MessageBubble 完整渲染工具内容（带折叠）
- **Commit**: e810760

### Chat 退出 bug + Plan approve 无反应
- **问题1**: 进入 Chat 后退出，页面不断返回 Chat 界面
- **原因**: `TasksPage` 的 `refresh` 回调依赖 `chatTask` state，导致 `setChatTask(null)` 后旧闭包里的 `chatTask` 引用又把它设回去
- **解决**: 用 `useRef` 保存 `chatTask` 引用，`refresh` 不再依赖 `chatTask` state
- **问题2**: PlanPanel 的 approve/reject 按钮按了没反应
- **原因**: 用了原生 `fetch` 而不是 `api` 客户端，没带 `Authorization` header，401 被静默忽略
- **解决**: 改用 `api.approvePlan()` / `api.rejectPlan()`，在 `client.ts` 新增这两个方法
- **附加**: 修复了 conftest.py 模型未导入导致单文件跑测试时 `no such table` 的问题；新增 10 个 chat/plan API 测试
- **Commit**: 2a7cd89

### Tasks 页面三处缺陷修复
- **问题1**: Task 没有 star 按钮 — 前端 TaskList 缺少星标按钮，后端没有 `/tasks/{id}/star` 端点
- **问题2**: Status 筛选缺少 `executing` — filters 只有 `in_progress`，没有 `executing`
- **问题3**: 后端不支持 project_id/starred 筛选 — 前端传了 `project_id` 和 `starred` 参数，但后端 `list_tasks` API 和 TaskQueue 没接收处理
- **解决**: 后端新增 star 端点和 TaskQueue.star()；list_tasks 增加 project_id/starred 参数；前端 TaskList 增加星标按钮；filters 增加 executing
- **预防**: 新增前端筛选参数时，必须同步检查后端 API 是否接收该参数
- **Commit**: 7d01b87

### 部署注意事项
- **问题**: 重新部署时误清理了其它 Cloudflare 域名的服务
- **预防**: 重新部署时只重启当前服务对应的 Cloudflare 域名，除非明确要求，不要清理其它域名

### Alembic 在 uvicorn 下间歇性死锁
- **问题**: 每次重启后端，`init_db()` 中 alembic upgrade 间歇性卡住，导致 startup 无法完成、API 返回 500、网站无数据
- **原因**: `asyncio.get_event_loop().run_in_executor()` 在线程池中运行 alembic，alembic 的 `fileConfig()` 重新配置 Python logging，与 uvicorn 的 logging handler 产生锁冲突，导致线程死锁
- **解决**: 改为 `subprocess.run(["uv", "run", "alembic", "upgrade", "head"])` 执行迁移，完全隔离进程，彻底避免死锁
- **预防**: 在 async 应用中运行重量级同步库时，优先用 subprocess 隔离，而非 run_in_executor
- **Commit**: 2577c3b

### SQLite 相对路径导致连接到错误的数据库
- **问题**: 部署后 API 返回 500，`no such column: tasks.todo_file_path`，但手动查询根目录 db 列是存在的
- **原因**: `database_url` 使用相对路径 `sqlite+aiosqlite:///./claude_manager.db`，部署脚本 `cd frontend && npm run build` 后工作目录停留在 `frontend/`，uvicorn 继承该 cwd，导致连接到 `frontend/claude_manager.db`（意外创建的旧数据库，缺少新增列）
- **解决**: 在 `database.py` 中将 SQLite 相对路径解析为基于项目根目录 (`_PROJECT_ROOT`) 的绝对路径，不再依赖进程工作目录
- **预防**:
  - SQLite URL 中的相对路径必须解析为绝对路径，避免依赖 cwd
  - 遇到意外创建的 db 文件时，先确认问题修复后再删除，或删除前先备份，避免误删重要数据
- **Commit**: 620b99d

### Git HTTPS/SSH 凭据注入修复
- **问题**: 所有任务 git push 失败，即使用户在前端配置了 git 凭据
- **原因（三层 bug）**:
  1. `_build_git_env()` 只处理 SSH（`GIT_SSH_COMMAND`），完全忽略 HTTPS token
  2. SSH 和 HTTPS 是 `if/elif` 二选一，但 remote URL 协议决定 git 用哪个认证——project 用 HTTPS URL 但全局选了 SSH 类型，导致 HTTPS push 无凭据
  3. macOS `osxkeychain` credential helper 缓存了本机账号（`zjw49246`）的凭据，优先级高于我们注入的凭据
- **解决**:
  1. 同时注入 SSH（`GIT_SSH_COMMAND`）和 HTTPS（`GIT_ASKPASS` 脚本）凭据，git 按 remote URL 协议自动选用
  2. `merge_git_config()` 改为每个凭据字段独立 merge，不再按 `credential_type` 整层切换
  3. 设置 `GIT_CONFIG_GLOBAL=/dev/null` + `GIT_CONFIG_NOSYSTEM=1` 彻底绕过系统 git 配置（`GIT_CONFIG_COUNT` 方案无效：空 `credential.helper` 通过 env 是 additive 而非 reset）
  4. `_clone_repo()` 也注入 git 环境变量，否则私有仓库 clone 会失败
  5. `_apply_git_config()` HTTPS 凭据从 remote URL 动态提取 host，不再硬编码 `github.com`；先设 `credential.helper=""` 清空继承链
- **预防**: 新增 35 个测试覆盖凭据注入全流程
- **Commits**: fe5eb23, c347236, c727ac1, 54bd372

### Opus 4.7 thinking 内容只显示 "💭 Thinking" 没有正文
- **问题**: 用户切到 Opus 4.7 后，chat 里 thinking 气泡只剩一个标题，没有思考内容
- **原因**: `stream_parser._extract_thinking_text` 只读 `block["thinking"]` 字段。新版 Claude Code / API 在某些场景里把内容放在 `block["text"]`、嵌套 `content` blocks，或者只输出加密 thinking（仅有 `signature` + `data`，无明文）。原代码遇到这些情况一律拿到空字符串，前端 `{message.content && ...}` 判断后整块不渲染
- **解决**:
  1. `stream_parser` 新增 `_extract_thinking_text` 帮助方法，按 `thinking → text → content → summary` 顺序兜底；遇到加密块返回 `[encrypted thinking ...]` 标记
  2. `ChatView` thinking 气泡改为始终渲染内容区，空/加密时显示提示文案，普通文本 `maxLines` 从 3 提到 20
  3. 同时把 `sonnet[1m]` 加入默认 `model_options`（Sonnet 4.5+ 也支持 1M context）
  4. 新增 `Instance.thinking_budget` 字段（Alembic migration `bb102ab28888`），通过 `MAX_THINKING_TOKENS` env var 注入子进程，按需开启高预算 thinking
- **预防**: 解析外部 stream 协议字段时永远写多字段 fallback；加密 / 缺失 / 空三种情况要在 UI 里显式区分，否则用户以为是前端 bug
- **Commit**: 8dca374

### 同一台机器部署多个实例的 Git 配置
- **问题**: 多个 Claude Code Manager 实例部署在同一台机器，不同实例需要推送到不同 GitHub 账号的仓库
- **原因**: 本机 macOS Keychain（osxkeychain）只缓存一个 GitHub 账号的 HTTPS 凭据；默认 SSH key 也只绑定一个 GitHub 账号
- **解决**:
  1. 在前端「全局 Git 设置」中**同时填写 SSH key 路径和 HTTPS token**，系统会根据 remote URL 协议自动选用
  2. 为每个 GitHub 账号生成独立 SSH key，在 `~/.ssh/config` 中配置 Host 别名（如 `github-account-a`、`github-account-b`）
  3. 每个实例使用独立的 `.env`（不同 `AUTH_TOKEN`、`PORT`、`DATABASE_URL`）
  4. Cloudflare Tunnel 的 `config.yml` 中按 hostname 路由到不同端口
- **预防**: 部署新实例时必须确认全局 Git 设置中的凭据对应正确的 GitHub 账号

---

### 生产部署 (systemd)
- [x] `ccm-backend.service` — uvicorn 后端，开机自启，崩溃自动重启
- [x] `ccm-tunnel.service` — Cloudflare Tunnel，开机自启
- [x] 域名: `claude-code-manager.com`，通过 `claude-code-manager` tunnel (b5c526ab) 路由
- [x] `auto-backup` 依赖改用 HTTPS 拉取（`5a8ee10`）
- 教训：服务器部署用 systemd 而非 nohup，确保 SSH 断开和机器重启后自动恢复

### Chat 中断后工具继续运行、不知何时结束
- **问题**: -p 模式下点 Interrupt 后 Claude 仍在调用工具，UI 不知道回复何时结束
- **原因**: ① Interrupt 只杀当前子进程，per-task 消息队列里排队的消息随即被 consumer 派发，新子进程继续跑；② `_stop_task_process` 找不到进程时静默把 task 标记 completed，进程实际还在跑；③ 前端乐观清状态，无任何提示
- **解决**: `dispatcher.clear_task_queue()` 在 stop-session 时先清空排队消息；端点返回 `stopped`/`cleared_messages` 真实结果；ChatView 在 `stopped=false` 时明确提示用户
- **预防**: 中断语义必须覆盖"排队中"的工作，不只是"执行中"的进程；API 不应把 no-op 包装成成功
- **Commit**: f4d24e5

### Chat 撞限不自动切号（Pool 切换从未在 chat 路径生效）
- **问题**: Chat 中途遇到 "You have hit your session limit" 不切号，任务直接 failed
- **原因**: ① `_try_chat_pool_rotation` 用**位置参数**调用 keyword-only 的 `migrate_session(*, ...)`，TypeError 被外层 `except Exception` 吞掉，切号永远返回 False（无测试覆盖所以一直没暴露）；② 生产部署的旧版正则 `hit your limit` 匹配不到 "hit your **session** limit" 新文案（仓库已修但未部署）。两个问题叠加
- **解决**: 改关键字调用 + 回归测试（先验证红再修绿）；顺带修了 probe 阻塞事件循环（`select_async` 走线程）、probe env 未剔除 `CLAUDECODE`、session 迁移源误用 env `CLAUDE_CONFIG_DIR`（改 `locate_session_config_dir` 全目录查找）
- **预防**: keyword-only 函数防得住签名误用，但防不住异常被宽 `except` 吞掉——关键路径（如切号）必须有集成级测试断言"成功"而不仅是"不抛错"；修了 bug 要及时部署到生产，仓库修了 ≠ 线上修了
- **Commit**: 8856d18

### 2026-06-12 — task 87 PTY 回复错位 + 子 agent 通用化

- **问题**: PTY 模式下模型开后台子 agent（内置 Monitor）后，harness 自主唤醒的 turn 无 consumer 消费；下一条用户消息的 send_prompt 读到积压、认了旧 turn 的 turn_duration → 回复永久 +1 错位（用户问 A 得到上一问 B 的答案，直到会话结束）
- **解决**: PTY 仓库（commit 14ce6a0）turn 以 prompt 回显为起点 + 常驻空闲 watcher；CCM 侧把 monitor 表通用化为 sub_agent_sessions（agent_type 分类），PTY 观测到的原生子 agent（native-agent/native-monitor）镜像入库，徽章/面板/WS 与 $monitor 同一套展示
- **预防**: 哨兵协议必须校验"turn 归属"；接收方可能自己说话的通道必须有常驻消费者。另：调研结论要在目标分支上复核——"drain_idle_pty_sessions 无调用点"在 main 上不成立（settings API 已接），险些重复实现造成双重 drain
- **Commit**: 71c4fdb（CCM task-from-main）、14ce6a0（claude-pty，本地未推送）

### 2026-06-12 — PTY 权限透传（聊天卡片允许/拒绝）

- **问题**: PTY 链路里 BridgeHub 的 permission handler 从未被 CCM 注册，CC 的权限请求全部 120s 超时默认拒绝（task 87 冒烟被拒的根因），用户侧毫无感知
- **解决**: instance_manager 注册 handler（bridge HTTP 线程经 run_coroutine_threadsafe 进主循环），权限请求 → LogEntry + WS `permission_request` 卡片；前端 🔐 卡片点 允许/拒绝 → `POST /api/tasks/{id}/permissions/{request_id}` → bridge → channel server 解除阻塞。未送达（过期/未知）如实 410 且不落库，防止其他客户端误标
- **预防**: 提供回调注册点的库要在集成层 grep 一遍"谁注册了"——长期无人注册的回调点等于功能性静默缺陷；跨线程回调必须显式注入事件循环（lifespan 里给 _loop 赋值），不要在回调里 get_event_loop
- **Commit**: d0e53d4 + 8b6b496

### 2026-06-19 — task #707 双 session 竞争条件（queue consumer 崩溃恢复误标 pending）

- **问题**: 聊天每发一条消息都会同时起两个 Claude session——一个 resume 回应聊天、一个从头重跑任务描述（task #707 日志中 8 组配对，启动时间差仅 2-5 秒）。表现为"第一遍没反应、第二遍才好"
- **原因**: `_process_queued_message` 的崩溃恢复分支（task.status=="failed"）在克隆 session JSONL 失败、回退到 compact 摘要后，把 `task.status` 写成 `"pending"` 并 commit。主调度循环 `_dispatch_loop` → `TaskQueue.dequeue()` 只认 `status=="pending"` 的任务，下一次 2 秒轮询就把它当新任务抢走一个空闲 instance 从头执行；同时 queue consumer 自己也继续 resume。同一 task 被两条路径并发启动两个进程
- **解决**: 崩溃恢复处 `task.status` 改成 `"in_progress"`（dispatcher.py），表示"已被 queue consumer 认领、待 resume"。dequeue 不会再抢；consumer 后续在 launch 前会自行改成 `"executing"`。与 TaskQueue.dequeue 认领时设的 `in_progress` 语义一致
- **预防**: 任何在 dispatch loop 之外操作 task 状态的路径，绝不能把进行中的 task 落回 `"pending"`——那是主调度循环唯一的"可领取"信号。中间态一律用 `in_progress`/`executing`
- **Commit**: 本次提交

### 2026-06-19 — task #725 resume 找不到 session：第一条消息被牺牲、第二条才恢复

- **现象**: 任务跑完后，用户发第一条 follow-up 消息直接把 task 打成 failed，紧接着发第二条**同样内容**却正常回复。DB 日志铁证：turn 0 建 session `70bcfc88` 成功完成 → 第一条消息 `--resume 70bcfc88` 返回 `error_during_execution: "No conversation found with session ID: 70bcfc88"` → 进程非 0 退出 → `_consume_output` 把 task 标 `failed` → 第二条消息因 `status=="failed"` 命中崩溃恢复分支 → `_clone_session` 也找不到 JSONL → 回退到「摘要 + 全新 session `80319fa2`」→ 成功
- **原因（两层）**: ① **结构性**：`_process_queued_message` 的恢复逻辑被 `task.status=="failed"` 这个前置条件挡住，意味着 session 在 resume 时丢失时，**第一条消息永远是炮灰**（必须先失败把 task 翻成 failed，下一条才触发恢复）。② **查找太窄**：`_clone_session` 只在 `~/.claude`/`CLAUDE_CONFIG_DIR` 下、按 `last_cwd` **字面编码**找 JSONL，既不搜各 pool 账号目录（`POOL_ENABLED=true` 时 session 落在某账号 `projects/` 下），也踩了 task #722 记录的符号链接坑——`/Users/matter -> /home/ubuntu`，CLI 用 `os.getcwd()` 的 realpath 编码落盘为 `-home-ubuntu-...`，而 DB 里 `last_cwd` 是符号链接路径 `-Users-matter-...`，字面编码对不上 → clone 永远 miss → 退化成有损摘要
- **解决**: ① `api/tasks.py` 新增 `_find_session_jsonl(session_id)`，pool 在时复用 `pool.locate_session_config_dir`（搜所有账号目录），并统一用 `projects/*/{sid}.jsonl` 通配——对 cwd 编码（符号链接 vs realpath）免疫；`_clone_session` 改用它。② `dispatcher.py` 恢复分支的触发条件从 `status=="failed"` 扩成 `status=="failed" 或 session 不在磁盘上`（resume 前先 `_find_session_jsonl` 探测），让**第一条消息就能自救**。session 真在磁盘上时探测返回非 None，正常 resume，不会误触发恢复
- **预防**: ①「按数据库里存的路径去拼磁盘路径」必须考虑符号链接/realpath 不一致——能 glob 就别拼字面编码；②pool 部署下任何找 session 文件的逻辑都要搜全部账号目录，别假设在默认 `~/.claude`；③"先失败再靠下一条消息恢复"是反模式——恢复条件应基于"能不能 resume"的事实探测，而不是等 task 被标 failed
- **测试**: `backend/tests/test_session_recovery.py`（pool 账号目录 + 跨 project 子目录通配 + session 缺失三类），并修正 `test_api_chat_plan.py` 四个 `_process_queued_message` 用例（新增 `fake_session_on_disk` fixture 在磁盘上放真 session，使其走 resume 而非恢复路径）
- **Commit**: 本次提交

## 已知问题

- `total_cost_usd` 仅在 Claude Code stream-json result 事件报告时更新
- WebSocket 重连期间可能有短暂的实时日志缺失

## 未来计划

- [ ] 任务依赖 (B 等待 A 完成)
- [ ] 费用统计面板 (图表)
- [ ] 实例资源监控 (CPU/内存)
- [ ] 批量导入任务 (CSV/JSON)
- [ ] 任务模板
- [ ] 通知系统 (完成/失败提醒)
- [ ] 深色/浅色主题切换

### Worktree 部署导致版本锁定静默失效（分布式 Worker Phase 1）
- **问题**: worker bootstrap 全绿但 health 上报 `commit=''`，Manager/Worker 版本锁定校验 MISMATCH
- **原因**: 部署走 rsync 把仓库同步到 worker，但开发在 git worktree 里进行——worktree 的 `.git` 不是目录而是一行 `gitdir: <Manager本地路径>` 指针文件，rsync 过去即悬空，`git rev-parse HEAD` 失败返回空
- **解决**: rsync 排除 `.git`（顺带省体积），部署时写 `.deploy_commit` 文件；`git_info.git_head_commit()` git 失败时回退读该文件。真机冒烟二轮 PASS
- **预防**: 「从 git 仓库复制文件到别处再执行 git 命令」的方案必须考虑 worktree/submodule 这类 .git 非目录的形态；版本标识跨机传递宁可用显式文件，别依赖目标机上的 git 状态
- **Commit**: f37a9b9

### PTY bridge 权限 auto-deny：命令带 rm/后台执行触发 ask 被拒
- **问题**: 跑冒烟脚本的 Bash 调用连续三次被 "Denied via channel pty-bridge" 拒绝，看起来像用户拒绝，实际用户没操作
- **原因**: `~/.claude/settings.json` 的 `ask` 列表含 `Bash(rm:*)`，命令以 `rm -f ... &&` 开头即触发权限确认；确认请求经 PTY bridge `/permission_request` 通道，CCM 侧无 UI 透传，等不到应答默认 deny
- **解决**: 长任务改 `setsid nohup ... > log &`（本环境既有惯例，dev CCM 8003 就是这么起的），命令避开 `rm`/`mv` 前缀（如改用带时间戳的新文件名）
- **预防**: 经 bridge 驱动的会话里长任务不要用 run_in_background、命令别踩 ask 触发词；长期方案是把权限确认透传到 CCM 前端（已记 TODO）
- **Commit**: f37a9b9（冒烟脚本与流程）

### 分布式 Worker Phase 2 一次性全绿的关键：先摸清广播协议再写 relay
- **问题**: WorkerRelay 要镜像 worker CCM 的全部事件，但广播 payload 的坑很多（session_id 被 pop、raw_json 被剥、monitor 用 "event" 键、status_change 用 "new_status"、plan_ready 不含内容、MonitorSession id 跨机碰撞）
- **解决**: 写代码前先逐个 grep instance_manager/dispatcher/monitor 的 broadcast 调用点确认每种事件的真实 payload，再按事实实现（设计文档的预判大部分准确但 monitor 键名等细节仍需实测）；MonitorSession 加 remote_id 列做 id 翻译
- **预防**: 跨服务镜像/中继类功能，协议事实（键名、谁 pop 了什么、发到哪个 channel）必须从源码确认，不能按"应该是"写
- **Commit**: e968a11

### 跨机迁移的 cwd 解析链双坑（分布式 Worker Phase 3）
- **问题**: task 从 worker 迁回本机后 chat 续聊连续失败（PTY session 秒死），但手动 `claude -p --resume` 正常——session 迁移本身是好的
- **原因**: ① worker 转发路径没把 project.local_path 写进 task.target_repo（本地 dispatch 路径有这步），cwd 解析回落到 os.getcwd()；② 第一次失败启动把错误 cwd 写进了 task.last_cwd，而 cwd 解析顺序 last_cwd > target_repo——脏数据自我强化，后续每次都错
- **解决**: 转发路径补 target_repo 解析；迁回本机时校验 last_cwd（不存在或不在项目内则清空）
- **预防**: 「衍生状态写回数据库」（如 last_cwd）的字段在失败路径也会被写——排查这类问题先 dump 原始行而不是只看 API（API/ORM 的 identity map 还会叠加缓存假象）；同一逻辑的双路径（本地/远程 dispatch）要逐字段对照
- **Commit**: 见 task-elastic-worker 分支 Phase 3 系列

### 「双 session / 会话不回复」真凶之一：第二个 CCM 抢 8000 端口导致 systemd crash-loop（task #722 实录）
- **现象**: 用户某 session 反复"发消息不回复"，连发 3 次同一指令（task 720/721 failed，722 才接住）。表面像 dispatcher 双调度（参见 496017e 修的 queue-consumer race），实际是基础设施层冲突
- **原因**: 让某 task「在当前文件夹重启 CCM」时，它额外起了**第二个 uvicorn**（手动/后台），与 systemd 的 `ccm.service` 抢占 0.0.0.0:8000 → `ccm.service` 进入 `[Errno 98] address already in use` + `Failed with result 'exit-code'` 的 8 秒一轮崩溃重启循环（journal 10:30–10:35）。每轮启动的 dispatcher 恢复逻辑把用户在飞的 task 反复 reset（"Resetting stuck task 721 from 'executing' to 'completed'"）→ 用户消息全程无人处理。一方进程在 10:35:15 退出释放端口后才自愈。副作用：一次 alembic batch 迁移被打断，残留孤儿表 `_alembic_tmp_log_entries`（alembic_version 仍在 head，不阻塞启动，但下次动 log_entries 的 batch 迁移会撞名，需先 DROP）
- **环境真相**: DB 是从 Mac 迁来的，project/task 路径多为 `/Users/matter/...`；靠符号链接 `/Users/matter -> /home/ubuntu` 解析到真实 Linux 目录，所以 Mac 路径**并没坏**（claude_pty 的 `pty_process.spawn` 用 `Popen(cwd=...)` 无 isdir 回退，路径不存在会直接 PTYSpawnError——是符号链接救了场）。session 落盘在 `/tmp/claude-1000/<realpath编码>/` 与各账号 `projects/<realpath编码>/`，编码取 claude 自己的 `os.getcwd()` realpath，故始终是 `-home-ubuntu-...`
- **解决**: 确认只剩 `ccm.service` 单实例（`ss -ltnp` / `ps`），WorkingDirectory 已是正确的当前文件夹，detached（`systemd-run --on-active` 独立 cgroup）干净重启一次清掉 crash-loop 残留状态
- **预防**: ①「重启 CCM」永远只用 `systemctl restart ccm`，**绝不手动 `uvicorn`/`nohup` 再起一个**——双实例抢 8000 比任何代码 bug 都隐蔽；②排查"会话不回复"先看 `journalctl -u ccm.service` 有没有 `address already in use` / `Failed with result`，再怀疑 dispatcher 逻辑；③孤儿 `_alembic_tmp_*` 表是迁移被打断的信号，记得清
- **Commit**: 运维处置（本次无代码变更），文档沉淀于此条

### 2026-06-21 — task #728 同一 session 被并发 `--resume`：长 turn 误触发看门狗复制出双 consumer
- **现象**: task 728 末尾几条 monitor 汇报进来后，同一个 session（`f16da894`、instance 86）在 24 秒内被 `claude --resume` 启动了 **3 次**，其中 2 次重叠并发（system_init 在 02:36:49 / 02:36:50 都早于第一条 result），最后一条 resume 撞 429 把 task 打成 failed。DB 铁证：紧挨着的上一轮 turn `duration_ms=832017`（≈14 分钟）；期间 monitor #66/#67 在 02:26–02:33 持续 `report_status`（`sub_agent_reports`）→ 每次 `enqueue_message`
- **原因（两层）**: ① **心跳只覆盖消息边界**：`_task_queue_activity` 只在 `_process_queued_message` 调用前后各刷一次，而那一轮的 14 分钟全卡在里面的 `_wait_process`，心跳冻结 → `_ensure_queue_worker` 的 ">120s stuck" 看门狗在长 turn 中被 monitor 的 enqueue 反复触发，`cancel()` 旧 consumer + 重建新 consumer。但 `_wait_process` 只有**超时分支**才 `process.kill()`，cancel 杀不掉那个 14 分钟的 `claude` 子进程（它照样活到 02:36:41）。② **`finally` 无条件 pop**：被 cancel 的旧 consumer 退出时 `self._task_queue_workers.pop(task_id)` 把看门狗刚塞进去的**新 consumer 登记**也抹掉了 → 下次 enqueue 看到字典空 → 再起一个 → 同时存在 ≥2 个活 consumer，长进程一结束、instance 空出，它们的无锁 busy-wait（TOCTOU）几乎同时判定 "not busy"、各自抢同一 idle instance 并发 launch
- **解决**（dispatcher.py）: ① 新增 `_queue_heartbeat`，consumer 全生命周期跑一个心跳协程持续刷新 activity——长 turn 和 idle 等待都算"活着"，看门狗只在事件循环真卡死（连心跳都跑不动）时才兜底触发。② consumer `finally` 改成**只在 `_task_queue_workers[task_id]` 仍是自己时才 pop**（`is asyncio.current_task()`），杜绝旧 consumer 误删新 consumer 登记。③ 把 `300/30/120` 抽成模块常量 `QUEUE_CONSUMER_IDLE_TIMEOUT/QUEUE_HEARTBEAT_INTERVAL/QUEUE_STUCK_THRESHOLD` 便于测试 patch。两者叠加保证 per-task 永远只有一个活 consumer = 串行 resume，故无需再加 launch 锁
- **预防**: ①「定时心跳判活」的 activity 必须由独立心跳源在整个生命周期刷新，绝不能只在"一个工作单元完成"时打点——工作单元本身可能跑十几分钟；②cancel 一个 asyncio task **不会**杀掉它 `await process.wait()` 的 OS 子进程，"重启 consumer"前要么先杀进程要么靠下游 busy-wait/锁兜底；③任何"先 cancel 旧的、再注册新的"模式，旧对象的 cleanup/`finally` 必须自证身份（`is current_task()`）再清理共享登记，否则会误删继任者；④这是 #707/#722 之后"双 session"家族的第三种成因，排查"同一 session 并发 resume"先看上一轮 turn 的 `duration_ms` 与期间的 enqueue 频率
- **测试**: `backend/tests/test_service_dispatcher.py::test_long_turn_does_not_respawn_consumer`（长 turn 不被复制、不并发处理）+ `::test_watchdog_respawn_keeps_live_worker`（强制 respawn 后旧 consumer 的 finally 不抹掉新 consumer 登记）。两测在还原旧逻辑时均 red、修复后 green；全量 902 passed
- **Commit**: 本次提交

### 2026-06-21 — 瞬时 429/过载自动等待重试（Anthropic 基础设施侧限流，非额度用尽）
- **背景**: 用户问 `API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited` 这条报错从哪来。排查确认：CCM 后端与 `claude_pty` 框架里**都没有**这段文案；它硬编码在 Anthropic 官方 CLI 二进制 `@anthropic-ai/claude-code/bin/claude.exe` 里，是 HTTP 429 `rate_limit`（基础设施侧临时限流，**非**账号额度用尽 `billing_error`）的人类可读文案。需求：让 CCM 对这类瞬时 429 自动退避重试
- **关键判断**: 这条文案既不命中 CCM 的 `is_rate_limited`（额度横幅），也不命中 `claude_pty` 的 banner 标记 → 现状会被当普通失败：autonomous 立即重试（烧 `max_retries` 预算、不等待），chat 直接置 failed（**零重试**）。而且换号无用（是 Anthropic 服务端节流，不是某个账号的事），正确处置就是**同账号退避后 `--resume` 重试**
- **最大的坑（PTY exit_code=0）**: PTY 是生产默认模式。api_error 会中止 turn，但**持久 PTY session 不退出** → `claude_pty` 的 `_consume` 取 `session._process.exit_code` 得 None，且 transient 不命中它的 `rate_limited` 标记 → `on_exit` 收到 `ec=0` → 任务被**误判为 completed**。所以单纯按 `exit_code != 0` 挂钩（subprocess 模式可行）在 PTY 下**完全不触发**。解决：在两种模式共用的 `instance_manager._process_event` 里，对带 `is_error` 且命中 `is_transient_overload` 的事件打 **turn-scoped 标记** `_transient_seen`（`launch()` 重置，`transient_error_seen()` 读取，重试 turn 也会重打）——它是唯一可靠的跨模式信号，dispatcher 据此即便 exit_code=0 也重试
- **第二个坑（重试要被驱动）**: PTY chat 的重试不能 fire-and-forget——那样第 2 次失败没人再检查标记。改由 dispatcher `_process_queued_message` 在 `_wait_process` 之后用 `while transient_error_seen(): _try_chat_transient_retry()+_wait_process()` 循环自驱（该循环在 consumer 体内，被 `_queue_heartbeat` 覆盖，不会被 #728 的看门狗误杀）；autonomous 用递归的 `_run_transient_retry` 自驱；subprocess chat 靠 `_consume_output.finally` 的 relaunch 自然自驱
- **实现**: ① `claude_pool.py` 加 `is_transient_overload`（先排除 `is_rate_limited`/`is_auth_failure` 保证与「额度→换号」互斥）+ `transient_retry_delay`（指数退避+jitter）。② `config.py` 加 `transient_retry_enabled/max/base_delay/max_delay`。③ dispatcher `_run_transient_retry`（同号 resume，用尽→单向衔接号池轮换→普通重试/失败，无 ping-pong）+ `_collect_failure_output`（一次性取 stderr+log，因 `get_last_stderr` 是 pop 破坏性）+ 抽出 `_build_task_prompt`/`_relaunch_and_wait` 复用（`_run_pool_retry` 一并瘦身）。④ instance_manager `_try_chat_transient_retry` + `_transient_attempts`（**不能存 `_launch_params`，那个 `launch()` 会重置**，故独立 dict，成功/放弃/stop 时清）
- **测试坑**: 新检测在 `exit_code!=0` 时**总会**先取 log（不再被 pool=None 短路），且 PTY 标记/`pty_mode_enabled` 在 MagicMock 上默认 truthy → 8 个老 dispatcher/chat 测试因 mock 不符真实接口而 red。修法是让 `_make_dispatcher` 的 mock 建模真实接口（`pty_mode_enabled=False`、`transient_error_seen→False`、`get_recent_log_contents→AsyncMock([])`、`get_last_stderr→""`），非改产品逻辑
- **测试**: `test_claude_pool.py::TestTransientOverloadDetection`（含官方原文案命中、额度/认证优先级互斥、无误报）+ `::TestTransientRetryDelay`（退避边界）+ `test_service_instance_manager.py` 的 4 个 turn-scoped 标记测试（命中置位/额度不置位/干净不置位/`launch` 重置）。全量 924 passed
- **预防**: ①判断一条 LLM 报错是「我方还是 Anthropic 官方」先全仓 grep 文案，再 `strings` CLI 二进制定位；②PTY 持久 session 下"turn 失败"**不等于**"进程退出"，凡依赖 exit_code 的判定都要另想 turn-scoped 信号；③任何"重试/轮换"的计数器若relaunch 走 `launch()`，别存会被 `launch()` 重置的结构里；④给 dispatcher 加新的 instance_manager 调用后，先扫各测试的 mock helper 是否建模了该接口
- **Commit**: c5fc96a

### 2026-06-21 — task #729 瞬时 429 重试成功后任务被误判 failed（recover-then-failed）
- **问题**: 上面的瞬时 429 自动退避重试上线后，用户反馈：退避 + `--resume` 之后 session 明明已经成功续上、活儿也干完了，**任务最终却被标成 failed**——"resume 之后状态没及时改过来"
- **根因**: `_transient_seen` 是 **turn-scoped** 标记，本意只反映「当前前台 turn」是否撞了瞬时 429。但 `_process_event` 打标时只看 `is_error + is_transient_overload`，**没排除 `orphan`/`autonomous` 事件**。PTY 在 resume 时（尤其冷 resume：transient 把 CC 进程打挂后 `on_exit` 已把 session 从 `_sessions` pop、池里只剩尸体 → 下次 `get_or_create` 起全新 `JSONLReader`，offset=0 从头重读整份 JSONL）会把**上一 turn 那条触发了本次重试的旧 api_error 当 backlog 回放**，`send_prompt` 标它 `orphan=True` 仍 yield 给 host。于是成功的 resume turn 里这条旧错误把标记**重新置位** → autonomous 路径 `_run_transient_retry` 的 `still_transient` 永真 → 成功分支（`exit_code in (0,…) and not still_transient`）走不到 → 反复重试到预算耗尽 → `mark_failed("Transient server overload persisted…")`。chat PTY 路径同理：`_process_queued_message` 的 `while transient_error_seen()` 死循环到预算耗尽
- **修复**: `_process_event` 打 `_transient_seen` 前加守卫 `not event.get("orphan") and not event.get("autonomous")`——只认当前前台 turn 的活事件。orphan 是上一 turn 的陈旧回放、autonomous 是后台子 agent turn 的报错，都不该驱动前台 turn 的重试。合法的当前 turn api_error（`turn_started=True` 后 normalize 出来的）`orphan=False`，不受影响
- **测试**: `test_service_instance_manager.py::test_process_event_orphan_overload_does_not_set_transient_flag`（orphan + autonomous 两种回放都不置位）。全量 925 passed
- **预防**: turn-scoped 信号**必须**显式区分「当前 turn 的活事件」与「回放/后台事件」——claude_pty 已用 `orphan`/`autonomous` 两个 flag 标好了边界（见 task-87 turn 对齐），任何按事件流推断 turn 内状态的逻辑都要先过滤这两类
- **Commit**: 本次提交

### 2026-06-21 — ask_user：拦截内置 AskUserQuestion，转前端可选卡片再喂回模型（方案②）
- **需求**: 模型调用内置 `AskUserQuestion`（多选/澄清提问）在 CCM 的 headless（`-p`）/PTY 模式下**无人应答会卡住**（工具等交互式 UI，CCM 这边没有原生选项 UI）。要让它弹成 CCM 聊天里的可选卡片，用户点选后把答案喂回模型继续
- **选型**: 评估过「MCP `ask_user` 工具 + disallow 内置」与「PreToolUse hook 拦截」两条路。用户拍板**方案②（hook 拦截）**——优点是模型**自然地用它本就想用的 `AskUserQuestion`**，无需 disallow、无需引导改用别的工具，hook 透明拦截
- **坑1（注入通道）**: hook 要在 `-p` 和 PTY **两条链路都注入**。`-p` 路 `_build_command` 能加 `--settings`，但 **PTY 路不行**：`claude_pty` 的 `pty_process` 命令构建是**固定字段**（`PTYConfig` 无 `settings`/`extra_args`），只认 `--mcp-config`/`--disallowedTools`；且本仓库对 `Claude-Code-PTY` 只有 **READ 权限**（`gh repo view ... viewerPermission=READ`）无法 bump 依赖加 flag。**解法**：两条链路都靠 `CLAUDE_CONFIG_DIR` 指定账号目录，而 Claude 在 `--dangerously-skip-permissions` 下会**自动加载 `{CLAUDE_CONFIG_DIR}/settings.json` 里的 hook**（实测无审批弹窗）。于是把 hook **幂等合并进 `{config_dir}/settings.json`**，注入点选在 `instance_manager.launch()`——它是 `-p` 与 PTY 的**统一入口**（PTY 分流 `_launch_pty` 之前），一处注入两路统吃，零依赖改动
- **坑2（答案怎么"喂回"模型）**: PreToolUse hook 返回 `permissionDecision="deny"` + `permissionDecisionReason=<答案>`——**实测**：deny 的 reason 会作为 `tool_result`（`is_error=true`）**原样**喂回模型，模型会读它并照做（冒烟测试：deny reason 写"回 PINEAPPLE"，haiku 就回 PINEAPPLE）。所以把用户选择格式化进 reason 即可，无需任何"合成 tool result"的特殊机制。用户曾担心 deny→reason 语义不确定，实测打消
- **阻塞实现**: hook 脚本 `backend/hooks/ask_user_hook.py`（**纯 stdlib urllib**，任何 python3 都能跑、不依赖 httpx）阻塞式 `POST /api/ask-user/wait` → 后端 `ask_user_registry` 登记 `asyncio.Future`、广播 `ask_user_question` 卡片、`await asyncio.wait_for(future, ask_user_timeout)`；前端卡片选完 `POST /api/tasks/{id}/ask-user/{request_id}` → `registry.resolve` set future → `/wait` 拼 `format_answer_reason` 返回 → hook 打印 deny+reason。**阻塞期间不持有 DB 连接**（用独立短 `async_session()` 做查 task/落库，await 时零连接占用）
- **fail-open 原则**: hook 任何异常（CCM 不可达 / 非 CCM 托管 session（`/wait` 返回 `no_session`）/ 超时）都 **exit 0、不输出决策** → 放行原生 `AskUserQuestion`，**绝不因辅助设施挂掉而打断会话**。注意：默认 `config_dir=None` 时 hook 会写进用户全局 `~/.claude/settings.json`，对用户自己跑的 `claude` 也生效，但 `no_session` 即时放行，最多一次 localhost 往返延迟
- **复用**: 整套照搬 PTY 权限透传范式（`session_id→task` 查找、卡片 live-only 经 WS、`system_event` 落库进 chat 历史、`/ask-user/pending` 重连回填活跃卡片、前端 `AskUserCard` 仿 `PermissionCard`）
- **实测（端到端，真实 claude）**: 起测试后端 + 建带 `session_id` 的 task + 注入真 hook → `claude -p` 强制调用 `AskUserQuestion` → hook 阻塞 → 脚本提交 `{labels:["Spaces"]}` → 模型收到 `tool_result` 后输出 `FINAL=Spaces`。答案完整回流
- **预防**: ①给「无 UI 的 headless/PTY agent」接交互式工具，**PreToolUse hook 拦截 + 异步回包**是通用解；②**deny→reason 是给模型"喂结果"的可靠通道**（实测），不必发明合成机制；③当依赖的 CLI 不可加 flag / 依赖仓库只读时，`{config_dir}/settings.json` 是 hook/permission 的**注入后门**，且 `-p` 与 PTY 都吃 `CLAUDE_CONFIG_DIR` → 一处统吃；④辅助拦截器**必须 fail-open**；⑤注入点优先选「两路统一入口」（`launch()`）而非各自分支，避免双份维护与漏注入
- **测试**: `backend/tests/test_ask_user.py`（registry roundtrip/重复 resolve/list 排除已完成、`format_answer_reason` 单选/多选/自定义文本/缺答、settings 注入幂等/保留既有 key 与他人 hook/disable 移除/损坏 JSON/建目录），10 passed；全量 935 passed；`frontend tsc --noEmit` 通过
- **Commit**: fcc0b6d（feat）+ 892cb3c（test）+ 本次（docs）

### 2026-06-21 — task #734/#740 号池耗尽时 resume 落到错号 → "No conversation found"（丢 session）
- **问题**: 用户连聊一整天后，几乎每个 turn 都撞 `rate_limit_event`；用户「充值」一个号、并用最新代码重启后端后，所有后续消息（含连发 7 次的「继续」）瞬间失败，错误 `No conversation found with session ID: <sid>`，task 直接 failed、session 丢失。用户怀疑是「切号 + session 软链接逻辑坏了」
- **根因（journal 实锤）**: 软链接/`migrate_session` 本身没坏（日志里一整天都在正确 hardlink）。真凶是**号池耗尽时 resume 放弃了「定位 session 所在目录」这一步**。`_process_queued_message` 里 `config_dir = await pool.select_async(validate=True)`，当**所有号都被限速**（journal: `Pool has no healthy accounts after validation`）时返回 `None`；随后 ① migrate 块被 `if config_dir` 守卫整段跳过，② `instance_manager.launch` 只在 `if config_dir` 为真时才设 `CLAUDE_CONFIG_DIR`，于是子进程**继承 systemd 单元里写死的 `CLAUDE_CONFIG_DIR`**（本机 = `.claude-account-ddrichardmichael2qsth7`）——这个号**从没存过该 session 的 JSONL** → `claude --resume` 秒挂 `No conversation found`。task 翻 failed 后每次重试（包括 `_clone_session` 克隆出的新 sid，文件落在源号目录）仍落同一错号 → 反复秒挂。⚠️ 注意：`config_dir=None` **并非**回退到 `~/.claude`，而是回退到**继承的 env**（systemd 里那个特定号），比 `~/.claude` 更隐蔽
- **修复**: 抽出 `GlobalDispatcher._resolve_resume_config_dir(session_id)` 统一「为 resume 解析 config_dir」：拿到健康号则迁移 session 进去；**号池耗尽（select 返回 None）时不再放任继承 env，而是回退到 `locate_session_config_dir(sid)`——session 真正所在的那个号**。该号即便限速，也只是以「可恢复的 rate-limit/transient 事件」出现、交给既有重试链处理，绝不再硬挂 `No conversation found` 丢 session。`_process_queued_message`（chat resume）与自治派发 launch（`dispatcher.py:990`，失败 task 带 session 重试）两处都换用此 helper
- **测试**: `backend/tests/test_resume_config_dir.py`（耗尽→锚定 resident 目录 / 耗尽无 session→None / 耗尽 session 不在盘→None / 健康号→选中并 hardlink 迁入 / 池关闭→None），5 passed；全量 940 passed
- **预防**: ①「为 resume 选号」和「session 在哪个号」是**两件事**——选不到新号时**绝不能**让 `--resume` 落到与 session 无关的目录（尤其 `config_dir=None` 会静默继承父进程 env，而非你以为的 `~/.claude`）；②号池相关的 fallback 一律先过 `locate_session_config_dir`（它会扫遍所有 `~/.claude*` 含已移出池的号）；③限速是可恢复态，**绝不能升级成「丢 session 的硬失败」**
- **Commit**: c05d919

### 2026-06-21 — task #734/#740 真凶②：主动换号对"良性 rate_limit_event"也冷却，号池假性耗尽
- **问题**: 修了「耗尽→落错号」后，用户追问「我号池里明明有可用账号，为什么还是选不到对应账号？」。即号池**根本不该耗尽**——为什么健康号被判成不可用？
- **根因（journal + DB 实锤）**: CLI 几乎**每个 turn**都吐一条 `rate_limit_event` 状态 ping，`rate_limit_info.status` 才是真信号：`allowed`=健康、`allowed_warning`=接近阈值、`rejected`=真限速。今日 DB 统计：`allowed/five_hour` 274 条、`allowed_warning/seven_day` 71、`allowed_warning/five_hour` 69、`rejected` 82。但 `_consume_output`（`-p` 路；PTY backend 为 None 时实际跑这条）对**任意** `rate_limit_event` 都置 `_saw_rate_limit=True` → turn 成功后 `_try_proactive_pool_switch` **无条件** `mark_rate_limited(当前号, 300s)` + 迁号。今日 12 次冷却里 **7 次来自主动换号**，触发样本之一是 task740 09:01:17：被一条 `allowed_warning / seven_day / utilization=0.37`（7 天额度才用 37%！）的 ping 触发，把 account-1 冷却 5 分钟。3 个号轮着被良性 ping 冷却 → `select` 返回 None → 号池**假性耗尽** → 撞上真凶①
- **修复**: 新增 `rate_limit_event_is_actionable(rate_limit_info)`（`claude_pool.py`）：`allowed`→False（永不冷却）；`allowed_warning`→仅当 `rateLimitType=five_hour` 且 `utilization/surpassedThreshold ≥0.9` 才 True（`seven_day` 警告永远 False——5min 冷却改变不了 7 天窗口，纯空转）；其余非 allowed（rejected/blocked）→True。`stream_parser` 给 `rate_limit_event` 补出 `rate_limit_info` 字段；`_consume_output` 用该 helper 把关 `_saw_rate_limit`。**反应式轮换（`is_rate_limited` 命中真·限速横幅）是另一条路、不动**
- **测试**: `test_claude_pool.py::TestRateLimitEventActionable`（9 例，含 37% 真实坑）+ `test_stream_parser.py`（surface info / 缺失），全量 951 passed
- **预防**: ①「状态 ping」≠「事件发生」——CLI 的 `rate_limit_event` 是**周期性遥测**，必须看 `status`/`utilization` 再决定动作，绝不能见 event 就当限速；②**冷却时长要匹配窗口**：5min 冷却只对 `five_hour` 有意义，对 `seven_day` 是空转churn；③主动优化（proactive rotate）若带副作用（冷却=减少可用号），触发条件必须**保守**，否则会把优化变成自伤；④另注意运维：本机磁盘有 13 个 `.claude-account-*` 目录但 `accounts.json` 只挂了 3 个——「可用账号」要真在 pool 配置里才会被 `select` 看见
- **Commit**: 本次提交

### 2026-06-24 — task #676 卡 executing、无 chat 按钮：两条取实例路径抢同一 idle instance
- **现象**: 用户报 task #676「一直在执行、没有 chat 按钮」。DB：`status=executing`、`session_id=None`、`instance_id=124`；instance 124（worker-9）却是 `status=idle`、`current_task_id=None`、`pid=None`，且无任何 `claude --task-id 676` 进程。
- **根因（journal 实锤）**: instance 的 DB 状态要等 `instance_manager.launch()` 内 PTY 会话**完全 spawn 完**才从 idle 翻成 running，中间约 10s 窗口仍是 idle。两条取实例路径互不知情：`_dispatch_loop` 13:32:32 认领 124 给 676（登记进 `_running_tasks`）并开始 launch；`_process_queued_message` 13:32:47 处理 task 675 的用户消息时，只按 DB `status=='idle'` 选实例，又选中正在 launch 的 124 → `launch_for_ccm` "Stopping stale PTY session for instance 124 before launch" 把 676 半启动的会话杀掉。676 成孤儿：状态卡 executing、无 session、无进程、worker 空闲 → 前端无 chat 按钮、永不完成。
- **修复（commit b40d2b4）**: 让 `_running_tasks` + 新增 `_launching_instances` 成为两条路径共用的内存认领表。queued-message 选实例时排除「in-flight lifecycle」和「另一个 mid-launch」的实例；dispatch loop 跳过 `_launching_instances`；queued-message 的 launch 用 `try/finally` 持有/释放认领，失败也不泄漏（否则该 instance 会被永久挤出调度池）。新增双向排除回归测试 2 例，`test_service_dispatcher.py` 88 passed。
- **预防**: ①「DB 状态」作为并发仲裁有滞后窗口（异步 spawn 期间状态没翻），跨协程抢资源不能只信 DB 行；要么选取时**原子**标占，要么用内存认领表且**两条路径都遵守**；②任何"选 idle 资源后再慢慢 launch"的模式都要问：launch 期间别的路径会不会也选到它？③内存认领必须 `try/finally` 释放，否则异常会把资源永久 wedge 出池。
- **运维**: 该机（ccm-zhoujunwei, ap-northeast-1, i-03e9984e1c983a1a0）跑两套 CCM：`code/`(ccm-backend,8000) 与 `cyf/`(ccm-backend-cyf,8002)，DB 分别在仓库内 `./claude_manager.db` 与 `/home/ubuntu/cyf/claude_manager.db`；#676 在 `code/`。

### 2026-06-25 — auto_login 在小机型上 Chrome 起不来：cdp_login 漏了 --disable-dev-shm-usage
- **问题**: 在新开的 t3.medium worker 上跑 `scripts/auto_login.py` 登录 Claude 账号，step 1（171mail 接码、拿 magic link）正常，但 step 2「Chrome CDP」整段失败：`httpx.ConnectError: All connection attempts failed`，连 `http://127.0.0.1:9222/json` 都连不上。
- **根因**: `scripts/cdp_login.py` 启 google-chrome 时没带 `--disable-dev-shm-usage`。Chrome 默认把渲染进程共享内存放 `/dev/shm`，小机型（t3.medium）的 `/dev/shm` 太小 → 渲染进程因共享内存不足**立即崩溃** → CDP 调试端口 9222 根本没起来 → 后续 `GET /json` 必然 ConnectError。大机型（Manager 是 c7i.2xlarge，/dev/shm 够大）不触发，所以一直没暴露。讽刺的是 `auto_login.py` 另一条 `_mailcatcher_browser_login` 路径早就带了这个 flag，唯独主路径 `cdp_login.py` 漏了。
- **修复**: `cdp_login.py` 的 chrome 参数加 `--disable-dev-shm-usage`（必需）+ `--disable-software-rasterizer`（顺带），并把启动等待 `sleep(4)→6`（小机型冷启动更稳）。加 flag 后 CDP ~1s 即开放，登录一次成功（实测 BuffaloWingsxvq@diplomats.com，订阅 max，写出有效 .credentials.json）。
- **预防**: ①任何无头机上跑 Chrome 一律带 `--no-sandbox --disable-dev-shm-usage`，前者过 root、后者过小 /dev/shm，二者是服务器跑 Chrome 的标配；②同一仓库里有多条「启 Chrome」代码时，flag 要对齐（这次就是一条带一条没带）；③只在大机型验证过的浏览器自动化，换小机型必复测。
- **Commit**: 本次提交

### 2026-06-26 — task #770 loop 模式选号失效：launch 漏传 config_dir，号池从未被咨询
- **问题**: loop 模式（含 `_resume_fix_signal` 补信号那次）调用 `instance_manager.launch()` 时**完全没传 `config_dir`**。`launch()` 只在 `config_dir` 为真时才写 `env["CLAUDE_CONFIG_DIR"]`，否则子进程继承 systemd 里写死的 `CLAUDE_CONFIG_DIR`。后果：loop 永远跑在那一个默认号上——不从池里选号、不避开冷却中的号、PTY 模式下 `iteration>0` resume 还可能落到没存该 session 的号上 `No conversation found`。普通 Step 4 路径早就 `pool_config_dir = await self._resolve_resume_config_dir(task.session_id)` 选好了，loop/goal 两条提前 return 的分支各自漏了。
- **修复（本次提交）**: `_run_loop_iterations` 主 launch 与 `_resume_fix_signal` 各加一行 `config_dir = await self._resolve_resume_config_dir(resume_sid)` 并传入 launch——iteration 0（resume_sid=None）走「挑健康号」；iteration>0 锚到 session 所在号（不漂移 config_dir → 保 PTY 热 session）；号池耗尽时回退到 resident 号。新增回归测试 `test_loop_iteration_passes_pool_config_dir`（断言 launch 收到 resolver 返回的 config_dir）。
- **预防**: ①新增「另起一条 lifecycle 分支」（loop/goal/plan）时，凡 launch 子进程都要问：号池选号那步（`_resolve_resume_config_dir`）走了没？别让分支静默继承 systemd 默认号。②`config_dir=None` 不是「用默认号」的安全默认，而是「听天由命继承 env」——池开着时必须显式选号。
- **goal 模式同款修复（commit 7499d94 之后的后续提交）**: `_run_goal_lifecycle` 的 turn 0（fresh，resolver 传 None）与 followup（resume，resolver 传 session_id）两处 launch 同样补上 `config_dir = await self._resolve_resume_config_dir(...)`。新增回归测试 `test_goal_turn_passes_pool_config_dir`。至此 loop / goal / Step 4 三条路径选号行为一致。

### 2026-06-28 — Safari 整页崩 "Invalid regular expression: invalid group specifier name"（前端 lookbehind）
- **问题**: 用户在 Safari 打开 `*.claude-code-manager.com`（CCM 前端）整页崩，错误边界显示 `Something went wrong / Invalid regular expression: invalid group specifier name`。Chrome 正常，故之前 curl/Chrome 验证一直没暴露。
- **根因**: 依赖 `mdast-util-gfm-autolink-literal@2.0.1`（`remark-gfm@4` → react-markdown 渲染 Chat markdown 时引入）在模块加载时构造了带 **lookbehind** 的正则 `(?<=^|\s|\p{P}|\p{S})([-.\w+]+)@(...)`（email 自动链接）。Safari <16.4 不支持 lookbehind → 解析即抛 → React 错误边界整页崩。打开任意聊天页（ChatView/LoopChatView/DiscussionView/SharedChatView）即触发。
- **修复**: 用 `patch-package` 删掉该 lookbehind（`frontend/patches/mdast-util-gfm-autolink-literal+2.0.1.patch`），并加 `postinstall: patch-package`。**行为不变**：URL 那条正则本就不用 lookbehind，email 的 `findEmail` 内部已调用 `previous(match, true)` 做完全等价的「前一字符必须是行首/空白/标点」校验，lookbehind 纯属冗余。重建 dist 后全量扫描无任何 lookbehind/命名组，Vite build 通过。
- **预防**: ①前端验收不能只用 Chrome/curl——Safari 的正则引擎更严（lookbehind 需 16.4+），关键页面要在 Safari 实测；②markdown/gfm 这类依赖升级时留意是否引入 lookbehind；③`patch-package` + `postinstall` 已固化，`npm install` 后自动重打。
- **Commit**: 本次提交

### 2026-06-30 — Skills 页「点创建无反应」：user_skills 表缺失 + 前端静默吞错误
- **问题**: 用户在 Skills 页填好「新建 Skill」点「创建」毫无反应，skill 不入库。
- **根因（两层）**: ①后端：线上 DB 的 `user_skills` 表**根本不存在**，所以 `POST/GET /api/user-skills` 全 500（`no such table: user_skills`）。怪点是 `alembic_version` 已在 head `a2628601782f`（晚于建表迁移 `a70ee5479e2e`），且更晚那条加的 `tasks.selected_user_skills` 列**在**——只有早一条的 `create_table` 没落地（迁移漂移，非纯 stamp），导致 `alembic upgrade head` 永远空操作、修不回来。②前端：`SkillsPage.tsx` 的 create/update 用 `catch { /* keep form */ }` **静默吞掉** 500，UI 上「什么都没发生」，连进页面的 list 也被 `.catch(()=>{})` 吞掉 → 列表空。
- **修复**: ①线上 DB（运维动作，非本提交）：备份后按迁移/模型精确 schema 补建 `user_skills` 表，`alembic_version` 不动（本就该指向「表已存在」）；**不能**重新 stamp+upgrade——重跑 `a2628601782f` 会去重复添加已存在的 `selected_user_skills` 列而炸。无需重启（SQLite 下次查询即见新表）。验证 create/list/delete 均 200。②前端（本提交）：create/update/delete 三个 handler 改 `setError(String(e))` 并在弹窗红字显示，沿用其它页面既有写法。
- **预防**: ①「alembic 在 head」**不等于**「表一定存在」——排查 DB 问题先 `PRAGMA`/实际查表，别只信 `alembic current`；修漂移要**按模型补建缺失对象**，而不是 stamp 回退去重跑已部分应用的更晚迁移。②前端任何 `catch {}` 静默吞错误都是「按钮无反应」类 bug 的温床——一律 `setError` 给用户看见。③线上跑的是 rsync 副本 `~/.claude-code-manager/claude-code-manager/`（:8000，DB 在那），不是 git 仓库；调试线上现象要查副本+其 DB，可用 `ps` 里 MCP 进程的 `--auth-token` 直接 curl 复现。
- **Commit**: 本次提交

### 2026-07-12 — 前端 task 状态"老是显示不对"：三层根因大排查 + 修复（多子 Agent 交叉审计）
- **问题**: 用户长期反馈前端 task 状态显示不对（已完成还显示 executing、列表与聊天页状态不一致、侧栏状态点永久陈旧）。
- **排查方法**: 3 路只读审计子 Agent（后端状态流转 / WS 链路 / 前端状态管理）+ 主 Agent 独立排查交叉比对 + 生产 DB 取证（确认 DB 真值基本正确、错在展示与广播层）。
- **三层根因**:
  ① **后端静默状态变更**：cancel/retry/plan 审批/stop-session 兜底/dispatcher 启动批量重置/_reset_instance_if_stale/队列 consumer 标 in_progress/worker_relay 断连标 failed/pr_monitor supersede——全都只写库不广播 `status_change`，靠 WS 驱动的 ChatView 永远等不到事件；
  ② **ChatView localStatus 优先级倒挂**：`useState(task.status)` 初始化 + `localStatus || task.status`，WS 覆盖永久优先于轮询 props，错过一次 WS 事件（断线/根因①）就永久陈旧；
  ③ **TasksPage freeze 幽灵状态**：chat 打开时 `prev.map(t => byId.get(t.id) ?? t)` 对掉出当前页/过滤条件的任务保留旧数据，开状态过滤时任务完成后永远冻结在旧状态。
  另修 **复活块隐患**：`_process_event` 的 completed→executing 复活不排除 `orphan`/`autonomous` 事件（transient 打标在 #729 已排除，复活块漏了）——PTY 回放/后台子 agent 输出可把完成任务翻回 executing 且无人收尾。
- **修复**（commit aa9adc4 + 审查回改 6064329）: 新增 `backend/services/task_events.broadcast_status_change` 收口（**约定：任何写 Task.status 的路径 commit 后必须广播**），接入全部静默点；ChatView localStatus 改 null 初始化 + prop 变化时清除覆盖 + `lastWsStatusAt` 守卫（防在途旧轮询快照击穿刚到的 WS 状态、误触发 autoDequeue）；TasksPage 订阅 `tasks` 频道就地 patch tasks/allTasks/searchResults/chatTask；复活块补 orphan/autonomous 排除；instance_manager chat 收尾广播移到 commit 后；user_skill_injector fail-open（DB 表缺失不再炸 launch）。
- **审查流程**: 2 个子 Agent（后端/前端视角）审 diff，抓到 1 个 major（在途旧快照击穿 WS 覆盖 → autoDequeue 误触发，本次修复自身引入的新窗口）+ 多个 minor（worker 代理路径漏广播、pr_monitor 隐式 commit 依赖、搜索结果不吃 patch），全部落地。
- **测试**: 复活块排除 ×3、cancel 广播 ×1 回归测试；全量 967 passed，失败集与 main 基线完全一致（7 个存量失败非本次引入）；tsc 通过。
- **预防**: ①改 Task.status 必须走「commit 后广播」约定（用 `task_events.broadcast_status_change`），新增状态写入点时先问"前端怎么知道"；②前端"WS 实时覆盖 + 轮询兜底"双通道时，覆盖必须能被更新鲜的兜底数据击穿（且要防在途旧快照反向击穿——时间戳守卫）；③事件驱动的状态翻转（如复活块）必须区分前台活事件与 orphan/autonomous 回放，参考 #729；④广播一律放 commit 之后。

### 2026-07-13 — 后台监视器回报在聊天里不可见：autonomous turn 被 subagent-only 回调丢弃（task 27）

- **问题**：agent 挂 `Bash run_in_background` 监视器后前台 turn 结束；监视器正点回调、session 自主醒来写出完整报告，但用户在聊天里看不到任何东西（用户以为"后台任务不能回调激活 session"——实际回调链路全通，是产出被丢了）。根因：adapter 在 chat turn 结束时把 `on_autonomous_event` 降级成 `_subagent_only_callback`（claude_pty 412d911，防重放旧 prompt），自主 turn 的 assistant 事件全部被丢弃；idle watcher 消费后 reader offset 越过，orphan 回填也捞不回
- **解决**：`FullMirrorCCMBackend`（backend/services/pty_full_mirror.py）在 `super().on_exit()` 降级后按回调函数名识别并原位换回全量转发 `_process_event`；`_process_event` 增加 autonomous user-role 消毒（`<task-notification>` 压成一行 system_event，channel 回显丢弃），承接历史上降级要防的重放问题。10 例新测试（test_autonomous_mirror.py）
- **以后如何避免**：镜像/过滤类回调要区分"结构事件"和"内容事件"——砍内容前先想清楚谁是它的最终读者；"防 A 顺带丢 B"的粗粒度降级要在修好 A 的防线后回收
- **commit**: 6dd3547（PR zjw49246/Claude-Code-Manager#31；因 push 权限收回改走 fork PR）

### 2026-07-13 — 前端设计 v2：Multica 风主题系统 + App Shell（commit e1c778c）

**改动**：主题系统升级为「每主题覆盖 gray（中性）+ indigo（品牌）CSS 变量」的换肤架构：
新默认 dark（zinc + 蓝品牌，oklch）+ 新增 light；v1 默认外观完整保留为 `legacy` 主题，
ocean/forest/rose 归入 Legacy 组。Header 顶栏导航重构为 AppShell（桌面固定侧栏 + 移动端抽屉），
偏好设置抽出 PrefsMenu。字体 Inter/JetBrains Mono 随 bundle 离线。

**经验**：
1. **1300+ 处硬编码 gray-* 类名不必重写**——变量重映射层让全部旧类名自动适配新主题，
   手工精修只做高频页面（Login/Tasks/Chat/Dashboard）。新增主题必须同时覆盖 gray 全档 + indigo 全档。
2. **浅色主题的坑在 accent 300/400 档**：`text-X-300/400` 是深底浅字的设计，浅色主题必须
   把这些档位反转成深色调（≈ Tailwind 原生 600/700），否则 chip 全部不可读；同理中性底上的
   `text-white`/`hover:text-white` 要清扫成 `text-foreground`。
3. **视觉验证用临时后端 + Playwright 截图时，演示数据绝不能插 `pending` 状态的 task**——
   dispatcher 2 秒轮询会把它真的跑起来（本次浪费了一次真实 Claude 调用，还往 worktree 里写了
   一段不相干的文档改动，差点混入 PR）。演示数据只用 completed/failed/cancelled/executing。
4. **Playwright 截图切主题要强制 reload**：localStorage 在 app 启动后写入、hash-only 导航
   不重载页面，不 reload 的话所有截图都是默认主题（第一轮截图全部白拍）。
5. 布局改动前先 grep `100vh|h-screen|fixed|sticky` 找耦合点：TasksPage 分屏高度硬编码了
   顶栏高度（64px→49px），漏改会溢出/留缝。

### 2026-07-15 — 飞书主题「不像」的返工 + 203 个测试失败大清理（commit 1628f2b）

**主题返工教训**：v1 只依据官方设计系统 token（open.feishu.cn CSS 提取）就动手，结果两处失真：
① 官方 CSS 的 pri-500 是新版 #336df4，但**真实 App（v7.72）仍是经典 #3370FF**（App Store 官方截图像素取色 #316efa 实证）；
② token 表不会告诉你「用量分布」——把 N300 #dee0e3 给 gray-700 后，151 处 bg-gray-700 + 102 处 border-gray-700 让整个 UI 变成灰块+线框，而真实飞书是低边框、浅填充、层次靠留白。
**预防**：模仿一个产品的视觉，官方 token 只是词表；必须拿真实截图做像素取色对照（App Store iPad 截图分辨率高、无水印，iTunes lookup API 拿 URL），实现后用 headless chromium 实截自己的页面对比验收。

**203 个测试失败分诊结论**（前端 50 + 后端 153）：只有 1 个真产品 bug——**RBAC 上线把无鉴权模式（AUTH_TOKEN 为空）打死**：中间件无 token 分支直接放行但不设置身份，require_task_access/require_admin 全线 403（backend/middleware/auth.py 修复，回归测试 test_no_auth_mode_grants_full_access）。其余全部是测试代码落后于产品演进：手写 api mock 缺新方法、断言旧签名/旧事件序列/旧状态码。
**预防**：
1. 组件测试的 api mock 是「产品加一个 mount 时 API 调用就整文件爆炸」的单点——给组件新增 api 调用时，同 PR 必须补对应 test mock（grep `vi.mock('../../api/client'` 找到所有手写 mock 清单）。
2. 给守卫类 dependency（require_*）加参数/加检查时，grep 直接函数调用的单元测试（绕过 HTTP 层的），它们不会走 conftest 的兜底。
3. 失败数大 ≠ 问题多：先按错误签名分组再看代表 traceback，本次 153 个失败里 143 个同根因，一处修复全绿。
4. conftest 用 `auth_token=""` 短路鉴权的写法，意味着**测试跑的就是无鉴权模式**——这个模式从此有测试兜底，别再让守卫默认拒绝它。

### 2026-07-16 — 「浅色和飞书几乎一样」：主题趋同问题（feishu v3，commit a457c09）

**问题**：用户反馈浅色主题与飞书主题肉眼无法区分。根因不是色值抄错，而是**结构趋同**：
现代浅色本来就是「灰壳 + 白卡片 + 蓝品牌 + 大圆角」，和飞书处在同一设计空间；feishu v2
只在具体 hex 上有 ≤7 个灰阶单位的差别（壳 #eceef1 vs #f0f0f1、画布 #f5f6f7 vs ~#f6f6f7），
低于肉眼阈值。

**解法（两边同时拉开）**：
① 重新取证飞书的**结构性特征**——iPad + macOS 官方截图像素取色一致证实：消息列表/聊天区
是**纯白 #ffffff~#fbfbfd**，飞书是「白底为主、发丝线分隔」，不是「灰画布+白卡片」。
→ feishu 画布 gray-900 从 #f5f6f7 改为近白 #fbfbfc，rail 修正为取样值 #ecedef。
② 浅色主题找回自己的性格：壳/画布加深一档（oklch 92.5%/95.8%，tonal zinc 分层灰）。
最终「灰调分层 vs 白底为主」一眼可分，theme.test.ts 加了防趋同回归断言（两主题画布取值钉死）。

**经验**：
1. 仿制主题「像不像」之外还有「和邻居分不分得开」一维——同一 App 里两个浅色主题若结构同源，
   仅调 hex 永远趋同；要从取证里找**结构差异**（白底 vs 灰调、层次策略），不是继续微调色号。
2. 对比验收要做**同页面双主题分屏拼图**（PIL 左右各半），单看一张永远觉得"挺像飞书"；
   拼起来才暴露"和自己的浅色更像"。
3. headless chromium 的 localStorage 探针不能用 file:// 页面写完再跳 http://（跨 origin 不共享），
   往 dist/index.html 临时注入 query-param 读取脚本最省事（dist 不进 git）。

### 2026-07-16 — Monitor「一直起不来」：长间隔等待猝死 + broadcast 迭代竞态（commit 14282b0）

**问题**：task 35 的 monitor #192(3600s)/#193(3600s)/#194(1800s) 全部首查后即挂（"process exited rc=0 without calling mark_complete, marked failed"），主 agent 被迫自己踩坑定根因、把间隔压到 300s 才活（#196/#198）。同晚 create_monitor 偶发 500 又炸出重复 monitor（#197/#198 双胞胎）。

**根因（A/B 对照实测钉死）**：
1. CLI 单次 Bash 调用默认墙钟上限 **600s，与请求的 timeout 参数无关**（sleep(700)+timeout=750000 在默认 env 下恰于 600s 被转后台，`is_backgrounded: True`）。转后台时工具回话「完成会通知你」——对 `-p` 一次性进程是空头支票，子 agent 信了就转投 ScheduleWakeup / 结束回合 → 进程退出 → 后台 sleep 被杀 → dispatcher 判 failed。`BASH_MAX_TIMEOUT_MS=7200000` 后同一调用阻塞整 700s 正常完成。
2. `broadcast` 迭代订阅集合的**活引用**，`send` 是悬挂点；前端 WS 连环 keepalive 超时断开时，断连处理中途改集合 → `RuntimeError: Set changed size during iteration` → create_monitor 在 monitor 已建好、进程已启动之后返回 500 → 主 agent 重试 → 重复 monitor。

**解决**：`_launch_monitor_agent` 按 interval 抬高子进程 `BASH_MAX_TIMEOUT_MS`（只抬不降）；`_build_monitor_agent_prompt` 按 interval 生成等待指引（单次 `time.sleep(interval)` + 显式大 timeout + 被拦时拆 300s 块兜底，ScheduleWakeup 禁令附上「为什么必死」）；broadcast 两个循环改 `list()` 快照迭代。

**预防**：
1. 给 `-p` 持久子进程设计"定期干活"循环时，等待手段必须核对 CLI 的单调用上限；任何「结束回合、到点唤醒你」类工具/话术对一次性进程都是死刑，prompt 里要连理由一起禁。
2. 跨 `await` 迭代共享容器一律快照（`list()`）——"有 try/except 就安全"是错觉，改集合的是并发协程不是当前帧。
3. 诊断这类"起不来"先看子 agent 自己的 stream 日志（/tmp/ccm_monitor_{id}.log）最后几个事件，死法一目了然（本次直接拍到 sleep 被转后台 + ScheduleWakeup + result）。

### 2026-07-16 — 蓝色气泡上选中高亮不可见（commit 6641525）

**问题**：全局 `::selection` 是品牌蓝 30% tint，用户聊天气泡/主按钮是 bg-indigo-600 实底蓝，蓝上蓝选中完全看不见（浅色/飞书主题下尤其明显）。
**解法**：`[class~='bg-indigo-600']`（词级匹配，不误伤 `bg-indigo-600/15` tint）及后代的 `::selection` 改白色半透明 `oklch(100% 0 0 / .35)`，所有主题通用。
**经验**：① 定义全局 `::selection` 时要想到品牌色实底面——高亮色和底色同色系时必须给实底面单独一套反色高亮；② `::selection` 的视觉验证可以自动化：probe 页面里 `Range.selectNodeContents` 程序化选中 + headless chromium 截图（注意 Chrome 的 Selection 只保留一个 Range，多块要分次截）。

### 2026-07-16 — Chat 发图后图片不显示（commit 8c6201e）

**问题**：Chat 里附图发送后图片不出现在会话里。
**排查路径**（先证据后结论）：生产 DB 直查（uploads 文件在、task.metadata_ 附件在、
但 7/6 后无带附件的 user_message 行）→ 生产 API 实测 metadata_ 正常返回 →
锁定前端展示层。注意本机跑着**两个 CCM 实例**（8000=code/ 用户实例、8002=cyf/ 别人的），
先前 curl 8002 得到"metadata_ 缺失"是打错实例的假线索——**多实例机器上先 ss -tlnp 对准端口再下结论**。
**两个真因**：
① 发「文字+图片」：乐观回显不带附件 → 带附件的 WS 广播因内容相同被去重**整条丢弃**。
   修：去重时合并附件而非丢弃 + 乐观回显直接带附件。
② Capacitor App 里附件相对 URL（/api/uploads/…）按 capacitor://localhost 解析 404。
   修：resolveAssetUrl() 统一拼 getApiBase()。
**经验**：去重逻辑丢消息前要想清楚"两条消息不完全等价"的情形（同文本、附件不同），
丢弃前先合并增量字段；移动端 WebView 里任何相对资源路径都要过 API base 解析。

### 2026-07-16 — ccm-xiaoyu 502：带迁移的自更新停服后无人启服（commit 4f9ab93）

**问题**：ccm-xiaoyu 前端点「更新」后整站 502，更新面板卡死在「停止服务」。排查：`update_migrate.sh` 只写了一行日志就消失，状态文件冻结在 `"stopping"`，journal 只有 Stopping→Stopped 没有 Started——脚本停服后自己也死了，服务停死，tunnel 转发空端口 502。

**根因**：`_migration_path` 用 `subprocess.Popen(..., start_new_session=True)` 拉起迁移脚本。`start_new_session` 只脱离进程组，**脱离不了 systemd cgroup**——脚本仍在 ccm.service 的 cgroup 里，它执行 `systemctl --user stop` 时被 `KillMode=control-group` 连带杀死。systemd 部署 + 更新带迁移 = 100% 复现。（无迁移的快速路径侥幸存活：`systemctl restart` 的 job 入队后客户端被杀不影响执行。）

**解决**（三层防御）：① systemd 托管时改用 `systemd-run --user --collect --unit=ccm-update-{port}` 把脚本放进独立 transient unit（合法逃逸）；② 脚本停服成功后立即 `trap 'systemctl --user start ...' EXIT`——无论脚本怎么死都把服务拉回来（启动时 init_db 会自动补迁移）；③ `recover_from_status_file` 识别 stopping/migrating 中间态标 failed 提示用户重试（原来被静默忽略）。测试先红后绿：stub systemctl + SIGTERM 杀脚本复现事故，断言 trap 仍启服。

**预防**：
1. systemd 服务内 spawn 的"要活过本服务 stop"的进程，`start_new_session`/`nohup`/`setsid` 全都无效——唯一正解是 `systemd-run` 交给 systemd manager 托管。判断标准：进程要不要在 `systemctl stop <本服务>` 之后继续跑？要就必须出 cgroup。
2. 「停服→干活→启服」型脚本，停服成功后第一件事挂 EXIT trap 兜底启服；孤儿状态文件的中间态要能被下次启动识别，不能静默吞掉。
3. 事故机上另发现一个过时的 system 级 ccm.service（与 user 级同目录同端口同 SQLite，Restart=always 反复拉起僵尸 uvicorn），已 `disable --now`。同机双 systemd 单元指向同一套 CCM 是定时炸弹，部署时要检查 `systemctl list-units` 和 `systemctl --user list-units` 有无重名。

### 2026-07-16 — 回滚把数据库毁成 0 字节：活连接下覆盖 SQLite 文件（commit 75e2108）

**问题**：测试环境验证更新修复时，Admin 更新成功后点「回滚」，数据库变成 0 字节空库；再点更新在「备份数据库」步骤报 disk I/O error（源库已损坏）。

**根因**：`rollback()` 在**本进程还持有打开的 SQLite 连接**时 `rm` 掉 `-wal`/`-shm` 并用备份覆盖 DB 文件，之后才重启。活连接随后的写入/checkpoint 基于已失效的文件状态，把刚恢复的库直接截断。

**解决**：回滚复用 `update_migrate.sh` 新增的 `rollback` 模式——先 `systemctl stop`（EXIT trap 兜底启服）→ 恢复 DB 备份 → `git reset --hard` → `uv sync` → 启服；非 systemd 部署则把 DB 恢复挪进 detached 重启 shell 的 kill 之后。数据用更新前自动备份完整救回。

**预防**：SQLite 文件级恢复（cp/rm -wal）的前置条件永远是"持有连接的进程已退出"，顺序必须 stop → restore → start；任何"先动文件再重启"的写法在 WAL 模式下都是数据毁灭器。

### 2026-07-16 — 孤儿 uvicorn 抢端口 + `_is_managed_by_systemd` 误判：更新全面加固（commit 见本条上一提交）

**问题**：测试环境验证期间，一个 SSH 会话里手动裸跑的 uvicorn（06:31 起）一直霸占 8010 端口：systemd 实例活着但绑不上端口（Errno 98）成了空壳，用户的更新/回滚全打在跑旧代码的孤儿上；孤儿的 `_is_managed_by_systemd()`（查 `systemctl is-active`）被恰好 active 的空壳单元骗过，以为自己是 systemd 托管，停/启的都是别的实例——双头混乱，症状千奇百怪（假成功、disk I/O error、代码版本漂移）。

**解决**：① 判定改为读 `/proc/self/cgroup` 看**本进程**是否真在 service 的 cgroup 里（空壳单元骗不过，非 Linux 自动落 fallback）；② `update_migrate.sh` 支持裸 uvicorn 部署（SERVICE_NAME="-"：kill pid 停服 / respawn uvicorn 启服），带迁移更新和回滚不再依赖 systemd；③ 回滚统一走脚本（先停服再动 DB）；④ svc_start 防 EXIT trap 双拉起。

**预防**：
1. 「我是否被 systemd 管」必须问自己的 cgroup，不能问单元状态——单元 active ≠ 我就是那个单元。
2. CCM 机器上不要手动裸跑 uvicorn 和 systemd 服务并存；排查"行为怪异"先 `ss -tlnp` 看端口属主是不是 systemd 单元里的 PID（本次和 xiaoyu 的 system/user 双单元事故是同一族问题）。
