# Monitor 功能设计方案

## 概述

为 CCM 新增 Monitor（监控）功能，解决长时间运行任务无法主动汇报进度的问题。Monitor 通过**独立的 Claude session** 定期轮询检查任务进展并向用户汇报，与主任务并行运行、互不干扰。

Monitor 包含两个层面：

1. **Monitor 模式** — 与 loop/goal 并列的新任务模式，适用于"启动一个长任务然后定期检查"的场景
2. **监控叠加** — loop 和 goal 模式可选开启监控，为已有模式附加定期汇报能力

## 核心设计

### 架构：双 Session 并行

```
┌─────────────────────────────────────────────────┐
│  Task (monitor_enabled = true)                  │
│                                                 │
│  ┌──────────────┐    ┌────────────────────────┐ │
│  │  主 Session   │    │  Monitor Session (独立) │ │
│  │  执行任务     │    │  定期检查 + 汇报        │ │
│  │  (loop/goal/  │    │  每隔 interval 唤醒     │ │
│  │   monitor)    │    │  只读，不修改文件       │ │
│  └──────────────┘    └────────────────────────┘ │
│        ↓ 共享项目目录、日志文件 ↑                  │
└─────────────────────────────────────────────────┘
```

- **主 Session**：按原有模式逻辑执行任务（loop 的信号文件、goal 的评估器等）
- **Monitor Session**：完全独立的 Claude 进程，每隔 `monitor_interval` 秒启动一次，检查项目目录下的日志、进程状态、文件变化等，向用户汇报进展
- 两者**并行运行**，Monitor 从主 Session 启动的同时就开始轮询

### 各模式下的行为差异

| | 主 Session 完成判断 | Monitor 角色 | 主 Session 额外提示 |
|---|---|---|---|
| **Monitor 模式** | 由轮询 Session 判断 | 汇报 + 判断完成 | "尽量后台运行后退出，保持日志输出" |
| **Loop + 监控** | Signal file（原逻辑不变） | 仅汇报，不影响流程 | "已开启监控，请保持充分日志输出" |
| **Goal + 监控** | 评估器（原逻辑不变） | 仅汇报，不影响流程 | "已开启监控，请保持充分日志输出" |
| **Auto 模式** | 不可开启监控 | — | — |
| **Plan 模式** | 不可开启监控 | — | — |

### 监控开启规则

- **Monitor 模式**：默认开启监控，必须配置 `monitor_interval`
- **Loop / Goal 模式**：可选开启，开启后需配置 `monitor_interval`
- **Auto / Plan 模式**：不允许开启监控（前端不展示监控选项，后端做兜底校验）

## 数据模型变更

### Task 模型新增字段

```python
# backend/models/task.py

# mode 字段扩展：新增 "monitor" 选项
mode: Mapped[str] = mapped_column(String(20), default="auto")
# 可选值: "auto", "plan", "loop", "goal", "monitor"

# Monitor 相关字段
monitor_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
monitor_interval: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 轮询间隔（秒）
monitor_model: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 轮询用模型（默认 opus）
monitor_max_checks: Mapped[int] = mapped_column(Integer, default=100)  # 最大检查次数（兜底）
monitor_checks_done: Mapped[int] = mapped_column(Integer, default=0)  # 已完成检查次数
monitor_last_summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # 最近一次汇报摘要
```

### Schema 变更

```python
# backend/schemas/task.py

class TaskCreate(BaseModel):
    # ... 现有字段 ...
    monitor_enabled: bool = False
    monitor_interval: int | None = None      # 秒，如 300 = 5 分钟
    monitor_model: str | None = None         # 默认 claude-opus-4-6
    monitor_max_checks: int = 100

    @model_validator(mode="after")
    def validate_monitor(self):
        if self.mode == "monitor":
            self.monitor_enabled = True
            if not self.monitor_interval:
                raise ValueError("monitor 模式必须配置 monitor_interval")
        if self.monitor_enabled and not self.monitor_interval:
            raise ValueError("开启监控必须配置 monitor_interval")
        # Plan/Auto 模式不在前端展示监控选项，此处做兜底校验
        if self.monitor_enabled and self.mode in ("auto", "plan"):
            raise ValueError("该模式不支持开启监控")
        return self
```

