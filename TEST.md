# 测试指南

> **重要：Claude Code 必须自主维护本文件。** 新增功能时同步更新测试，修改代码前先跑测试，修改后再跑一遍确认无回归。

## 快速命令

```bash
# 安装依赖（首次，包含测试依赖）
uv sync --group dev

# 运行全部后端测试
uv run python -m pytest backend/tests/ -v

# 运行单个测试文件
uv run python -m pytest backend/tests/test_task_queue.py -v

# 运行匹配名称的测试
uv run python -m pytest backend/tests/ -k "dequeue" -v

# 前端类型检查
cd frontend && npx tsc --noEmit
```

---

## 自动化测试

### 后端测试（pytest + pytest-asyncio）

测试使用内存 SQLite，不依赖真实数据库或外部服务。

#### `test_task_queue.py` — 任务队列核心逻辑

| 测试 | 验证内容 |
|------|---------|
| `test_create_task` | 创建任务，确认默认值正确（status=pending, priority=0） |
| `test_dequeue_priority_order` | **关键**：P0 先于 P1 先于 P10 出队（数字越小优先级越高） |
| `test_dequeue_fifo_within_same_priority` | 同优先级按创建时间 FIFO |
| `test_dequeue_returns_none_when_empty` | 队列空时返回 None |
| `test_mark_completed` | 标记完成，确认 status 和 completed_at |
| `test_mark_failed` | 标记失败，确认 error_message 存储 |
| `test_mark_status_generic` | 通用状态更新（如 executing、merging） |
| `test_retry_increments_count` | 重试时 retry_count+1，error_message 清空 |
| `test_cancel_task` | 取消 pending 任务 |
| `test_cancel_executing_task` | 取消 executing/merging 状态的任务 |
| `test_delete_conflict_task` | 允许删除 conflict 状态的任务 |
| `test_delete_running_task_rejected` | 禁止删除 in_progress 状态的任务 |
| `test_list_tasks_ordered` | 列表按优先级排序 |
| `test_list_tasks_filter_status` | 按状态筛选 |

#### `test_stream_parser.py` — NDJSON 解析

| 测试 | 验证内容 |
|------|---------|
| `test_empty_line` | 空行返回 None |
| `test_invalid_json` | 非 JSON 返回 parse_error 事件 |
| `test_system_init` | 解析 session_id |
| `test_assistant_message` | 提取助手消息内容 |
| `test_tool_use` | 解析工具调用名称和输入 |
| `test_tool_result` | 解析工具结果 |
| `test_tool_result_error` | 检测错误结果 |
| `test_result_with_cost` | 提取 session_id 和 cost_usd |
| `test_result_is_error` | 检测错误结果事件 |
| `test_content_extraction_*` | 各种 content 格式（string, list, nested） |
| `test_assistant_tool_use_block` | assistant 事件含 tool_use 块 → 正确提取 tool_name/tool_input |
| `test_assistant_thinking_block` | assistant 事件含 thinking 块 → 提取为 thinking 事件 |
| `test_thinking_with_text_field` | thinking 块用 `text` 字段（Opus 4.7+ 兼容） |
| `test_thinking_with_nested_content_blocks` | thinking 块用嵌套 `content` 列表 |
| `test_thinking_encrypted_block` | 仅 signature/data 的加密 thinking → `[encrypted thinking ...]` 标记 |
| `test_thinking_completely_empty_block` | 空 thinking 块 → content 为空字符串 |
| `test_thinking_legacy_field_still_works` | 原 `thinking` 字段仍是首选路径 |
| `test_user_event_tool_result` | type=user 事件 → 映射为 tool_result，提取 tool_output |
| `test_user_event_tool_result_error` | type=user 事件含 is_error → 正确设置错误标记 |
| `test_system_non_init` | system 非 init 子类型 → 映射为 system_event |
| `test_assistant_empty_content_blocks` | assistant 空 content 块 → 默认为 message 事件 |

#### `test_models.py` — ORM 模型

