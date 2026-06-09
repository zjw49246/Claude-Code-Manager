# Monitor Session 实施计划

> 基于 `docs/plans/monitor-mode-design.md` V2 方案。
> 分支: `feature/monitor-session`
> 约束: 仅本地修改，不 push，不影响部署。

---

## Phase 1 — 数据层（Model + Schema + Migration）

### 1.1 创建 MonitorSession & MonitorCheck Model
- [ ] 新建 `backend/models/monitor_session.py`
  - MonitorSession 表: id, task_id(FK→tasks.id), description, monitor_context, interval(default=300), max_checks(default=100), model, status(running/completed/failed/cancelled), checks_done(default=0), last_summary, source(manual/loop/goal), created_at, completed_at
  - MonitorCheck 表: id, monitor_session_id(FK→monitor_sessions.id), check_number, status, summary, full_output, created_at
  - 参考: `backend/models/task.py` 的写法（使用 Mapped[] 类型注解）

### 1.2 注册 Model（用于 Alembic autogenerate）
- [ ] 在 `alembic/env.py` 中 import MonitorSession, MonitorCheck（项目的 model 注册在这里，不是 `__init__.py`）

### 1.3 创建 Alembic Migration
- [ ] `alembic revision --autogenerate -m "add_monitor_sessions_and_checks_tables"`
- [ ] 检查生成的 migration 文件，确认表结构正确
- [ ] `alembic upgrade head` 验证迁移成功

### 1.4 创建 Schema
- [ ] 新建 `backend/schemas/monitor_session.py`
  - MonitorSessionCreate(BaseModel): description, interval(=300), max_checks(=100), model(optional)
  - MonitorSessionResponse(BaseModel, from_attributes=True): 完整字段
  - MonitorCheckResponse(BaseModel, from_attributes=True): 完整字段

---

## Phase 2 — 后端核心：Monitor Session 运行逻辑

### 2.1 Dispatcher 初始化
- [ ] 在 `dispatcher.py` 的 `__init__` 中添加 `self._monitor_tasks: dict[int, asyncio.Task] = {}`

### 2.2 Signal File 工具方法
- [ ] `_get_monitor_signal_path(self, monitor_session_id: int, cwd: str) -> Path`
- [ ] `_read_monitor_signal(self, signal_path: Path) -> dict`

### 2.3 Monitor Session Prompt
- [ ] `_build_monitor_session_prompt(self, monitor_session, task) -> str`
  - 包含: 检查次数、监控描述、上下文、signal file 路径
  - 指令: ps aux 检查进程、tail 日志、写 signal file（status: running/done, summary）

### 2.4 Monitor 子进程
- [ ] `_run_monitor_subprocess(self, prompt, cwd, model, task_id, monitor_session_id) -> str`
  - 使用 `settings.claude_binary` CLI
  - 参数: `-p`, `--model`, `--output-format stream-json`, `--dangerously-skip-permissions`, `--disallowedTools Edit,Write,NotebookEdit`
  - 环境变量: 继承当前环境，排除 CLAUDECODE/CLAUDE_CODE
  - 使用已有的 `StreamParser` 解析输出（不要重新实现）
  - 整体用 `asyncio.wait_for` 包裹，超时 300 秒

### 2.5 Monitor Session 主循环
- [ ] `_run_monitor_session(self, monitor_session, task) -> bool`
  - system monitor (source=loop/goal): `while True` 无限循环，不受 max_checks 限制
  - manual monitor (source=manual): `while checks_done < max_checks`
  - 每轮: sleep(interval) → 检查 task status → 检查 monitor status → 清理旧 signal → 运行子进程 → 读 signal → 写 DB(MonitorCheck) → 广播 monitor_check 事件 → 判断 done
  - 完成时广播 `monitor_session_status` 事件（completed/failed）
  - 返回 True=完成，False=取消或耗尽

### 2.6 后台 Monitor 启动器（Auto 模式用）
- [ ] `_run_monitor_session_background(self, monitor_session, task_id)`
  - 从 DB 获取 task
  - 注册 `asyncio.current_task()` 到 `self._monitor_tasks[monitor_session.id]`
  - try/finally 清理 `_monitor_tasks`

---

## Phase 3 — Loop 模式集成

### 3.1 Loop Prompt 扩展
- [ ] 新增 `_get_loop_monitor_hint(self) -> str` 方法
  - 告诉 Claude: 如果启动了后台任务，在 signal file 中设置 `needs_monitor: true` 和 `monitor_context`
  - 包含资源争用提示: 一次迭代只启动一个需要独占资源的任务
- [ ] 在 `_build_loop_prompt` 返回值中追加此 hint

