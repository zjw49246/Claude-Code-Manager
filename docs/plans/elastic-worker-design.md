# CCM + Elastic Agent 分布式方案设计

> 本文档是 CCM 分布式 Worker 功能的完整设计方案，持续更新。

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│  Manager 机器 (用户的服务器)                              │
│  ┌──────────────────┐  ┌─────────────────────────────┐  │
│  │  CCM 服务         │  │  Elastic Agent Manager      │  │
│  │  (UI + API +     │  │  (开机/bootstrap/健康监控)    │  │
│  │   本地 Dispatcher)│  │                             │  │
│  └────────┬─────────┘  └──────────┬──────────────────┘  │
│           │                       │                      │
│  本地 Claude Code 实例            │ SSH + WebSocket       │
│  (用 manager 自己的账号)          │                      │
└───────────────────────────────────┼──────────────────────┘
                                    │
            ┌───────────────────────┼──────────────────┐
            │                       │                  │
     ┌──────▼──────┐        ┌──────▼──────┐    ┌──────▼──────┐
     │  Worker 1   │        │  Worker 2   │    │  Worker 3   │
     │  CCM 服务   │        │  CCM 服务   │    │  CCM 服务   │
     │  自己的账号池│        │  自己的账号池│    │  自己的账号池│
     └─────────────┘        └─────────────┘    └─────────────┘
```

**核心原则：**
- 用户只在 Manager CCM UI 上操作
- 每个 Worker 上跑完整 CCM 服务，有自己独立的账号池
- 账号在 Worker 本机登录，不从 Manager 分配（避免单机账号过多被封）
- 项目数据在 Worker 存活期间存放在 Worker 上
- Worker 销毁时，项目文件 + session 文件全部迁移回 Manager
- Manager ↔ Worker 通信含 auth token 和代码数据，**生产环境建议使用 VPC 内网 IP
  或 SSH 隧道**，避免在公网上明文传输。如必须走公网，需在 Worker 上配置 TLS
  (HTTPS/WSS)

---

## 2. Task ID 全局管理

### 2.1 问题

Manager 和 Worker 各自有独立的 CCM 数据库，task ID 各自自增。
当 Worker 销毁、数据迁移回 Manager 时，ID 会冲突。

### 2.2 方案：Manager 统一分配 ID

Manager 是 task ID 的唯一来源。所有 task（无论本机还是 Worker）的 ID 都由 Manager 的序列生成。

**流程：**
1. 用户在 Manager 创建 task → Manager DB 分配 ID（如 42）
2. 如果 task 在 Worker 执行 → Manager 调 Worker API 创建 task 时**指定 ID = 42**
3. Worker 上该 task 的 ID 也是 42
4. 日志、session、所有引用都用同一个 ID
5. Worker 销毁迁移时，ID 天然一致，不会冲突

**CCM 代码改动 — Task 创建 API 支持指定 ID：**

```python
# backend/api/tasks.py

class TaskCreate(BaseModel):
    # 新增可选字段
    id: int | None = None  # 不指定 → 自增；指定 → 使用该 ID
    ...

@router.post("/api/tasks")
async def create_task(body: TaskCreate, db: AsyncSession = Depends(get_db)):
    task = Task(**body.model_dump(exclude_unset=True))
    if body.id is not None:
        task.id = body.id  # 使用 Manager 指定的 ID
    db.add(task)
    await db.commit()
    ...
```

**本机 task：** 不指定 ID，正常自增。
**Worker task：** Manager 先在本地创建 task 拿到 ID，然后用这个 ID 在 Worker 上创建。

### 2.3 防止 ID 冲突

Worker CCM 可能也有自己本地直接创建的 task（理论上不应该有，但防御性设计）。
Worker 的 auto-increment 起始值设为很大的数（如 100000），避免和 Manager 分配的 ID 碰撞：

```python
# Worker .env 或 bootstrap 时配置
TASK_ID_OFFSET=100000  # Worker 本地自增从 100001 开始
```

或更简单：Worker 上只允许通过 Manager 指定 ID 创建 task，禁止自增。

---

## 3. Add Worker 流程

### 3.1 用户在 UI 上填写的内容

只需填写账号信息，其他配置（云厂商、机型、区域等）全部继承 Manager 的配置。

```
┌─ Add Worker ────────────────────────────────────┐
│                                                  │
│  账号数量:  [ 2 ]                                │
│                                                  │
│  账号 1:                                         │
│    Email:    [ alice@example.com           ]     │
│    Password: [ ••••••••••                  ]     │
│                                                  │
│  账号 2:                                         │
│    Email:    [ bob@example.com             ]     │
│    Password: [ ••••••••••                  ]     │
│                                                  │
│              [ 取消 ]  [ 创建 ]                   │
└──────────────────────────────────────────────────┘
```

> **账号信息字段：** 取决于 `auto_login.py` 需要什么。当前是 email
> （171mail OAuth 流程），如果登录方式变化，字段随之调整。

### 3.2 后端创建流程

```
POST /api/workers
Body: {
  "accounts": [
    {"email": "alice@example.com", "password": "..."},
    {"email": "bob@example.com", "password": "..."}
  ]
}
```

1. 在 Worker 表创建记录，status = `creating`
2. 调用 Elastic Agent Manager：开云实例
3. 等实例 Running，获取 IP
4. 执行 Bootstrap Pipeline（见 3.3）
5. 自动创建 SSH Server 配置（用于 Files 界面访问）
6. 等 Worker CCM 服务健康检查通过，status = `ready`

### 3.3 Bootstrap 步骤

所有云配置从 Manager 的 GlobalSettings 或 .env 读取：

```
Step 1: system-init
  apt-get update
  apt-get install -y python3 python3-venv python3-pip git curl
  # Node.js
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
  # uv (Python 包管理器)
  curl -LsSf https://astral.sh/uv/install.sh | sh

Step 2: ccm-deploy
  git clone <CCM_REPO_URL> -b <CCM_REPO_BRANCH> /opt/ccm
  cd /opt/ccm && uv sync
  cd /opt/ccm/frontend && npm install && npm run build

Step 3: claude-code-install
  npm install -g @anthropic-ai/claude-code@latest

Step 4: auto-login-deps (auto_login.py 的依赖)
  # Playwright + Chrome
  pip install playwright mitmproxy
  playwright install chromium
  playwright install-deps chromium
  # Xvfb (无头 Chrome 需要)
  apt-get install -y xvfb

Step 5: ccm-config
  # 写 .env（关键配置）
  cat > /opt/ccm/.env << EOF
  AUTH_TOKEN=<生成的 worker 专用 token>
  AUTO_START_DISPATCHER=true
  MAX_CONCURRENT_INSTANCES=<账号数量>
  POOL_ENABLED=true
  WORKSPACE_DIR=<与 Manager 完全一致的 workspace 路径>
  HOST=0.0.0.0
  PORT=8000
  EOF

Step 6: account-login
  # 在 Worker 本机登录每个账号（不从 Manager 分配）
  # 注意：auto_login.py 当前 CLI 可能不支持 --add-to-pool 参数，
  # 需要适配：增加 --add-to-pool <account-name> 参数，登录完成后
  # 自动将账号添加到 ~/.claude-pool/accounts.json 中
  cd /opt/ccm
  python3 scripts/auto_login.py --email alice@example.com --add-to-pool account-1
  python3 scripts/auto_login.py --email bob@example.com --add-to-pool account-2

  # 登录完成后，Worker 上会有：
  # ~/.claude-pool/accounts.json  (账号池配置)
  # ~/.claude-account-1/          (第一个账号的 config_dir)
  # ~/.claude-account-2/          (第二个账号的 config_dir)
  # 通过 CCM 的硬连接机制，所有 session 都可从第一个账号的 config_dir 访问

