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

---

## 2. Add Worker 流程

### 2.1 用户在 UI 上填写的内容

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

### 2.2 后端创建流程

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
4. 执行 Bootstrap Pipeline（见 2.3）
5. 等 Worker CCM 服务健康检查通过，status = `ready`

### 2.3 Bootstrap 步骤

所有云配置从 Manager 的 GlobalSettings 或 .env 读取：

```
Step 1: system-init
  apt-get update
  apt-get install -y python3 python3-venv git curl
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs

Step 2: ccm-deploy
  git clone <CCM_REPO_URL> -b <CCM_REPO_BRANCH> /opt/ccm
  cd /opt/ccm && uv sync
  cd /opt/ccm/frontend && npm install && npm run build

Step 3: claude-code-install
  npm install -g @anthropic-ai/claude-code@latest

Step 4: ccm-config
  # 写 .env（关键配置）
  cat > /opt/ccm/.env << EOF
  AUTH_TOKEN=<生成的 worker 专用 token>
  AUTO_START_DISPATCHER=true
  MAX_CONCURRENT_INSTANCES=<账号数量>
  POOL_ENABLED=true
  WORKSPACE_DIR=<与 Manager 一致的 workspace 路径>
  HOST=0.0.0.0
  PORT=8000
  EOF

Step 5: account-login
  # 对每个账号执行 auto_login.py
  cd /opt/ccm
  python3 scripts/auto_login.py --email alice@example.com --add-to-pool account-1
  python3 scripts/auto_login.py --email bob@example.com --add-to-pool account-2

  # 登录完成后，Worker 上会有：
  # ~/.claude-pool/accounts.json  (账号池配置)
  # ~/.claude-account-1/          (第一个账号的 config_dir)
  # ~/.claude-account-2/          (第二个账号的 config_dir)

Step 6: ccm-service
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

Step 7: health-check
  # 轮询直到 Worker CCM 就绪
  curl -f -H "Authorization: Bearer <token>" http://localhost:8000/api/system/health
```

### 2.4 Bootstrap 失败处理

- 任何 step 失败 → Worker status = `error`，在 UI 显示失败原因
- 用户可以选择：重试（从失败步骤继续）或 销毁重建
- 特别是 Step 5 (account-login)：如果某个账号登录失败，记录哪个失败了，
  允许用户修改账号信息后重试

---

## 3. Worker 数据模型

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

    # 账号信息
    accounts: JSON               # [{"email": "...", "status": "logged_in"/"failed"}]
    account_count: int           # 配置的账号数量

    # 健康监控
    last_heartbeat: datetime | None
    bootstrap_step: str | None   # 当前 bootstrap 进度
    bootstrap_error: str | None  # 失败原因

    # 时间
    created_at: datetime
    updated_at: datetime
```

---

## 4. Task 创建 — 选择执行位置

### 4.1 Task 模型改动

```python
class Task(Base):
    # 新增字段
    worker_id: int | None = None        # None = 本机执行
    remote_task_id: int | None = None   # 在 Worker CCM 上的 task ID
```

### 4.2 前端改动

创建 Task 表单新增 select：

```
执行位置:  [ 本机 ▾ ]
           ├─ 本机
           ├─ Worker 1 (2 账号, ready)
           ├─ Worker 2 (3 账号, ready)
           └─ Worker 3 (error)  ← 灰掉不可选
```

### 4.3 Dispatcher 改动

```python
# dispatcher.py

async def _assign_task(self, task, instance):
    if task.worker_id is None:
        # 本机执行 — 现有逻辑完全不变
        await self._run_task_locally(task, instance)
    else:
        # 远程执行 — 转发到 Worker CCM
        await self._forward_task_to_worker(task)

async def _forward_task_to_worker(self, task):
    worker = await self._get_worker(task.worker_id)

    # 1. 确保 Worker 上有这个项目
    #    如果项目有 git remote → Worker CCM 会自己 clone
    #    如果项目是纯本地 → 需要先 rsync 到 Worker（见第 7 节）

    # 2. 调 Worker CCM API 创建 task
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"http://{worker.public_ip}:{worker.ccm_port}/api/tasks",
            headers={"Authorization": f"Bearer {worker.auth_token}"},
            json={
                "title": task.title,
                "description": task.description,
                "project_id": <worker 上对应的 project_id>,
                "mode": task.mode,
                "model": task.model,
                ...
            }
        )
        remote_task = resp.json()
        task.remote_task_id = remote_task["id"]

    # 3. 开始订阅 Worker 的日志流（见第 5 节）
    await self._subscribe_worker_logs(worker, task)
```

---

## 5. 日志流转发

Manager 后端作为 WebSocket 代理，将 Worker 的日志转发给前端。

```
前端 ←──WS──→ Manager CCM ←──WS──→ Worker CCM
               (代理转发)
