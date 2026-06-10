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