### TaskResponse 新增字段

```python
class TaskResponse(BaseModel):
    # ... 现有字段 ...
    monitor_enabled: bool
    monitor_interval: int | None
    monitor_model: str | None
    monitor_max_checks: int
    monitor_checks_done: int
    monitor_last_summary: str | None
```

## 后端实现

### Dispatcher 变更

#### 1. 主流程入口（_execute_task_on_instance）

在现有模式分发逻辑后，启动 monitor 并行循环：

```python
# dispatcher.py

async def _execute_task_on_instance(self, instance_id, task, ...):
    # 先启动主 Session，拿到 session log 路径后再启动 monitor
    # （主 Session launch 后 session ID 和日志路径才确定）
    if task.mode == "monitor":
        await self._launch_monitor_main_session(instance_id, task, cwd, ...)
    elif task.mode == "loop":
        self._launch_loop_session(instance_id, task, cwd, ...)
    elif task.mode == "goal":
        self._launch_goal_session(instance_id, task, cwd, ...)
    else:
        # auto / plan
        ...

    # 主 Session 已 launch，现在可以拿到日志路径，启动 monitor 并行轮询
    if task.monitor_enabled and task.monitor_interval:
        session_log_path = self.instance_manager.get_session_log_path(instance_id)
        monitor_task = asyncio.create_task(
            self._run_monitor_loop(task, instance_id, session_log_path)
        )
        # 注册到 _monitor_tasks，供 cancel_task 使用
        self._monitor_tasks[task.id] = monitor_task

    # 等待主任务执行完成
    if task.mode == "monitor":
        await self._wait_monitor_main_session(instance_id, task)
    elif task.mode == "loop":
        await self._run_loop_lifecycle(instance_id, task, cwd, ...)
    elif task.mode == "goal":
        await self._run_goal_lifecycle(instance_id, task, cwd, ...)

    # 主任务结束后，如果是 loop/goal 模式，取消 monitor
    if task.id in self._monitor_tasks and task.mode != "monitor":
        self._monitor_tasks[task.id].cancel()
        del self._monitor_tasks[task.id]
```

#### 2. Monitor 模式主 Session

```python
async def _launch_monitor_main_session(self, instance_id, task, cwd, ...):
    """Monitor 模式：启动主 Session 执行用户任务。
    仅 launch，不等待完成（等待在 _wait_monitor_main_session 中）。"""

    prompt = self._build_monitor_main_prompt(task)

    await self.instance_manager.launch(
        instance_id=instance_id,
        prompt=prompt,
        task_id=task.id,
        cwd=cwd,
        model=task.model,
        ...
    )

async def _wait_monitor_main_session(self, instance_id, task):
    """等待 Monitor 模式主 Session 进程结束。
    不标记 completed，任务完成由 _run_monitor_loop 判断。"""

    process = self.instance_manager.processes.get(instance_id)
    if process:
        await process.wait()
```

#### 3. Monitor 轮询循环（核心）

