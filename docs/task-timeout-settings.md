# 任务级别超时配置 + Model/Effort 对齐

## 背景

Task 625 暴露了全局 30 分钟超时会无差别杀死长时间运行的任务（如 deep-research workflow）。深入分析后还发现两个系统性问题：
1. **Chat 路径完全没有超时** — chat 发起的运行可以无限占住 instance
2. **Dispatcher 和 Chat 两条路径的设置解析不一致** — model 回写、effort 刷新、timeout 执行各有差异

目标：
- 每个任务可配置 `timeout_hours`，创建时设置 + chat 过程中可调
- Chat 路径也受超时约束
- 所有设置（model、effort、timeout）在两条路径下行为一致
- Chat 界面加设置下拉面板

## 现状审计：各设置在两条路径下的流转

| 设置 | Dispatcher（首次执行） | Dispatcher（goal/loop 每轮） | Chat（继续对话） |
|------|----------------------|---------------------------|-----------------|
| **model** | `task.model` ← 无则 fallback `instance.model` → **写回 task.model** | 用首次 resolve 的值（不刷新） | `task.model or inst.model` → **不写回** |
| **effort_level** | `task → instance → default` → resolve 一次，作为参数传递 | 用首次 resolve 的值（不刷新） | `task → inst → default` → 每次重新 resolve |
| **thinking_budget** | 从 `instance.thinking_budget` 读 | 每轮通过 `_get_thinking_budget()` 重读 | 从当前 idle instance 读 |
| **timeout** | `settings.task_timeout_seconds`（全局 2h） | 同上 | **无超时** |

### 发现的问题

1. **Chat 没有超时** — chat 发起的进程可以永远跑下去
2. **Chat 的 model fallback 不持久化** — 如果 `task.model` 为空，chat 会 fallback 到当时分配到的 idle instance 的 model。不同 instance 可能有不同 model → 每次 chat 可能用不同模型。Dispatcher 通过写回 `task.model` 避免了这个问题
3. **Goal/loop 不会在轮次间刷新设置** — `effort_level` 在 `_run_task_lifecycle` 开头 resolve 一次，作为参数一路传下去。用户在 chat 设置面板里改了 effort，正在运行的 goal/loop 任务看不到变化
4. **thinking_budget 是 instance 级别** — 设计如此（是 worker 的硬件/成本设置，不是任务设置）。两条路径都从 instance 读，已经一致。**不改。**

## 方案设计

### 1. 数据层 — Task 表加 `timeout_hours` 字段

- 类型：`Float`，nullable
- `NULL` = 用全局默认（`settings.task_timeout_seconds`）
- `0` = 不限时
- `> 0` = 指定小时数（支持 0.5 = 30 分钟）

涉及文件：
- `backend/models/task.py` — 加列
- `backend/schemas/task.py` — TaskCreate、TaskUpdate、TaskResponse 都加
- `frontend/src/api/client.ts` — Task 接口、createTask、updateTask 参数
- 新建 Alembic migration（`down_revision = '70c7c8140b1a'`）

### 2. Dispatcher — 按任务超时 + 轮次间刷新设置

**超时辅助方法：**
```python
def _resolve_timeout(self, task: Task) -> float | None:
    """解析有效超时（秒）。None = 不限时。"""
    if task.timeout_hours is not None:
        return task.timeout_hours * 3600 if task.timeout_hours > 0 else None
    return settings.task_timeout_seconds
```

**替换 5 处超时逻辑** — 按返回值决定是否包裹 `wait_for`：
```python
timeout = self._resolve_timeout(task)
if timeout:
    await asyncio.wait_for(process.wait(), timeout=timeout)
else:
    await process.wait()
```

5 个位置：
1. `_run_task_lifecycle`（~540 行）— auto 模式
2. `_run_pool_retry`（~739 行）— pool 账号轮换
3. `_run_loop_lifecycle`（~892 行）— loop 每轮
4. `_run_goal_lifecycle`（~1086 行）— goal 每轮
5. `_run_plan_phase`（~1471 行）— plan 生成

**Goal/loop 每轮刷新设置：**

在 `_run_goal_lifecycle` 和 `_run_loop_lifecycle` 的循环体开头（已有取消检查的地方），同时刷新 `effort_level`、`task`（含 model、timeout_hours）：

```python
async with self.db_factory() as db:
    t = await db.get(Task, task.id)
    if not t or t.status == "cancelled":
        return
    # 刷新可变设置
    task = t
    effort_level = t.effort_level or (instance.effort_level if instance else None) or settings.default_effort
```

这样用户在 chat 设置面板改了 effort/model/timeout，下一轮 goal/loop 就能生效。

### 3. Chat 路径 — 加超时 + 对齐 model 持久化

**给 chat 加超时（watchdog 机制）：**

在 `instance_manager.launch()` 增加 `timeout_seconds` 参数。传入时启动一个 watchdog 协程：

```python
async def launch(self, ..., timeout_seconds: float | None = None) -> int:
    ...
    consumer = asyncio.create_task(
        self._consume_output(instance_id, task_id, process, ...)
    )
    # 超时看门狗（chat 场景使用）
    if timeout_seconds and timeout_seconds > 0:
        async def _timeout_watchdog():
            try:
                await asyncio.sleep(timeout_seconds)
                if process.returncode is None:
                    logger.warning(f"Task {task_id} chat 超时 ({timeout_seconds}s)，终止进程")
                    process.kill()
            except asyncio.CancelledError:
                pass
        watchdog = asyncio.create_task(_timeout_watchdog())
        consumer.add_done_callback(lambda _: watchdog.cancel())
```

