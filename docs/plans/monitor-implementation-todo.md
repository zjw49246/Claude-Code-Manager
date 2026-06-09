# Monitor Session 实施计划

> 基于 `docs/plans/monitor-mode-design.md` V2 方案。
> 分支: `feature/monitor-session`
> 约束: 仅本地修改，不 push，不影响部署。

---

## Phase 1 — 数据层（Model + Schema + Migration）

### 1.1 创建 MonitorSession Model
- [ ] 新建 `backend/models/monitor_session.py`
  - MonitorSession 表: id, task_id(FK), description, monitor_context, interval(default=300), max_checks(default=100), model, status(enum: running/completed/failed/cancelled), checks_done(default=0), last_summary, source(enum: manual/loop/goal), created_at, completed_at
  - MonitorCheck 表: id, monitor_session_id(FK), check_number, status, summary, full_output, created_at
  - 参考: `backend/models/task.py` 的写法

### 1.2 注册 Model
- [ ] 在 `backend/models/__init__.py` 中 import MonitorSession, MonitorCheck

### 1.3 创建 Alembic Migration
- [ ] `alembic revision --autogenerate -m "add_monitor_sessions_and_checks_tables"`
- [ ] 检查生成的 migration 文件，确认无误
- [ ] `alembic upgrade head` 验证

### 1.4 创建 Schema
- [ ] 新建 `backend/schemas/monitor_session.py`
  - MonitorSessionCreate: description, monitor_context(optional), interval(default=300), max_checks(default=100), model(optional)
  - MonitorSessionResponse: 完整字段
  - MonitorCheckResponse: 完整字段

---

## Phase 2 — 后端核心：Monitor Session 运行逻辑

### 2.1 Monitor Session Prompt
- [ ] 在 `backend/services/dispatcher.py` 添加 `_build_monitor_session_prompt(self, monitor_session, task)` 方法
  - 包含: 检查次数、监控描述、上下文、signal file 路径
  - 指令: ps aux 检查进程、tail 日志、写 signal file（status: running/done, summary）

### 2.2 Monitor 子进程
- [ ] 在 `backend/services/dispatcher.py` 添加 `_run_monitor_subprocess(self, monitor_session, task)` 方法
  - 使用 `claude` CLI，加 `--dangerously-skip-permissions` 和 `--disallowedTools Edit,Write,NotebookEdit`
  - stream-json 模式解析输出
  - 读取 signal file 返回结果
  - 超时处理 (120s)

### 2.3 Signal File 工具方法
- [ ] `_get_monitor_signal_path(self, monitor_session_id, cwd)` → Path
- [ ] `_read_monitor_signal(self, signal_path)` → dict

### 2.4 Monitor Session 主循环
- [ ] `_run_monitor_session(self, monitor_session, task)` 方法
  - system monitor (loop/goal): `while True` 无限循环
  - manual monitor: `while checks_done < max_checks`
  - 每次检查: 清理旧 signal → 运行子进程 → 读取 signal → 写 DB(MonitorCheck) → 广播 WebSocket → 判断 done/继续
  - 完成/失败时广播 `monitor_session_status` 事件
  - `asyncio.sleep(interval)` 等待下次检查

### 2.5 后台 Monitor 启动器（Auto 模式用）
- [ ] `_start_monitor_background(self, monitor_session, task)` — 用 `asyncio.create_task`，注册到 `self._monitor_tasks`
- [ ] 初始化 `self._monitor_tasks: dict[int, asyncio.Task] = {}` in `__init__`

---

## Phase 3 — Loop 模式集成

### 3.1 Loop Signal 扩展
- [ ] 在 `_run_loop_lifecycle` 中解析 signal file 时，读取新增字段:
  - `needs_monitor: bool`
  - `monitor_context: str`

### 3.2 Loop Prompt 扩展
- [ ] 在 `_build_loop_prompt` (或等效方法) 中追加 monitor 相关引导文本
  - 告诉 Claude: 如果启动了后台任务，在 signal file 中设置 needs_monitor=true
  - 包含资源争用提示: 一次迭代只启动一个 GPU 任务

### 3.3 Gate Monitor 插入
- [ ] 在 `_run_loop_lifecycle` 的迭代完成后、下一轮开始前:
  - 检查 signal 中 `needs_monitor`
  - 如果 true: 创建 MonitorSession(source="loop")，调用 `_run_monitor_session` (同步 await，阻塞循环)
  - monitor 完成后继续下一轮迭代

---

## Phase 4 — API 端点

### 4.1 创建 API 文件
- [ ] 新建 `backend/api/monitor.py`
  - `POST /tasks/{task_id}/monitor-sessions` — 创建 manual monitor session
    - 校验 task 存在且为 auto 模式
    - 调用 dispatcher._start_monitor_background
  - `DELETE /tasks/{task_id}/monitor-sessions/{session_id}` — 删除 monitor session
    - 校验归属 (task_id)
    - 校验 source == "manual"，system monitor 不可删除
    - 取消 asyncio task
  - `GET /tasks/{task_id}/monitor-sessions` — 列表
  - `GET /tasks/{task_id}/monitor-sessions/{session_id}/checks` — 检查记录列表

### 4.2 注册路由
- [ ] 在 `backend/main.py` 中 include monitor router

---

## Phase 5 — 前端

### 5.1 Monitor 列表面板
- [ ] 在 Task 详情页添加 "监控列表(N)" 展开面板
  - 显示所有 monitor session: 描述、状态、最新摘要
  - Auto 模式: 显示 [+ 新建监控] 按钮
  - Loop/Goal 模式: 不显示新建按钮

### 5.2 新建监控对话框（仅 Auto 模式）
- [ ] 弹窗表单: 描述(必填)、上下文(选填)、间隔(默认300秒)、最大检查次数(默认100)
- [ ] 提交后调用 POST API

### 5.3 Monitor 详情页
- [ ] 显示检查记录列表（时间倒序）
- [ ] 每条记录: 检查编号、时间、状态、摘要、可展开的完整输出
- [ ] manual monitor 显示 "5/100 次"，system monitor 显示 "已检查 5 次"

### 5.4 WebSocket 事件处理
- [ ] 监听 `monitor_check` 事件 → 实时更新最新摘要
- [ ] 监听 `monitor_session_status` 事件 → 更新状态标签
- [ ] 监听 `monitor_output` 事件 → 实时流式输出（如果用户在详情页）

### 5.5 删除功能
- [ ] manual monitor 显示删除按钮，确认后调用 DELETE API
- [ ] system monitor 不显示删除按钮

---

## Phase 6 — Goal 模式集成（可选，与 Loop 类似）

- [ ] 在 Goal 评估流程中加入 gate monitor 逻辑
- [ ] 复用 Loop 的 `needs_monitor` 信号机制

---

## Phase 7 — 测试 & 文档

### 7.1 测试
- [ ] 测试 MonitorSession / MonitorCheck CRUD
- [ ] 测试 `_run_monitor_session` 的 done 判断、max_checks 耗尽
- [ ] 测试 API 权限（manual 可删除，system 不可删除）
- [ ] 测试 Loop 集成: needs_monitor → gate → 完成后继续

### 7.2 文档更新
- [ ] 更新 CLAUDE.md（如有架构变化）
- [ ] 更新 README.md（新增 Monitor 功能说明）
- [ ] 更新 TEST.md（新增测试用例）