Step 7: ccm-service
  # 创建 systemd service
  cat > /etc/systemd/system/ccm.service << EOF
  [Unit]
  Description=Claude Code Manager
  After=network.target

  [Service]
  Type=simple
  WorkingDirectory=/opt/ccm
  ExecStart=/opt/ccm/.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
  Restart=always
  RestartSec=5

  [Install]
  WantedBy=multi-user.target
  EOF

  systemctl daemon-reload
  systemctl enable ccm
  systemctl start ccm

Step 8: health-check
  # 轮询直到 Worker CCM 就绪
  curl -f -H "Authorization: Bearer <token>" http://localhost:8000/api/system/health
```

### 3.4 Bootstrap 失败处理

- 任何 step 失败 → Worker status = `error`，在 UI 显示失败原因和当前步骤
- 用户可以选择：重试（从失败步骤继续）或 销毁重建
- 特别是 Step 6 (account-login)：如果某个账号登录失败，记录哪个失败了，
  允许用户修改账号信息后重试

### 3.5 Worker 创建后自动配置 SSH

Worker 创建成功后，Manager 自动在内部注册一条 SSH Server 信息，供 Files 界面使用：

```python
# Worker ready 后自动执行
ssh_server = {
    "host": worker.public_ip,
    "user": worker.ssh_user,
    "key_path": worker.ssh_key_path,
    "label": f"Worker {worker.id}"
}
# 存入 Worker 表或关联表，Files API 查询时自动使用
```

---

## 4. Worker 数据模型

```python
class Worker(Base):
    __tablename__ = "workers"

    id: int                      # 主键
    name: str                    # 显示名称，自动生成 "Worker 1" / "Worker 2"
    status: str                  # creating / bootstrapping / ready / error / stopping / terminated

    # 云实例信息
    cloud_instance_id: str       # AWS/Aliyun 实例 ID
    public_ip: str | None
    private_ip: str | None

    # 连接信息
    ssh_user: str                # 默认 "ubuntu"，从 Manager 配置继承
    ssh_key_path: str            # 从 Manager 配置继承
    ccm_port: int                # 默认 8000
    auth_token: str              # 访问 Worker CCM API 的 Bearer Token

    # 账号信息（在 Worker 本机登录，不从 Manager 分配）
    accounts: JSON               # [{"email": "...", "status": "logged_in"/"failed"}]
    account_count: int           # 配置的账号数量

    # Project ID 映射（Manager project_id → Worker project_id）
    project_mapping: JSON        # {42: 1, 43: 2, ...}

    # 健康监控
    last_heartbeat: datetime | None
    bootstrap_step: str | None   # 当前 bootstrap 进度
    bootstrap_error: str | None  # 失败原因

    # 时间
    created_at: datetime
    updated_at: datetime
```

---

## 5. Task 创建 — 选择执行位置

### 5.1 Task 模型改动

```python
class Task(Base):
    # 新增字段
    worker_id: int | None = None   # None = 本机执行，有值 = Worker 执行
```

不再需要 `remote_task_id`，因为 Manager 和 Worker 上的 task ID 是同一个（见第 2 节）。

### 5.2 前端改动

创建 Task 表单新增 select：

```
执行位置:  [ 本机 ▾ ]
           ├─ 本机
           ├─ Worker 1 (2 账号, ready)
           ├─ Worker 2 (3 账号, ready)
           └─ Worker 3 (error)  ← 灰掉不可选
```

### 5.3 Dispatcher 改动

**关键：Worker task 不消耗本地 Instance。**

当前 Dispatcher 的 `_dispatch_loop` 流程是：找空闲 instance → 取 pending task → 绑定执行。
Worker task 不需要本地 instance，所以需要在 dispatch loop 里分两条路径：

```python
# dispatcher.py

async def _dispatch_loop(self):
    while self._running:
        # 路径 1: Worker task — 不需要本地 instance，直接转发
        # 注意：取出后立即标记 status="in_progress" 防止下次循环重复转发
        worker_tasks = await self._get_pending_worker_tasks()
        for task in worker_tasks:
            worker = await self._get_worker(task.worker_id)
            if worker and worker.status == "ready":
                # 先标记为 in_progress，防止 2 秒后重复 dispatch
                async with self.db_factory() as db:
                    await db.execute(
                        update(Task).where(Task.id == task.id)
                        .values(status="in_progress")
                    )
                    await db.commit()
                # 广播到前端（与本地 task 一致，避免 UI 延迟等 Worker relay 回传）
                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change",
                    "task_id": task.id,
                    "old_status": "pending",
                    "new_status": "in_progress",
                })
                asyncio.create_task(self._safe_forward_to_worker(task))

        # 路径 2: 本地 task — 现有逻辑不变，需要空闲 instance
        idle_instance = await self._find_idle_instance()
        if idle_instance:
            local_task = await self._dequeue_local_task()  # worker_id IS NULL
            if local_task:
                await self._run_task_locally(local_task, idle_instance)

        await asyncio.sleep(2)

async def _safe_forward_to_worker(self, task):
    """包装 _forward_task_to_worker，失败时回退 task 状态。"""
    try:
        await self._forward_task_to_worker(task)
    except Exception as e:
        async with self.db_factory() as db:
            await db.execute(
                update(Task).where(Task.id == task.id)
                .values(status="failed", error_message=f"转发到 Worker 失败: {e}")
            )
            await db.commit()

async def _forward_task_to_worker(self, task):
    worker = await self._get_worker(task.worker_id)

    # 1. 确保 Worker 上有这个项目（见第 8 节）
    await self._ensure_worker_project(worker, task)

    # 2. 先订阅 WS relay（必须在创建 task 之前，否则 Worker Dispatcher
    #    可能在 task 创建后立即取到并执行，导致初始事件丢失）
    await worker_relay.subscribe_task(worker, task)

    # 3. 调 Worker CCM API 创建 task，指定 ID = task.id
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"http://{worker.public_ip}:{worker.ccm_port}/api/tasks",
            headers={"Authorization": f"Bearer {worker.auth_token}"},
            json={
                "id": task.id,  # 关键：使用 Manager 分配的 ID
                "title": task.title,
                "description": task.description,
                "project_id": worker_project_id,
                "mode": task.mode,
                "model": task.model,
                "provider": task.provider,
                "effort_level": task.effort_level,
                "thinking_budget": task.thinking_budget,
                "tags": task.tags,
                ...
            }
        )
        # 必须检查响应状态：Worker 可能返回 422（字段校验失败）或 500（内部错误）
        # 不检查的话 task 永远卡在 in_progress，relay 订阅了但收不到任何事件
        resp.raise_for_status()
