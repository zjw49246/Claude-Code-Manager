# Dev 环境 Monitor Sub-Agent 代码审查

审查时间: 2026-06-09

---

## 严重问题（必须修）

### 1. 进程退出后用过期数据判断状态
- **文件**: `backend/services/dispatcher.py:1652`
- `_monitor_session_lifecycle` 在进程退出后检查 `ms.status == "running"` 来决定是否标记 failed
- 但 `ms` 是在启动进程之前从 DB 读的，子 Agent 在运行期间可能已经通过 `mark_complete` 把状态改成了 completed
- **结果**: 子 Agent 正确调了 `mark_complete`，但 lifecycle 拿着旧数据覆写成 failed

### 2. `create_monitor_check` 的 checks_done 非原子递增
- **文件**: `backend/api/monitor.py:153`
- `ms.checks_done += 1` 是 read → increment → write，并发请求可能丢失计数
- 影响 `max_checks` 的自动完成判断

### 3. 自动完成后子进程继续空转
- **文件**: `backend/api/monitor.py:157-170`
- `checks_done >= max_checks` 时只改 DB 状态为 completed，但子 Agent 进程继续运行直到 4 小时超时
- 没有机制通知子进程停下来

### 4. `proc.kill()` 后没有 `await proc.wait()`
- **文件**: `backend/api/monitor.py:111`, `backend/api/tasks.py:186`
- 会留下僵尸进程

---

## 高危问题

### 5. task 取消时不等待 monitor 任务结束
- **文件**: `backend/api/tasks.py:181-186`
- `atask.cancel()` 没有 await，立刻返回，monitor 可能正在写 DB

### 6. 子 Agent 遇到 404/400 会无限重试
- **文件**: `backend/mcp/ccm_monitor_agent_server.py`
- session 被删除后，`report_status` 返回 404，子 Agent 不知道该停止，一直重试到 4 小时超时

### 7. 日志文件句柄泄漏
- **文件**: `backend/services/dispatcher.py:1734-1748`
- `_launch_monitor_agent` 里如果 `create_subprocess_exec` 失败，`log_fh` 虽然在 except 里关闭了，但正常路径下 `log_fh.close()` 后文件描述符虽然关了，log 文件本身在 finally 里才删除；如果 finally 没执行到就泄漏

---

## 中等问题

### 8. MCP config 里含 auth token 写入 `/tmp`
- **文件**: `backend/services/mcp_config.py:51`
- 文件是 world-readable，共享系统上有 token 泄漏风险

### 9. `max_checks` 和 `interval` 没有校验
- **文件**: `backend/api/monitor.py:23-57`
- 传 0 或负数会导致立刻完成或 CPU 空转

### 10. `/tmp/ccm_monitor_*.log` 不会被主动清理
- 依赖 OS 清理 `/tmp`，长期运行会堆积磁盘

---

## 修复优先级

| 优先级 | 问题 | 原因 |
|--------|------|------|
| P0 | #1 过期数据覆写状态 | 子 Agent 完成了但被标成 failed，用户可见 bug |
| P0 | #3 自动完成后进程空转 | 浪费 API 费用 |
| P0 | #4 proc.kill() 不 await | 僵尸进程堆积 |
| P1 | #2 非原子递增 | 并发低时不易触发，但逻辑有隐患 |
| P1 | #5 取消不等待 | 可能导致 DB 不一致 |
| P1 | #6 子 Agent 无限重试 | 浪费资源 |
| P2 | #7-#10 | 长期运行才显现 |

---
---

# 第二轮审查

审查时间: 2026-06-09（未提交的工作区变更）

主要新增: per-task message queue、chat.py 重构为入队模式、monitor 汇报转发主 Agent、子 Agent prompt 强化、disallowedTools 动态生成

---

## 第一轮问题修复情况

| # | 问题 | 状态 | 说明 |
|---|------|------|------|
| 1 | 过期数据覆写状态 | **已修** | 进程退出后重新 `db.get(MonitorSession)` |
| 2 | checks_done 非原子递增 | **未修** | 并发低暂可接受 |
| 3 | 自动完成后进程空转 | **已修** | auto_complete 时 `sub_proc.kill()` + `await sub_proc.wait()` |
| 4 | proc.kill() 不 await | **已修** | monitor.py:112, tasks.py:186 均加了 `await proc.wait()` |
| 5 | task 取消不等待 | **已修** | tasks.py 先 kill+wait 再 cancel |
| 6 | 子 Agent 无限重试 | **已修** | 400/404 返回 `session_ended: true` |
| 7 | 日志文件句柄泄漏 | **部分修** | log_fh 存入 `_monitor_log_fhs` 在 finally 关闭，但日志文件不再主动删除 |
| 8 | MCP config 权限 | **已修** | `os.open(..., 0o600)` |
| 9 | 参数校验 | **已修** | `interval >= 5`, `max_checks >= 1` |
| 10 | log 文件清理 | **未修** | finally 里删除逻辑被移除了，只关闭 fh |