| 测试 | 验证内容 |
|------|---------|
| `test_task_defaults` | Task 所有默认值正确 |
| `test_task_with_project_id` | project_id 外键可正常存储 |
| `test_instance_defaults` | Instance 所有默认值正确 |
| `test_project_defaults` | Project 所有默认值正确 |
| `test_project_no_git_url` | 无 git_url 项目：git_url=None, has_remote=False |
| `test_project_unique_name` | 项目名唯一约束生效 |

#### `test_api_tasks.py` — Task API 端点

| 测试 | 验证内容 |
|------|---------|
| `test_create_task` | POST 创建任务，状态码 201 |
| `test_create_task_with_project_id` | 支持 project_id 创建 |
| `test_list_tasks` | GET 列出全部任务 |
| `test_get_task` | GET 获取单个任务 |
| `test_get_task_not_found` | 404 处理 |
| `test_delete_task` | DELETE 删除任务 |
| `test_cancel_task` | 取消任务 |
| `test_retry_task` | 重试任务 |

#### `test_api_chat_plan.py` — Chat 和 Plan API

| 测试 | 验证内容 |
|------|---------|
| `test_chat_history_not_found` | 不存在的 task 返回 404 |
| `test_chat_history_empty` | 无历史消息返回空数组 |
| `test_chat_history_returns_tool_fields` | 历史消息包含 tool_input 和 tool_output 字段 |
| `test_chat_send_no_session` | 无 session 的 task 发消息返回 400 |
| `test_chat_send_task_not_found` | 不存在的 task 发消息返回 404 |
| `test_chat_send_no_idle_instance` | 所有 instance 都在运行时返回 503 |
| `test_chat_send_task_being_processed` | task 正在被处理时返回 409 |
| `test_chat_send_cwd_uses_last_cwd` | 使用 last_cwd 作为工作目录 |
| `test_chat_send_cwd_not_found` | 工作目录不存在返回 400 |
| `test_plan_approve_not_plan_review` | 非 plan_review 状态 approve 返回 400 |
| `test_plan_reject_not_plan_review` | 非 plan_review 状态 reject 返回 400 |
| `test_plan_approve_success` | plan_review 状态 approve → status=pending, plan_approved=True |
| `test_plan_reject_success` | plan_review 状态 reject → status=cancelled, plan_approved=False |
| `test_plan_approve_not_found` | 不存在的 task approve 返回 404 |
| `test_plan_reject_not_found` | 不存在的 task reject 返回 404 |

#### `test_api_system.py` — 系统 API

| 测试 | 验证内容 |
|------|---------|
| `test_health` | GET /api/system/health → {"status": "ok"} |
| `test_stats_empty` | 无数据时所有计数为 0 |
| `test_stats_with_tasks` | 不同状态 task 计数正确 |
| `test_stats_running_instances` | running 实例计数正确 |

#### `test_api_auth.py` — 认证 API

| 测试 | 验证内容 |
|------|---------|
| `test_login_no_auth_configured` | auth_token="" 时任何请求都通过 |
| `test_login_valid_token` | 正确 token 登录成功 |
| `test_login_invalid_token` | 错误 token 返回 401 |
| `test_login_missing_token_field` | 空 body 返回 422 |

#### `test_api_projects.py` — 项目 API

| 测试 | 验证内容 |
|------|---------|
| `test_list_projects_empty` | 空项目列表 |
| `test_create_project_with_git_url` | 201, has_remote=True |
| `test_create_project_local_no_git_url` | 201, has_remote=False |
| `test_create_project_duplicate_name` | 重复名称 400 |
| `test_get_project` / `test_get_project_not_found` | 获取/404 |
| `test_update_project` / `test_update_project_not_found` | 更新/404 |
| `test_update_project_git_url_sets_has_remote` | 设置 git_url 后 has_remote=True |
| `test_delete_project` / `test_delete_project_not_found` | 删除/404 |
| `test_reclone_success` | re-clone 成功 |
| `test_reclone_local_project_rejected` | 本地项目拒绝 re-clone |

#### `test_api_instances.py` — 实例 API

