# Monitor Session 设计方案（v2）

## 概述

为 CCM 新增 Monitor Session（监控会话）功能。Monitor Session 是一个**独立的 Claude 子 session**，可以挂在任何 task 下面，定期检查后台任务状态并汇报。

与 v1 方案的核心区别：**Monitor 不再是一个独立模式，而是一个通用能力**，可以附加到任何模式的 task 上，且能参与任务决策。

### 解决的核心问题

1. **Loop 模式资源串行**：训练 10 个模型但 GPU 只能跑 1 个，loop 迭代间需要等待后台任务完成再开下一轮
2. **Auto 模式 session 占用**：用户让 Claude 训练+监控，监控占着 session 无法继续对话
3. **进度可见性**：长时间后台任务缺乏进度汇报

### 设计原则

- Monitor Session 是**通用的独立子 session**，底层统一，行为由 prompt 决定
- 是否参与决策（门控 vs 纯观察）由 prompt 逻辑控制，不在系统层面区分
- 每个 task 可以挂多个 Monitor Session
- Monitor Session 不支持对话，但可以被用户删除
- Monitor Session 与主 session 完全解耦

## 各模式下的接入方式

### Loop 模式 — 迭代间门控

```
迭代 N
  ↓
Claude 写 signal: { action: "continue", needs_monitor: true, monitor_context: "..." }
  ↓
Dispatcher 读 signal，发现 needs_monitor=true
  ↓
启动 Monitor Session，定期轮询检查
  ↓
Monitor 确认后台任务完成，写 signal: { status: "done" }
  ↓
Dispatcher 读 monitor signal，开始迭代 N+1
```

**触发条件**：Claude 在 loop signal file 中设置 `needs_monitor: true`，由 Claude 自行判断是否需要。

**Prompt 引导**：loop prompt 中告知 Claude——如果启动了后台任务（训练、构建等），在 signal 中设置 `needs_monitor: true` 和 `monitor_context`（描述启动了什么进程、PID、日志路径等）。

### Goal 模式 — evaluation 间门控

与 loop 类似，在 evaluation 之间插入 monitor 检查。goal 的 signal 同样扩展 `needs_monitor` 字段。

### Auto 模式 — 用户手动创建

```
主 Chat（Auto）
├── 用户正常对话
├── [+ 新建监控] 按钮
│   └── 弹出输入框，描述要监控什么
│       例如："监控 PID 12345 的训练进度，日志在 /tmp/train.log"
├── 监控列表
│   ├── Monitor #1: "监控训练进度" — 运行中 (3/100)
│   └── Monitor #2: "监控磁盘空间" — 运行中 (1/100)
└── 点击 Monitor 可查看详情，可删除
```

## 数据模型

### MonitorSession 模型（新表）

```python
# backend/models/monitor_session.py

class MonitorSession(Base):
    __tablename__ = "monitor_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), index=True)

    # 监控配置
    description: Mapped[str] = mapped_column(Text)                # 用户/系统描述的监控内容
    monitor_context: Mapped[str | None] = mapped_column(Text, nullable=True)  # 来自 signal file 的上下文（PID、日志路径等）
    interval: Mapped[int] = mapped_column(Integer, default=300)   # 轮询间隔（秒）
    max_checks: Mapped[int] = mapped_column(Integer, default=100)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 默认 claude-opus-4-6

    # 状态
    status: Mapped[str] = mapped_column(String(20), default="running")
    # 可选值: "running", "completed", "failed", "cancelled"
    checks_done: Mapped[int] = mapped_column(Integer, default=0)
    last_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 来源
    source: Mapped[str] = mapped_column(String(20), default="manual")
    # 可选值: "manual"（用户手动创建）, "loop"（loop 迭代间自动创建）, "goal"（goal 间自动创建）

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

### MonitorCheck 模型（新表）

每次检查的完整记录。

```python
# backend/models/monitor_check.py

class MonitorCheck(Base):
    __tablename__ = "monitor_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    monitor_session_id: Mapped[int] = mapped_column(Integer, ForeignKey("monitor_sessions.id"), index=True)
    check_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))       # "running" | "done"
    summary: Mapped[str] = mapped_column(Text)             # 中文摘要
    full_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
