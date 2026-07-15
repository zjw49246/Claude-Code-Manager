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

#### `test_autonomous_mirror.py` — PTY autonomous turn 全量镜像

| 测试 | 验证内容 |
|------|---------|
| `test_task_notification_becomes_system_event` | autonomous user `<task-notification>` 压成一行 system_event 入库+广播 |
| `test_channel_echo_dropped` | autonomous user channel 回显直接丢弃（防重放旧 prompt） |
| `test_non_autonomous_user_event_unchanged` | 非 autonomous user 事件维持原行为（orphan 回填不受影响） |
| `test_autonomous_assistant_message_logged_and_unread` | 自主 turn 的 assistant 产出入库 + has_unread + task 频道广播 |
| `test_restore_replaces_subagent_only` | on_exit 后降级回调被换回全量转发 |
| `test_mirror_forwards_to_process_event` | 镜像回调转发 event.to_dict() 给 _process_event |
| `test_mirror_swallows_process_event_errors` | 镜像回调异常不外抛（不打断 idle watcher） |
| `test_restore_skips_fresh_binding` | 轮换 relaunch 的新绑定 _on_autonomous 不被覆盖 |
| `test_restore_skips_none_session` | session 缺失时安全跳过 |
| `test_init_wires_full_mirror_backend` | use_pty_mode 开启时 IM 构造即接线 FullMirrorCCMBackend |

#### `test_native_sub_agents.py` — 原生子 Agent 接入（通用 sub_agent 表）

| 测试 | 验证内容 |
|------|---------|
| `test_generic_model_defaults` | SubAgentSession 默认 agent_type=monitor / source=ccm |
| `test_native_agent_record` | native-agent 记录 + meta JSON（tool_use_id） |
| `test_legacy_aliases_still_work` | MonitorSession/MonitorCheck 别名 + monitor_session_id synonym 兼容 |
| `test_spawn_progress_done_lifecycle` | spawn→progress→done 生命周期 + 去重 + sub_agent_* 广播 |
| `test_progress_for_unknown_agent_is_noop` | 未注册 tool_use_id 的 progress 为 no-op |
| `test_missing_tool_use_id_ignored` | 无 tool_use_id 不入库 |
| `test_summary_groups_by_agent_type` | /sub-agents/summary 按 agent_type 分组，running/completed 恒存在 |

> PTY 侧 turn 对齐与子 agent 观测的测试在 PTY 仓库 `tests/test_turn_alignment.py`
>（task87 错位回归：backlog orphan、in-flight turn_duration 不结束新 turn、
> 空闲 watcher 消费自主 turn、挂起子 agent 的 session 不被驱逐）。

#### `test_permission_relay.py` — PTY 权限透传

| 测试 | 验证内容 |
|------|---------|
| `test_permission_request_logged_and_broadcast` | 权限请求 → LogEntry + WS 卡片事件 + pending 登记 |
| `test_resolve_permission_roundtrip` | allow 回包 bridge + resolved 广播 + 二次回包幂等失败 |
| `test_resolve_not_delivered_no_broadcast` | bridge 送达失败不落库不广播（防误标已允许） |
| `test_resolve_unknown_or_expired` | 未知/过期 request 返回 False |
| `test_permission_endpoint_*` | API：200 / 410 过期 / 400 非法 behavior / 404 任务不存在 |

#### `test_ask_user.py` — 拦截内置 AskUserQuestion → 前端卡片