| 测试 | 验证内容 |
|------|---------|
| `test_list_instances_empty` | 空实例列表 |
| `test_create_instance` / `test_create_instance_custom_model` | 创建实例 |
| `test_create_instance_with_thinking_budget` | 创建时携带 `thinking_budget` 字段 |
| `test_create_instance_default_thinking_budget_is_null` | 不传 `thinking_budget` → 响应为 null |
| `test_run_instance_forwards_thinking_budget` | `/run` 把 instance 的 budget 传给 `launch()` |
| `test_get_instance` / `test_get_instance_not_found` | 获取/404 |
| `test_delete_instance` / `test_delete_instance_not_found` | 删除/404 |
| `test_stop_instance_success` / `test_stop_instance_not_running` | 停止/非运行 |
| `test_run_with_prompt` / `test_run_with_task_id` | 运行实例 |
| `test_run_already_running` / `test_run_no_prompt_no_task` | 运行异常 |
| `test_get_logs` | 获取日志 |
| `test_dispatcher_status/start/stop` | 调度器控制 |
| `test_ralph_start/stop/status` | Ralph Loop 控制 |

#### 服务层单元测试

##### `test_service_ws_broadcaster.py` — WebSocket 广播

| 测试 | 验证内容 |
|------|---------|
| `test_subscribe` / `test_subscribe_multiple_channels` | 订阅单/多频道 |
| `test_unsubscribe` / `test_unsubscribe_cleans_empty_channels` | 取消订阅 + 清理空频道 |
| `test_broadcast_sends` | 广播消息到所有订阅者 |
| `test_broadcast_removes_dead_connections` | 自动移除断开连接 |
| `test_broadcast_no_subscribers` | 无订阅者不报错 |

##### `test_service_whisper_client.py` — Whisper 客户端

| 测试 | 验证内容 |
|------|---------|
| `test_transcribe_success` | 正常转录成功 |
| `test_transcribe_no_api_key` | 无 API key 报 ValueError |
| `test_transcribe_wav` / `test_transcribe_mp3` | 不同音频格式 |
| `test_transcribe_api_error` | API 错误抛 HTTPStatusError |

##### `test_service_instance_manager.py` — 实例管理器

| 测试 | 验证内容 |
|------|---------|
| `test_launch_creates_subprocess` | 启动子进程，正确参数 |
| `test_launch_with_resume` / `test_launch_with_model` | resume/model 参数 |
| `test_launch_updates_db` / `test_launch_saves_cwd` | DB 状态更新 |
| `test_launch_unsets_claude_env` | 排除 CLAUDECODE 环境变量 |
| `test_launch_with_thinking_budget_sets_env` | `thinking_budget>0` → 设置 `MAX_THINKING_TOKENS` env |
| `test_launch_without_thinking_budget_omits_env` | 默认不设置 `MAX_THINKING_TOKENS` |
| `test_launch_with_zero_thinking_budget_omits_env` | `thinking_budget=0` 视为无预算 |
| `test_stop_terminates` / `test_stop_kills_on_timeout` | 正常停止/超时 kill |
| `test_is_running` | 运行状态检测 |

##### `test_service_worktree_manager.py` — Worktree 管理器

| 测试 | 验证内容 |
|------|---------|
| `test_create_success` | 创建 worktree + DB 记录 |
| `test_create_fetch_fails_continues` | fetch 失败继续创建 |
| `test_create_origin_branch_missing_fallback` | origin 分支不存在时回退 |
| `test_sync_latest_success` / `test_sync_latest_conflict` | 同步成功/冲突 |
| `test_merge_to_main_success` / `test_merge_to_main_conflict` | 合并成功/冲突 |
| `test_remove_worktree` | 删除 worktree + DB 更新 |

##### `test_service_backup.py` — 数据库备份服务

