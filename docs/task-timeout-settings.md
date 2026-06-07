# 任务级别设置重构：设置归 Task，Instance 只做 Worker

## 背景

### 直接触发

Task 625 暴露了全局 30 分钟超时会无差别杀死长时间运行的任务（如 deep-research workflow）。需要支持按任务配置超时。

### 深层问题

分析过程中发现当前架构存在更根本的设计问题：**任务执行设置（model、effort、thinking_budget）分散在 Instance 和 Task 两个层级**，导致：

1. **Chat 路径完全没有超时** — chat 发起的运行可以无限占住 instance
2. **设置解析链复杂且不一致** — `task.X → instance.X → global default`，Dispatcher 和 Chat 各自实现一套，行为有差异
3. **thinking_budget 只存在 Instance 上** — 任务无法指定自己需要的 thinking budget，完全取决于被分配到哪个 instance
4. **Instance 上的 model 导致耦合** — Dispatcher 按 Instance.model 匹配 Task，instance 不再是通用 worker，而是和特定 model 绑定。换模型需要创建新 instance

### 目标

**让 Instance 成为纯粹的 worker 排队槽位，所有任务执行设置归 Task 所有。**

## 现状审计

### 设置存储位置

| 设置 | Task 上 | Instance 上 | 说明 |
|------|---------|------------|------|
| model | 有（nullable） | 有（default="default"） | 两处都有，解析链复杂 |
| effort_level | 有（nullable） | 有（nullable） | 两处都有 |
| thinking_budget | **无** | 有 | 只在 Instance 上 |
| timeout | **无** | **无** | 只有全局 `settings.task_timeout_seconds` |
| provider | 有 | 有 | 两处都有 |

### 当前设置解析链（Dispatcher vs Chat）

| 设置 | Dispatcher（首次执行） | Dispatcher（goal/loop 每轮） | Chat（继续对话） |
|------|----------------------|---------------------------|-----------------|
| **model** | `task.model` ← fallback `instance.model` → **写回 task.model** | 用首次 resolve 的值（不刷新） | `task.model or inst.model` → **不写回** |
| **effort_level** | `task → instance → default` → resolve 一次 | 用首次 resolve 的值（不刷新） | `task → inst → default` → 每次重新 resolve |
| **thinking_budget** | 从 `instance.thinking_budget` 读 | 每轮通过 `_get_thinking_budget()` 重读 | 从当前 idle instance 读 |
| **timeout** | `settings.task_timeout_seconds`（全局） | 同上 | **无超时** |

### 发现的问题

1. **Chat 没有超时** — chat 发起的进程可以永远跑下去，占住 instance
2. **Chat 的 model fallback 不持久化** — 如果 `task.model` 为空，chat fallback 到当时分配到的 idle instance 的 model。不同 instance 可能有不同 model → 同一个任务每次 chat 可能用不同模型
3. **Goal/loop 不会在轮次间刷新设置** — `effort_level` 在 `_run_task_lifecycle` 开头 resolve 一次，作为参数一路传递。用户在 chat 设置面板里改了设置，正在运行的 goal/loop 任务看不到变化
4. **thinking_budget 只在 Instance 上** — 任务无法指定 thinking budget。被分配到哪个 instance 就用哪个的 budget，且不同 instance 可能配置不同
5. **Instance model 匹配增加复杂度** — `dequeue()` 和 `_ensure_instances_for_pending_tasks()` 都要处理 instance.model 和 task.model 的匹配逻辑，"default" 的等价判断等

## 方案设计

### 核心原则

**Instance 只是 worker 排队槽位。** 所有任务执行设置（model、effort_level、thinking_budget、timeout_hours）只存在 Task 上。Instance 不再参与设置解析。

### 1. 数据层变更

#### Task 表 — 加字段、改默认值

```python
# 新增
timeout_hours: Float | None        # NULL=全局默认, 0=不限时, >0=指定小时数
thinking_budget: Integer | None     # NULL=CLI 默认, >0=指定 token 数

# 已有但行为变更
model: String(100) | None          # NULL → 创建时自动填入 settings.default_model
effort_level: String(20) | None    # NULL → 创建时自动填入 settings.default_effort
```

**关键变更：创建任务时，如果 model/effort_level 为空，立即填入全局默认值。** 不再依赖 Instance fallback。这样 Task 上的值就是最终值，整个解析链变为：