| 测试 | 验证内容 |
|------|---------|
| `test_registry_create_resolve_roundtrip` | registry 登记 future → resolve set 答案 → await 拿到；list_for_task 过滤 task |
| `test_registry_resolve_unknown_and_double` | 未知 request_id / 已完成 future 二次 resolve 均返回 False |
| `test_registry_discard_and_list_excludes_done` | discard 移除；已 resolve（future done）的从 pending 列表排除 |
| `test_format_answer_reason_*` | 喂回模型的 deny reason 文案：单选 / 多选 / 自定义文本 / 缺答兜底 |
| `test_inject_adds_hook_and_is_idempotent` | hook 合并进 settings.json，保留既有 key 与他人 hook，重复注入不重复 |
| `test_disable_removes_our_hook_only` | `ask_user_enabled=False` 时只移除我们的项，不动他人 hook |
| `test_inject_handles_corrupt_settings` | 损坏 JSON 的 settings.json 不报错、照常注入 |
| `test_inject_creates_missing_dir` | config_dir 不存在时自动建目录 + 写入 |

> 完整 HTTP+claude 回环（模型调用 AskUserQuestion → hook 阻塞 → 提交答案 → 模型续答）由真实环境集成测试验证，见 PROGRESS.md「ask_user」条目。

#### `test_api_monitor.py` — Monitor API 端点

| 测试 | 验证内容 |
|------|---------|
| `test_create_monitor_session` | POST 创建 monitor session，状态码 200 |
| `test_create_monitor_no_skill` | enabled_skills 无 monitor 时 → 403 |
| `test_create_monitor_task_not_found` | task 不存在 → 404 |
| `test_create_monitor_task_completed` | task 已完成 → 400 |
| `test_create_monitor_concurrency_limit` | 超过 5 个并发 monitor → 429 |
| `test_list_monitor_sessions` | GET 列出 task 下所有 monitor sessions |
| `test_get_monitor_session` | GET 获取单个 monitor session |
| `test_get_monitor_session_not_found` | 404 处理 |
| `test_delete_monitor_session` | DELETE 停止 monitor session |
| `test_get_monitor_checks` | GET 获取 monitor 检查历史 |
| `test_task_delete_cleans_monitors` | task 删除 → MonitorCheck 和 MonitorSession 全部清理 |
| `test_task_cancel_cancels_monitors` | task 取消 → 所有 running monitor 变为 cancelled |

#### `test_api_pr_monitor.py` — PR Monitor API（CRUD + GitHub Webhook）

| 测试 | 验证内容 |
|------|---------|
| `test_create_repo_success` / `test_create_repo_duplicate` / `test_create_repo_invalid_format` | 创建仓库成功（detail 返回完整 secret）/ 重复 → 409 / 非 `owner/repo` 格式 → 422 |
| `test_list_repos_masks_secret` | 列表响应 secret 被掩码（前 4 位 + `***`） |
| `test_update_repo_settings` / `test_update_repo_not_found` | 更新 auto_merge/branch/authors / 404 |
| `test_toggle_repo` / `test_regenerate_secret` / `test_delete_repo` | 启停切换 / 重新生成 secret / 删除（级联清理 reviews） |
| `test_webhook_info_configured` / `test_webhook_info_unconfigured` | PUBLIC_BASE_URL 设置时返回 webhook URL，否则 null |
| `test_webhook_valid_signature_creates_review_and_task` | 合法 HMAC 签名 → 创建 PRReview + Task |
| `test_webhook_invalid_signature_rejected` / `test_webhook_missing_signature_rejected` | 签名错误/缺失 → 403 |
| `test_webhook_unknown_repo_ignored` / `test_webhook_disabled_repo_ignored` | 未监控/已禁用仓库忽略 |
| `test_webhook_non_pull_request_event_ignored` / `test_webhook_draft_pr_ignored` / `test_webhook_wrong_base_branch_ignored` / `test_webhook_author_not_allowed_ignored` | 各类过滤条件忽略 |
| `test_webhook_duplicate_opened_ignored_while_in_progress` | 进行中重复 opened 事件去重 |
| `test_webhook_synchronize_supersedes_old_review` | synchronize 将旧 review 标记 superseded 并新建 |

#### `test_mcp_server.py` — MCP Server 工具