```

### Loop Signal File 扩展

```json
{
    "action": "continue",
    "reason": "已启动 model_a 训练",
    "progress": "1/10",
    "summary": "第一个模型训练已启动",
    "needs_monitor": true,
    "monitor_context": "启动了 model_a 训练 (PID 1234, 日志 /tmp/train_a.log)，需要等待训练完成后再开始下一个模型"
}
```

新增字段：
- `needs_monitor: bool` — 是否需要 monitor session 门控下一轮
- `monitor_context: string` — 给 monitor session 的上下文信息（启动了什么进程、PID、日志路径等）

## 后端实现

### 1. Loop 模式集成（_run_loop_lifecycle 变更）

在现有 loop 循环的 `action == "continue"` 分支中，增加 monitor 等待逻辑：

```python
# dispatcher.py — _run_loop_lifecycle 中 action=="continue" 的处理

if action == "continue":
    # 检查是否需要 monitor 门控
    if signal.get("needs_monitor"):
        monitor_context = signal.get("monitor_context", "")

        # 创建 MonitorSession 记录（使用默认配置）
        async with self.db_factory() as db:
            monitor_session = MonitorSession(
                task_id=task.id,
                description=f"Loop 迭代 {iteration} 后台任务监控",
                monitor_context=monitor_context,
                interval=300,       # 默认 5 分钟
                max_checks=100,     # 默认最多 100 次
                model="claude-opus-4-6",
                source="loop",
            )
            db.add(monitor_session)
            await db.commit()
            await db.refresh(monitor_session)

        # 广播：monitor session 创建
        await self.broadcaster.broadcast(f"task:{task.id}", {
            "event_type": "monitor_session_created",
            "monitor_session_id": monitor_session.id,
            "description": monitor_session.description,
        })

        # 阻塞等待 monitor 确认完成
        # loop gate monitor 不设 max_checks 上限，只要后台进程还活着就一直检查
        completed = await self._run_monitor_session(monitor_session, task)

        if not completed:
            # 任务被用户取消，退出 loop
            return

    iteration += 1
    continue
```

### 2. Monitor Session 执行核心

```python
async def _run_monitor_session(self, monitor_session: MonitorSession, task: Task) -> bool:
    """执行 monitor session 的轮询循环。
    返回 True 表示后台任务完成，False 表示被取消或 max_checks 耗尽。

    循环策略：
    - source="loop"/"goal"（系统 monitor）：无 max_checks 限制，一直检查直到 done 或任务取消
    - source="manual"（用户 monitor）：受 max_checks 限制
    """

    cwd = task.last_cwd or task.target_repo
    signal_path = Path(cwd) / ".claude-manager" / f"monitor_signal_{monitor_session.id}.json"
    signal_path.parent.mkdir(parents=True, exist_ok=True)
    has_limit = monitor_session.source == "manual"

    while True:
        # manual monitor 有次数限制
        if has_limit and monitor_session.checks_done >= monitor_session.max_checks:
            break

        await asyncio.sleep(monitor_session.interval)

        # 检查 task 是否已被取消
        async with self.db_factory() as db:
            t = await db.get(Task, task.id)
            if not t or t.status in ("cancelled", "completed", "failed"):
                return False

        # 检查 monitor session 是否已被取消（用户取消整个任务，或手动删除 manual monitor）
        async with self.db_factory() as db:
            ms = await db.get(MonitorSession, monitor_session.id)
            if not ms or ms.status == "cancelled":
                return False

        # 清除 signal file
        signal_path.unlink(missing_ok=True)

        # 构建 prompt
        prompt = self._build_monitor_session_prompt(monitor_session, task)

        # 启动独立 Claude 进程
        result = await self._run_monitor_subprocess(
            prompt=prompt,
            cwd=cwd,
            model=monitor_session.model or "claude-opus-4-6",
            task_id=task.id,
            monitor_session_id=monitor_session.id,
        )

        monitor_session.checks_done += 1

        # 读取 signal file
        signal = self._read_monitor_signal(signal_path)
        summary = signal.get("summary", "")

        # 写入 monitor_checks 表
        async with self.db_factory() as db:
            check_record = MonitorCheck(
                monitor_session_id=monitor_session.id,
                check_number=monitor_session.checks_done,
                status=signal.get("status", "running"),
                summary=summary,
                full_output=result,
            )
            db.add(check_record)
            # 更新 monitor session 状态
            await db.execute(
                update(MonitorSession)
                .where(MonitorSession.id == monitor_session.id)
                .values(
                    checks_done=monitor_session.checks_done,
                    last_summary=summary,
                )
            )
            await db.commit()

        # 广播监控汇报
        await self.broadcaster.broadcast(f"task:{task.id}", {
            "event_type": "monitor_check",
            "monitor_session_id": monitor_session.id,
            "check_number": monitor_session.checks_done,
            "summary": summary,
            "status": signal.get("status", "running"),
        })

        # 判断是否完成
        if signal.get("status") == "done":
            async with self.db_factory() as db:
                await db.execute(
                    update(MonitorSession)
                    .where(MonitorSession.id == monitor_session.id)
                    .values(status="completed", completed_at=func.now())
                )
                await db.commit()
            await self.broadcaster.broadcast(f"task:{task.id}", {
                "event_type": "monitor_session_status",
                "monitor_session_id": monitor_session.id,
                "status": "completed",
            })
            return True

    # max_checks 耗尽（仅 manual monitor 会走到这里）
    async with self.db_factory() as db:
        await db.execute(
            update(MonitorSession)
            .where(MonitorSession.id == monitor_session.id)
            .values(status="failed")
        )
        await db.commit()
    await self.broadcaster.broadcast(f"task:{task.id}", {
        "event_type": "monitor_session_status",
        "monitor_session_id": monitor_session.id,
        "status": "failed",
    })
    return False