| 测试 | 验证内容 |
|------|---------|
| `TestBuildDestination::test_local_ok` | local 目标 dict 字段正确 |
| `TestBuildDestination::test_local_empty_path_returns_none` | local 路径为空时返回 None（禁用备份） |
| `TestBuildDestination::test_s3_ok` | S3 目标 dict 包含 bucket/region/access_key/secret_key |
| `TestBuildDestination::test_s3_missing_bucket_returns_none` | S3 缺少 bucket 时返回 None |
| `TestBuildDestination::test_oss_ok` | OSS 目标 dict 包含 endpoint/bucket/access_key/secret_key |
| `TestBuildDestination::test_oss_missing_endpoint_returns_none` | OSS 缺少 endpoint 时返回 None |
| `TestBuildDestination::test_oss_missing_bucket_returns_none` | OSS 缺少 bucket 时返回 None |
| `TestBuildDestination::test_unknown_type_returns_none` | 未知类型返回 None |
| `TestResolveDbPath::test_strips_async_prefix` | 去掉 `sqlite+aiosqlite:///` 前缀 |
| `TestResolveDbPath::test_strips_sync_prefix` | 去掉 `sqlite:///` 前缀 |
| `TestResolveDbPath::test_absolute_path_unchanged` | 绝对路径正确解析 |
| `TestStart::test_local_starts_scheduler` | 调用 add_task + start，interval/max_copies 正确 |
| `TestStart::test_returns_false_when_destination_not_configured` | 目标未配置时返回 False，不实例化 AutoBackup |
| `TestStart::test_s3_passes_correct_destination` | S3 目标 dict 传给 add_task |
| `TestStart::test_oss_passes_correct_destination` | OSS 目标 dict 传给 add_task |
| `TestStart::test_custom_interval_and_max_copies` | 自定义 interval/max_copies 生效 |
| `TestStop::test_stop_calls_backup_stop` | stop() 调用底层 stop()，清空 _backup |
| `TestStop::test_stop_without_start_is_safe` | 未 start 时 stop() 不报错 |
| `TestStop::test_stop_idempotent` | 重复 stop() 只调用一次底层 stop() |

##### `test_service_ralph_loop.py` — Ralph Loop 生命周期

| 测试 | 验证内容 |
|------|---------|
| `test_start_creates_task` / `test_start_idempotent` | 启动/幂等性 |
| `test_stop_cancels` | 停止取消任务 |
| `test_is_running_true` / `test_is_running_false` | 运行状态检测 |

##### `test_service_dispatcher.py` — 全局调度器

| 测试 | 验证内容 |
|------|---------|
| `test_status_not_running` | 初始状态 running=False |
| `test_start_sets_running` / `test_start_idempotent` | 启动/幂等性 |
| `test_stop` | 停止并取消所有任务 |
| `test_ensure_instances_creates_workers` | 自动创建 worker 实例 |
| `test_ensure_instances_skips_if_enough` | 已有足够实例时跳过 |
| `test_lifecycle_success` | 完整成功生命周期 |
| `test_lifecycle_failure_retry` / `test_lifecycle_failure_max_retries` | 失败重试/达到上限 |
| `test_lifecycle_exception` | 异常标记 task failed |
| `test_plan_phase` | plan 模式进入 plan_review |

### 前端检查

| 检查 | 命令 | 说明 |
|------|------|------|
| TypeScript 类型检查 | `cd frontend && npx tsc --noEmit` | 确认无类型错误 |
| 构建检查 | `cd frontend && npm run build` | 确认生产构建成功 |

---

## 人机协作测试

> **流程：人在浏览器操作 UI → 告诉 Claude Code 做了什么 → Claude Code 查库/查日志/查 git 验证结果。**
>
> 人负责操作和观察 UI，Claude Code 负责查数据确认后端状态是否正确。两者配合完成验证。

### 测试 1：启动与调度器

| 步骤 | 谁 | 做什么 |
|------|-----|--------|
| 1 | 人 | 启动后端 `uvicorn backend.main:app --reload` |
| 2 | AI | 查 DB 确认 worker instances 已自动创建：`SELECT * FROM instances` |
| 3 | 人 | 打开 Dashboard，观察 instances 列表是否显示 worker |
| 4 | 人 | 点击「Stop Dispatcher」按钮 |
| 5 | AI | 调用 `GET /api/dispatcher/status` 确认 `running: false` |
| 6 | 人 | 点击「Start Dispatcher」按钮 |
| 7 | AI | 再次确认 `running: true` |

### 测试 2：项目管理