| 测试 | 验证内容 |
|------|---------|
| `test_mcp_server_tools_registered` | MCP server 启动，3 个 tool 正确注册 |
| `test_api_url` | API URL 拼接正确 |
| `test_create_monitor_success` | create_monitor → HTTP POST 成功 |
| `test_check_monitors_returns_sessions` | check_monitors → HTTP GET 返回状态 |
| `test_check_monitors_empty` | 无 monitor 时返回空列表 |
| `test_stop_monitor_success` | stop_monitor → HTTP DELETE 成功 |
| `test_create_monitor_api_error` | API 不可达 → `{"success": false}` |
| `test_check_monitors_api_error` | API 不可达 → `{"success": false}` |
| `test_stop_monitor_api_error` | API 不可达 → `{"success": false}` |

#### `test_monitor_models.py` — Monitor 数据层

| 测试 | 验证内容 |
|------|---------|
| `test_monitor_session_crud` | MonitorSession CRUD 操作 |
| `test_monitor_check_crud` | MonitorCheck CRUD 操作 |
| `test_monitor_session_defaults` | MonitorSession 默认值正确 |
| `test_enabled_skills_json_field` | enabled_skills JSON 字段读写 |
| `test_enabled_skills_none` | enabled_skills 为 None 时正常 |
| `test_enabled_skills_multiple` | 多 skill 的 JSON 读写 |
| `test_multiple_checks_per_session` | 单 session 多次 check 记录 |

#### `test_mcp_config.py` — MCP Config 生成

| 测试 | 验证内容 |
|------|---------|
| `test_generate_mcp_config_none_skills` | enabled_skills 为 None → 返回 None |
| `test_generate_mcp_config_empty_skills` | 空 dict → 返回 None |
| `test_generate_mcp_config_no_matching_skills` | 无匹配 skill → 返回 None |
| `test_generate_mcp_config_monitor_enabled` | monitor: true → 生成包含 ccm_skills server 的配置 |
| `test_generate_mcp_config_file_path` | 配置文件路径格式正确 |
| `test_cleanup_mcp_config` | 正确清理临时文件 |
| `test_cleanup_mcp_config_missing_file` | 文件不存在时不报错 |

> **注意**: `generate_monitor_agent_mcp_config()` 和 `cleanup_monitor_agent_mcp_config()` 为子 Agent 专用 MCP 配置生成/清理函数，目前通过集成测试验证（见下方「子 Agent 系统集成测试」）。

#### `test_monitor_dispatcher.py` — Monitor Dispatcher 生命周期

| 测试 | 验证内容 |
|------|---------|
| `test_build_monitor_prompt` | prompt 构建包含描述和上下文 |
| `test_build_monitor_prompt_no_context` | 无上下文时 prompt 正常 |
| `test_start_monitor_session` | 启动 monitor session 创建 asyncio task |
| `test_lifecycle_max_checks_reached` | max_checks 耗尽 → completed |
| `test_lifecycle_task_ended` | task 结束 → monitor 联动结束 |
| `test_lifecycle_subprocess_timeout` | 子进程超时 → failed check → 继续 |
| `test_lifecycle_subprocess_crash` | 子进程崩溃 → failed check → 继续 |
| `test_lifecycle_cancelled` | CancelledError → kill 子进程 |
| `test_lifecycle_done_status` | STATUS: done → completed |
| `test_lifecycle_unexpected_exception_marks_failed` | 未预期异常 → failed |
| `test_lifecycle_writes_check_record` | check 结果写入 DB |
| `test_lifecycle_broadcasts_check_event` | check 结果广播 WebSocket |

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
| `test_process_event_sets_transient_flag_on_overload_error` | 带 `is_error` 的瞬时 429/过载事件置 turn-scoped 标记（PTY 下 exit_code=0 仍可重试的关键信号） |
| `test_process_event_usage_limit_does_not_set_transient_flag` | 额度横幅**不**置标记（应走换号而非同号重试） |
| `test_process_event_clean_event_leaves_flag_unset` / `test_launch_resets_transient_flag` | 干净事件不置位 / 新 `launch()` 重置标记 |
| `test_process_event_orphan_overload_does_not_set_transient_flag` | resume 回放的旧 api_error（`orphan`）与后台子 agent 报错（`autonomous`）**不**置标记——否则成功 resume 被误判 failed（task #729 recover-then-failed） |

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