### 3.2 Gate Monitor 插入
- [ ] 在 `_run_loop_lifecycle` 的 `action == "continue"` 分支中（约 line 946+）:
  - 读取 signal 的 `needs_monitor` 和 `monitor_context` 字段
  - 如果 `needs_monitor == true`:
    - 创建 MonitorSession 记录（source="loop", interval=300）
    - 广播 `monitor_session_created` 事件
    - `await self._run_monitor_session(monitor_session, task)` — 同步阻塞直到后台任务完成
    - 如果返回 False（被取消），退出循环
  - 继续下一轮迭代

---

## Phase 4 — API 端点 + 取消逻辑

### 4.1 创建 API 文件
- [ ] 新建 `backend/api/monitor.py`（项目路由在 `backend/api/` 目录，不是 `backend/routers/`）
  - `POST /tasks/{task_id}/monitor-sessions` — 创建 manual monitor session
    - 校验 task 存在（404 if not found）
    - 创建 MonitorSession(source="manual")
    - 调用 `asyncio.create_task(dispatcher._run_monitor_session_background(...))`
    - response_model=MonitorSessionResponse
  - `DELETE /tasks/{task_id}/monitor-sessions/{session_id}` — 删除 monitor session
    - 校验 task_id 归属
    - 校验 source == "manual"，system monitor 返回 403
    - 更新 DB status="cancelled"
    - 取消 `dispatcher._monitor_tasks` 中的 asyncio task
  - `GET /tasks/{task_id}/monitor-sessions` — 列表（response_model=list[MonitorSessionResponse]）
  - `GET /tasks/{task_id}/monitor-sessions/{session_id}/checks` — 检查记录列表（response_model=list[MonitorCheckResponse]）

### 4.2 注册路由
- [ ] 在 `backend/main.py` 中 import 并 include monitor router（参考其他 router 的注册方式）

### 4.3 任务取消时清理 Monitor
- [ ] 在 `backend/services/task_queue.py` 的 `cancel()` 方法中追加:
  - 批量更新该 task 下所有 running 的 MonitorSession 为 cancelled
- [ ] 在 `backend/api/tasks.py` 的 `cancel_task` endpoint 中追加:
  - 遍历 `dispatcher._monitor_tasks`，cancel 对应的 asyncio task（立即中断 sleep）

### 4.4 服务重启清理
- [ ] 在 `dispatcher.py` 的 `_cleanup_stale_state()` 末尾追加:
  - 查询所有 status="running" 的 MonitorSession，标记为 "failed"

---

## Phase 5 — 前端

### 5.1 API Client 扩展
- [ ] 在 `frontend/src/api/client.ts` 中添加 monitor 相关 API 方法
  - createMonitorSession, deleteMonitorSession, listMonitorSessions, getMonitorChecks
- [ ] 添加 TypeScript 接口: MonitorSession, MonitorCheck

### 5.2 Monitor 列表面板组件
- [ ] 新建 `frontend/src/components/Chat/MonitorPanel.tsx`
  - 在 Task 详情页（ChatView/LoopChatView）添加 [监控列表(N)] 按钮
  - 侧面板/抽屉展示所有 monitor session
  - Auto 模式: 显示 [+ 新建监控] 按钮
  - Loop 模式: 不显示新建按钮
  - 其他模式（Plan/Goal）: 不显示监控相关 UI
  - manual monitor 显示删除按钮，system monitor 不显示

### 5.3 新建监控对话框（仅 Auto 模式）
- [ ] 弹窗表单: 描述(必填)、间隔(默认300秒)、最大检查次数(默认100)
- [ ] 提交后调用 createMonitorSession API

### 5.4 Monitor 详情页
- [ ] 显示检查记录列表（时间倒序）
- [ ] 每条记录: 检查编号、时间、状态、摘要、可展开的完整输出
- [ ] manual monitor 显示 "5/100 次"，system monitor 显示 "已检查 5 次"

### 5.5 WebSocket 事件处理
- [ ] 在 ChatView/LoopChatView 中监听 WebSocket 事件（使用 onMessage 回调模式，不用 lastMessage）:
  - `monitor_session_created` → 列表添加新条目
  - `monitor_check` → 实时更新最新摘要
  - `monitor_session_status` → 更新状态标签
  - `monitor_output` → 实时流式输出（如果用户在详情页）

---

## Phase 6 — 测试 & 文档

### 6.1 测试
- [ ] 测试 MonitorSession / MonitorCheck CRUD（model 层）
- [ ] 测试 `_run_monitor_session` 的 done 判断、max_checks 耗尽、取消检测
- [ ] 测试 API 权限（manual 可删除，system 不可删除，task_id 归属校验）
- [ ] 测试 Loop 集成: needs_monitor → gate → 完成后继续
- [ ] 测试取消流程: 取消 task → monitor sessions 全部 cancelled
- [ ] 测试服务重启: running 的 monitor session 被清理为 failed

### 6.2 文档更新
- [ ] 更新 CLAUDE.md（如有架构变化）
- [ ] 更新 README.md（新增 Monitor 功能说明）
- [ ] 更新 TEST.md（新增测试用例）