```python
async def _run_monitor_loop(self, task: Task, instance_id: str, session_log_path: str):
    """独立的监控轮询循环，与主任务并行运行。
    每隔 interval 秒启动一个新的 Claude session 检查进度。

    Args:
        task: 任务对象
        instance_id: 主 Session 的 instance ID，用于检查主进程存活状态
        session_log_path: 主 Session 的 JSONL 日志路径，传给 monitor prompt
    """

    cwd = task.last_cwd or task.target_repo
    signal_path = Path(cwd) / ".claude-manager" / f"monitor_signal_{task.id}.json"
    signal_path.parent.mkdir(parents=True, exist_ok=True)
    checks_done = 0

    while checks_done < task.monitor_max_checks:
        await asyncio.sleep(task.monitor_interval)

        # 检查 task 是否已被取消/删除/完成（loop/goal 模式可能已结束）
        async with self.db_factory() as db:
            t = await db.get(Task, task.id)
            if not t or t.status in ("cancelled", "completed", "failed"):
                return

        # 检查主进程是否还在运行
        process = self.instance_manager.processes.get(instance_id)
        main_alive = process is not None and process.returncode is None

        # 清除 signal file
        signal_path.unlink(missing_ok=True)

        # 构建轮询 prompt（根据主进程状态使用不同策略）
        prompt = self._build_monitor_check_prompt(
            task, checks_done, signal_path,
            main_alive=main_alive,
            session_log_path=session_log_path,
        )

        # 启动独立 Claude 进程（不复用主 session，不占用 instance）
        result = await self._run_monitor_subprocess(
            prompt=prompt,
            cwd=cwd,
            model=task.monitor_model or "claude-opus-4-6",
            task_id=task.id,
        )

        checks_done += 1

        # 读取 signal file
        signal = self._read_monitor_signal(signal_path)
        summary = signal.get("summary", "")

        # 写入 monitor_checks 表（持久化每次检查的完整记录）
        async with self.db_factory() as db:
            check_record = MonitorCheck(
                task_id=task.id,
                check_number=checks_done,
                status=signal.get("status", "running"),
                summary=summary,
                full_output=result,
                main_alive=main_alive,
            )
            db.add(check_record)
            # 同时更新 Task 表的检查计数和摘要
            await db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    monitor_checks_done=checks_done,
                    monitor_last_summary=summary,
                )
            )
            await db.commit()

        # 广播监控汇报
        await self.broadcaster.broadcast(f"task:{task.id}", {
            "event_type": "monitor_check",
            "check_number": checks_done,
            "summary": summary,
            "status": signal.get("status", "running"),
            "main_alive": main_alive,
        })

        # 判断是否完成（仅 monitor 模式）
        if task.mode == "monitor" and signal.get("status") == "done":
            async with self.db_factory() as db:
                queue = TaskQueue(db)
                await queue.mark_completed(task.id)
            await self.broadcaster.broadcast("tasks", {
                "event": "status_change",
                "task_id": task.id,
                "new_status": "completed",
            })
            # kill 主进程，让 _wait_monitor_main_session 返回、释放 instance
            await self._kill_main_process(instance_id)
            self._monitor_tasks.pop(task.id, None)
            return

    # while 循环正常退出 = max_checks 耗尽
    # 仅 monitor 模式需要处理（loop/goal 的 monitor 会被 cancel，不会走到这里）
    if task.mode == "monitor":
        async with self.db_factory() as db:
            queue = TaskQueue(db)
            await queue.mark_failed(
                task.id,
                error=f"监控已达最大检查次数 {task.monitor_max_checks}，未能判断任务完成"
            )
        await self.broadcaster.broadcast("tasks", {
            "event": "status_change",
            "task_id": task.id,
            "new_status": "failed",
            "reason": "monitor_max_checks_exhausted",
        })
        # kill 主进程，让 _wait_monitor_main_session 返回、释放 instance
        await self._kill_main_process(instance_id)

    # 清理 _monitor_tasks 注册
    self._monitor_tasks.pop(task.id, None)

async def _kill_main_process(self, instance_id: str):
    """终止主 Session 进程，释放 instance。"""
    process = self.instance_manager.processes.get(instance_id)
    if process and process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
```

#### 4. Monitor 轮询子进程