##### `test_service_pr_review.py` — PR 审核服务

| 测试 | 验证内容 |
|------|---------|
| `test_build_review_prompt_auto_merge_on` / `..._off` | auto_merge 开关影响 prompt（是否含 `gh pr merge`） |
| `test_create_pr_review_task_happy_path` | 创建 PRReview + Task 并广播 `review_created` |
| `test_create_pr_review_task_broadcast_failure_logged_not_raised` | 广播失败 → logger.warning，不中断流程 |
| `test_check_and_update_review_merged` / `..._approved` / `..._changes_requested` | gh 状态映射 merged/approved/commented |
| `test_check_and_update_review_skips_terminal_status` | 终态 review 不再调用 gh |
| `test_check_and_update_review_auth_error_no_retry` | gh 认证错误（HTTP 401 等）→ 不重试，error 信息提示 `gh auth login` |
| `test_check_and_update_review_transient_failure_retried_then_error` / `..._retry_succeeds` | 瞬时失败重试一次（失败→error / 成功→正常） |
| `test_gh_pr_view_*` | subprocess mock：成功解析 / 401 → auth 分类 / 网络错误 → transient / spawn 失败包装为 GhError |

### 子 Agent 系统集成测试

> 以下功能通过启动开发服务（`./start-dev.sh`，端口 8003）进行端到端集成验证，暂无独立单元测试文件。

#### 子 Agent MCP Server (`ccm_monitor_agent_server.py`)

| 验证项 | 说明 |
|--------|------|
| MCP 进程启动 | `claude --mcp-config` 启动子 Agent 进程，进程正常运行 |
| `report_status` tool | 子 Agent 调用 → POST `/checks` → DB MonitorCheck 记录 + WebSocket 广播 |
| `mark_complete` tool | 子 Agent 调用 → POST `/complete` → session 状态变为 completed，进程自行退出 |
| `get_context` tool | 子 Agent 调用 → GET session 信息，返回 description/context/checks_done |

#### 子 Agent API (`sub_agents.py`)

| 验证项 | 说明 |
|--------|------|
| `GET /api/tasks/{id}/sub-agents/summary` | 返回 `by_type.monitor` 的 running/completed 计数 |
| task 不存在 | 返回 404 |

#### Monitor API 新增端点 (`monitor.py`)

| 验证项 | 说明 |
|--------|------|
| `POST /{session_id}/checks` | 子 Agent 报告状态，创建 MonitorCheck 记录，WebSocket 广播 |
| `POST /{session_id}/complete` | 子 Agent 标记完成，session 状态更新，WebSocket 广播 |
| checks_done >= max_checks | 自动标记 session 为 completed |

#### Dispatcher 子 Agent 生命周期

| 验证项 | 说明 |
|--------|------|
| `_launch_monitor_agent()` | 构建 Claude CLI + MCP config 命令，启动持久子进程 |
| `_build_monitor_agent_prompt()` | Agent 风格 prompt 包含监控目标、上下文、MCP 工具说明 |
| `_monitor_session_lifecycle()` | 启动子进程 → wait → 检查状态 → 清理（MCP config + 日志） |
| `stop_monitor` → 进程 kill | delete_monitor_session 终止子进程 + 清理 MCP 配置文件 |

#### 前端子 Agent UI

| 验证项 | 说明 |
|--------|------|
| 工具权限按钮 (Wrench) | `enabled_skills` 有启用项时显示，点击展开已启用 skill 列表 |
| 子 Agent 徽章 (Users) | `active_sub_agents > 0` 时显示计数 + pulse 动画 |
| 子 Agent 详情展开 | 点击徽章调用 summary API，按类型显示 running/completed 计数 |

### 时区与时间戳测试