```
旧：task.X → instance.X → settings.default_X  （三级 fallback）
新：task.X（创建时已填好默认值，不需要 fallback）
```

#### Instance 表 — 字段保留但不再使用

Instance 上的 `model`、`effort_level`、`thinking_budget` 字段**暂时保留**（避免破坏现有数据），但：
- Dispatcher 和 Chat 不再读取这些字段
- `_ensure_instances_for_pending_tasks()` 不再按 model 创建 instance
- `dequeue()` 不再按 instance.model 匹配
- 前端 Instance 创建表单移除这些配置项
- 后续版本可以通过 migration 移除这些列

### 2. Dispatcher 调度逻辑简化

#### dequeue — 移除 model 匹配

```python
# 旧：按 instance.model 匹配 task
task = await queue.dequeue(instance_model=instance.model, instance_provider=instance.provider)

# 新：任意 idle instance 领取最高优先级的 pending task
task = await queue.dequeue()
```

简化后的 `dequeue()`：
```python
async def dequeue(self) -> Task | None:
    """领取最高优先级的 pending 任务。不再按 model 匹配。"""
    stmt = (
        select(Task)
        .where(Task.status == "pending")
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(1)
    )
    result = await self.db.execute(stmt)
    task = result.scalar_one_or_none()
    if task:
        task.status = "in_progress"
        task.started_at = datetime.utcnow()
        task.error_message = None
        await self.db.commit()
        await self.db.refresh(task)
    return task
```

#### _ensure_instances_for_pending_tasks — 简化

不再按 model 创建 instance。只需确保有足够的 idle instance：
```python
async def _ensure_instances_for_pending_tasks(self):
    """如果有 pending 任务但没有 idle instance，自动创建一个。"""
    async with self.db_factory() as db:
        pending_count = await db.scalar(
            select(func.count()).where(Task.status == "pending")
        )
        if not pending_count:
            return
        idle_count = await db.scalar(
            select(func.count()).where(Instance.status == "idle")
        )
        if idle_count == 0:
            instance = Instance(name=f"worker-auto-{pending_count}")
            db.add(instance)
            await db.commit()
```

#### 设置解析 — 全部从 Task 读

```python
# 旧（_run_task_lifecycle）
thinking_budget = instance.thinking_budget
effort_level = task.effort_level or instance.effort_level or settings.default_effort
if not task.model and instance:
    resolved_model = instance.model if instance.model != "default" else None
    if resolved_model:
        task.model = resolved_model

# 新 — 直接从 task 读，不需要 fallback
model = task.model                    # 创建时已填好
effort_level = task.effort_level      # 创建时已填好
thinking_budget = task.thinking_budget # 可能为 None（= CLI 默认）
timeout = self._resolve_timeout(task)  # 新增
```

#### _resolve_timeout — 新增

```python
def _resolve_timeout(self, task: Task) -> float | None:
    """解析有效超时（秒）。None = 不限时。"""
    if task.timeout_hours is not None:
        return task.timeout_hours * 3600 if task.timeout_hours > 0 else None
    return settings.task_timeout_seconds
```

替换 5 处超时逻辑（auto、pool retry、loop、goal、plan），按返回值决定是否包裹 `wait_for`：
```python
timeout = self._resolve_timeout(task)
if timeout:
    await asyncio.wait_for(process.wait(), timeout=timeout)
else:
    await process.wait()
```

#### Goal/loop 每轮刷新设置

在 `_run_goal_lifecycle` 和 `_run_loop_lifecycle` 的循环体开头（已有取消检查的地方），同时刷新所有可变设置：

```python
async with self.db_factory() as db:
    t = await db.get(Task, task.id)
    if not t or t.status == "cancelled":
        return
    # 刷新可变设置（用户可能在 chat 设置面板中修改了这些值）
    task = t
    # model、effort_level、thinking_budget、timeout_hours 全部从 task 读，无需 fallback
```

### 3. Chat 路径 — 加超时 + 简化设置读取

#### 加超时（watchdog 机制）

在 `instance_manager.launch()` 增加 `timeout_seconds` 参数，传入时启动 watchdog 协程：

```python
async def launch(self, ..., timeout_seconds: float | None = None) -> int:
    ...
    consumer = asyncio.create_task(
        self._consume_output(instance_id, task_id, process, ...)
    )
    # 超时看门狗
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

#### 简化 chat.py 设置读取

```python
# 旧 — 需要从 instance 做 fallback
model = task.model or inst.model
effort_level = task.effort_level or inst.effort_level or app_settings.default_effort
thinking_budget = inst.thinking_budget