```python
async def _run_monitor_subprocess(self, prompt, cwd, model, task_id) -> str:
    """启动一个独立的 Claude 进程进行监控检查。
    不占用 instance，不使用 --resume。
    返回 monitor session 的完整文本输出（用于存入 monitor_checks.full_output）。"""

    cmd = [
        settings.claude_binary,
        "-p", prompt,
        "--model", model,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        # 禁止写操作工具，只保留 Read + Bash（Bash 中写 signal file 由 prompt 控制）
        "--disallowedTools", "Edit", "Write", "NotebookEdit",
    ]

    env = {
        k: v for k, v in os.environ.items()
        if k.upper() not in ("CLAUDECODE", "CLAUDE_CODE")
    }

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    # 消费输出：收集完整文本 + 实时广播到前端
    full_output = await self._consume_monitor_output(process, task_id)

    await asyncio.wait_for(process.wait(), timeout=120)
    return full_output

async def _consume_monitor_output(self, process, task_id) -> str:
    """解析 monitor 子进程的 stream-json 输出。
    - 实时广播每条消息到前端（通过 task:{id} channel，标记 source="monitor"）
    - 收集 assistant 文本并返回完整输出

    stream-json 格式：每行一个 JSON，包含 type 字段（如 "assistant", "tool_use", "tool_result" 等）。
    """
    full_text_parts = []

    async for line in process.stdout:
        line = line.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # 提取 assistant 文本
        if event.get("type") == "assistant" and "content" in event:
            for block in event["content"]:
                if block.get("type") == "text":
                    full_text_parts.append(block["text"])

        # 实时广播到前端，标记为 monitor 输出以区分主 Session
        await self.broadcaster.broadcast(f"task:{task_id}", {
            "event_type": "monitor_output",
            "data": event,
        })

    return "\n".join(full_text_parts)
```

#### 5. Signal File 读取

```python
def _read_monitor_signal(self, signal_path: Path) -> dict:
    """读取 monitor 写入的 signal file，返回解析后的 dict。
    容错处理：文件不存在、JSON 格式错误、字段缺失时返回安全默认值。"""

    if not signal_path.exists():
        return {"status": "running", "summary": "（signal file 未生成）"}

    try:
        text = signal_path.read_text().strip()
        data = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return {"status": "running", "summary": "（signal file 解析失败）"}

    # 确保必要字段存在
    return {
        "status": data.get("status", "running"),
        "summary": data.get("summary", ""),
    }
```

### Prompt 构建

#### Monitor 模式主 Session Prompt

```python
def _build_monitor_main_prompt(self, task: Task) -> str:
    return (
        "请阅读项目根目录的 CLAUDE.md 了解项目规范。\n\n"
        "【监控模式】这是一个监控任务。请执行用户的任务后尽量让自己退出：\n"
        "- 对于需要长时间运行的进程（训练、构建等），请放到后台运行"
        "（nohup/tmux/screen），确保日志输出到文件\n"
        "- 如果任务确实需要你持续操作，可以继续运行\n"
        "- 确保有清晰的日志文件，监控进程会依据日志判断任务进展\n\n"
        f"任务:\n{task.description}"
    )
```

#### Monitor 轮询 Prompt

Monitor 的检查策略按主进程状态分两套 prompt，信息源优先级：
1. **Claude Code session JSONL 日志** — 最有价值，能看到主 Session 的完整操作链（一读一写，无并发安全问题）
2. **git status / git diff** — 了解文件变更结果
3. **ps aux** — 确认进程存活状态
4. **用户任务产生的日志文件** — 如果有的话

JSONL 日志路径由 dispatcher 传入（启动主 Session 时已知 session ID 和路径），不让 monitor 自己去找。

```python
def _build_monitor_check_prompt(self, task, check_number, signal_path, main_alive, session_log_path):
    base = (
        f"你是一个监控进程，这是第 {check_number + 1} 次检查。\n\n"
        f"需要监控的任务:\n{task.description}\n\n"
    )

    if main_alive:
        base += (
            "主 Session 仍在运行中，请通过以下方式了解进展：\n"
            f"1. 读取主 Session 的日志文件 {session_log_path}（JSONL 格式，读最后 200 行即可）了解当前操作\n"
            "2. 运行 git status / git diff 查看已有的文件变更\n"
            "3. 不要修改任何文件，不要干扰主 Session 的工作\n"
            "4. 不需要判断任务是否完成，只需汇报当前进展\n"
        )
    else:
        base += (
            "主 Session 已退出。请检查：\n"
            "1. 是否有后台进程仍在运行（ps aux 检查相关进程）\n"
            f"2. 查看主 Session 日志 {session_log_path} 了解最终状态\n"
            "3. 查看 git diff 了解最终变更\n"
            "4. 判断任务是已完成还是异常退出\n"
        )

    base += (
        "\n【重要】你是只读的监控者，除了写 signal file 外不要修改任何文件或执行任何会影响任务的操作。\n\n"
        "用中文写一句话摘要（不超过 200 字）。\n"
        "检查完毕后，将结果写入 signal file：\n"
        f"echo '{{\"status\": \"running 或 done\", \"summary\": \"简短进度摘要\"}}' > {signal_path}\n"
        "- status: 任务还在进行写 running，已完成写 done\n"
        "- summary: 中文一句话描述当前进度\n"
    )
    return base
```