#### 后端 — 时间戳 UTC 序列化

| 测试文件 | 测试用例 | 说明 |
|----------|---------|------|
| `test_task_schema.py` | `test_naive_created_at_serialized_with_utc_suffix` | 无时区 datetime 序列化为 `+00:00` |
| `test_task_schema.py` | `test_aware_created_at_preserved` | 已有时区的 datetime 保留 UTC 标记 |
| `test_task_schema.py` | `test_started_at_none_serialized_as_none` | None 保持 None |
| `test_task_schema.py` | `test_started_at_naive_gets_utc_suffix` | started_at 同样加 UTC |
| `test_task_schema.py` | `test_completed_at_naive_gets_utc_suffix` | completed_at 同样加 UTC |
| `test_task_schema.py` | `test_all_three_timestamps_have_utc` | 三个时间字段全部含 UTC 后缀 |
| `test_chat_timestamp.py` | `test_chat_history_timestamp_has_z_suffix` | 聊天历史时间戳带 Z 后缀 |
| `test_chat_timestamp.py` | `test_chat_history_null_timestamp` | 空时间戳返回 None |

#### 前端 — 时区转换与显示 (`timezone.test.ts`)

| 测试用例 | 说明 |
|---------|------|
| `treats naive timestamp (no Z) as UTC` | 无 Z 后缀的时间戳按 UTC 解析 |
| `naive timestamp converts correctly to non-UTC timezone` | 无 Z 后缀正确转换到用户时区 |
| `naive timestamp with microseconds is handled` | 含微秒的无 Z 后缀时间戳正常处理 |
| `timestamp with positive/negative offset is preserved` | 已有偏移量的时间戳不被二次转换 |
| `formatDateTime always includes date even for today` | 通用格式化始终包含日期 |
| `formatDateTime shows YYYY prefix for different year` | 不同年份显示完整年月日 |
| `formatDateTime treats naive timestamp as UTC` | formatDateTime 同样按 UTC 解析 |
| `formatDateTime converts UTC to user timezone` | 正确将 UTC 转为用户选定时区 |

#### Claude Pool (`test_claude_pool.py`)

| 测试用例 | 说明 |
|---------|------|
| `TestRateLimitDetection` / `TestAuthFailureDetection` / `TestPoolRotatable` | 限速/认证失败文案检测（窄正则，含中英文与各时区变体） |
| `TestTransientOverloadDetection` | **瞬时 429/过载检测**：命中 Anthropic 官方文案 `Server is temporarily limiting requests (not your usage limit)` / overloaded；与「额度用尽/认证失败」互斥（那些走换号）；无误报 |
| `TestTransientRetryDelay` | 退避计算：首次≈base、指数增长、封顶 cap、最小 1s |
| `TestClaudePool` | 账号加载、select 轮转、冷却标记/过期/清除、status 汇总 |
| `TestSessionMigration` | session JSONL 硬链接迁移（成功/已链接/缺文件/inode 冲突） |
| `TestChatPoolRotationRegression` | **回归**：chat 路径切号必须成功并迁移 session（曾因位置参数调用 keyword-only 的 `migrate_session` 静默失败） |
| `TestLocateSessionConfigDir` | 在所有账号目录中定位 session 实际所在的 config_dir |
| `TestSelectAsync` | `select_async` 在线程中执行，不阻塞事件循环 |
| `TestProbeEnvCleanup` | 探测子进程 env 必须剔除 `CLAUDECODE` / `CLAUDE_CODE` |
| `TestFetchUsage` | OAuth usage API 额度查询：正常返回、凭据缺失、token 过期、60s 缓存 |

**Pool 额度抽屉（手动测试）**：`POOL_ENABLED=true` 时 Header 左侧出现 "Pro" 徽标 → 点击打开抽屉 → 每个账号显示 5h/7d 利用率进度条（<60% 绿 / 60–85% 黄 / ≥85% 红）、冷却状态与解除冷却按钮；`GET /api/pool/usage` 返回合并了 usage 的账号列表。