```

### 3. Auto 模式 — 手动创建 Monitor Session

用户通过前端按钮创建，后端提供 API：

```python
# backend/routers/monitor.py

@router.post("/tasks/{task_id}/monitor-sessions")
async def create_monitor_session(
    task_id: int,
    request: MonitorSessionCreate,
    db: AsyncSession = Depends(get_db),
):
    """用户手动创建 monitor session（Auto 模式等场景）"""
    monitor_session = MonitorSession(
        task_id=task_id,
        description=request.description,
        interval=request.interval or 300,
        max_checks=request.max_checks or 100,
        model=request.model,
        source="manual",
    )
    db.add(monitor_session)
    await db.commit()
    await db.refresh(monitor_session)

    # 启动后台轮询（非阻塞，不影响主 session）
    asyncio.create_task(
        dispatcher._run_monitor_session_background(monitor_session, task_id)
    )

    return monitor_session

@router.delete("/tasks/{task_id}/monitor-sessions/{session_id}")
async def delete_monitor_session(
    task_id: int,
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """用户删除 monitor session（仅限 manual monitor）"""
    # 校验归属 + 来源
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")
    if ms.source != "manual":
        raise HTTPException(403, "System monitor cannot be deleted")

    await db.execute(
        update(MonitorSession)
        .where(MonitorSession.id == session_id)
        .values(status="cancelled")
    )
    await db.commit()

    # 取消正在运行的轮询
    monitor_handle = dispatcher._monitor_tasks.get(session_id)
    if monitor_handle:
        monitor_handle.cancel()
        del dispatcher._monitor_tasks[session_id]

    return {"status": "cancelled"}

@router.get("/tasks/{task_id}/monitor-sessions")
async def list_monitor_sessions(
    task_id: int,
    db: AsyncSession = Depends(get_db),
):
    """获取 task 下所有 monitor sessions"""
    result = await db.execute(
        select(MonitorSession)
        .where(MonitorSession.task_id == task_id)
        .order_by(MonitorSession.created_at.desc())
    )
    return result.scalars().all()

@router.get("/tasks/{task_id}/monitor-sessions/{session_id}/checks")
async def get_monitor_checks(
    task_id: int,
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """获取某个 monitor session 的所有检查记录"""
    result = await db.execute(
        select(MonitorCheck)
        .where(MonitorCheck.monitor_session_id == session_id)
        .order_by(MonitorCheck.check_number.desc())
    )
    return result.scalars().all()
```

### 4. 后台运行（Auto 模式专用）

Auto 模式的 monitor 是非阻塞的，不门控任何流程：

```python
async def _run_monitor_session_background(self, monitor_session, task_id):
    """后台运行 monitor session（Auto 模式用）。
    与 _run_monitor_session 逻辑相同，但不阻塞主流程。"""

    async with self.db_factory() as db:
        task = await db.get(Task, task_id)
        if not task:
            return

    # 注册到 _monitor_tasks
    self._monitor_tasks[monitor_session.id] = asyncio.current_task()

    try:
        await self._run_monitor_session(monitor_session, task)
    except asyncio.CancelledError:
        pass
    finally:
        self._monitor_tasks.pop(monitor_session.id, None)
```

### 5. Monitor Session Prompt

```python
def _build_monitor_session_prompt(self, monitor_session: MonitorSession, task: Task) -> str:
    base = (
        f"你是一个监控进程，这是第 {monitor_session.checks_done + 1} 次检查。\n\n"
        f"监控任务描述:\n{monitor_session.description}\n\n"
    )

    if monitor_session.monitor_context:
        base += (
            f"任务上下文（来自主 session）:\n{monitor_session.monitor_context}\n\n"
        )

    base += (
        "请检查相关后台任务的执行状态：\n"
        "1. 检查相关进程是否还在运行（ps aux）\n"
        "2. 查看日志文件的最新内容\n"
        "3. 判断任务是否完成\n\n"
        "【重要】你是只读的监控者，除了写 signal file 外不要修改任何文件。\n\n"
        "用中文写一句话摘要。\n"
        "检查完毕后，将结果写入 signal file：\n"
        f"echo '{{\"status\": \"running 或 done\", \"summary\": \"简短进度摘要\"}}' > "
        f"{self._get_monitor_signal_path(monitor_session.id, task.last_cwd or task.target_repo)}\n"
        "- status: 任务还在进行写 running，已完成写 done\n"
        "- summary: 中文一句话描述当前进度\n"
    )
    return base
```

### 6. Monitor 子进程

```python
async def _run_monitor_subprocess(self, prompt, cwd, model, task_id, monitor_session_id) -> str:
    """启动独立 Claude 进程执行一次监控检查。
    返回完整文本输出。"""

    cmd = [
        settings.claude_binary,
        "-p", prompt,
        "--model", model,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--disallowedTools", "Edit,Write,NotebookEdit",
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

    try:
        full_output = await asyncio.wait_for(
            self._consume_monitor_output(process, task_id, monitor_session_id),
            timeout=300,
        )
    except asyncio.TimeoutError:
        process.kill()
        full_output = "（监控子进程超时，已终止）"
    return full_output

async def _consume_monitor_output(self, process, task_id, monitor_session_id) -> str:
    """解析 stream-json 输出，实时广播，返回完整文本。"""
    full_text_parts = []

    async for line in process.stdout:
        line = line.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "assistant" and "content" in event:
            for block in event["content"]:
                if block.get("type") == "text":
                    full_text_parts.append(block["text"])

        await self.broadcaster.broadcast(f"task:{task_id}", {
            "event_type": "monitor_output",
            "monitor_session_id": monitor_session_id,
            "data": event,
        })

    return "\n".join(full_text_parts)
```

### 7. Signal File 路径与读取

```python
def _get_monitor_signal_path(self, monitor_session_id: int, cwd: str) -> Path:
    """获取 monitor session 的 signal file 路径。"""
    return Path(cwd) / ".claude-manager" / f"monitor_signal_{monitor_session_id}.json"

def _read_monitor_signal(self, signal_path: Path) -> dict:
    """读取 signal file，容错处理。"""
    if not signal_path.exists():
        return {"status": "running", "summary": "（signal file 未生成）"}

    try:
        text = signal_path.read_text().strip()
        data = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return {"status": "running", "summary": "（signal file 解析失败）"}

    return {
        "status": data.get("status", "running"),
        "summary": data.get("summary", ""),
    }
```

### 8. Loop Prompt 变更

在现有 `_build_loop_prompt` 中追加 monitor 相关引导：

```python
def _get_loop_monitor_hint(self) -> str:
    return (
        "\n\n【后台任务监控】如果你在本次迭代中启动了后台任务（训练、构建等），"
        "请在 signal file 中设置以下字段：\n"
        '- "needs_monitor": true\n'
        '- "monitor_context": "描述启动了什么进程、PID、日志路径等"\n'
        "这样系统会启动独立的监控 session 检查后台任务是否完成，确认完成后才开始下一次迭代。\n"
        "如果你的操作是同步的（改代码、写文件等），不需要设置这些字段。\n\n"
        "【资源竞争注意】如果任务需要独占资源（GPU、特定端口、大量内存等），"
        "每次迭代只执行一个需要该资源的任务项，不要在同一次迭代中启动多个竞争同一资源的后台任务。"
        "执行一个后通过 needs_monitor 等待完成，下一次迭代再执行下一个。"
        "例如：10 个模型需要训练但只有 1 个 GPU，每次迭代只启动 1 个训练任务。"
    )
```

## 中断逻辑

### 用户取消任务时

```python
async def cancel_task(self, task_id: int):
    # 现有逻辑：停止主进程
    ...

    # 新增：取消该 task 下所有运行中的 monitor sessions
    async with self.db_factory() as db:
        result = await db.execute(
            select(MonitorSession)
            .where(MonitorSession.task_id == task_id)
            .where(MonitorSession.status == "running")
        )
        for ms in result.scalars().all():
            ms.status = "cancelled"
            # 取消对应的 asyncio task
            handle = self._monitor_tasks.pop(ms.id, None)
            if handle:
                handle.cancel()
        await db.commit()
```

### 用户删除单个 Monitor Session 时

通过 `DELETE /tasks/{task_id}/monitor-sessions/{session_id}` API。
仅限 `source="manual"` 的 monitor session 可以被删除。
`source="loop"` 或 `source="goal"` 的系统创建 monitor 不允许删除，前端不显示删除按钮，后端 API 校验拒绝。

```python
@router.delete("/tasks/{task_id}/monitor-sessions/{session_id}")
async def delete_monitor_session(...):
    ms = await db.get(MonitorSession, session_id)
    if ms.source != "manual":
        raise HTTPException(400, "系统创建的监控不允许删除，请取消整个任务")
    ...
```

## 前端变更

### 1. Chat 界面顶部按钮

按钮因模式而异：

**Auto 模式：**
```
┌──────────────────────────────────────────────┐
│  Task #12: 训练模型      [+ 新建监控] [监控列表(2)] │
├──────────────────────────────────────────────┤
│  Chat 主区域                                  │
│  ...                                         │
└──────────────────────────────────────────────┘
```

**Loop / Goal 模式：**
```
┌──────────────────────────────────────────────┐
│  Task #12: 训练模型               [监控列表(2)] │
├──────────────────────────────────────────────┤
│  Chat 主区域                                  │
│  ...                                         │
└──────────────────────────────────────────────┘
```

- **[+ 新建监控]**：仅 Auto 模式显示，点击弹出创建对话框
- **[监控列表(N)]**：所有模式都有，显示当前 task 下的 monitor 数量，点击打开监控列表面板

### 2. 新建监控对话框

点击 [+ 新建监控] 弹出：

```
┌────────────────────────────────────────┐
│ 新建监控                                │
│                                        │
│ 监控内容:                               │
│ ┌────────────────────────────────────┐ │
│ │ 监控 PID 12345 的训练进度，         │ │
│ │ 日志在 /tmp/train.log              │ │
│ └────────────────────────────────────┘ │
│                                        │
│ 检查间隔: [300] 秒                      │
│                                        │
│           [取消]  [创建]                │
└────────────────────────────────────────┘
```

### 3. 监控列表面板

点击 [监控列表] 打开侧面板或抽屉，显示该 task 下所有 monitor sessions：

```
┌────────────────────────────────────────┐
│ 监控列表                          [×]  │
│                                        │
│ ┌────────────────────────────────────┐ │
│ │ ● Monitor #1: "迭代3后台任务监控"    │ │
│ │   来源: loop 迭代 3 | 已检查 5 次   │ │
│ │   最新: epoch 45/100, loss=0.23    │ │
│ └────────────────────────────────────┘ │
│                                        │
│ ┌────────────────────────────────────┐ │
│ │ ● Monitor #2: "监控磁盘空间" [🗑️] │ │
│ │   来源: 手动 | 已检查 1 次          │ │
│ │   最新: 磁盘使用率 72%              │ │
│ └────────────────────────────────────┘ │
│                                        │
│ ┌────────────────────────────────────┐ │
│ │ ✓ Monitor #3: "迭代1后台任务监控"    │ │
│ │   来源: loop 迭代 1 | 已完成        │ │
│ │   结果: 训练完成，accuracy=0.95     │ │
│ └────────────────────────────────────┘ │
└────────────────────────────────────────┘
```

- 运行中的 monitor 显示 ● 绿点
- 仅 `source="manual"` 的 monitor 显示 🗑️ 删除按钮
- `source="loop"/"goal"` 的系统 monitor 不显示删除按钮（只能通过取消整个任务来终止）
- 已完成的 monitor 显示 ✓
- 点击任意一条进入详情页

### 4. Monitor 详情页

点击某个 monitor session 进入详情页，显示所有检查记录：

```
┌────────────────────────────────────────┐
│ ← 返回列表     Monitor #1: "监控训练进度" │
│ 状态: 运行中 | 间隔: 300秒 | 已检查 5 次  │
│ （manual monitor 显示 "5/100 次"，      │
│   system monitor 显示 "已检查 5 次"）    │
├────────────────────────────────────────┤
│                                        │
│ ▼ 检查 #5  2024-01-15 14:30           │
│   状态: running                        │
│   摘要: epoch 45/100, loss=0.23        │
│   ┌──────────────────────────────────┐ │
│   │ (完整 monitor session 输出)       │ │
│   │ $ ps aux | grep train            │ │
│   │ root 1234 ... python train.py    │ │
│   │ $ tail -20 /tmp/train.log        │ │
│   │ Epoch 45/100, loss=0.2312...     │ │
│   └──────────────────────────────────┘ │
│                                        │
│ ▶ 检查 #4  2024-01-15 14:25           │
│   摘要: epoch 38/100, loss=0.28        │
│                                        │
│ ▶ 检查 #3  2024-01-15 14:20           │
│   摘要: epoch 30/100, loss=0.31        │
│                                        │
└────────────────────────────────────────┘
```

- 最新的检查在最上面
- 每条检查默认折叠，显示摘要
- 点击展开显示完整的 monitor session 输出（来自 MonitorCheck.full_output）

### 4. WebSocket 事件

```typescript
// monitor session 创建
interface MonitorSessionCreatedEvent {
    event_type: "monitor_session_created";
    monitor_session_id: number;
    description: string;
}

// 每次检查完成
interface MonitorCheckEvent {
    event_type: "monitor_check";
    monitor_session_id: number;
    check_number: number;
    summary: string;
    status: "running" | "done";
}

// 实时流式输出
interface MonitorOutputEvent {
    event_type: "monitor_output";
    monitor_session_id: number;
    data: object;
}

// monitor session 状态变更（completed/failed/cancelled）
interface MonitorSessionStatusEvent {
    event_type: "monitor_session_status";
    monitor_session_id: number;
    status: "completed" | "failed" | "cancelled";
}
```

## Schema

```python
class MonitorSessionCreate(BaseModel):
    description: str
    interval: int = 300
    max_checks: int = 100
    model: str | None = None

class MonitorSessionResponse(BaseModel):
    id: int
    task_id: int
    description: str
    monitor_context: str | None
    interval: int
    max_checks: int
    model: str | None
    status: str
    checks_done: int
    last_summary: str | None
    source: str
    created_at: datetime
    completed_at: datetime | None

class MonitorCheckResponse(BaseModel):
    id: int
    monitor_session_id: int
    check_number: int
    status: str
    summary: str
    full_output: str | None
    created_at: datetime
```

## Alembic Migration

```python
"""add monitor_sessions and monitor_checks tables

Revision ID: xxxx
"""

def upgrade():
    op.create_table(
        'monitor_sessions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('task_id', sa.Integer(), sa.ForeignKey('tasks.id'), nullable=False, index=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('monitor_context', sa.Text(), nullable=True),
        sa.Column('interval', sa.Integer(), nullable=False, default=300),
        sa.Column('max_checks', sa.Integer(), nullable=False, default=100),
        sa.Column('model', sa.String(100), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, default='running'),
        sa.Column('checks_done', sa.Integer(), nullable=False, default=0),
        sa.Column('last_summary', sa.Text(), nullable=True),
        sa.Column('source', sa.String(20), nullable=False, default='manual'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'monitor_checks',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('monitor_session_id', sa.Integer(), sa.ForeignKey('monitor_sessions.id'), nullable=False, index=True),
        sa.Column('check_number', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('full_output', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )

def downgrade():
    op.drop_table('monitor_checks')
    op.drop_table('monitor_sessions')
```

## 实现顺序建议

1. **Phase 1 — 数据层**：MonitorSession + MonitorCheck 模型 + Schema + Migration
2. **Phase 2 — 后端核心**：`_run_monitor_session` 轮询循环 + 子进程 + Prompt + Signal 读取
3. **Phase 3 — Loop 集成**：扩展 loop signal file + `_run_loop_lifecycle` 中插入 monitor 门控
4. **Phase 4 — API**：Monitor Session CRUD 端点（创建/删除/列表/详情）
5. **Phase 5 — 前端**：监控列表面板 + 新建对话框 + 详情页 + WebSocket 事件
6. **Phase 6 — Goal 集成**：与 loop 类似的 signal 扩展
7. **Phase 7 — 测试**：loop + monitor 门控、手动创建/删除、各种边界情况