---

## 新代码发现的问题

### P0: `_process_queued_message` 和 `_consume_output` 双重管理 task 状态
- **文件**: `backend/services/dispatcher.py:1964-1968`
- `_process_queued_message` 在 `process.wait()` 后检查 `task.status == "executing"` 就改成 completed
- 但 `instance_manager._consume_output`（`chat_initiated=True`）已经在做同样的事
- **风险**: 如果 `_consume_output` 把 task 改成 failed（exit_code != 0），但 `_process_queued_message` 的 `db.refresh` 在 `_consume_output` commit 之前执行，会读到 `executing` 并覆写成 completed，**吞掉 failed 状态**
- **建议**: 去掉 `_process_queued_message` 里的状态管理，完全交给 `_consume_output`

### P0: DB session 贯穿整个进程生命周期
- **文件**: `backend/services/dispatcher.py:1868`
- `async with self.db_factory() as db:` 打开后，在里面 `await process.wait()`，进程可能运行数分钟
- SQLite 连接被长期占用；长时间闲置后 `db.refresh(task)` 可能遇到连接池问题
- **建议**: launch 后关闭 session，`process.wait()` 完成后重新开一个

### P1: chat.py 不再返回 409，前端状态不同步
- **文件**: `backend/api/chat.py:27-107`
- 旧代码在 task 忙时返回 409，前端据此显示"任务执行中"
- 新代码直接入队返回 `{"ok": true, "queued": true}`，但前端 `handleSend` 仍 `setSending(true)` 等待 `process_exit`
- **结果**: 如果消息排队等了很久才处理，用户看到的是一直在"思考中"；如果前面有多条排队，等待时间更长
- **建议**: 前端需要适配入队模式——收到 `queued: true` 时不进入 sending 状态，或显示"已排队"

### P1: 消息忙碌超时后被丢弃而非重新入队
- **文件**: `backend/services/dispatcher.py:1887`
- 等待 task 空闲的 for 循环超 60 轮（120s）后直接 `return`，消息丢失
- 没有 idle instance 时会重新入队（line 1896），但 busy timeout 不会
- 用户消息和 monitor 重要汇报都可能被静默丢弃
- **建议**: busy timeout 也应该重新入队，或者至少通知前端消息发送失败

### P1: `complete_monitor_session` 的 is_important 改成了 False
- **文件**: `backend/api/monitor.py:280`
- 子 Agent 调 `mark_complete` 时 WebSocket 广播的 `is_important` 从 True 改成 False
- `mark_complete` 通常代表监控目标完成/失败，是最重要的事件
- **影响**: 前端可能不会突出显示这个关键消息

### P2: `instance_id=1` 硬编码
- **文件**: `backend/api/monitor.py:178`, `backend/api/chat.py:73`
- `LogEntry(instance_id=1, ...)` 硬编码。如果 id=1 的 instance 被删，外键约束可能失败
- **建议**: 用实际的 instance id 或设为 nullable

### P2: SubSessionIndicator.tsx 被清空但没删除
- **文件**: `frontend/src/components/Chat/SubSessionIndicator.tsx`
- 内容清空成 0 字节但文件还在，应该 `git rm`

---

## 新架构评价

### 做得好的地方
- **per-task message queue** 是正确的架构方向，解决了 chat 和 monitor 汇报的串行化问题
- **QueuedMessage 优先级** 设计合理：用户消息 > monitor 完成通知 > monitor 重要汇报
- **disallowedTools 动态生成**（`SKILL_DISALLOWED_BUILTINS`）比硬编码更灵活
- **子 Agent prompt 强化** 明确禁止用内置 Agent/Monitor 工具，用 python sleep 替代 bash sleep
- **monitor 汇报转发主 Agent** 通过 `enqueue_message` 实现，让主 Agent 能向用户转达监控结果
- **chat history 的 source 标记** 可以区分普通消息和 monitor 来源的消息

### 需要改进的地方
- 前后端对"入队模式"的适配还不完整（前端仍按即时发送设计）
- `_process_queued_message` 和 `_consume_output` 的职责边界需要厘清
- DB session 生命周期管理需要拆分

---

## 修复优先级总览

| 优先级 | 问题 | 原因 |
|--------|------|------|
| P0 | 双重 task 状态管理 | 可能吞掉 failed 状态 |
| P0 | DB session 跨进程生命周期 | 连接池问题、长期占用 |
| P1 | 前端未适配入队模式 | 用户体验：一直显示"思考中" |
| P1 | 消息 busy timeout 被丢弃 | 用户消息静默丢失 |
| P1 | mark_complete is_important=False | 关键事件不被突出显示 |
| P2 | instance_id=1 硬编码 | 外键约束风险 |
| P2 | SubSessionIndicator.tsx 残留 | 代码卫生 |