| 步骤 | 谁 | 做什么 |
|------|-----|--------|
| 1 | 人 | 在 TaskForm 选 "+ New project"，输入项目名 + 有效 git URL，创建任务 |
| 2 | AI | 查 DB 确认 project 和 task 都创建了，project status 从 `pending` → `cloning` → `ready`，CLAUDE.md 已生成 |
| 3 | 人 | 再次创建任务，选 "+ New project"，只输入项目名（不填 URL） |
| 4 | AI | 查 DB 确认 project.has_remote=False，目录已 git init，CLAUDE.md 已生成 |
| 5 | 人 | 创建一个同名 Project |
| 6 | 人 | 确认 UI 提示错误（400） |

### 测试 3：任务创建与执行

| 步骤 | 谁 | 做什么 |
|------|-----|--------|
| 1 | 人 | 在 TaskForm 下拉选择一个 ready 的 Project，填写标题和 Prompt，创建任务 |
| 2 | AI | 查 DB 确认 task 的 project_id 正确，target_repo 为空（等 dispatcher 填充） |
| 3 | 人 | 观察 TaskList，确认任务状态从 pending → executing（蓝色闪烁） |
| 4 | AI | 查 DB 确认 task.status = `executing`，instance_id 已分配，target_repo 已填充为项目路径 |
| 5 | 人 | 等任务执行完，观察状态变为 completed（绿色） |
| 6 | AI | 查 DB 确认 task.status = `completed` |

### 测试 4：优先级调度

| 步骤 | 谁 | 做什么 |
|------|-----|--------|
| 1 | 人 | 先停 Dispatcher |
| 2 | 人 | 创建 3 个任务：P5、P0、P3 |
| 3 | 人 | 启动 Dispatcher |
| 4 | AI | 查 DB 确认第一个变为 in_progress 的是 P0 的任务 |
| 5 | 人 | 在 TaskList 上确认 P0 最先显示执行状态 |

### 测试 5：Git 工作流验证

| 步骤 | 谁 | 做什么 |
|------|-----|--------|
| 1 | 人 | 创建一个简单任务（如 "在 README 末尾加一行注释"） |
| 2 | 人 | 等待任务完成 |
| 3 | AI | 在项目 repo 中执行 `git log --oneline -5` 确认有新 commit 并已 push 到 main |
| 4 | AI | 执行 `git worktree list` 确认 worktree 已被清理 |
| 5 | AI | 执行 `git branch` 确认 task 分支已被删除 |

### 测试 6：并发控制（原测试 7）

| 步骤 | 谁 | 做什么 |
|------|-----|--------|
| 1 | 人 | 一次性创建 10 个任务 |
| 2 | 人 | 观察同时 executing 的任务数量 |
| 3 | AI | 查 DB `SELECT COUNT(*) FROM instances WHERE status='running'`，确认不超过 MAX_CONCURRENT_INSTANCES |

### 测试 7：前端 UI 状态

| 步骤 | 谁 | 做什么 |
|------|-----|--------|
| 1 | 人 | 打开 Dashboard，截图统计栏 |
| 2 | AI | 查 `GET /api/system/stats` 对比统计数字是否一致 |
| 3 | 人 | 在 TaskForm 选择已有项目 → 确认正常 |
| 4 | 人 | 选 "+ New project" → 确认展开项目名称和 Remote URL 输入框 |
| 5 | 人 | 观察 TaskList 各状态颜色：pending 黄、executing 蓝闪、completed 绿、failed 红 |

### 测试 8：兼容性

| 步骤 | 谁 | 做什么 |
|------|-----|--------|
| 1 | 人 | 创建一个 Plan Mode 任务 → 确认进入 plan_review（紫色） |
| 2 | 人 | 点击 Approve → 确认任务重新入队执行 |
| 3 | 人 | 任务完成后点 Chat 按钮 → 发送追问消息 |
| 4 | AI | 查 DB 确认 task.session_id 存在，`--resume` 会被使用 |
| 5 | 人 | 测试语音按钮 → 确认录音转文字填入输入框 |

### 测试 9：Monitor Session