```

### 实现方式

```python
# 新增 services/worker_log_relay.py

class WorkerLogRelay:
    """订阅 Worker CCM 的 WebSocket，转发日志到 Manager 的 broadcaster。"""

    def __init__(self, broadcaster: WebSocketBroadcaster):
        self.broadcaster = broadcaster
        self._connections: dict[int, websockets.WebSocketClientProtocol] = {}

    async def subscribe(self, worker: Worker, task: Task):
        """订阅 Worker 上某个 task 的日志。"""
        ws_url = f"ws://{worker.public_ip}:{worker.ccm_port}/ws"
        ws = await websockets.connect(
            ws_url,
            extra_headers={"Authorization": f"Bearer {worker.auth_token}"}
        )

        # 在 Worker 端订阅 task channel
        await ws.send(json.dumps({
            "type": "subscribe",
            "channels": [f"task:{task.remote_task_id}"]
        }))

        # 转发循环
        asyncio.create_task(self._relay_loop(ws, task))

    async def _relay_loop(self, ws, task):
        async for message in ws:
            data = json.loads(message)
            # 替换 task ID 为 Manager 侧的 ID
            if "task_id" in data:
                data["task_id"] = task.id
            # 广播到 Manager 的前端
            await self.broadcaster.broadcast(f"task:{task.id}", data)