```

---

## 6. Chat 映射 — Worker Chat 在 Manager 上完整操作

这是最核心的部分。用户在 Manager 上对 Worker task 的所有 Chat 操作必须和本地 task 体验一致。

### 6.1 设计原则

- Manager 同时存储日志副本（LogEntry）— 不依赖 Worker 在线才能看历史
- 实时消息通过 WebSocket 中继
- 发送消息/停止/重试等操作代理到 Worker API
- 前端完全不感知 task 是本地还是远程

### 6.2 日志中继 + 本地存储

**每个 Worker 一个 WS 连接（非每 task 一个），** 订阅该 Worker 上所有活跃 task 的 channel。

> **前置改动（CCM 代码）：**
> 1. `LogEntry.instance_id` 改为 nullable（当前是 NOT NULL），或者新增一个
>    id=0 name="remote" 的虚拟 Instance 记录。远程 task 的 LogEntry 用这个值。
> 2. Dispatcher 的各事件 channel 映射（已确认）：
>    - `status_change`, `plan_ready` → 只发 `"tasks"` channel
>    - `loop_iteration_end`, chat events → 只发 `"task:{id}"` channel
>    - `goal_evaluation` → 同时发 `"task:{id}"` 和 `"tasks"`
>    WorkerRelay 需要订阅 `"tasks"` + `"task:{id}"`，并按来源 channel 转发到 Manager
>    相同的 channel，保持与本地 task 行为完全一致。
> 3. Worker CCM 的 `/ws` 端点需要加 Bearer Token 认证（当前无认证）。

```python
# 新增 services/worker_relay.py