#### Loop/Goal + 监控的额外提示

在现有的 `_build_loop_prompt` 和 `_build_goal_initial_prompt` 中追加：

```python
def _get_monitor_hint(self, task: Task) -> str:
    if not task.monitor_enabled:
        return ""
    return (
        "\n\n【监控已开启】有独立的监控进程会每隔 "
        f"{task.monitor_interval} 秒检查任务进展。"
        "请在执行过程中保持充分的日志输出（进度、状态、关键结果），"
        "方便监控进程判断进展并向用户汇报。"
    )
```

## 中断逻辑

### 用户点击中断时需要停止的内容

| 模式 | 停止主 Session | 停止 Monitor 轮询 |
|---|---|---|
| Monitor 模式 | 如果还在跑，kill 掉 | 取消轮询循环 |
| Loop + 监控 | kill 当前迭代进程 | 取消轮询循环 |
| Goal + 监控 | kill 当前 turn 进程 | 取消轮询循环 |

### 实现

```python
async def cancel_task(self, task_id: int):
    # 现有逻辑：停止主进程
    ...

    # 新增：取消 monitor 循环
    monitor_handle = self._monitor_tasks.get(task_id)
    if monitor_handle:
        monitor_handle.cancel()
        del self._monitor_tasks[task_id]
```

Dispatcher 需要维护一个 `_monitor_tasks: dict[int, asyncio.Task]` 来追踪活跃的 monitor 循环。

## 前端变更

### 1. TaskForm — 新增 Monitor 相关控件

**模式选择扩展：**

```
Mode: [auto] [plan] [loop] [goal] [monitor]  ← 新增
```

**Monitor 模式选中时显示：**

```
┌─────────────────────────────────────┐
│ 轮询间隔:  [300] 秒 (5 分钟)         │
│ 监控模型:  [claude-opus-4-6   ▼]    │
│ 最大检查次数: [100]                   │
└─────────────────────────────────────┘
```

**Loop / Goal 模式时显示可选监控开关：**

```
┌─────────────────────────────────────┐
│ ☐ 开启监控                          │
│   （勾选后展开 interval/model 配置）  │
└─────────────────────────────────────┘
```

### 2. ChatView — 监控输出折叠区域

在 Chat 主区域之外，新增一个可折叠的监控面板：

```
┌─────────────────────────────────────────────┐
│  Chat 主区域（主 Session 输出）               │
│  [User] 运行训练脚本 train.py                │
│  [Claude] 已启动训练，日志输出到 train.log... │
│  ...                                        │
├─────────────────────────────────────────────┤
│  ▼ 监控汇报 (3/100)          下次检查: 2:34  │
│  ┌─────────────────────────────────────────┐│
│  │ [#3] 训练进行中，epoch 45/100，          ││
│  │      loss=0.23，预计还需 40 分钟          ││
│  ├─────────────────────────────────────────┤│
│  │ [#2] 训练进行中，epoch 30/100，          ││
│  │      loss=0.31                           ││
│  ├─────────────────────────────────────────┤│
│  │ [#1] 训练已启动，epoch 5/100，           ││
│  │      loss=1.24                           ││
│  └─────────────────────────────────────────┘│
└─────────────────────────────────────────────┘
```

**关键 UI 元素：**