# 新 — 全部从 task 读
model = task.model
effort_level = task.effort_level
thinking_budget = task.thinking_budget
timeout_seconds = resolve_timeout(task)  # 复用同样的解析逻辑

pid = await instance_manager.launch(
    ...,
    model=model,
    effort_level=effort_level,
    thinking_budget=thinking_budget,
    timeout_seconds=timeout_seconds,
)
```

不再需要 "model resolve 后写回 task" 的逻辑，因为 task 上的值在创建时就已经是最终值了。

### 4. 前端 — TaskForm 加 Timeout 和 Thinking Budget

在 Effort 下拉后面加 Timeout 和 Thinking Budget：

```
[Priority] [Mode ▾] [Model ▾] [Effort ▾] [Timeout ▾] [Thinking Budget: ___]
```

**Timeout 下拉选项：**
- `""` → "2h (default)"
- `0.5` → "30 min"
- `1` → "1 hour"
- `2` → "2 hours"
- `4` → "4 hours"
- `8` → "8 hours"
- `12` → "12 hours"
- `24` → "24 hours"
- `0` → "No limit"

**Thinking Budget 输入框：**
- 数字输入，placeholder "CLI default"
- 空 = NULL（使用 CLI 默认）
- 填数字 = 指定 token 数

样式复用现有控件。

### 5. 前端 — ChatView 设置下拉面板

**齿轮图标**放在 header 中 Star 和 Interrupt 按钮之间：

```
[← Back] [Task #625 | Project | Session active | Context] [⚙️] [⭐] [Interrupt]
```

**新组件 `TaskSettingsDropdown`：**

```
┌───────────────────────────────────┐
│ Task Settings                      │
│                                    │
│ Model      [opus-4-6[1m]      ▾]  │
│ Effort     [xhigh              ▾]  │
│ Timeout    [2 hours            ▾]  │
│ Thinking   [________] tokens       │
│                                    │
│ ⓘ 修改在下一轮生效                  │
└───────────────────────────────────┘
```

- 点击外部关闭（复用 ProjectSelect 的 click-outside 模式）
- 每个字段变更时调用 `api.updateTask(taskId, { field: value })`
- Model/Effort 选项从 `api.config()` 获取（和 TaskForm 一致）
- Timeout 选项和 TaskForm 一致
- Thinking Budget 为数字输入框，清空 = 恢复 CLI 默认
- 底部始终显示 "修改在下一轮生效" 提示
- Props: `task: Task`, `onTaskUpdated: (task: Task) => void`

### 6. 前端 — InstanceGrid 简化

Instance 创建表单移除 Model、Effort、Thinking Budget 选项，只保留 Name：

```
旧：[Name] [Provider ▾] [Model ▾] [Effort ▾] [Thinking Budget] [+ Add]
新：[Name] [+ Add]
```

Instance 卡片上也不再显示 model/effort/thinking_budget 信息。

**注意：Provider 也不需要了。** 现在 Provider 在 Task 上已有，Instance 上的 Provider 失去意义。

### 7. 后端 Schema 更新

**TaskCreate：**
```python
class TaskCreate(BaseModel):
    ...  # 现有字段
    timeout_hours: float | None = None
    thinking_budget: int | None = None
    # model 和 effort_level 已有，但后端创建时如果为空要填入默认值
```

**TaskUpdate：**
```python
class TaskUpdate(BaseModel):
    ...  # 现有字段
    timeout_hours: float | None = None
    thinking_budget: int | None = None
    model: str | None = None
    effort_level: str | None = None
```

**TaskResponse：**
```python
class TaskResponse(BaseModel):
    ...  # 现有字段
    timeout_hours: float | None
    thinking_budget: int | None
```

**`updateTask`（client.ts）** — 扩展类型：
```typescript
updateTask: (id: number, data: {
  title?: string; description?: string; priority?: number;
  timeout_hours?: number | null;
  thinking_budget?: number | null;
  model?: string | null;
  effort_level?: string | null;
}) => ...
```

**InstanceCreate** — 简化：
```python
class InstanceCreate(BaseModel):
    name: str
    # model、effort_level、thinking_budget 移除
```

### 8. 任务创建时填充默认值

在 `tasks.py` 的 `create_task` 路由中，创建 Task 时自动填充默认值：

```python
@router.post("/api/tasks", status_code=201)
async def create_task(body: TaskCreate, ...):
    task = Task(
        ...
        model=body.model or settings.default_model,
        effort_level=body.effort_level or settings.default_effort,
        thinking_budget=body.thinking_budget,      # None = CLI 默认
        timeout_hours=body.timeout_hours,           # None = 全局超时
    )
```

这确保了 task.model 和 task.effort_level 在创建后一定有值，后续不需要任何 fallback。

### 9. 数据迁移

Alembic migration 需要处理：

1. Task 表加 `timeout_hours`（Float, nullable）和 `thinking_budget`（Integer, nullable）
2. **回填现有任务的 model 和 effort_level**：将 NULL 值填入全局默认值
3. Instance 表的字段暂不删除（保持向后兼容）

```python
def upgrade():
    # 加新列
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.add_column(sa.Column('timeout_hours', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('thinking_budget', sa.Integer(), nullable=True))

    # 回填 model/effort_level 默认值（只填 pending 状态的任务，已完成的不需要）
    op.execute(
        "UPDATE tasks SET model = 'claude-opus-4-6' WHERE model IS NULL AND status = 'pending'"
    )
    op.execute(
        "UPDATE tasks SET effort_level = 'medium' WHERE effort_level IS NULL AND status = 'pending'"
    )
```

## 修改文件清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | `backend/models/task.py` | 加 `timeout_hours`、`thinking_budget` 列 |
| 2 | `backend/schemas/task.py` | Create/Update/Response 加 `timeout_hours`、`thinking_budget`；Update 加 `model`、`effort_level` |
| 3 | `backend/schemas/instance.py` | InstanceCreate 移除 `model`、`effort_level`、`thinking_budget` |
| 4 | `backend/api/tasks.py` | 创建任务时填充 model/effort_level 默认值 |
| 5 | `backend/services/dispatcher.py` | 加 `_resolve_timeout()`；设置全部从 task 读不走 instance fallback；简化 dequeue 调用；简化 `_ensure_instances_for_pending_tasks`；goal/loop 每轮刷新设置 |
| 6 | `backend/services/task_queue.py` | `dequeue()` 移除 model/provider 匹配逻辑 |
| 7 | `backend/services/instance_manager.py` | `launch()` 加 `timeout_seconds` 参数 + watchdog 协程 |
| 8 | `backend/api/chat.py` | 设置全部从 task 读；解析超时传给 launch |
| 9 | `alembic/versions/xxx_task_settings_refactor.py` | 新 migration：加列 + 回填默认值 |
| 10 | `frontend/src/api/client.ts` | Task 接口加 `timeout_hours`、`thinking_budget`；扩展 createTask/updateTask；简化 Instance 接口和 createInstance |
| 11 | `frontend/src/components/Tasks/TaskForm.tsx` | 加 Timeout 下拉 + Thinking Budget 输入框 |
| 12 | `frontend/src/components/Chat/TaskSettingsDropdown.tsx` | 新组件 |
| 13 | `frontend/src/components/Chat/ChatView.tsx` | 加齿轮按钮 + 集成 dropdown |
| 14 | `frontend/src/components/Instances/InstanceGrid.tsx` | 创建表单移除 model/effort/thinking_budget 选项 |

## 验证步骤

1. `uv run alembic upgrade head` — migration 正常，现有数据的 model/effort 默认值已回填
2. `uv run python -m pytest backend/tests/ -v` — 测试通过
3. `cd frontend && npx tsc --noEmit` — 类型检查通过
4. 创建任务不指定 model → task.model 自动填入全局默认值
5. 创建任务指定 timeout=30min → dispatcher 30 分钟后 kill
6. 创建任务指定 timeout=No limit → 无限运行
7. 创建任务指定 thinking_budget=16000 → 子进程 env 中 MAX_THINKING_TOKENS=16000
8. Chat 发消息 → watchdog 在配置时间后触发
9. Chat 设置面板改 model → 下一轮 chat 用新 model
10. Chat 设置面板改 effort（goal 任务进行中）→ 下一轮 goal 用新 effort
11. Chat 设置面板改 thinking_budget → 下一轮生效
12. Instance 创建 → 只需填 name，无 model/effort/thinking_budget 选项
13. 不同 instance 领取同一个 task → 使用完全相同的设置（来自 task，不受 instance 影响）