class WorkerRelay:
    """管理所有 Worker 的日志中继、状态同步和操作代理。

    每个 Worker 维护一个 WS 连接，订阅该 Worker 上所有活跃 task 的 channel
    + `tasks` 全局 channel（用于接收 status_change 事件）。
    """

    def __init__(self, db_factory, broadcaster: WebSocketBroadcaster):
        self.db_factory = db_factory
        self.broadcaster = broadcaster
        # key = worker.id, 每个 Worker 只有一个 WS 连接
        self._worker_ws: dict[int, websockets.WebSocketClientProtocol] = {}
        # worker_id -> set of task_ids being relayed
        self._worker_tasks: dict[int, set[int]] = {}

    async def ensure_connection(self, worker: Worker):
        """确保与 Worker 有 WS 连接，没有则建立。"""
        if worker.id in self._worker_ws:
            return
        ws_url = f"ws://{worker.public_ip}:{worker.ccm_port}/ws"
        ws = await websockets.connect(
            ws_url,
            additional_headers={"Authorization": f"Bearer {worker.auth_token}"}
        )
        # 订阅 `tasks` 全局 channel（接收 status_change 事件）
        await ws.send(json.dumps({
            "action": "subscribe",
            "channels": ["tasks"]
        }))
        self._worker_ws[worker.id] = ws
        self._worker_tasks[worker.id] = set()
        asyncio.create_task(self._relay_loop(ws, worker))

    async def subscribe_task(self, worker: Worker, task: Task):
        """订阅 Worker 上某个 task 的日志。"""
        await self.ensure_connection(worker)
        ws = self._worker_ws[worker.id]

        # 在已有连接上追加订阅 task 专属 channel
        await ws.send(json.dumps({
            "action": "subscribe",
            "channels": [f"task:{task.id}"]
        }))
        self._worker_tasks[worker.id].add(task.id)

    async def _relay_loop(self, ws, worker):
        """接收 Worker 日志，同时存入 Manager DB 和广播到前端。"""
        try:
            async for raw in ws:
                msg = json.loads(raw)
                channel = msg.get("channel", "")
                data = msg.get("data", msg)
                event_type = data.get("event_type") or data.get("event")

                # 提取 task_id：优先从 data 中取，否则从 channel 名解析
                # 原因：instance_manager 广播到 task:{id} 的事件（message, tool_use 等）
                # 不在 data 中包含 task_id，只能从 channel 名获取
                task_id = data.get("task_id")
                if not task_id and channel.startswith("task:"):
                    try:
                        task_id = int(channel.split(":", 1)[1])
                    except (ValueError, IndexError):
                        pass

                # 过滤：只处理我们关心的 task
                if not task_id:
                    continue
                if task_id not in self._worker_tasks.get(worker.id, set()):
                    continue

                # 1. 跳过 user_message：由 _send_worker_chat 在转发前已存入 DB 并广播，
                # Worker 回传的 user_message 直接丢弃，避免重复存储和双重广播
                if event_type == "user_message":
                    continue
                if event_type in CHAT_EVENT_TYPES:
                    async with self.db_factory() as db:
                        log = LogEntry(
                            instance_id=None,  # 远程 task，无本地 instance（需 schema 改为 nullable）
                            task_id=task_id,
                            event_type=event_type,
                            role=data.get("role"),
                            content=data.get("content"),
                            tool_name=data.get("tool_name"),
                            tool_input=data.get("tool_input"),
                            tool_output=data.get("tool_output"),
                            raw_json=data.get("raw_json"),
                            is_error=data.get("is_error", False),
                            loop_iteration=data.get("loop_iteration"),
                        )
                        db.add(log)
                        await db.commit()

                # 2. 同步 task 状态
                # 注意：Dispatcher 广播用 "new_status" 而非 "status"
                # session_id 不在 status_change 广播中（被 instance_manager pop 掉了），
                # 由 _send_worker_chat 从 chat 响应中同步
                if event_type == "status_change":
                    async with self.db_factory() as db:
                        task_obj = await db.get(Task, task_id)
                        if task_obj:
                            new_status = data.get("new_status")
                            if new_status:
                                task_obj.status = new_status
                            await db.commit()

                # 3. 同步 cost / context_window_usage
                # 实际广播格式: {input_tokens, output_tokens, cache_read_input_tokens, ...}
                if event_type == "context_usage":
                    async with self.db_factory() as db:
                        task_obj = await db.get(Task, task_id)
                        if task_obj:
                            task_obj.context_window_usage = {
                                k: v for k, v in data.items()
                                if k not in ("event_type", "task_id")
                            }
                            await db.commit()

                # 4. 同步 Plan 模式（plan_content + 状态变为 plan_review）
                # 注意：plan_ready 广播只含 task_id + instance_id，不含 plan_content
                # 需要从 Worker API 单独获取 plan_content
                if event_type == "plan_ready":
                    plan_content = await self._fetch_plan_content(worker, task_id)
                    async with self.db_factory() as db:
                        task_obj = await db.get(Task, task_id)
                        if task_obj:
                            task_obj.plan_content = plan_content
                            task_obj.status = "plan_review"
                            await db.commit()

                # 5. 同步 Loop 模式进度
                if event_type == "loop_iteration_end":
                    async with self.db_factory() as db:
                        task_obj = await db.get(Task, task_id)
                        if task_obj:
                            task_obj.loop_progress = data.get("progress")
                            await db.commit()

                # 6. 同步 Goal 模式评估结果
                # 实际广播字段：task:{id} channel 有 turn/max_turns/achieved/reason
                #              tasks channel 只有 task_id/turn/achieved（无 reason）
                if event_type == "goal_evaluation":
                    async with self.db_factory() as db:
                        task_obj = await db.get(Task, task_id)
                        if task_obj:
                            task_obj.goal_turns_used = data.get("turn", task_obj.goal_turns_used)
                            if data.get("reason"):
                                task_obj.goal_last_reason = data["reason"]
                            await db.commit()

                # 7. 广播到 Manager 前端 — 保持与本地 task 完全一致的 channel 映射
                # 本地 task 的 channel 规则：
                #   status_change, plan_ready → "tasks" only
                #   loop_iteration_end, chat events, context_usage → "task:{id}" only
                #   goal_evaluation → BOTH "task:{id}" AND "tasks"（两次独立 broadcast）
                # 所以：转发到事件来源的同一 channel，完美镜像本地行为。
                # 同时剥离 Worker 的 instance_id（对 Manager 无意义，可能导致前端
                # 尝试查找不存在的 Instance 记录）
                forward_data = {k: v for k, v in data.items() if k != "instance_id"}
                if channel.startswith("task:"):
                    await self.broadcaster.broadcast(f"task:{task_id}", forward_data)
                elif channel == "tasks":
                    await self.broadcaster.broadcast("tasks", forward_data)

        except websockets.ConnectionClosed:
            # Worker 断线 → 尝试重连
            asyncio.create_task(self._reconnect(worker))

    async def _reconnect(self, worker):
        """Worker 断线后重连并补全缺失日志。"""
        self._worker_ws.pop(worker.id, None)
        task_ids = self._worker_tasks.pop(worker.id, set())

        for attempt in range(10):
            await asyncio.sleep(min(2 ** attempt, 60))
            try:
                await self.ensure_connection(worker)
                # 重新订阅所有 task
                for tid in task_ids:
                    ws = self._worker_ws[worker.id]
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "channels": [f"task:{tid}"]
                    }))
                    self._worker_tasks[worker.id].add(tid)

                # 补全断线期间缺失的日志
                await self._backfill_missing_logs(worker, task_ids)
                return
            except Exception:
                continue

        # 10 次重连失败 → 标记相关 task 为 error
        async with self.db_factory() as db:
            for tid in task_ids:
                task_obj = await db.get(Task, tid)
                if task_obj and task_obj.status in ("executing", "in_progress"):
                    task_obj.status = "failed"
                    task_obj.error_message = f"Worker {worker.name} 断连且无法重连"
            await db.commit()

    async def _backfill_missing_logs(self, worker, task_ids):
        """从 Worker 拉取缺失的日志补入 Manager DB。
        使用条数对比（而非时间戳）避免时钟不一致导致的丢失/重复。

        关键：必须排除 user_message 进行对比。原因：
        user_message 由 _send_worker_chat 通过 HTTP 直接存入 Manager DB（不经过 relay），
        而 relay 收到 Worker 回传的 user_message 时会 skip。如果 relay 断连期间用户
        发送了新 Chat，Manager 和 Worker 的 user_message 位置会错位，导致 count 不匹配，
        backfill 会把 Worker 的 user_message 重复存入 Manager。
        """
        async with httpx.AsyncClient() as client:
            for tid in task_ids:
                # 只计算非 user_message 的条数（user_message 由 _send_worker_chat 存入）
                async with self.db_factory() as db:
                    count_result = await db.execute(
                        select(func.count()).where(
                            LogEntry.task_id == tid,
                            LogEntry.event_type != "user_message",
                        )
                    )
                    local_count = count_result.scalar() or 0

                # 从 Worker 拉完整历史（带排序）
                resp = await client.get(
                    f"http://{worker.public_ip}:{worker.ccm_port}/api/tasks/{tid}/chat/history?compact=false",
                    headers={"Authorization": f"Bearer {worker.auth_token}"}
                )
                if resp.status_code != 200:
                    continue

                remote_msgs = resp.json()
                # 过滤掉 user_message（Manager 已通过 _send_worker_chat 存入），
                # 只对比非 user_message 条数
                remote_non_user = [m for m in remote_msgs
                                   if m.get("event_type") != "user_message"]
                missing = remote_non_user[local_count:]
                async with self.db_factory() as db:
                    for msg in missing:
                        log = LogEntry(
                            instance_id=None,
                            task_id=tid,
                            event_type=msg.get("event_type"),
                            role=msg.get("role"),
                            content=msg.get("content"),
                            tool_name=msg.get("tool_name"),
                            tool_input=msg.get("tool_input"),
                            tool_output=msg.get("tool_output"),
                            raw_json=msg.get("raw_json"),
                            is_error=msg.get("is_error", False),
                            loop_iteration=msg.get("loop_iteration"),
                        )
                        db.add(log)
                    await db.commit()

    async def _fetch_plan_content(self, worker, task_id):
        """plan_ready 广播不含 plan_content，需要从 Worker API 获取。"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://{worker.public_ip}:{worker.ccm_port}/api/tasks/{task_id}",
                headers={"Authorization": f"Bearer {worker.auth_token}"}
            )
            if resp.status_code == 200:
                return resp.json().get("plan_content")
            return None

    async def stop_worker(self, worker_id: int):
        """断开与 Worker 的连接。"""
        ws = self._worker_ws.pop(worker_id, None)
        self._worker_tasks.pop(worker_id, None)
        if ws:
            await ws.close()

CHAT_EVENT_TYPES = {
    "user_message", "message", "result", "tool_use", "tool_result",
    "system_init", "system_event", "thinking", "process_exit"
}
```

### 6.3 Chat 操作代理

Manager 的 Chat API 需要判断 task 是本地还是远程，远程的代理到 Worker：

```python
# backend/api/chat.py 改动

@router.post("/api/tasks/{task_id}/chat")
async def send_chat(task_id: int, body: ChatRequest, db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)

    if task.worker_id is None:
        # 本机 — 现有逻辑完全不变
        return await _send_local_chat(task, body, db)
    else:
        # Worker — 代理到 Worker CCM
        return await _send_worker_chat(task, body, db)

async def _send_worker_chat(task, body, db):
    worker = await db.get(Worker, task.worker_id)

    # 1. 先在 Manager DB 存 user_message（保持日志完整）
    log = LogEntry(
        instance_id=None,
        task_id=task.id,
        event_type="user_message",
        role="user",
        content=body.message,
    )
    db.add(log)
    await db.commit()

    # 2. 广播 user_message 到 Manager 前端
    await broadcaster.broadcast(f"task:{task.id}", {
        "event_type": "user_message",
        "role": "user",
        "content": body.message,
    })

    # 3. 确保日志中继已订阅（必须在转发之前，与 _forward_task_to_worker 一致）
    # 原因：Manager 重启后已完成的 task 不会被 _recover_worker_relays 重新订阅，
    # 用户此时 chat 时 relay 未订阅，如果在 forward 之后才 subscribe，
    # Worker chat 端的 "executing" 状态广播和初始事件可能丢失
    await worker_relay.subscribe_task(worker, task)

    # 4. 转发到 Worker CCM
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"http://{worker.public_ip}:{worker.ccm_port}/api/tasks/{task.id}/chat",
            headers={"Authorization": f"Bearer {worker.auth_token}"},
            json={
                "message": body.message,
                "image_paths": body.image_paths,
                "file_paths": body.file_paths,
                "secret_ids": body.secret_ids,
            }
        )
        result = resp.json()

    # 5. 同步 session_id 到 Manager DB
    # 原因：Worker 的 instance_manager._process_event 用 event.pop("session_id")
    # 把 session_id 从事件中移除后才广播，relay 永远收不到。
    # 只有 chat 响应中包含 session_id，必须在这里存入 Manager DB，
    # 否则 Worker 销毁后 --resume 无法找到 session 文件。
    if result.get("session_id"):
        task.session_id = result["session_id"]
        await db.commit()

    # 6. 替换 Worker 的 instance_id（对 Manager 无意义）
    result["instance_id"] = None
    return result
```

### 6.4 其他 Task 操作代理

所有需要转发到 Worker 的操作：

```python
# 统一代理模式

async def _proxy_to_worker(task, method, path, body=None):
    """通用 Worker API 代理。先确保 relay 订阅，再检查健康状态，再转发。

    必须在转发前 subscribe：
    - retry: failed task 不在 _recover_worker_relays 恢复列表中，
      Manager 重启后 relay 未订阅，不 subscribe 则 Worker 的所有后续事件丢失
    - 其他操作（cancel/stop/plan approve）：对应 task 通常已订阅，
      subscribe_task 是幂等的，调用无害
    """
    worker = await get_worker(task.worker_id)
    if worker.status != "ready":
        raise HTTPException(
            503,
            f"Worker {worker.name} 当前状态为 {worker.status}，无法执行操作。"
            "请等待 Worker 恢复或将 task 切回本机执行。"
        )
    # 确保 relay 已订阅（幂等；对 retry 场景至关重要）
    await worker_relay.subscribe_task(worker, task)
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.request(
                method,
                f"http://{worker.public_ip}:{worker.ccm_port}{path}",
                headers={"Authorization": f"Bearer {worker.auth_token}"},
                json=body
            )
            return resp.json()
        except httpx.ConnectError:
            raise HTTPException(503, f"无法连接到 Worker {worker.name}，请检查 Worker 状态")

# 需要代理的操作：
POST /api/tasks/{id}/chat          → _proxy_to_worker(task, "POST", f"/api/tasks/{id}/chat", body)
POST /api/tasks/{id}/stop-session  → _proxy_to_worker(task, "POST", f"/api/tasks/{id}/stop-session")
POST /api/tasks/{id}/cancel        → _proxy_to_worker(task, "POST", f"/api/tasks/{id}/cancel")
POST /api/tasks/{id}/retry         → _proxy_to_worker(task, "POST", f"/api/tasks/{id}/retry")

# 不需要代理的操作（Manager 本地处理）：
GET  /api/tasks/{id}/chat/history  → Manager DB 已有完整日志副本，直接查本地
GET  /api/tasks/{id}               → Manager DB
PUT  /api/tasks/{id}               → Manager DB（标题、标签等元数据）
POST /api/tasks/{id}/star          → Manager DB
POST /api/tasks/{id}/archive       → Manager DB
POST /api/tasks/{id}/read          → Manager DB
```

### 6.5 Chat History

因为 WorkerRelay 把所有日志都存入了 Manager DB，所以：
- `GET /api/tasks/{id}/chat/history` **不需要访问 Worker**，直接查 Manager 本地 LogEntry
- 即使 Worker 离线，历史记录也完整可用
- 这对 Worker 销毁后的数据迁移也有好处：日志已经在 Manager 了

### 6.6 文件附件

用户在 Manager 发送 Chat 时附带的文件：
1. 文件先上传到 Manager（现有逻辑）
2. 代理 Chat 到 Worker 时，需要把文件也传过去
3. 两种方式：
   - **方式 A（推荐）：** Manager 通过 SCP/rsync 把文件传到 Worker 的 uploads 目录，然后 Chat body 中引用 Worker 上的路径
   - **方式 B：** Worker CCM 暴露文件上传 API，Manager 调 Worker API 上传

### 6.7 clone_from_task_id 处理

CCM 支持 `clone_from_task_id` 克隆任务（复制 session + cwd）。Worker 场景需要特殊处理：

| 源 task | 目标位置 | 处理方式 |
|---------|---------|---------|
| 本机 task | 本机 | 现有逻辑不变 |
| 本机 task | Worker | 先将 session 文件 rsync 到 Worker 第一个账号的 config_dir，再在 Worker 上创建 task |
| Worker task | 本机 | 先从 Worker rsync session 文件回 Manager 第一个账号的 config_dir，再本地克隆 |
| Worker task | 另一 Worker | 不支持，返回错误提示用户先切回本机 |

```python
# Task 创建逻辑中
if body.clone_from_task_id:
    source_task = await db.get(Task, body.clone_from_task_id)

    # 禁止跨 Worker 克隆
    if (source_task.worker_id and body.worker_id
            and source_task.worker_id != body.worker_id):
        raise HTTPException(400, "不支持跨 Worker 克隆，请先将源 task 切回本机")

    # 源在 Worker → 先把 session 拉回 Manager（无论目标是本机还是 Worker）
    if source_task.worker_id:
        await _rsync_session_from_worker(source_task)

    # 目标在 Worker → 把 session 推到 Worker
    if body.worker_id:
        await _rsync_session_to_worker(body.worker_id, source_task.session_id)
```

### 6.8 前端零改动

以上所有代理逻辑都在 Manager 后端完成。前端视角：
- 所有 API 端点不变
- WebSocket channel 不变（`task:{id}`）
- Chat history 格式不变
- 发送消息流程不变

前端唯一的改动是：Task 详情页显示一个执行位置标签（如 "Worker 1"），纯展示。

---

## 7. 日志流转发

### 7.1 架构

```
前端 ←──WS──→ Manager CCM ←──WS──→ Worker CCM
               (中继 + 存储)
```

### 7.2 连接生命周期

```
Task 创建在 Worker 上
  → Dispatcher._forward_task_to_worker()
  → WorkerRelay.subscribe_task(worker, task)  ← 先建立 relay
  → POST Worker API 创建 task              ← 再创建 task（避免丢失初始事件）
  → 建立 WS 连接，开始中继

Task 完成（收到 process_exit）
  → WorkerRelay 收到事件
  → 存入 DB + 广播到前端
  → 更新 Manager task 状态
  → 保持 WS 连接（用户可能继续 Chat）

用户发送 Chat
  → Manager 代理到 Worker
  → WorkerRelay 自动收到 Worker 的响应日志
  → 中继到前端

Worker 断线
  → WorkerRelay 检测到 WS 断开
  → 尝试重连（指数退避）
  → 如果 Worker 恢复 → 重新同步缺失的日志
  → 如果 Worker 彻底挂了 → task 标记 error
```

### 7.3 断线重连 + 日志补全

详见 6.2 中 `WorkerRelay._reconnect(worker)` 的实现。每 Worker 一个连接，断线后：
1. 指数退避重连（最多 10 次）
2. 重新订阅 `tasks` 全局 channel + 所有活跃 task 的 `task:{id}` channel
3. 通过 Worker 的 `chat/history` API 补全断线期间缺失的日志
4. 10 次重连失败 → 标记所有关联 task 为 failed

---

## 8. Projects 适配

### 8.1 Project ID 映射

Manager 和 Worker 各有独立的 project 表，project_id 不一致。
解决方案：Manager 用 Worker 表新增 `project_mapping` JSON 字段记录映射关系。

```python
# Worker 数据模型新增
project_mapping: JSON  # {manager_project_id: worker_project_id, ...}
```

`_ensure_worker_project()` 的逻辑：
1. 获取 `asyncio.Lock` per `(worker_id, manager_project_id)`（防止并发 task 重复创建 project）
2. 查 Worker.project_mapping 是否已有这个 manager_project_id 的映射
3. 有 → 直接用 worker_project_id
4. 没有 → 调 Worker API 创建 project，拿到 worker_project_id，存入映射

```python
# 并发安全：同一 worker + project 同时只允许一个创建
_project_locks: dict[tuple[int, int], asyncio.Lock] = {}

async def _ensure_worker_project(self, worker, task):
    key = (worker.id, task.project_id)
    if key not in self._project_locks:
        self._project_locks[key] = asyncio.Lock()
    async with self._project_locks[key]:
        mapping = worker.project_mapping or {}
        if str(task.project_id) in mapping:
            return mapping[str(task.project_id)]
        # ... 创建 project，更新 mapping ...
```

### 8.2 有 git remote 的项目

Manager 转发 task 到 Worker 时，传递 project 信息（git_url, branch, credentials）。
Worker CCM 如果还没有这个项目，Manager 先调 Worker API 创建 project，Worker 自动 clone。

### 8.3 纯本地项目

如果用户选择在 Worker 上执行一个纯本地项目的 task：

1. Manager 在转发 task 前，先 rsync 项目文件到 Worker：
   ```
   rsync -az <local_path>/ worker:<workspace_dir>/<project_name>/
   ```
2. 在 Worker CCM 上创建对应的 project 记录（指向 rsync 后的路径）
3. Task 完成后，Worker 上的改动会在销毁时迁移回来

### 8.4 Worker 上的 Project 管理

Worker CCM 的 project 不由用户直接管理。Manager 按需自动创建：
- 转发 task 时自动确保 Worker 有对应 project
- Worker 的 Projects 页面不对用户暴露

### 8.5 Manager Projects 页面适配

Projects 页面需要知道某个项目是否有 task 在 Worker 上运行：
- 项目列表可以显示标签：如 "Worker 1 上有 3 个 task"
- 点击项目进入文件浏览时，如果项目在 Worker 上有活跃 task，
  可以选择查看 Worker 上的文件版本（通过 SSH）

---

## 9. Files 界面适配

### 9.1 后端改动

CCM 已有 SSH file API (`/api/files/ssh/*`)。改动 `/api/files/*` 的逻辑：

```python
# api/files.py 改动

@router.get("/api/files/list")
async def list_files(path: str, worker_id: int | None = None):
    if worker_id is None:
        # 本机 — 现有逻辑不变
        return list_local_dir(path)
    else:
        # Worker — 使用 Worker 创建时自动配置的 SSH 信息
        worker = await get_worker(worker_id)
        return await list_ssh_dir(
            host=worker.public_ip,
            user=worker.ssh_user,
            key_path=worker.ssh_key_path,
            path=path
        )

# read、download 同理
```

### 9.2 前端改动

- Task 详情页的文件浏览器，如果 task.worker_id 存在，
  请求时自动附加 `?worker_id=X` 参数
- Projects 页面文件浏览同理

---

## 10. Worker 销毁 — 数据迁移

### 10.1 触发

```
POST /api/workers/{id}/destroy
```

### 10.2 完整流程

```
Step 1: 停止 Worker 上所有任务
  ├─ GET worker/api/tasks?status=in_progress,executing
  ├─ 对每个运行中的 task: POST worker/api/tasks/{id}/stop-session
  └─ 等所有 task 完成/停止（超时后强制）

Step 2: 同步项目文件回 Manager
  ├─ GET worker/api/projects → 获取 Worker 上所有项目
  ├─ 对每个项目:
  │   rsync -az worker:<project_path>/ manager:<workspace_dir>/<project_name>/
  └─ 如果 Manager 上已有同名项目:
      └─ Worker 的版本覆盖 Manager 的（Worker 上是最新的）

Step 3: 归并 Session 到第一个账号（关键步骤）
  ├─ pool.select() 按 cooldown 排序选账号，不保证所有 session 都在第一个账号下
  │   例：account-1 rate limit → task 分配给 account-2 → session 只在 account-2
  ├─ 所以 rsync 前必须先在 Worker 上执行归并：
  │   ssh worker "python3 /opt/ccm/scripts/consolidate_sessions.py"
  │   该脚本遍历所有账号的 config_dir/projects/，
  │   把不在第一个账号下的 session 文件硬连接到第一个账号
  └─ 归并完成后再 rsync

Step 4: 同步 Session 文件回 Manager
  ├─ rsync -az worker:<第一个账号 config_dir>/projects/ \
  │              manager:<manager 第一个账号 config_dir>/projects/
  └─ 这样 Manager 的 Claude Code 用 --resume 就能找到所有 session

Step 5: 从 Worker 同步 task 详情 + 更新 Manager DB
  ├─ 关键原因：relay 无法同步的字段（instance_manager.pop session_id 等）
  │   必须在销毁前从 Worker API 拉取，否则 Manager DB 缺失关键数据
  ├─ 批量获取 Worker 上所有 task 详情：
  │   GET worker/api/tasks/{id} for each task
  ├─ 对每个 task 同步以下字段（仅在 Manager 为空时覆盖）：
  │   ├─ session_id  ← 最关键：instance_manager 用 event.pop() 移除后广播，
  │   │                relay 永远收不到。如不同步，--resume 无法找到 session
  │   ├─ last_cwd    ← instance_manager.launch() 在 Worker 上设置，
  │   │                chat 时需要用来定位工作目录
  │   ├─ error_message ← task 失败时由 Worker 的 mark_failed() 设置，
  │   │                   status_change 广播不含此字段
  │   └─ completed_at ← mark_completed/mark_failed 设置
  ├─ task.worker_id = None（切回本机执行）
  ├─ 日志不需要导入（WorkerRelay 已经实时存入 Manager DB 了）
  └─ session 文件已在 Step 4 rsync 回来，配合同步的 session_id 即可 --resume

Step 6: 断开 relay 连接
  ├─ worker_relay.stop_worker(worker.id)
  └─ 必须在销毁实例之前：stop_worker 清空 _worker_ws 和 _worker_tasks，
     否则实例销毁后 _reconnect 会尝试 10 次重连（指数退避，约 17 分钟浪费）

Step 7: 销毁云实例
  ├─ Elastic Agent: terminate_instance(worker.cloud_instance_id)
  └─ Worker status = terminated
```

**Step 5 实现代码：**

```python
async def _sync_task_details_from_worker(self, worker, db):
    """销毁前从 Worker 同步 relay 无法覆盖的 task 字段。

    原因：instance_manager 用 event.pop("session_id") 在广播前移除 session_id，
    relay 永远收不到。last_cwd 在 launch() 时直接写 Worker DB。error_message
    在 mark_failed() 时设置，status_change 广播不含此字段。
    这些字段只存在于 Worker DB，必须在销毁前拉取。
    """
    worker_tasks = (await db.execute(
        select(Task).where(Task.worker_id == worker.id)
    )).scalars().all()

    async with httpx.AsyncClient(timeout=30) as client:
        for task in worker_tasks:
            try:
                resp = await client.get(
                    f"http://{worker.public_ip}:{worker.ccm_port}/api/tasks/{task.id}",
                    headers={"Authorization": f"Bearer {worker.auth_token}"},
                )
                if resp.status_code != 200:
                    continue
                wt = resp.json()
                # 只在 Manager 为空时覆盖（避免覆盖 Manager 侧已有的正确值）
                if not task.session_id:
                    task.session_id = wt.get("session_id")
                if not task.last_cwd:
                    task.last_cwd = wt.get("last_cwd")
                if not task.error_message:
                    task.error_message = wt.get("error_message")
                if not task.completed_at and wt.get("completed_at"):
                    task.completed_at = wt["completed_at"]
            except Exception:
                pass  # Worker 可能已经不响应，尽力同步
            task.worker_id = None
    await db.commit()
```

### 10.3 无缝衔接原理

```
销毁前:
  Worker 上: ~/.claude-account-1/projects/<encoded_cwd>/<session_id>.jsonl
  (通过硬连接，所有 Worker 账号都能访问)

rsync 后:
  Manager 上: <manager 第一个账号 config_dir>/projects/<encoded_cwd>/<session_id>.jsonl

用户继续 Chat:
  1. task.worker_id 已经是 None → 走本地 Chat 逻辑
  2. task.session_id 已从 Worker API 同步（Step 5）
  3. Manager CCM 用 --resume <session_id>
  4. Claude Code 在 Manager 第一个账号的 config_dir 下找到 session 文件
  5. 对话无缝继续
```

前提条件：
- Manager 和 Worker 的 `WORKSPACE_DIR` 路径完全一致（方案 A）
- 这样 `<encoded_cwd>` 就一样，session 文件路径对得上

### 10.4 日志完整性

因为 WorkerRelay 在 task 执行期间就实时把日志存入了 Manager DB：
- **不需要在销毁时从 Worker 导入日志**
- 即使销毁过程中 Worker 突然断联，Manager 上已有的日志也是完整的（到断联为止）
- 只需要 rsync 项目文件和 session 文件

---

## 11. Plan / Loop / Goal 模式在 Worker 上的处理

这三种模式有特殊的状态流转，WorkerRelay 已经同步了对应事件（见 6.2），
但需要确保 Manager 的操作代理能正确处理。

### 11.1 Plan 模式

```
Worker task 进入 plan_review
  → WorkerRelay 收到 plan_ready 事件
  → Manager DB 更新: task.plan_content + task.status = "plan_review"
  → 前端显示 Plan Review 界面

用户在 Manager 审批:
  POST /api/tasks/{id}/plan/approve
  → Manager 后端代理到 Worker: POST worker/api/tasks/{id}/plan/approve
  → Worker Dispatcher 继续执行
  → Manager DB 同步状态变化
```

### 11.2 Loop 模式

```
Worker task 每完成一个 iteration:
  → WorkerRelay 收到 loop_iteration_end 事件
  → Manager DB 更新: task.loop_progress

用户在 Manager 取消 loop:
  POST /api/tasks/{id}/cancel
  → 代理到 Worker
```

### 11.3 Goal 模式

```
Worker 的 evaluator 每次评估:
  → WorkerRelay 收到 goal_evaluation 事件
  → Manager DB 更新: task.goal_turns_used + task.goal_last_reason
```

---

## 12. Manager 重启恢复

Manager CCM 重启后，所有 WorkerRelay 的 WS 连接丢失。需要重建。

### 12.1 恢复流程

```python
# main.py lifespan 中

async def _recover_worker_relays():
    """Manager 启动时，为所有活跃的 Worker task 重建 relay 连接。"""
    async with async_session() as db:
        # 找到所有在 Worker 上运行中的 task
        active_tasks = await db.execute(
            select(Task).where(
                Task.worker_id.isnot(None),
                Task.status.in_(["executing", "in_progress", "plan_review"])
            )
        )
        # 按 worker 分组，每个 worker 只需一次连接 + 一次 backfill
        worker_task_map = {}
        for task in active_tasks.scalars():
            worker = await db.get(Worker, task.worker_id)
            if worker and worker.status == "ready":
                await worker_relay.subscribe_task(worker, task)
                worker_task_map.setdefault(worker, set()).add(task.id)

        # 补全 Manager 重启期间丢失的日志
        for worker, task_ids in worker_task_map.items():
            await worker_relay._backfill_missing_logs(worker, task_ids)
```

### 12.2 日志补全

重连后，`_backfill_missing_logs()` 从 Worker 拉取 Manager 重启期间产生的日志，
通过**非 user_message 条数**对比避免重复。必须排除 user_message 的原因：
user_message 由 `_send_worker_chat` 通过 HTTP 直接存入 Manager DB（不经过 relay），
如果断连期间用户发送了新 Chat，Manager 和 Worker 的 user_message 位置会错位，
按总条数对比会导致重复存入。

---

## 13. Session 归并脚本

销毁 Worker 前需要执行的脚本，确保所有 session 都可从第一个账号的 config_dir 访问。

**原因：** `pool.select()` 按 cooldown 排序选账号。如果 account-1 被 rate limit，
task 会分配到 account-2，session 只存在于 account-2 的 config_dir。
硬连接只在发生 rotation 时创建（`migrate_session()`），不保证事后归并。

```python
# scripts/consolidate_sessions.py

"""归并所有账号的 session 文件到第一个账号的 config_dir。
在 Worker 销毁前执行，确保 rsync 第一个账号就能拿到所有 session。
"""

import json
import os
from pathlib import Path

def consolidate():
    pool_path = Path.home() / ".claude-pool" / "accounts.json"
    if not pool_path.exists():
        return

    data = json.loads(pool_path.read_text())
    accounts = data.get("accounts", [])
    if len(accounts) < 2:
        return

    first_dir = Path(os.path.expanduser(accounts[0]["config_dir"]))
    first_projects = first_dir / "projects"

    for account in accounts[1:]:
        other_dir = Path(os.path.expanduser(account["config_dir"]))
        other_projects = other_dir / "projects"
        if not other_projects.exists():
            continue

        for session_file in other_projects.glob("*/*.jsonl"):
            # 目标路径：第一个账号下同样的相对位置
            rel = session_file.relative_to(other_projects)
            target = first_projects / rel

            if target.exists():
                # 检查是否已经是同一个 inode（已硬连接）
                if target.stat().st_ino == session_file.stat().st_ino:
                    continue
                # 不同文件 → 跳过（不覆盖）
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(session_file, target)
            print(f"Hardlinked: {session_file} → {target}")

if __name__ == "__main__":
    consolidate()
```

---

## 14. Worker 重启

```
POST /api/workers/{id}/restart
Body: { "force": false }
```

### 14.1 流程

```
Step 1: (如果 force=false) 等当前 task 完成
  GET worker/api/tasks?status=in_progress,executing
  如果有运行中的 task → 等完成
  如果 force=true → 跳过等待

Step 2: 停 CCM 服务
  ssh worker "systemctl stop ccm"

Step 3: 拉最新代码
  ssh worker "cd /opt/ccm && git pull origin <branch>"

Step 4: 更新依赖
  ssh worker "cd /opt/ccm && uv sync"
  ssh worker "cd /opt/ccm/frontend && npm install && npm run build"

Step 5: 重启服务
  ssh worker "systemctl start ccm"

Step 6: 健康检查
  轮询 GET worker/api/system/health 直到返回 200
  更新 Worker status = ready
```

---

## 15. Worker 健康监控

Manager 定期（每 30s）检查所有 ready 状态的 Worker：

```python
async def _health_check_loop(self):
    _fail_counts: dict[int, int] = {}
    while True:
        async with self.db_factory() as db:
            workers = await self._get_ready_workers(db)
            for worker in workers:
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(
                            f"http://{worker.public_ip}:{worker.ccm_port}/api/system/health",
                            headers={"Authorization": f"Bearer {worker.auth_token}"},
                            timeout=10
                        )
                    worker.last_heartbeat = datetime.utcnow()
                    _fail_counts.pop(worker.id, None)
                except Exception:
                    _fail_counts[worker.id] = _fail_counts.get(worker.id, 0) + 1
                    if _fail_counts[worker.id] >= 3:
                        worker.status = "error"
                        worker.bootstrap_error = "健康检查连续 3 次失败"
            await db.commit()
        await asyncio.sleep(30)
```

---

## 16. Elastic Agent 的角色精简

在此方案中，Elastic Agent 只负责三件事：

| 功能 | 使用的组件 |
|------|-----------|
| 开机/关机云实例 | `CloudProvider` (AWS/Aliyun) |
| Bootstrap (SSH 安装依赖 + 部署 CCM) | `SSHExecutor` + `BootstrapPipeline` |
| 健康监控 | 可以用 Elastic Agent 的，也可以 CCM 自己做（更简单） |

**不需要的 Elastic Agent 组件：**
- TaskScheduler / TaskRouter — CCM Dispatcher 负责
- TaskRegistry — CCM Task 表负责
- WorkerRuntime — Worker 上跑完整 CCM，不需要裸 runtime
- FileSync (watchdog + OSS) — 用 SSH 直接读，销毁时 rsync
- CredentialBinding — 账号在 Worker 本机登录，不做跨机绑定
- WebSocket message protocol (Execute/Stop/Log) — CCM 有自己的 API + WS

**需要的 Elastic Agent 组件：**
- `CloudProvider` 接口 + AWS/Aliyun 实现
- `SSHExecutor` 执行远程命令
- `BootstrapPipeline` + `BootstrapHandler` 编排 bootstrap 步骤
- `NodeRegistry` 记录 worker 云实例状态（可选，也可以用 CCM 的 Worker 表替代）

实际上可以考虑把需要的部分直接集成到 CCM 中，作为一个 `services/worker_provisioner.py`，
避免运行两个独立服务。

---

## 17. Manager 配置新增

在 Settings 页面或 .env 中新增：

```env
# Worker 基础设施配置（所有 Worker 统一）
WORKER_ENABLED=false
WORKER_CLOUD_PROVIDER=aws           # aws / aliyun
WORKER_REGION=ap-northeast-1
WORKER_INSTANCE_TYPE=t3.large
WORKER_IMAGE_ID=ami-xxxxxxxx        # 基础 AMI (Ubuntu 22.04)
WORKER_SSH_KEY_NAME=ccm-worker
WORKER_SSH_KEY_PATH=~/.ssh/ccm-worker.pem
WORKER_SECURITY_GROUP=sg-xxxxxxxx
WORKER_SUBNET=subnet-xxxxxxxx
WORKER_SSH_USER=ubuntu

# AWS 凭证（用于开机/关机）
AWS_ACCESS_KEY_ID=xxx
AWS_SECRET_ACCESS_KEY=xxx

# CCM 部署源
CCM_REPO_URL=https://github.com/xxx/Claude-Code-Manager.git
CCM_REPO_BRANCH=main
```

或者更好的方式：放到 GlobalSettings 表 + Settings UI 页面中配置。

---

## 18. API 总览

### Worker 管理 API

```
GET    /api/workers                    列出所有 Worker
POST   /api/workers                    创建 Worker（开机 + bootstrap）
GET    /api/workers/{id}               获取 Worker 详情
POST   /api/workers/{id}/destroy       销毁 Worker（迁移数据 + 关机）
POST   /api/workers/{id}/restart       重启 Worker（pull 最新代码 + restart）
GET    /api/workers/{id}/status        健康状态
GET    /api/workers/{id}/tasks         Worker 上的任务列表
GET    /api/workers/{id}/logs          Bootstrap 日志
```

### 现有 API 改动

```
POST   /api/tasks                      新增 worker_id 字段 + 可选 id 字段
GET    /api/files/list                  新增 worker_id 参数
GET    /api/files/read                  新增 worker_id 参数
GET    /api/files/download              新增 worker_id 参数
```

### Task 操作 API（不变，后端自动判断本地/远程）

```
POST   /api/tasks/{id}/chat            发送消息（远程自动代理到 Worker）
GET    /api/tasks/{id}/chat/history     查看历史（始终查 Manager 本地 DB）
GET    /api/tasks/{id}/chat/{mid}/detail 查看消息详情（始终查 Manager 本地 DB）
POST   /api/tasks/{id}/stop-session    停止（远程自动代理到 Worker）
POST   /api/tasks/{id}/cancel          取消（远程自动代理到 Worker）
POST   /api/tasks/{id}/retry           重试（远程自动代理到 Worker）
POST   /api/tasks/{id}/plan/approve    审批 Plan（远程自动代理到 Worker）
POST   /api/tasks/{id}/plan/reject     拒绝 Plan（远程自动代理到 Worker）
```

---

## 19. 前端改动清单

| 页面/组件 | 改动 |
|-----------|------|
| **Settings 页面** | 新增 "Workers" tab — Worker 列表 + Add Worker 表单 + 基础设施配置 |
| **Task 创建表单** | 新增 "执行位置" select (本机 / Worker 1 / Worker 2 ...) |
| **Task 列表/Chat** | 显示执行位置标签（如 "Worker 1"），纯展示 |
| **Projects 页面** | 适配 Worker 情况：显示项目在哪些 Worker 上有 task |
| **Files 页面** | 通过 worker_id 参数自动使用 SSH 读取 Worker 文件（SSH Server 自动配置） |
| **Dashboard** | 无需改动（或可选：显示 Worker 状态概览卡片） |
| **Chat 组件** | 无需改动（所有代理逻辑在后端，前端 API 不变） |

---

## 20. 实现优先级

```
Phase 1: 基础框架
  - Worker 数据模型 + CRUD API
  - CCM Task 创建 API 支持指定 ID
  - 云实例管理（开机/关机）
  - Bootstrap Pipeline（账号在 Worker 登录）
  - Settings UI (基础设施配置 + Worker 管理)

Phase 2: 任务转发 + Chat 映射
  - Dispatcher 双路径（本地 vs Worker，Worker 不消耗本地 instance）
  - Task 创建时选择执行位置
  - WorkerRelay: 每 Worker 一个 WS，日志中继 + 本地存储
  - Chat API 代理（发送/停止/重试 → Worker）
  - Plan/Loop/Goal 模式状态同步 + 操作代理
  - Cost / context_window_usage 同步

Phase 3: 文件访问 + Projects
  - Worker 创建后自动配置 SSH Server
  - Files API 支持 worker_id
  - Projects 适配（自动在 Worker 创建项目）
  - 前端显示执行位置标签

Phase 4: Worker 销毁 + 数据迁移
  - consolidate_sessions.py 归并脚本
  - 项目文件 rsync 回 Manager
  - Session 文件迁移（归并后第一个账号 → Manager 第一个账号）
  - Task worker_id 清空，切回本机
  - 验证 Chat --resume 无缝衔接

Phase 5: 运维 + 健壮性
  - Worker 重启（git pull + restart）
  - 健康监控
  - WorkerRelay 断线重连 + 日志补全
  - Manager 重启后恢复所有 Worker relay 连接
  - Bootstrap 失败重试
  - 前端 Worker 状态实时更新
```