```

前端完全不需要知道 task 在 Worker 上运行。日志流的数据格式和本地 task 完全一致。

---

## 6. Files 界面适配

当用户查看 Worker 上运行的 task 的文件时，通过 SSH 读取 Worker 文件。

### 后端改动

CCM 已有 SSH file API (`/api/files/ssh/*`)。改动 `/api/files/*` 的逻辑：

```python
# api/files.py 改动

@router.get("/api/files/list")
async def list_files(path: str, worker_id: int | None = None):
    if worker_id is None:
        # 本机 — 现有逻辑不变
        return list_local_dir(path)
    else:
        # Worker — 通过 SSH
        worker = await get_worker(worker_id)
        return await list_ssh_dir(
            host=worker.public_ip,
            user=worker.ssh_user,
            key_path=worker.ssh_key_path,
            path=path
        )

# read、download 同理
```

### 前端改动

- Task 详情页的文件浏览器，如果 task.worker_id 存在，
  请求时自动附加 `?worker_id=X` 参数
- Projects 页面：如果项目关联了 Worker task，也需要支持
  通过 worker_id 浏览文件

---

## 7. Projects 适配

### 7.1 有 git remote 的项目

Worker CCM 自己 clone，无需额外处理。

Manager 转发 task 到 Worker 时，传递 project 信息（git_url, branch, credentials）。
Worker CCM 如果还没有这个项目，会自动 clone。

### 7.2 纯本地项目

如果用户选择在 Worker 上执行一个纯本地项目的 task：

1. Manager 在转发 task 前，先 rsync 项目文件到 Worker：
   ```
   rsync -az <local_path>/ worker:<workspace_dir>/<project_name>/
   ```
2. 在 Worker CCM 上创建对应的 project 记录（指向 rsync 后的路径）
3. Task 完成后，Worker 上的改动会在销毁时迁移回来

### 7.3 Worker 上的 Project 管理

Worker CCM 的 project 不由用户直接管理。Manager 按需自动创建：
- 转发 task 时自动确保 Worker 有对应 project
- Worker 的 Projects 页面不对用户暴露

---

## 8. Worker 销毁 — 数据迁移

### 8.1 触发

```
DELETE /api/workers/{id}
或
POST /api/workers/{id}/destroy
```

### 8.2 完整流程

```
Step 1: 停止 Worker 上所有任务
  ├─ GET worker/api/tasks?status=in_progress,executing
  ├─ 对每个运行中的 task: POST worker/api/tasks/{id}/stop
  └─ 等所有 task 完成/停止（超时后强制）

Step 2: 同步项目文件回 Manager
  ├─ GET worker/api/projects → 获取 Worker 上所有项目
  ├─ 对每个项目:
  │   rsync -az worker:<project_path>/ manager:<workspace_dir>/<project_name>/
  └─ 如果 Manager 上已有同名项目:
      └─ Worker 的版本覆盖 Manager 的（Worker 的更新，因为最近在上面跑）

Step 3: 同步 Session 文件回 Manager
  ├─ Worker 上所有 session 通过硬连接都可以从第一个账号的 config_dir 访问
  ├─ rsync -az worker:<第一个账号config_dir>/projects/ \
  │              manager:<manager第一个账号config_dir>/projects/
  └─ 这样 Manager 的 Claude Code 用 --resume 就能找到所有 session

Step 4: 更新 Manager DB
  ├─ 对所有 worker_id = 此 worker 的 task:
  │   ├─ task.worker_id = None          (切回本机)
  │   ├─ task.remote_task_id = None
  │   └─ execution_target = "local"
  ├─ 导入 Worker 的 log entries（如果需要保留完整日志）
  └─ session_id 不变，文件已 rsync 回来

Step 5: 销毁云实例
  ├─ Elastic Agent: terminate_instance(worker.cloud_instance_id)
  └─ Worker status = terminated
```

### 8.3 无缝衔接原理

```
销毁前:
  Worker 上: ~/.claude-account-1/projects/<encoded_cwd>/<session_id>.jsonl
  (通过硬连接，所有 Worker 账号都能访问)

rsync 后:
  Manager 上: ~/.claude/projects/<encoded_cwd>/<session_id>.jsonl
  (或 Manager 第一个账号的 config_dir 下)

用户继续 Chat:
  Manager CCM 用 --resume <session_id>
  Claude Code 在 Manager 第一个账号的 config_dir 下找到 session 文件
  对话无缝继续
```

前提条件：
- Manager 和 Worker 的 `WORKSPACE_DIR` 路径完全一致
- 这样 `<encoded_cwd>` 就一样，session 文件路径对得上

---

## 9. Worker 重启

```
POST /api/workers/{id}/restart
Body: { "force": false }
```

### 流程

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

## 10. Worker 健康监控

Manager 定期（每 30s）检查所有 ready 状态的 Worker：

```python
async def _health_check_loop(self):
    while True:
        for worker in await self._get_ready_workers():
            try:
                resp = await httpx.get(
                    f"http://{worker.public_ip}:{worker.ccm_port}/api/system/health",
                    headers={"Authorization": f"Bearer {worker.auth_token}"},
                    timeout=10
                )
                worker.last_heartbeat = datetime.utcnow()
            except Exception:
                # 连续 3 次失败 → worker status = error
                pass
        await asyncio.sleep(30)
```

---

## 11. Elastic Agent 的角色精简

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
- CredentialBinding — 账号在 Worker 本机，不做跨机绑定
- WebSocket message protocol (Execute/Stop/Log) — CCM 有自己的 API

**需要的 Elastic Agent 组件：**
- `CloudProvider` 接口 + AWS/Aliyun 实现
- `SSHExecutor` 执行远程命令
- `BootstrapPipeline` + `BootstrapHandler` 编排 bootstrap 步骤
- `NodeRegistry` 记录 worker 云实例状态（可选，也可以用 CCM 的 Worker 表替代）

实际上可以考虑把需要的部分直接集成到 CCM 中，作为一个 `services/worker_provisioner.py`，
避免运行两个独立服务。

---

## 12. Manager 配置新增

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

## 13. API 总览

### Worker 管理 API

```
GET    /api/workers                    列出所有 Worker
POST   /api/workers                    创建 Worker（开机 + bootstrap）
GET    /api/workers/{id}               获取 Worker 详情
DELETE /api/workers/{id}               销毁 Worker（迁移数据 + 关机）
POST   /api/workers/{id}/restart       重启 Worker（pull + restart）
GET    /api/workers/{id}/status        健康状态
GET    /api/workers/{id}/tasks         Worker 上的任务列表
GET    /api/workers/{id}/logs          Bootstrap 日志
```

### 现有 API 改动

```
POST   /api/tasks                      新增 worker_id 字段
GET    /api/files/list                  新增 worker_id 参数
GET    /api/files/read                  新增 worker_id 参数
GET    /api/files/download              新增 worker_id 参数
```

---

## 14. 前端改动清单

| 页面/组件 | 改动 |
|-----------|------|
| Settings 页面 | 新增 "Workers" tab — Worker 列表 + Add Worker 表单 + 基础设施配置 |
| Task 创建表单 | 新增 "执行位置" select (本机 / Worker 1 / Worker 2 ...) |
| Task 详情页 | 显示执行位置标签；文件浏览器支持 worker_id 参数 |
| Dashboard | 显示 Worker 状态概览卡片 |
| Files 页面 | 如果当前查看的是 Worker task，通过 SSH 代理读取 |

---

## 15. 实现优先级

```
Phase 1: 基础框架
  - Worker 数据模型 + API
  - 云实例管理（开机/关机）
  - Bootstrap Pipeline
  - Settings UI (基础设施配置)

Phase 2: 任务转发
  - Task 创建时选择执行位置
  - Manager → Worker API 转发
  - 日志流 WebSocket 代理

Phase 3: 文件访问
  - Files API 支持 worker_id
  - 前端文件浏览器适配

Phase 4: Worker 销毁 + 数据迁移
  - 项目文件 rsync
  - Session 文件迁移
  - Task 切回本机
  - 无缝 Chat 继续

Phase 5: 运维
  - Worker 重启（pull + restart）
  - 健康监控
  - Bootstrap 失败重试
```