- **折叠标题**：显示已检查次数 / 最大次数，下次检查倒计时
- **汇报列表**：最新的在最上面，每条显示检查编号 + 摘要
- **展开详情**：点击单条可展开完整的 monitor session 输出
- **视觉区分**：用不同背景色或边框与主 Chat 输出区分

### 3. WebSocket 事件

新增 `monitor_check` 事件类型：

```typescript
// 每次检查完成后的汇报摘要
interface MonitorCheckEvent {
    event_type: "monitor_check";
    check_number: number;
    summary: string;
    status: "running" | "done";
    main_alive: boolean;
}

// monitor 子进程的实时流式输出（用于"展开详情"）
interface MonitorOutputEvent {
    event_type: "monitor_output";
    data: object;  // stream-json 的原始事件
}
```

前端在 `task:{id}` channel 上监听这两类事件：
- `monitor_check`：更新折叠面板的摘要列表
- `monitor_output`：实时填充当前正在进行的检查的详情内容

## 数据存储

### MonitorCheck 模型（新表）

每次 monitor 检查的完整记录，支持前端"展开详情"查看历史汇报。

```python
# backend/models/monitor_check.py

class MonitorCheck(Base):
    __tablename__ = "monitor_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), index=True)
    check_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))       # "running" | "done"
    summary: Mapped[str] = mapped_column(Text)             # 中文摘要
    full_output: Mapped[str | None] = mapped_column(Text, nullable=True)  # monitor session 完整输出
    main_alive: Mapped[bool] = mapped_column(Boolean)      # 检查时主进程是否在运行
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

写入逻辑已集成在 `_run_monitor_loop` 主循环中（每次检查完成后同时写入 monitor_checks 和更新 Task 表）。

### API 端点

```python
# 获取某个任务的所有 monitor 检查记录
@router.get("/tasks/{task_id}/monitor-checks")
async def get_monitor_checks(task_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(MonitorCheck)
        .where(MonitorCheck.task_id == task_id)
        .order_by(MonitorCheck.check_number.desc())
    )
    return result.scalars().all()
```

## Alembic Migration

```python
"""add monitor fields and monitor_checks table

Revision ID: xxxx
"""

def upgrade():
    # Task 表新增字段
    op.add_column('tasks', sa.Column('monitor_enabled', sa.Boolean(), default=False))
    op.add_column('tasks', sa.Column('monitor_interval', sa.Integer(), nullable=True))
    op.add_column('tasks', sa.Column('monitor_model', sa.String(100), nullable=True))
    op.add_column('tasks', sa.Column('monitor_max_checks', sa.Integer(), default=100))
    op.add_column('tasks', sa.Column('monitor_checks_done', sa.Integer(), default=0))
    op.add_column('tasks', sa.Column('monitor_last_summary', sa.Text(), nullable=True))

    # 新建 monitor_checks 表
    op.create_table(
        'monitor_checks',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('task_id', sa.Integer(), sa.ForeignKey('tasks.id'), nullable=False, index=True),
        sa.Column('check_number', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('full_output', sa.Text(), nullable=True),
        sa.Column('main_alive', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

def downgrade():
    op.drop_table('monitor_checks')
    op.drop_column('tasks', 'monitor_last_summary')
    op.drop_column('tasks', 'monitor_checks_done')
    op.drop_column('tasks', 'monitor_max_checks')
    op.drop_column('tasks', 'monitor_model')
    op.drop_column('tasks', 'monitor_interval')
    op.drop_column('tasks', 'monitor_enabled')
```

## 实现顺序建议

1. **Phase 1 — 数据层**：Task 模型 + Schema + Migration
2. **Phase 2 — 后端核心**：Dispatcher monitor 轮询循环 + 子进程调用 + Prompt 构建
3. **Phase 3 — 前端 TaskForm**：Monitor 模式选项 + 监控开关 + 配置表单
4. **Phase 4 — 前端 ChatView**：监控折叠面板 + WebSocket 事件处理
5. **Phase 5 — 中断逻辑**：取消时同时停止主进程 + monitor 循环
6. **Phase 6 — 测试**：各模式组合测试（monitor 单独、loop+监控、goal+监控）