### 前端检查

| 检查 | 命令 | 说明 |
|------|------|------|
| TypeScript 类型检查 | `cd frontend && npx tsc --noEmit` | 确认无类型错误 |
| 前端时区单元测试 | `cd frontend && npx vitest run src/config/timezone.test.ts` | 时区格式化测试 |
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

## PR Monitor 测试

### 手动测试: CRUD API

```bash
# 创建监控仓库
curl -X POST http://localhost:8000/api/pr-monitor/repos \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"repo_full_name": "test/repo", "auto_merge": true, "allowed_authors": ["user1"]}'

# 列出仓库
curl http://localhost:8000/api/pr-monitor/repos -H "Authorization: Bearer <token>"

# 切换 enabled
curl -X POST http://localhost:8000/api/pr-monitor/repos/1/toggle -H "Authorization: Bearer <token>"

# 删除
curl -X DELETE http://localhost:8000/api/pr-monitor/repos/1 -H "Authorization: Bearer <token>"
```

### 手动测试: Webhook（需要构造 HMAC 签名）

```bash
# 生成签名并发送模拟 webhook
SECRET="<webhook_secret>"
PAYLOAD='{"action":"opened","pull_request":{"number":1,"title":"Test PR","draft":false,"user":{"login":"user1"},"base":{"ref":"main"},"html_url":"https://github.com/test/repo/pull/1"},"repository":{"full_name":"test/repo"}}'
SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print "sha256="$2}')

curl -X POST http://localhost:8000/api/github/webhook \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$PAYLOAD"
```

### 前端测试

1. 导航到 PR Monitor 页面
2. 添加仓库 → 验证表格显示
3. 点击仓库 → 验证详情页、Webhook 配置、复制按钮
4. 切换 enabled → 验证开关状态
5. 删除仓库 → 验证列表更新

## Task 状态同步（status_change 广播收口，2026-07-12）

### 自动化测试
```bash
# 复活块 orphan/autonomous 排除（completed 不被回放/后台事件翻回 executing）
uv run python -m pytest backend/tests/test_service_instance_manager.py -k reactivat -v
# cancel 广播 status_change
uv run python -m pytest backend/tests/test_api_tasks.py -k broadcasts_status_change -v
```

### 手动验证
1. 开两个浏览器页签（列表页 + 同一 task 的 chat 页），在列表页 cancel/retry 任务 → chat 页头部状态应立即变化（无需等 5s 轮询）
2. chat 打开 + 状态过滤 executing 时，让任务完成 → 侧栏状态点应实时变绿（不再永久冻结）
3. 断开 WS（devtools offline 几秒）错过 status_change → 恢复后 ≤5s 内 chat 页状态应被轮询数据纠正（不再永久陈旧）

## 前端主题系统 v2 测试（2026-07-13）

### 自动化
- `cd frontend && npx tsc --noEmit` — 类型检查
- `cd frontend && npm run build` — 构建（字体 woff2 应打进 dist/assets）
- `cd frontend && npx vitest run` — 组件测试（注意：main 上存在历史失败基线，对比失败数是否增加）
- `cd frontend && npx vitest run src/config/theme.test.ts` — 主题注册表 + index.css 变量覆盖完整性（gray/indigo 全档、浅色主题 color-scheme 与 accent 300/400 深色化、飞书官方 token 抽查）