| 步骤 | 谁 | 做什么 |
|------|-----|--------|
| 1 | AI | `pytest backend/tests/test_monitor_session.py -v` — 验证 Model CRUD、API 权限、取消/删除清理、服务重启清理 |
| 2 | 人 | 创建 Auto 模式任务 → 在 Chat 页点「监控列表」按钮 → 点「新建监控」→ 填写描述 → 创建 |
| 3 | 人 | 确认监控列表显示新创建的 session，状态为 running |
| 4 | 人 | 点击 session 进入详情 → 确认检查记录按时间倒序显示 |
| 5 | 人 | 删除 manual monitor → 确认状态变为 cancelled |
| 6 | 人 | Loop 模式任务页面 → 确认监控列表按钮存在但无「新建监控」按钮 |
| 7 | AI | 取消 task → 确认关联的 monitor session 全部变为 cancelled |
| 8 | AI | 删除 task → 确认 MonitorSession 和 MonitorCheck 无孤儿数据 |

### AI 验证命令速查

测试时 Claude Code 常用的验证命令：

```bash
# 查任务状态
sqlite3 claude_manager.db "SELECT id, title, status, priority, project_id, instance_id, merge_status FROM tasks ORDER BY id"

# 查实例状态
sqlite3 claude_manager.db "SELECT id, name, status, current_task_id, pid FROM instances"

# 查项目状态
sqlite3 claude_manager.db "SELECT id, name, status, local_path FROM projects"

# 查调度器
curl -s -H "Authorization: Bearer $AUTH_TOKEN" http://localhost:8000/api/dispatcher/status | python -m json.tool

# 查 git 状态（在项目目录下）
git log --oneline -5
git worktree list
git branch

# 查后端日志（看 dispatcher 行为）
# 启动时加 --log-level debug 或查看终端输出
```

---

## 开发规范

### Claude Code 开发时必须遵守：

1. **改代码前先跑测试**：`uv run python -m pytest backend/tests/ -v`，确认基线全绿
2. **改代码后再跑测试**：确认无回归，新增功能需要对应新增测试
3. **前端改动后检查类型**：`cd frontend && npx tsc --noEmit`
4. **新增 service/model/API 时**：在对应 test 文件中添加测试用例
5. **修 bug 时**：先写一个复现 bug 的测试（红），修复后确认测试变绿
6. **更新本文件**：新增测试后同步更新 TEST.md 的测试表格

### 测试文件对应关系

| 源文件 | 测试文件 |
|--------|---------|
| `backend/services/task_queue.py` | `backend/tests/test_task_queue.py` |
| `backend/services/stream_parser.py` | `backend/tests/test_stream_parser.py` |
| `backend/models/*.py` | `backend/tests/test_models.py` |
| `backend/api/tasks.py` | `backend/tests/test_api_tasks.py` |
| `backend/api/chat.py` + `backend/api/tasks.py` (plan) | `backend/tests/test_api_chat_plan.py` |
| `backend/api/system.py` | `backend/tests/test_api_system.py` |
| `backend/api/auth.py` | `backend/tests/test_api_auth.py` |
| `backend/api/projects.py` | `backend/tests/test_api_projects.py` |
| `backend/api/instances.py` | `backend/tests/test_api_instances.py` |
| `backend/services/dispatcher.py` | `backend/tests/test_service_dispatcher.py` |
| `backend/services/worktree_manager.py` | `backend/tests/test_service_worktree_manager.py` |
| `backend/services/instance_manager.py` | `backend/tests/test_service_instance_manager.py` |
| `backend/services/ralph_loop.py` | `backend/tests/test_service_ralph_loop.py` |
| `backend/services/ws_broadcaster.py` | `backend/tests/test_service_ws_broadcaster.py` |
| `backend/services/whisper_client.py` | `backend/tests/test_service_whisper_client.py` |
| `backend/services/backup_service.py` | `backend/tests/test_service_backup.py` |
| `backend/services/token_manager_service.py` | `backend/tests/test_service_token_manager.py` |
| `backend/api/monitor.py` | `backend/tests/test_monitor_session.py` |
| `backend/models/monitor_session.py` | `backend/tests/test_monitor_session.py` |
| `frontend/src/**` | TypeScript 类型检查 (`tsc --noEmit`) |