原理：watchdog 和 consumer 并行跑。进程正常结束 → consumer 完成 → 取消 watchdog。超时 → watchdog kill 进程 → stdout EOF → consumer 自然结束。

在 `chat.py` 中解析超时并传入：
```python
timeout_seconds = None
if task.timeout_hours is not None:
    timeout_seconds = task.timeout_hours * 3600 if task.timeout_hours > 0 else None
else:
    timeout_seconds = app_settings.task_timeout_seconds

pid = await instance_manager.launch(..., timeout_seconds=timeout_seconds)
```

**对齐 model 持久化：**

Chat 目前不会把 resolve 后的 model 写回 task。改为和 dispatcher 一致：

```python
resolved_model = task.model or inst.model
if not task.model and resolved_model:
    task.model = resolved_model
    await db.commit()
```

确保：一旦 model 确定（无论是用户指定还是 instance fallback），就锁定在 task 上，后续不管分配到哪个 idle instance 都用同一个模型。

### 4. 前端 — TaskForm 加 Timeout 下拉

在 Effort 下拉后面加：

```
[Priority] [Mode ▾] [Model ▾] [Effort ▾] [Timeout ▾] ...
```

选项：
- `""` → "2h (default)" — 用全局默认
- `0.5` → "30 min"
- `1` → "1 hour"
- `2` → "2 hours"
- `4` → "4 hours"
- `8` → "8 hours"
- `12` → "12 hours"
- `24` → "24 hours"
- `0` → "No limit"

样式复用现有 `<select>` 的 `bg-gray-700 text-foreground rounded px-2 py-1 text-sm`。

### 5. 前端 — ChatView 设置下拉面板

**齿轮图标**放在 header 中 Star 和 Interrupt 按钮之间：

```
[← Back] [Task #625 | Project | Session active | Context] [⚙️] [⭐] [Interrupt]
```

**新组件 `TaskSettingsDropdown`：**

```
┌─────────────────────────────┐
│ Task Settings                │
│                              │
│ Timeout   [2 hours      ▾]  │
│ Model     [opus-4-6[1m] ▾]  │
│ Effort    [xhigh         ▾]  │
│                              │
│ ⓘ 修改在下一轮生效            │
└─────────────────────────────┘
```

- 点击外部关闭（复用 ProjectSelect 的 click-outside 模式）
- 每个 select 变更时调用 `api.updateTask(taskId, { field: value })`
- Model/Effort 选项从 `api.config()` 获取（和 TaskForm 一致）
- Timeout 选项和 TaskForm 一致
- 底部始终显示 "修改在下一轮生效" 提示
- Props: `task: Task`, `onTaskUpdated: (task: Task) => void`

### 6. 后端 Schema 更新

**TaskUpdate** — 补充可编辑字段：
```python
class TaskUpdate(BaseModel):
    ...  # 现有字段
    timeout_hours: float | None = None
    model: str | None = None
    effort_level: str | None = None
```

**TaskCreate** — 加：
```python
timeout_hours: float | None = None
```

**TaskResponse** — 加：
```python
timeout_hours: float | None
```

**`updateTask`（client.ts）** — 扩展类型：
```typescript
updateTask: (id: number, data: {
  title?: string; description?: string; priority?: number;
  timeout_hours?: number | null;
  model?: string | null; effort_level?: string | null;
}) => ...
```

## 修改文件清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | `backend/models/task.py` | 加 `timeout_hours` 列 |
| 2 | `backend/schemas/task.py` | Create/Update/Response 加 `timeout_hours`；Update 加 `model`、`effort_level` |
| 3 | `backend/services/dispatcher.py` | 加 `_resolve_timeout()`；改 5 处超时逻辑；goal/loop 每轮刷新设置 |
| 4 | `backend/services/instance_manager.py` | `launch()` 加 `timeout_seconds` 参数 + watchdog 协程 |
| 5 | `backend/api/chat.py` | 解析超时传给 launch；model resolve 后写回 task |
| 6 | `alembic/versions/xxx_add_timeout_hours.py` | 新 migration |
| 7 | `frontend/src/api/client.ts` | Task 接口加 `timeout_hours`；扩展 createTask/updateTask 参数 |
| 8 | `frontend/src/components/Tasks/TaskForm.tsx` | 加 Timeout 下拉 |
| 9 | `frontend/src/components/Chat/TaskSettingsDropdown.tsx` | 新组件 |
| 10 | `frontend/src/components/Chat/ChatView.tsx` | 加齿轮按钮 + 集成 dropdown |

## 验证步骤

1. `uv run alembic upgrade head` — migration 正常
2. `uv run python -m pytest backend/tests/ -v` — 测试通过
3. `cd frontend && npx tsc --noEmit` — 类型检查通过
4. 创建 timeout=30min 的任务 → dispatcher 30 分钟后 kill
5. 创建 timeout=No limit 的任务 → 无限运行
6. Chat 发消息 → watchdog 在配置时间后触发
7. Chat 设置面板改 model → 下一轮 chat 用新 model
8. Chat 设置面板改 effort（goal 任务进行中）→ 下一轮 goal 用新 effort
9. Chat 设置面板改 timeout → 下一轮用新超时