### 手动验证（每个主题 × 桌面/移动端）
- [ ] 齿轮 → 主题下拉分「现代 / Legacy」两组：深色、浅色、飞书、经典深色、海蓝、森林、莓红
- [ ] 飞书主题：白卡片 + #f5f6f7 画布 + #eceef1 侧栏（飞书 rail 灰）；主按钮经典飞书蓝 #3370ff，hover 加深（#245bdb）；主文字 #1f2329；低边框风（#e8eaed，弱线框）；选中项浅蓝 pill #e1eaff + 蓝字
- [ ] 「经典深色」外观 = v1 默认深色（Tailwind 原生 gray/indigo 色板）
- [ ] 浅色主题：白色卡片 + 浅灰画布；chip 文字（text-X-300/400）可读；无白字白底（text-white 只允许出现在彩色实底上）
- [ ] 桌面 lg+：左侧固定侧栏导航高亮正确；顶栏 sticky；Tasks 分屏（≥1280px）无纵向溢出（100vh-49px）
- [ ] 移动端：汉堡 → 抽屉滑出导航，点遮罩关闭；safe-area 顶部不遮挡
- [ ] 切主题后手机状态栏 / PWA theme-color 跟随（meta 同步）
- [ ] 刷新后主题保持（localStorage cc_theme）；旧值 ocean/forest/rose 直接沿用，无迁移丢失

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
| `backend/schemas/task.py` (datetime serialization) | `backend/tests/test_task_schema.py` |
| `backend/api/chat.py` (timestamp Z suffix) | `backend/tests/test_chat_timestamp.py` |
| `frontend/src/config/timezone.ts` | `frontend/src/config/timezone.test.ts` |
| `backend/mcp/ccm_skills_server.py` | `backend/tests/test_mcp_server.py` |
| `backend/models/monitor_session.py` | `backend/tests/test_monitor_models.py` |
| `backend/services/mcp_config.py` | `backend/tests/test_mcp_config.py` |
| `backend/api/monitor.py` | `backend/tests/test_api_monitor.py` |
| `backend/api/settings.py` (runtime) | `backend/tests/test_api_settings_runtime.py`（含 context_compact_threshold 默认/更新/越界拒绝） |
| `backend/services/dispatcher.py` (monitor) | `backend/tests/test_monitor_dispatcher.py` |
| `backend/mcp/ccm_monitor_agent_server.py` | 集成测试（见「子 Agent 系统集成测试」） |
| `backend/api/sub_agents.py` | 集成测试（见「子 Agent 系统集成测试」） |
| `backend/models/pr_monitor.py` | Migration 验证（表创建） |
| `backend/api/pr_monitor.py` | curl 测试 CRUD + webhook |
| `backend/services/pr_review_service.py` | 集成测试（webhook → task 创建） |
| `frontend/src/pages/PRMonitorPage.tsx` | TypeScript 类型检查 + 手动 UI 测试 |
| `frontend/src/**` | TypeScript 类型检查 (`tsc --noEmit`) |

## 分布式 Worker 测试

### 单元/集成测试

```bash
uv run python -m pytest backend/tests/test_api_workers.py -v
```

覆盖：API 状态守卫（409/503/404）、双击防护（同步置过渡态）、provisioner 状态机
（收养/创建/stop/start/destroy/retry，cloud+SSH 全替身）、健康检查降级与自动恢复
（bootstrap 失败不被洗白）、.deploy_commit 版本回退。

### 真机冒烟（收养一台已有 EC2 跑完整 bootstrap）

```bash
WORKER_ENABLED=true WORKER_SSH_KEY_PATH=~/.ssh/xxx.pem PYTHONPATH=. \
  .venv/bin/python scripts/worker_phase1_smoke.py --adopt i-xxxxxxxx
```

预期：status=ready，health 返回非空 commit 且与 DB ccm_commit 一致（版本锁定 PASS）。
注意：经 PTY bridge 跑长任务用 `setsid nohup ... > /tmp/x.log &`，且命令里别带
`rm`/`mv`（权限 ask 列表会触发 bridge auto-deny）。

### Phase 2 端到端（已验证 2026-06-12，task 58）

manager(8003) 注册 worker → 建 git_url 项目 → 创建 task 选 worker → 验证：
转发同 ID、状态回流、43 条日志镜像、README 真实修改 + merge push、
chat 代理 + session_id 同步、回复经 relay 回流。测试仓库
github.com/youchengsong/ccm-worker-e2e-test（可删）。
